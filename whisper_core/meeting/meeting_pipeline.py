"""Явна post-meeting ASR-обробка 10-хв WAV-блоків.

Модуль не залежить від Qt. Запис лише готує WAV; цей pipeline запускається
окремою командою користувача у фоновому worker-потоці.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
import wave
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import postprocess
from .session import load_meta, update_processing
from .word_ledger import (
    WordRecord,
    assert_same_word_ids,
    write_word_ledger,
)


PIPELINE_VERSION = "meeting-asr-diarization-v2"
LEDGER_RATE = 16000
_UTTERANCE_GAP_SAMPLES = int(1.0 * LEDGER_RATE)
_UTTERANCE_MAX_SAMPLES = 20 * LEDGER_RATE
_TERMINAL = (".", "!", "?", "…")
_TRACK_RE = re.compile(r"^(?:mic\d*|sys)$")


class ProcessingUnavailable(RuntimeError):
    pass


class CancelToken:
    """Thread-safe кооперативне скасування між незалежними WAV-блоками."""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class ProcessingResult:
    status: str
    word_count: int
    tracks: dict


def _source_for_track(track: str) -> str:
    if track == "sys":
        return "others"
    if track == "mic":
        return "me"
    return track


def _speaker_names_for_tracks(
        tracks, stored: dict, microphone_label: str) -> dict:
    names = dict(stored or {})
    for track in tracks:
        match = re.fullmatch(r"mic(\d+)", track)
        if match:
            names.setdefault(
                track,
                microphone_label.format(number=match.group(1)),
            )
    return names


def _safe_audio_path(session_dir: Path, relative: str) -> Path:
    root = session_dir.resolve()
    path = (root / str(relative)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProcessingUnavailable("WAV поза текою сесії") from exc
    if path.suffix.lower() != ".wav":
        raise ProcessingUnavailable("ASR приймає лише WAV-артефакти сесії")
    return path


def _audio_manifest(session_dir: Path, meta) -> dict[str, list[Path]]:
    manifest = {}
    stored = dict(getattr(meta, "audio_files", {}) or {})
    for track in list(getattr(meta, "sources", []) or []):
        if not _TRACK_RE.fullmatch(str(track)):
            raise ProcessingUnavailable("Некоректний код аудіодоріжки")
        paths = [
            _safe_audio_path(session_dir, relative)
            for relative in (stored.get(track, []) or [])
        ]
        if not paths:
            legacy = session_dir / f"{track}.wav"
            if legacy.is_file():
                paths = [legacy]
        if paths:
            manifest[str(track)] = paths
    if not manifest:
        raise ProcessingUnavailable("Немає WAV-доріжок для обробки")
    return manifest


def _wav_timeline_frames(path: Path) -> int:
    with wave.open(str(path), "rb") as wav:
        frames = wav.getnframes()
        rate = wav.getframerate()
    if rate <= 0:
        raise ValueError("WAV не має коректної частоти")
    return int(round(frames * LEDGER_RATE / rate))


def _public_state(state: dict) -> dict:
    return json.loads(json.dumps(state, ensure_ascii=False))


def _save_state(session_dir: Path, state: dict, progress) -> None:
    update_processing(session_dir, **state)
    if progress is not None:
        progress(_public_state(state))


def _initial_state(manifest: dict) -> dict:
    total = sum(len(paths) for paths in manifest.values())
    return {
        "schema": 1,
        "pipeline_version": PIPELINE_VERSION,
        "status": "running",
        "stage": "transcribing",
        "progress": 0.0,
        "completed_chunks": 0,
        "total_chunks": total,
        "current_track": None,
        "cancel_requested": False,
        "tracks": {
            track: {"status": "pending", "completed_chunks": 0,
                    "total_chunks": len(paths), "words": 0}
            for track, paths in manifest.items()
        },
        "started_at": int(time.time()),
        "finished_at": None,
    }


def _records_from_timed(
        timed_words, *, track: str, offset_samples: int, ordinal: int,
        provenance: dict, audio_file: str, chunk_index: int):
    records = []
    for item in timed_words or []:
        text = str(item.get("word", "") or "").strip()
        if not text:
            continue
        local_start = max(0, int(round(float(item["start"]) * LEDGER_RATE)))
        local_end = max(
            local_start,
            int(round(float(item["end"]) * LEDGER_RATE)),
        )
        ordinal += 1
        detail = dict(provenance)
        detail.update({
            "pipeline_version": PIPELINE_VERSION,
            "audio_file": audio_file,
            "chunk_index": chunk_index,
        })
        records.append(WordRecord(
            word_id=f"{track}:{ordinal:08d}",
            track=track,
            start_sample=offset_samples + local_start,
            end_sample=offset_samples + local_end,
            text=text,
            source=_source_for_track(track),
            asr_provenance=detail,
        ))
    return records, ordinal


def _synthesize_timed_words(segments) -> list[dict]:
    """С4: ASR інколи віддає текст без пословних таймкодів (короткі/музичні/
    VAD-крайові блоки). Замість того щоб втратити весь хвіст доріжки, рівномірно
    розкладаємо слова кожного сегмента в його часових межах."""
    out = []
    for seg in segments or []:
        try:
            start, end, text = float(seg[0]), float(seg[1]), str(seg[2] or "")
        except (TypeError, ValueError, IndexError):
            continue
        tokens = text.split()
        if not tokens:
            continue
        span = max(0.0, end - start)
        step = span / len(tokens)
        for index, token in enumerate(tokens):
            out.append({
                "start": start + index * step,
                "end": start + (index + 1) * step if step else end,
                "word": token,
            })
    return out


def _join_words(words) -> str:
    text = " ".join(word.text for word in words).strip()
    text = re.sub(r"\s+([,.;:!?%…)\]}»”])", r"\1", text)
    text = re.sub(r"([(\[{«“])\s+", r"\1", text)
    return text


def _utterances_from_words(words, speaker_of=None) -> list[postprocess.Utterance]:
    """``speaker_of`` (word_id → speaker_id) з діаризації: sys-слова розбиваються
    ще й при ЗМІНІ мовця. mic/multimic (без запису в мапі) поводяться як раніше."""
    speaker_of = speaker_of or {}
    by_track = {}
    for word in sorted(
            words,
            key=lambda item: (
                item.track, item.start_sample, item.end_sample, item.word_id)):
        by_track.setdefault(word.track, []).append(word)
    utterances = []
    for track_words in by_track.values():
        group = []
        for word in track_words:
            speaker_changed = bool(
                group and speaker_of.get(word.word_id)
                != speaker_of.get(group[0].word_id))
            split = bool(
                group and (
                    speaker_changed
                    or word.start_sample - group[-1].end_sample
                    > _UTTERANCE_GAP_SAMPLES
                    or word.end_sample - group[0].start_sample
                    > _UTTERANCE_MAX_SAMPLES
                    or group[-1].text.endswith(_TERMINAL)
                )
            )
            if split:
                utterances.append(_utterance(group, speaker_of))
                group = []
            group.append(word)
        if group:
            utterances.append(_utterance(group, speaker_of))
    utterances.sort(
        key=lambda item: (item.start, item.end, item.source, item.word_ids))
    return utterances


def _utterance(words, speaker_of=None) -> postprocess.Utterance:
    source = words[0].source
    speaker_id = (speaker_of or {}).get(words[0].word_id)
    return postprocess.Utterance(
        start=words[0].start_sample / LEDGER_RATE,
        end=max(word.end_sample for word in words) / LEDGER_RATE,
        # Діаризований sys → speaker=speaker_id; решта → код джерела (як раніше).
        speaker=speaker_id or source,
        text=_join_words(words),
        source=source,
        word_ids=tuple(word.word_id for word in words),
        speaker_id=speaker_id,
    )


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _publish(
        session_dir: Path, records_by_track: dict, *,
        me_label: str, others_label: str, speaker_names: dict,
        speaker_of: dict = None):
    all_words = [
        word
        for track in records_by_track
        for word in records_by_track[track]
    ]
    utterances = _utterances_from_words(all_words, speaker_of)
    json_view = postprocess.to_transcript_json(utterances)
    exported_ids = [
        word_id
        for utterance in json_view
        for word_id in utterance.get("word_ids", [])
    ]
    assert_same_word_ids(all_words, exported_ids)

    for track, records in records_by_track.items():
        write_word_ledger(session_dir / f"words.{track}.jsonl", records)
    text = postprocess.to_transcript_text(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names)
    markdown = postprocess.to_transcript_markdown(
        utterances, me_label=me_label, others_label=others_label,
        speaker_names=speaker_names, meta={"type": "meeting"})
    _atomic_write_text(session_dir / "transcript.txt", text)
    _atomic_write_text(
        session_dir / "transcript.json",
        json.dumps(json_view, ensure_ascii=False, indent=2),
    )
    _atomic_write_text(session_dir / "transcript.md", markdown)
    return utterances, all_words


def _republish_from_ledgers(
        session_dir: Path, ledger_paths: dict, manifest: dict, *,
        me_label: str, others_label: str, speaker_names: dict,
        progress) -> ProcessingResult:
    """С5: леджери вже опубліковані, а транскриптів нема (крах export на повному
    диску). Доопублікувати текст з наявних леджерів БЕЗ повторного ASR. Запис у
    леджери ідемпотентний (той самий вміст), тож immutability не порушується."""
    from .word_ledger import read_word_ledger
    records_by_track = {}
    for track in manifest:
        if ledger_paths[track].exists():
            records_by_track[track] = read_word_ledger(ledger_paths[track])
    # Крах між леджерами й транскриптом міг статися ПІСЛЯ діаризації: якщо
    # speaker-assignments.jsonl уже є, доопублікування зберігає мітки мовців
    # без повторного sherpa (§5.7 — читаємо готові derived-артефакти).
    speaker_of = _load_speaker_assignments(session_dir)
    _publish(session_dir, records_by_track, me_label=me_label,
             others_label=others_label, speaker_names=speaker_names,
             speaker_of=speaker_of)
    word_count = sum(len(records) for records in records_by_track.values())
    complete = all(ledger_paths[track].exists() for track in manifest)
    status = "complete" if complete else "partial"
    state = _initial_state(manifest)
    for track in manifest:
        track_state = state["tracks"][track]
        if ledger_paths[track].exists():
            track_state.update({
                "status": "complete",
                "completed_chunks": track_state["total_chunks"],
                "words": len(records_by_track.get(track, [])),
            })
        else:
            track_state["status"] = "error"
    state.update({
        "status": status, "stage": "complete", "progress": 1.0,
        "completed_chunks": state["total_chunks"], "current_track": None,
        "word_count": word_count,
        "artifacts": [
            *(f"words.{track}.jsonl" for track in records_by_track),
            "transcript.txt", "transcript.md", "transcript.json",
        ],
        "republished": True,
        "finished_at": int(time.time()),
    })
    _save_state(session_dir, state, progress)
    return ProcessingResult(status, word_count, _public_state(state["tracks"]))


def _load_speaker_assignments(session_dir: Path) -> dict:
    """Прочитати speaker-assignments.jsonl → {word_id: speaker_id} (лише мітковані).
    Відсутній/битий файл → порожня мапа (доопублікування без міток, без краху)."""
    path = session_dir / "speaker-assignments.jsonl"
    speaker_of = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("speaker_id"):
                speaker_of[str(row["word_id"])] = str(row["speaker_id"])
    except (OSError, ValueError):
        return {}
    return speaker_of


def _write_assignments(path: Path, assignments) -> None:
    """Серіалізувати SpeakerAssignment у ``speaker-assignments.jsonl`` (sys+решта).
    Гейт нуль-втрат: рівно один рядок на слово. Без ембедингів."""
    lines = []
    for a in assignments:
        lines.append(json.dumps({
            "word_id": a.word_id,
            "speaker_id": a.speaker_id,
            "assignment_reason": a.assignment_reason,
            "overlap_suspected": a.overlap_suspected,
            "candidates": [
                {"speaker_id": c.speaker_id, "overlap_samples": c.overlap_samples}
                for c in a.candidates],
        }, ensure_ascii=False, sort_keys=True))
    _atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))


def _run_sys_diarization(session_dir, sys_paths, sys_records, settings,
                         runtime_loader, speaker_label, *, cancel, progress,
                         provenance):
    """Прогнати діаризацію sys, зв'язати слова, опублікувати сайдкари.

    Ніколи не кидає у ASR-шлях: будь-який збій → status у стані + порожня мапа
    (викликач публікує звичайний транскрипт). Повертає ``(state, speaker_of, names)``.
    """
    from .diarization_pipeline import (run_system_diarization,
                                       write_diarization_artifact)
    from .word_binding import bind_words
    try:
        runtime = runtime_loader()
    except Exception as exc:
        return {"status": "unavailable", "error": str(exc)[:300]}, {}, {}
    if runtime is None:
        return {"status": "unavailable", "reason": "runtime_missing"}, {}, {}
    result = run_system_diarization(
        sys_paths, settings, runtime=runtime, cancel=cancel, progress=progress)
    if result.status != "complete":
        return ({"status": result.status,
                 "diagnostics": result.diagnostics}, {}, {})
    assignments = bind_words(sys_records, result.spans)
    speaker_of = {a.word_id: a.speaker_id for a in assignments if a.speaker_id}
    write_diarization_artifact(
        session_dir / "diarization.final.json", result, provenance)
    # Центроїди-біометрію НЕ пишемо в артефакт сесії. За згоди складаємо їх у
    # per-профільне voice_pending/ (поза текою сесії) для бутстрапу голосу з ренейму.
    if (getattr(settings, "voice_memory_enabled", False)
            and getattr(settings, "profile", None) is not None
            and result.voice_centroids):
        from . import voice_memory
        try:
            voice_memory.save_pending_centroids(
                settings.profile, Path(session_dir).name, result.voice_centroids)
        except Exception:
            logging.exception("Не вдалося скласти voice_pending-центроїди сесії")
    _write_assignments(session_dir / "speaker-assignments.jsonl", assignments)
    matched = result.diagnostics.get("matched_speaker_names", {})
    names = {sid: matched.get(sid, speaker_label.format(number=i))
             for i, sid in enumerate(result.speaker_ids, 1)}
    state = {
        "status": "complete",
        "num_speakers": len(result.speaker_ids),
        "speakers": list(result.speaker_ids),
        "settings": {"num_speakers": settings.num_speakers,
                     "distance_threshold": settings.distance_threshold},
        "diagnostics": result.diagnostics,
        "artifacts": ["diarization.final.json", "speaker-assignments.jsonl"],
    }
    return state, speaker_of, names


def process_meeting(
        session_dir, *, transcribe, asr_provenance: dict,
        me_label: str, others_label: str,
        microphone_label: str = "Microphone {number}",
        diarization=None,                       # DiarizationSettings | None
        diarization_runtime_loader=None,        # callable()->runtime (лениво)
        speaker_label: str = "Speaker {number}",
        cancel: "CancelToken | None" = None,
        progress=None) -> ProcessingResult:
    """Розпізнати кожен WAV окремо й опублікувати immutable ledger + views."""
    # .resolve() симетрично зі шляхами WAV (_safe_audio_path теж резолвить): під
    # junction/OneDrive нерезольвнутий session_dir роз'їхався б із резольвнутими
    # WAV, і provenance-виклик relative_to кинув би ValueError → уся нарада
    # «failed» з нулем транскриптів попри вдалий ASR (Б2).
    session_dir = Path(session_dir).resolve()
    meta = load_meta(session_dir)
    if meta is None:
        raise ProcessingUnavailable("meeting.json відсутній або пошкоджений")
    manifest = _audio_manifest(session_dir, meta)
    speaker_names = _speaker_names_for_tracks(
        manifest, meta.speaker_names, microphone_label)
    # Коли діаризація ввімкнена, ASR займає 0–85%, діаризація 85–97%, експорт →100%.
    # Інакше ASR розтягується на весь діапазон (без фейкової паузи).
    diar_on = bool(diarization and getattr(diarization, "enabled", False)
                   and diarization_runtime_loader and "sys" in manifest)
    asr_ceiling = 0.85 if diar_on else 1.0
    ledger_paths = {
        track: session_dir / f"words.{track}.jsonl" for track in manifest}
    existing_ledgers = [t for t in manifest if ledger_paths[t].exists()]
    transcripts_ready = (session_dir / "transcript.json").exists()
    if existing_ledgers and not transcripts_ready:
        # Ретрай сесії, де леджери є, а транскриптів нема — доопублікувати без ASR.
        return _republish_from_ledgers(
            session_dir, ledger_paths, manifest,
            me_label=me_label, others_label=others_label,
            speaker_names=speaker_names, progress=progress)
    if existing_ledgers:
        raise ProcessingUnavailable(
            "Word Ledger уже опублікований і є незмінним")

    token = cancel or CancelToken()
    state = _initial_state(manifest)
    records_by_track = {track: [] for track in manifest}
    _save_state(session_dir, state, progress)

    def finish_cancelled():
        current = state.get("current_track")
        if current and state["tracks"][current]["status"] == "running":
            state["tracks"][current]["status"] = "cancelled"
        state.update({
            "status": "cancelled",
            "stage": "cancelled",
            "cancel_requested": True,
            "current_track": None,
            "progress": (
                state["completed_chunks"] / state["total_chunks"] * asr_ceiling
                if state["total_chunks"] else 0.0
            ),
            "finished_at": int(time.time()),
        })
        _save_state(session_dir, state, progress)
        count = sum(len(records) for records in records_by_track.values())
        return ProcessingResult("cancelled", count, _public_state(state["tracks"]))

    for track, paths in manifest.items():
        track_state = state["tracks"][track]
        track_state["status"] = "running"
        state["current_track"] = track
        _save_state(session_dir, state, progress)
        offset_samples = 0
        ordinal = 0
        for chunk_index, path in enumerate(paths, 1):
            if token.is_cancelled():
                return finish_cancelled()
            try:
                timeline_frames = _wav_timeline_frames(path)
                result = transcribe(path, include_word_timestamps=True)
                timed_words = result[5] if len(result) > 5 else []
                final_text = str(result[1] or "") if len(result) > 1 else ""
                chunk_provenance = asr_provenance
                if final_text.strip() and not timed_words:
                    # Деградуємо, а не валимо доріжку (С4): синтезуємо таймкоди з
                    # меж сегментів; якщо й сегментів нема — одне слово на весь блок.
                    segments = result[4] if len(result) > 4 else []
                    timed_words = _synthesize_timed_words(segments)
                    if not timed_words:
                        timed_words = [{
                            "start": 0.0,
                            "end": max(0.0, timeline_frames / LEDGER_RATE),
                            "word": final_text.strip(),
                        }]
                    chunk_provenance = dict(asr_provenance)
                    chunk_provenance["word_timestamps"] = "synthesized_from_segments"
                records, ordinal = _records_from_timed(
                    timed_words,
                    track=track,
                    offset_samples=offset_samples,
                    ordinal=ordinal,
                    provenance=chunk_provenance,
                    audio_file=path.resolve().relative_to(session_dir).as_posix(),
                    chunk_index=chunk_index,
                )
                records_by_track[track].extend(records)
                offset_samples += timeline_frames
            except Exception as exc:
                skipped = len(paths) - chunk_index
                track_state.update({
                    "status": "error",
                    "error": str(exc)[:300],
                    "words": len(records_by_track[track]),
                    "skipped_chunks": skipped,
                })
                track_state["completed_chunks"] += 1
                state["completed_chunks"] += 1 + skipped
                state["cancel_requested"] = token.is_cancelled()
                state["progress"] = (
                    state["completed_chunks"] / state["total_chunks"])
                _save_state(session_dir, state, progress)
                break
            track_state["completed_chunks"] += 1
            track_state["words"] = len(records_by_track[track])
            state["completed_chunks"] += 1
            state["cancel_requested"] = token.is_cancelled()
            state["progress"] = (
                state["completed_chunks"] / state["total_chunks"] * asr_ceiling)
            _save_state(session_dir, state, progress)
        if track_state["status"] == "running":
            track_state["status"] = "complete"
            _save_state(session_dir, state, progress)

    if token.is_cancelled():
        return finish_cancelled()

    errors = any(
        track["status"] == "error" for track in state["tracks"].values())
    word_count = sum(len(records) for records in records_by_track.values())
    if errors and not word_count:
        state.update({
            "status": "failed",
            "stage": "failed",
            "current_track": None,
            "progress": 1.0,
            "finished_at": int(time.time()),
        })
        _save_state(session_dir, state, progress)
        return ProcessingResult("failed", 0, _public_state(state["tracks"]))

    # --- діаризація sys (опційне збагачення; ніколи не валить ASR) ---
    speaker_of: dict = {}
    diar_state = {"status": "disabled"}
    if diar_on and word_count and not token.is_cancelled():
        sys_records = records_by_track.get("sys") or []
        sys_paths = manifest.get("sys")
        if sys_records and sys_paths:
            state["stage"] = "diarizing"
            state["progress"] = asr_ceiling
            _save_state(session_dir, state, progress)

            def diar_progress(done, total):
                frac = (done / total) if total else 1.0
                state["progress"] = round(0.85 + 0.12 * min(1.0, frac), 4)
                _save_state(session_dir, state, progress)

            try:
                diar_state, speaker_of, diar_names = _run_sys_diarization(
                    session_dir, sys_paths, sys_records, diarization,
                    diarization_runtime_loader, speaker_label,
                    cancel=token.is_cancelled, progress=diar_progress,
                    provenance={"pipeline_version": PIPELINE_VERSION,
                                **asr_provenance})
            except Exception as exc:
                # Захист-пояс: будь-який непередбачений збій → звичайний транскрипт.
                diar_state, speaker_of, diar_names = (
                    {"status": "failed", "error": str(exc)[:300]}, {}, {})
            for sid, name in (diar_names or {}).items():
                speaker_names.setdefault(sid, name)
            if diar_names:
                try:
                    from .session import ensure_speaker_names
                    ensure_speaker_names(session_dir, diar_names)
                except Exception:
                    pass

    state["stage"] = "exporting"
    state["current_track"] = None
    state["progress"] = max(state.get("progress", 0.0), 0.97)
    _save_state(session_dir, state, progress)
    try:
        _utterances, all_words = _publish(
            session_dir,
            records_by_track,
            me_label=me_label,
            others_label=others_label,
            speaker_names=speaker_names,
            speaker_of=speaker_of,
        )
        if Counter(word.word_id for word in all_words) != Counter(
                word.word_id
                for records in records_by_track.values()
                for word in records):
            raise ValueError("Counter(word_id) змінився після export")
    except Exception as exc:
        state.update({
            "status": "failed",
            "stage": "failed",
            "error": str(exc)[:300],
            "finished_at": int(time.time()),
        })
        _save_state(session_dir, state, progress)
        return ProcessingResult("failed", word_count, _public_state(state["tracks"]))

    final_status = "partial" if errors else "complete"
    diar_artifacts = (diar_state.get("artifacts", [])
                      if diar_state.get("status") == "complete" else [])
    state.update({
        "status": final_status,
        "stage": "complete",
        "progress": 1.0,
        "completed_chunks": state["total_chunks"],
        "word_count": word_count,
        # Збій діаризації НЕ перетворює завершений ASR на partial — лишається
        # статусом у processing.diarization, а транскрипт публікується як завжди.
        "diarization": diar_state,
        "artifacts": [
            *(f"words.{track}.jsonl" for track in records_by_track),
            "transcript.txt", "transcript.md", "transcript.json",
            *diar_artifacts,
        ],
        "finished_at": int(time.time()),
    })
    _save_state(session_dir, state, progress)
    return ProcessingResult(
        final_status, word_count, _public_state(state["tracks"]))


__all__ = [
    "PIPELINE_VERSION", "CancelToken", "ProcessingResult",
    "ProcessingUnavailable", "process_meeting",
]
