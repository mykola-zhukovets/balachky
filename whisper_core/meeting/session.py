"""Життєвий цикл сесії наради на диску: сегментний запис сирого float32, метадані
meeting.json, реєстр сесій, осиротілі, повне видалення.

БЕЗ Qt, БЕЗ мережі. Сесія переживає краш, бо кожна доріжка пишеться інкрементально
сирими файлами-сегментами по ~45 с (не тримається в пам'яті), а meeting.json одразу
позначає стан `recording` — знайдений при старті застосунку, він виказує краш.

Формат на диску — КОНТРАКТ (розділ 2.2 спеки); константи схеми/статусів/доріжок
живуть у пакеті whisper_core.meeting, звідси ре-експортуються, щоб і сторона
пост-обробки читала їх звідти ж, а не дублювала.
"""
import json
import logging
import os
import re
import shutil
import threading
import time
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import (
    DEFAULT_EXPORT_SEGMENT_SECONDS, DEFAULT_SEGMENT_SECONDS,
    NATIVE_CHANNELS, NATIVE_RATE, SCHEMA,
    STATUS_DONE, STATUS_ERROR, STATUS_INTERRUPTED, STATUS_PROCESSING,
    STATUS_RECORDING, STATUS_STOPPED, STATUS_CORRUPTED, TRACK_MIC, TRACK_SYS, TRACKS,
    PRESET_BOTH, PRESET_MULTIMIC, PRESET_ONLYMIC,
)
from ..paths import anonymize_path

# ре-експорт для контракту (session.SCHEMA, session.STATUS_*, session.PRESET_* —
# так білдер UI і не дублює рядки, і не тягне з двох місць)
__all__ = [
    "SCHEMA", "DEFAULT_SEGMENT_SECONDS", "DEFAULT_EXPORT_SEGMENT_SECONDS",
    "STATUS_RECORDING", "STATUS_STOPPED", "STATUS_PROCESSING", "STATUS_DONE",
    "STATUS_ERROR", "STATUS_INTERRUPTED", "PRESET_ONLYMIC", "PRESET_BOTH", "PRESET_MULTIMIC",
    "MeetingMeta", "MeetingSession", "create_session", "list_sessions", "atomic_write_json",
    "load_meta", "find_orphans", "finalize_dir", "mark_interrupted",
    "delete_session", "is_safe_session_id", "set_speaker_name", "add_bookmark",
    "record_audio_exports", "update_processing", "ensure_speaker_names",
    "encrypt_session", "resume_encryption", "mark_encryption_pending",
    "count_pending_encryption", "queue_plaintext_encryption",
    "migrate_unencrypted_sessions", "read_artifact", "write_artifact",
    "write_artifact_file", "decrypt_session_to", "materialize_session",
    "sync_materialized_session",
]

_META_NAME = "meeting.json"
_ENCRYPT_MARKER = "encrypting.marker"
_ARTIFACT_COPY_BUFFER_BYTES = 1024 * 1024
# С3: заздалегідь зарезервовані блоки під аварійний запис meeting.json. На
# повному диску atomic_write_json сам не має де писати; звільнивши цей файл,
# ми гарантовано вивільняємо місце під позначку storage_error.
_RESERVE_NAME = ".storage_reserve"
_RESERVE_BYTES = 65536
_BYTES_PER_SAMPLE = 4               # float32
_META_REPLACE_ATTEMPTS = 4
_META_REPLACE_DELAY = 0.05
_ATOMIC_WRITE_LOCKS = {}
_ATOMIC_WRITE_LOCKS_GUARD = threading.Lock()

# Безпечний id сесії = рівно те, що генерує _alloc_dir: локальний час старту
# "РРРР-ММ-ДД_гг-хх-сс" з опційним суфіксом колізії "-N". Один компонент шляху,
# без роздільників / ".." / диска — не дає підкладеному meeting.json з
# traversal-id ("..\\інша-тека") стати шляхом видалення.
_SAFE_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?$")


def is_safe_session_id(session_id) -> bool:
    """id безпечний для побудови filesystem-шляху: формат create_session, без
    роздільників шляху, "..", абсолютних. Рубіж 2 захисту від traversal."""
    return bool(_SAFE_ID_RE.match(str(session_id)))


def atomic_write_json(path: Path, payload: object) -> None:
    """Надійно замінити JSON: temp поруч → flush+fsync → ``os.replace``.

    Temp лежить на тому самому томі, тому replace атомарний. Якщо процес або ОС
    впаде до нього, попередній ``meeting.json`` лишається цілим; якщо після —
    уже цілою буде нова версія. Каталог синхронізуємо там, де ОС це підтримує.
    """
    path = Path(path)
    # Унікальний tmp не дає двом незалежним записам стерти тимчасовий файл одне
    # одного; локальний lock ще й серіалізує послідовність write → replace.
    key = str(path.resolve())
    with _ATOMIC_WRITE_LOCKS_GUARD:
        write_lock = _ATOMIC_WRITE_LOCKS.setdefault(key, threading.Lock())
    with write_lock:
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(_META_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError as exc:
                    # Ретраїмо лише Windows sharing violation (32/33) — коротке
                    # утримання файла антивірусом/індексатором. Справжня відмова
                    # доступу (winerror 5 / EACCES без winerror) не мине сама.
                    if getattr(exc, "winerror", None) not in (32, 33):
                        raise
                    if attempt + 1 == _META_REPLACE_ATTEMPTS:
                        raise
                    time.sleep(_META_REPLACE_DELAY)
            try:
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                pass
        finally:
            # Після невдалого replace старий JSON цілий; тимчасовий файл не
            # лишаємо накопичуватися до наступного запуску.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def _within_root(target: Path, root: Path) -> bool:
    """target ФІЗИЧНО під root: обидва через realpath (резолвить "..", симлінки,
    регістр тому), тоді commonpath == realpath(root) і target ≠ сам root. Рубіж 3
    — остання перевірка перед rmtree, ловить будь-який шлях за межі сховища."""
    try:
        rt = os.path.realpath(target)
        rr = os.path.realpath(root)
        return rt != rr and os.path.commonpath([rt, rr]) == rr
    except (ValueError, OSError):
        return False


@dataclass
class MeetingMeta:
    schema: int
    id: str
    created: int
    status: str
    preset: str                    # PRESET_ONLYMIC | PRESET_BOTH
    sources: list                  # ["mic"] або ["mic","sys"] (похідні від пресету)
    rate: int = NATIVE_RATE
    channels: int = NATIVE_CHANNELS
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS
    export_segment_seconds: int = DEFAULT_EXPORT_SEGMENT_SECONDS
    mic_device: "str | None" = None
    sys_device: "str | None" = None
    track_devices: dict = field(default_factory=dict)  # track to stable device name
    recording_sources: list = field(default_factory=list)  # immutable start snapshot
    duration: "float | None" = None
    title: "str | None" = None
    protocol: object = None        # зарезервовано під v1.2 (протокол наради); MVP → None
    marks: list = field(default_factory=list)  # застарілий ключ v1.2; читаємо для сумісності
    bookmarks: list = field(default_factory=list)  # [{timestamp: float, title: str}]
    storage_error: "dict | None" = None  # перша ENOSPC: track + секунда від старту
    # Підсумок підтверджених device-outage по доріжках. Не зберігаємо кожну
    # мітку окремо: JSON лишається компактним, а запис не витікає у UI-вибір
    # пристроїв feature/audio-center.
    audio_interruptions: dict = field(default_factory=dict)
    audio_discontinuities: list = field(default_factory=list)
    audio_files: dict = field(default_factory=dict)  # track -> relative 16 kHz WAV paths
    # Окремий стан post-meeting конвеєра. status вище описує життєвий цикл
    # запису; обробка ніколи не стартує автоматично після stop.
    processing: dict = field(default_factory=dict)
    speaker_names: dict = field(default_factory=dict)  # diarization id → display name
    screen_started_at: "float | None" = None  # UNIX timestamp старту screen.webm
    screen_monitor: "int | None" = None       # mss monitor index (1..N)
    screen_status: str = "failed"             # ok лише після першого кадру відео

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "MeetingMeta":
        """Відсутні поля → дефолти (сумісність назад: стара сесія без protocol/marks
        читається без падіння)."""
        d = json.loads(text)
        return cls(
            schema=d.get("schema", SCHEMA),
            id=d.get("id", ""),
            created=d.get("created", 0),
            status=d.get("status", STATUS_INTERRUPTED),
            preset=d.get("preset", PRESET_ONLYMIC),
            sources=d.get("sources", [TRACK_MIC]),
            rate=d.get("rate", NATIVE_RATE),
            channels=d.get("channels", NATIVE_CHANNELS),
            segment_seconds=d.get("segment_seconds", DEFAULT_SEGMENT_SECONDS),
            export_segment_seconds=d.get(
                "export_segment_seconds", DEFAULT_EXPORT_SEGMENT_SECONDS),
            mic_device=d.get("mic_device"),
            sys_device=d.get("sys_device"),
            track_devices=d.get("track_devices", {}),
            recording_sources=d.get("recording_sources", []),
            duration=d.get("duration"),
            title=d.get("title"),
            protocol=d.get("protocol"),
            marks=d.get("marks", []),
            bookmarks=d.get("bookmarks", d.get("marks", [])),
            storage_error=d.get("storage_error"),
            audio_interruptions=d.get("audio_interruptions", {}),
            audio_discontinuities=d.get("audio_discontinuities", []),
            audio_files=d.get("audio_files", {}),
            processing=d.get("processing", {}),
            speaker_names=d.get("speaker_names", {}),
            screen_started_at=d.get("screen_started_at"),
            screen_monitor=d.get("screen_monitor"),
            screen_status=d.get("screen_status", "failed"),
        )


def set_speaker_name(session_dir, speaker_id: str, name: str):
    """Зберегти відображуване ім’я мовця у meeting.json і повернути метадані."""
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    names = dict(meta.speaker_names or {})
    speaker_id = str(speaker_id)
    # Порожнє поле не видаляє id: UI має лишити можливість відредагувати його
    # знову. Локалізований дефолт встановлює викликач-фронт.
    names[speaker_id] = name.strip() or names.get(speaker_id, speaker_id)
    meta.speaker_names = names
    _write_meta(session_dir, meta)
    return meta


def ensure_speaker_names(session_dir, names: dict):
    """Додати дефолтні імена мовців діаризації, НЕ чіпаючи вже задані користувачем.

    ``setdefault`` зберігає inline-перейменування: повторний прогін/доопублікування
    не затирає «Директор» назад на «Спікер 1». Повертає оновлені метадані.
    """
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    merged = dict(meta.speaker_names or {})
    for speaker_id, name in (names or {}).items():
        merged.setdefault(str(speaker_id), name)
    meta.speaker_names = merged
    atomic_write_json(session_dir / _META_NAME, asdict(meta))
    return meta


def set_title(session_dir, title: str):
    """Зберегти назву наради у meeting.json і повернути метадані (feature/
    diary-calendar). Порожня назва → title=None (картка знову покаже дату)."""
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    title = (title or "").strip()[:120]
    meta.title = title or None
    _write_meta(session_dir, meta)
    return meta

def add_bookmark(session_dir, timestamp: float, title: str = ""):
    """Атомарно додати мітку моменту у meeting.json і повернути нові метадані.

    Timestamp завжди від початку наради; коротка назва необов’язкова. Старі
    ``marks`` лишаються лише для сумісного читання, новий запис — ``bookmarks``.
    """
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    mark = {"timestamp": round(max(0.0, float(timestamp)), 3), "title": (title or "").strip()[:120]}
    meta.bookmarks = list(meta.bookmarks or [])
    meta.bookmarks.append(mark)
    meta.bookmarks.sort(key=lambda item: float(item.get("timestamp", 0)))
    _write_meta(session_dir, meta)
    return meta

def _preset_for(sources) -> str:
    if any(str(track).startswith("mic") and str(track) != TRACK_MIC for track in sources):
        return PRESET_MULTIMIC
    return PRESET_BOTH if TRACK_SYS in sources else PRESET_ONLYMIC


def _alloc_dir(root: Path) -> Path:
    """Тека сесії = локальний час старту; колізія (два старти за секунду) →
    суфікс -1, -2… Tombstone теж резервує ім'я, навіть якщо папку вже видалено."""
    root.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y-%m-%d_%H-%M-%S")
    d = root / base
    n = 1
    while (d.exists()
           or (root / f".{d.name}.audit.deleted").exists()):
        d = root / f"{base}-{n}"
        n += 1
    d.mkdir()
    return d


class MeetingSession:
    """Життєвий цикл одного запису. Володіє текою сесії, приймає сирі байти від
    CaptureStream через mic_sink/sys_sink, ріже їх на сегменти по segment_seconds,
    пише meeting.json.

    Межі безпеки потоків: доріжки між собою незалежні (свій read-потік → свій
    файл), але ВСЕРЕДИНІ доріжки писар не один — watchdog capture викликає
    close_segment із потоку-монітора паралельно з _write у read-потоці. Тому кожна
    доріжка несе власний замок: _write / close_segment / фіналізація доріжки
    беруть його, і форсована ротація не влучає у файл, який щойно закрили (гонка
    давала ValueError на закритому файлі й тихо вбивала потік). finalize
    викликають після join потоків захоплення — там замок беззмаганний, але дешевий."""

    def __init__(self, root: Path, sources: list, *, rate=NATIVE_RATE,
                 channels=NATIVE_CHANNELS, segment_seconds=DEFAULT_SEGMENT_SECONDS,
                 export_segment_seconds=DEFAULT_EXPORT_SEGMENT_SECONDS,
                 mic_device=None, sys_device=None, track_devices=None,
                 recording_sources=None, speaker_names=None):
        self._dir = _alloc_dir(Path(root))
        self._rate = rate
        self._channels = channels
        self._segment_seconds = segment_seconds
        self._export_segment_seconds = export_segment_seconds
        self._bytes_per_frame = channels * _BYTES_PER_SAMPLE
        self._segment_frames = segment_seconds * rate
        self._sources = list(dict.fromkeys(str(track) for track in sources))
        self._finalized = False
        self._meta_lock = threading.RLock()
        # ENOSPC може не дати записати meeting.json саме тоді, коли його треба
        # позначити. Прапорець живе до першого наступного успішного sink/finalize.
        self._storage_error_persist_pending = False
        # стан сегмент-writer'а на доріжку (замок — межі безпеки в докстрінгу
        # класу); підтека створюється лениво при першому байті
        self._tracks = {t: {"file": None, "index": 0, "frames": 0,
                            "lock": threading.Lock()} for t in self._sources}
        self._meta = MeetingMeta(
            schema=SCHEMA, id=self._dir.name, created=int(time.time()),
            status=STATUS_RECORDING, preset=_preset_for(self._sources), sources=self._sources,
            rate=rate, channels=channels, segment_seconds=segment_seconds,
            export_segment_seconds=export_segment_seconds,
            mic_device=mic_device, sys_device=sys_device,
            track_devices=dict(track_devices or {}),
            recording_sources=list(recording_sources or []),
            speaker_names=dict(speaker_names or {}))
        self._write_meta()
        self._alloc_storage_reserve()

    def _alloc_storage_reserve(self) -> None:
        """Зайняти блоки під аварійний meta-запис, поки на диску ще є місце."""
        try:
            path = self._dir / _RESERVE_NAME
            with open(path, "wb") as f:
                f.write(b"\0" * _RESERVE_BYTES)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            # Диск уже повний на старті — сесія й так не запишеться; не заважаємо.
            logging.warning("Не вдалося зарезервувати місце під storage_error")

    def _release_storage_reserve(self) -> bool:
        """Звільнити резерв (аварія ENOSPC або фіналізація). True, якщо був."""
        try:
            (self._dir / _RESERVE_NAME).unlink()
            return True
        except OSError:
            return False

    @property
    def dir(self) -> Path:
        return self._dir

    @property
    def id(self) -> str:
        return self._dir.name

    def sink(self, track: str):
        """Sink для довільної іменованої доріжки CaptureStream."""
        if track not in self._tracks:
            raise KeyError(f"Unknown meeting track: {track}")
        return lambda pcm: self._write(track, pcm)

    def mic_sink(self, pcm: bytes) -> None:
        self._write(TRACK_MIC, pcm)

    def sys_sink(self, pcm: bytes) -> None:
        self._write(TRACK_SYS, pcm)

    def _write(self, track: str, pcm: bytes) -> None:
        st = self._tracks[track]
        with st["lock"]:
            if st["file"] is None:
                self._open_segment(track)
            st["file"].write(pcm)
            st["frames"] += len(pcm) // self._bytes_per_frame
            if st["frames"] >= self._segment_frames:
                self._rotate(track)
        self._retry_pending_storage_error()

    def _open_segment(self, track: str) -> None:
        st = self._tracks[track]
        tdir = self._dir / track
        tdir.mkdir(exist_ok=True)
        st["file"] = open(tdir / f"{st['index']:04d}.f32", "wb")
        st["frames"] = 0

    def _rotate(self, track: str) -> None:
        st = self._tracks[track]
        if st["file"] is not None:
            # Сегмент ~45с (DEFAULT_SEGMENT_SECONDS) — природна межа для fsync:
            # синхронізувати на кожен блок PCM дав би затримку захоплення живого
            # звуку, а лише при finalize() лишав би до 45с найсвіжішого запису
            # без гарантії диска при аварії живлення. Ротація вже трапляється з
            # цим кроком, тож fsync тут не додає окремої паузи поверх наявної.
            st["file"].flush()
            os.fsync(st["file"].fileno())
            st["file"].close()
            st["file"] = None
        st["index"] += 1
        st["frames"] = 0

    def close_segment(self, track: str) -> None:
        """Форсована ротація поточного сегмента (watchdog on_stall): те, що вже
        прочитано, гарантовано лягає у закритий файл; наступний байт відкриє новий.
        Викликається з потоку-монітора — замок доріжки прикриває гонку з _write."""
        st = self._tracks.get(track)
        if st is None:
            return
        with st["lock"]:
            if st["file"] is not None:
                self._rotate(track)

    def add_bookmark(self, timestamp: float, title: str = "") -> MeetingMeta:
        """Додати мітку до живих метаданих і одразу зберегти її під meta-lock."""
        mark = {"timestamp": round(max(0.0, float(timestamp)), 3),
                "title": (title or "").strip()[:120]}
        with self._meta_lock:
            self._meta.bookmarks = list(self._meta.bookmarks or [])
            self._meta.bookmarks.append(mark)
            self._meta.bookmarks.sort(key=lambda item: float(item.get("timestamp", 0)))
            self._write_meta()
            return self._meta

    def record_audio_gap(self, track: str, seconds: float,
                         reason: str = "device_reconnect") -> None:
        """Зафіксувати заповнений reconnect-gap у meeting.json атомарно.

        Викликається з reader-потоку кожної доріжки, тому метадані мають окремий
        замок від segment-writer'ів. Нульові/від'ємні значення не є outage і не
        засмічують підсумок.
        """
        if seconds <= 0:
            return
        with self._meta_lock:
            item = self._meta.audio_interruptions.setdefault(
                track, {"count": 0, "duration": 0.0})
            item["count"] += 1
            item["duration"] = round(item["duration"] + float(seconds), 3)
            self._meta.audio_discontinuities.append({
                "track": str(track),
                "occurred_at": int(time.time()),
                "duration_seconds": round(float(seconds), 3),
                "silence_inserted_seconds": round(float(seconds), 3),
                "reason": str(reason),
            })
            self._write_meta()

    def finalize(self, status: str = STATUS_STOPPED) -> MeetingMeta:
        """Флаш хвостових сегментів, порахувати duration за к-стю кадрів на диску,
        записати meeting.json зі статусом. Ідемпотентна.

        fsync кожного артефакту (хвостовий сегмент доріжки, meeting.json) —
        best-effort: збій одного файлу (напр. диск відвалився саме зараз) не
        має заблокувати решту фіналізації чи лишити сесію в стані "recording"
        назавжди. Усі збої збираються в один WARNING без стека на кожен файл —
        деталь у самому OSError і так каже все потрібне."""
        if self._finalized:
            return self._meta
        fsync_failures = []
        for track in self._sources:
            st = self._tracks[track]
            with st["lock"]:
                if st["file"] is not None:
                    f = st["file"]
                    st["file"] = None
                    try:
                        # Хвостовий сегмент коротший за 45с і не пройшов
                        # _rotate() — без цього fsync останні секунди наради
                        # лишались би лише в буфері ОС до природного flush.
                        f.flush()
                        os.fsync(f.fileno())
                    except OSError:
                        # anonymize_path — конвенція модуля: жоден шлях у лог
                        # не несе C:\Users\<логін> (рецензія 31.07).
                        fsync_failures.append(anonymize_path(
                            self._dir / track / f"{st['index']:04d}.f32"))
                    finally:
                        f.close()
        with self._meta_lock:
            self._meta.status = status
            self._meta.duration = _measure_duration(
                self._dir, self._rate, self._channels, self._sources)
            try:
                self._write_meta()
            except OSError:
                fsync_failures.append(anonymize_path(self._dir / _META_NAME))
        if fsync_failures:
            logging.warning(
                "Фіналізація наради: не вдалося fsync %d файл(ів): %s",
                len(fsync_failures), ", ".join(fsync_failures))
        # Запис завершено — аварійний резерв більше не потрібен, повертаємо місце.
        self._release_storage_reserve()
        self._finalized = True
        return self._meta

    def set_screen_recording(self, started_at: float, monitor_index: int) -> None:
        """Зберегти часову опору відео поруч з аудіо для майбутньої синхронізації."""
        self._meta.screen_started_at = float(started_at)
        self._meta.screen_monitor = int(monitor_index)
        self._meta.screen_status = "ok"
        self._write_meta()

    def set_screen_failed(self) -> None:
        self._meta.screen_status = "failed"
        self._write_meta()

    def _write_meta(self) -> None:
        with self._meta_lock:
            atomic_write_json(self._dir / _META_NAME, asdict(self._meta))
            self._storage_error_persist_pending = False

    def _retry_pending_storage_error(self) -> None:
        """Після ENOSPC повторити запис позначки на першому вдалому sink.

        Помилка повторного meta-write не повинна зупиняти захоплення: callback
        уже повідомив UI про втрату, а наступний успішний sink повторить спробу.
        """
        with self._meta_lock:
            if not self._storage_error_persist_pending:
                return
            try:
                self._write_meta()
            except OSError:
                logging.exception("Не вдалося повторно зберегти storage_error")

    def mark_storage_error(self, track: str, elapsed_seconds: float) -> None:
        """Зафіксувати першу втрату запису (напр. ENOSPC) у самій сесії.

        Захоплення не зупиняємо: UI має гучно попередити, а вже записані сегменти
        лишаються придатними до відновлення. Позначка переживає postprocess і
        пояснює, з якої хвилини аудіо більше не гарантовано збережене.
        """
        with self._meta_lock:
            if self._meta.storage_error is not None:
                return
            self._meta.storage_error = {
                "kind": "disk_full", "track": track,
                "elapsed_seconds": max(0, int(elapsed_seconds)),
            }
            self._storage_error_persist_pending = True
            # Звільняємо заздалегідь виділені блоки — тепер atomic_write_json має
            # де писати навіть на повному диску, і позначка доходить до meeting.json.
            self._release_storage_reserve()
            self._write_meta()


# --- модульні функції: реєстр сесій, стани сховища ---

def create_session(root, sources, **kw) -> MeetingSession:
    return MeetingSession(Path(root), sources, **kw)


def _artifact_relative(session_dir: Path, relative_path) -> Path:
    """Normalize a relative artifact path without allowing traversal."""
    session_dir = Path(session_dir)
    relative = Path(relative_path)
    if relative.is_absolute() or relative == Path(".") or ".." in relative.parts:
        raise ValueError("meeting artifact must be inside the session")
    if not _within_root(session_dir / relative, session_dir):
        raise ValueError("meeting artifact escapes its session")
    return relative


def _session_context(session_dir: Path, plain_name: str) -> bytes:
    return (session_dir.name + "/" + plain_name.replace("\\", "/")).encode("utf-8")


def _has_v2_artifact(session_dir: Path) -> bool:
    for path in session_dir.rglob("*.enc"):
        try:
            with path.open("rb") as stream:
                if stream.read(1) == b"\x02":
                    return True
        except OSError:
            pass
    return False


def read_artifact(session_dir: Path, relative_path) -> bytes:
    """Read a plaintext active artifact or decrypt a completed one in memory."""
    session_dir = Path(session_dir)
    relative = _artifact_relative(session_dir, relative_path)
    plain = session_dir / relative
    if plain.exists():
        return plain.read_bytes()
    encrypted = Path(str(plain) + ".enc")
    if not encrypted.exists():
        raise FileNotFoundError(plain)
    from .storage_crypto import decrypt_to_memory, ensure_dek
    context = _session_context(session_dir, relative.as_posix())
    if _has_v2_artifact(session_dir):
        return decrypt_to_memory(encrypted, ensure_dek(session_dir.parent), context=context)
    return decrypt_to_memory(encrypted, ensure_dek(session_dir.parent))


def write_artifact(session_dir: Path, relative_path, data: bytes) -> Path:
    """Write an artifact without downgrading an encrypted session to plaintext."""
    if not isinstance(data, bytes):
        raise TypeError("meeting artifact data must be bytes")
    session_dir = Path(session_dir)
    relative = _artifact_relative(session_dir, relative_path)
    plain = session_dir / relative
    encrypted = Path(str(plain) + ".enc")
    encrypted_session = (session_dir / (_META_NAME + ".enc")).exists()
    if (encrypted.exists() and not plain.exists()) or encrypted_session:
        from .storage_crypto import encrypt_bytes, ensure_dek
        encrypt_bytes(data, encrypted, ensure_dek(session_dir.parent),
                      context=_session_context(session_dir, relative.as_posix()))
        plain.unlink(missing_ok=True)
        return encrypted
    plain.parent.mkdir(parents=True, exist_ok=True)
    tmp = plain.with_name(f"{plain.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, plain)
    finally:
        tmp.unlink(missing_ok=True)
    return plain


def write_artifact_file(session_dir: Path, relative_path, source_path: Path) -> Path:
    """Streaming counterpart to write_artifact for large WAV/video outputs."""
    session_dir = Path(session_dir)
    relative = _artifact_relative(session_dir, relative_path)
    source_path = Path(source_path)
    plain = session_dir / relative
    encrypted = Path(str(plain) + ".enc")
    encrypted_session = (session_dir / (_META_NAME + ".enc")).exists()
    if encrypted_session or (encrypted.exists() and not plain.exists()):
        from .storage_crypto import encrypt_file, ensure_dek
        encrypt_file(source_path, encrypted, ensure_dek(session_dir.parent),
                     context=_session_context(session_dir, relative.as_posix()))
        plain.unlink(missing_ok=True)
        return encrypted
    plain.parent.mkdir(parents=True, exist_ok=True)
    temp = plain.with_name(f"{plain.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source_path.open("rb") as source, temp.open("wb") as output:
            shutil.copyfileobj(
                source, output, length=_ARTIFACT_COPY_BUFFER_BYTES)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp, plain)
    finally:
        temp.unlink(missing_ok=True)
    return plain


def _write_meta(session_dir: Path, meta: MeetingMeta) -> None:
    write_artifact(session_dir, _META_NAME,
                   json.dumps(asdict(meta), ensure_ascii=False, indent=2).encode("utf-8"))


def decrypt_session_to(session_dir: Path, destination: Path) -> Path:
    """Materialize one session for path-only consumers; caller owns cleanup.

    Без fsync навмисно: обидва виклики (materialize_session,
    _materialized_meeting_dir у fronts/desktop/app.py) пишуть у
    tempfile.TemporaryDirectory — одноразовий кеш для програвання/перегляду,
    що видаляється при виході або зміні сесії. `session_dir` (зашифрований
    оригінал — єдиний довговічний носій наради) тут лише читається, не
    змінюється. Аварія живлення під час копіювання лишає биту тимчасову
    теку, яку наступний виклик просто перестворить з оригіналу — втрати
    даних немає, тож синхронізація на диск тут не потрібна.
    """
    from .storage_crypto import decrypt_file, ensure_dek
    session_dir = Path(session_dir)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    dek = None
    v2 = _has_v2_artifact(session_dir)
    for source in session_dir.rglob("*"):
        if not source.is_file() or source.name.endswith(".tmp"):
            continue
        relative = source.relative_to(session_dir)
        if source.name.endswith(".enc"):
            plain_relative = Path(str(relative)[:-4])
            target = destination / plain_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if dek is None:
                dek = ensure_dek(session_dir.parent)
            context = _session_context(session_dir, plain_relative.as_posix())
            decrypt_file(source, target, dek, context=context if v2 else None)
        elif source.name != _ENCRYPT_MARKER:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return destination


@contextmanager
def materialize_session(session_dir: Path):
    """Yield a temporary plaintext session and remove it on every exit path."""
    session_dir = Path(session_dir)
    if not (session_dir / (_META_NAME + ".enc")).exists():
        yield session_dir
        return
    with tempfile.TemporaryDirectory(prefix="balachky-meeting-") as outer:
        materialized = Path(outer) / session_dir.name
        decrypt_session_to(session_dir, materialized)
        yield materialized


def sync_materialized_session(materialized: Path, session_dir: Path) -> int:
    """Import durable outputs from a temporary worker tree into sealed storage."""
    materialized, session_dir = Path(materialized), Path(session_dir)
    count = 0
    for source in materialized.rglob("*"):
        if not source.is_file() or source.name.endswith(".tmp"):
            continue
        relative = source.relative_to(materialized)
        write_artifact_file(session_dir, relative, source)
        count += 1
    return count


def load_meta(session_dir: Path) -> "MeetingMeta | None":
    session_dir = Path(session_dir)
    try:
        return MeetingMeta.from_json(read_artifact(session_dir, _META_NAME).decode("utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        from .storage_crypto import VaultKeyLost, VaultPasswordRequired
        if isinstance(exc, (VaultKeyLost, VaultPasswordRequired)):
            raise
        try:
            from cryptography.exceptions import InvalidTag
        except ImportError:
            InvalidTag = ()
        if isinstance(exc, (InvalidTag, ValueError, UnicodeError)) and (
                session_dir / (_META_NAME + ".enc")).exists():
            logging.error("Encrypted meeting metadata failed authentication: %s",
                          anonymize_path(session_dir))
            try:
                created = int(session_dir.stat().st_mtime)
            except OSError:
                created = 0
            return MeetingMeta(SCHEMA, session_dir.name, created,
                               STATUS_CORRUPTED, PRESET_ONLYMIC, [TRACK_MIC])
        return None


def _marker_status(session_dir: Path, status):
    marker = session_dir / _ENCRYPT_MARKER
    if marker.exists():
        try:
            return json.loads(marker.read_text(encoding="utf-8"))["status"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("corrupted meeting encryption marker") from exc
    write_artifact(session_dir, _ENCRYPT_MARKER,
                   json.dumps({"status": status or STATUS_DONE}).encode("utf-8"))
    return status or STATUS_DONE


def mark_encryption_pending(session_dir: Path, status=None) -> None:
    """Queue a plaintext session for encryption once its vault is unlocked."""
    _marker_status(Path(session_dir), status)


def count_pending_encryption(root: Path, *, include_plaintext=False) -> int:
    """Count sessions that still contain data awaiting at-rest encryption."""
    root = Path(root)
    if not root.is_dir():
        return 0
    count = 0
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        marker = (session_dir / _ENCRYPT_MARKER).exists()
        plaintext = (
            include_plaintext
            and (session_dir / _META_NAME).exists()
            and not (session_dir / (_META_NAME + ".enc")).exists()
        )
        if marker or plaintext:
            count += 1
    return count


def queue_plaintext_encryption(root: Path) -> int:
    """Queue plaintext sessions left by a crash before the sealing phase.

    A recording/stopped/processing session has no live writer after restart, so
    its durable status becomes interrupted before encryption. Completed legacy
    sessions retain their recorded status.
    """
    root = Path(root)
    if not root.is_dir():
        return 0
    queued = 0
    unfinished = (STATUS_RECORDING, STATUS_STOPPED, STATUS_PROCESSING)
    for session_dir in root.iterdir():
        if not session_dir.is_dir():
            continue
        if not (session_dir / _META_NAME).exists():
            continue
        if (session_dir / (_META_NAME + ".enc")).exists():
            continue
        meta = load_meta(session_dir)
        if meta is None:
            continue
        status = meta.status
        if status in unfinished:
            meta = mark_interrupted(session_dir)
            status = getattr(meta, "status", STATUS_INTERRUPTED)
        mark_encryption_pending(session_dir, status)
        queued += 1
    return queued


def encrypt_session(session_dir: Path, dek: bytes, *, status=None) -> None:
    """Crash-resumable v2 migration of every session artifact, recursively."""
    from .storage_crypto import _decrypt_chunks, encrypt_bytes, encrypt_file
    session_dir = Path(session_dir)
    target_status = _marker_status(session_dir, status)
    meta = session_dir / _META_NAME
    temporary = []
    files = []
    for path in session_dir.rglob("*"):
        if not path.is_file() or path == meta or path.name == _ENCRYPT_MARKER:
            continue
        if path.name.endswith(".enc"):
            continue
        if path.name.endswith(".tmp"):
            temporary.append(path)
            continue
        files.append(path)
    for source in files:
        relative = source.relative_to(session_dir).as_posix()
        encrypted = Path(str(source) + ".enc")
        context = _session_context(session_dir, relative)
        if encrypted.exists():
            for _ in _decrypt_chunks(encrypted, dek, context=context):
                pass
        else:
            encrypt_file(source, encrypted, dek, context=context)
        source.unlink(missing_ok=True)
    encrypted_meta = Path(str(meta) + ".enc")
    if meta.exists():
        current = MeetingMeta.from_json(meta.read_text(encoding="utf-8"))
        current.status = target_status
        encrypt_bytes(current.to_json().encode("utf-8"), encrypted_meta, dek,
                      context=_session_context(session_dir, _META_NAME))
        meta.unlink()
    elif encrypted_meta.exists():
        for _ in _decrypt_chunks(encrypted_meta, dek,
                                 context=_session_context(session_dir, _META_NAME)):
            pass
    else:
        raise FileNotFoundError(meta)
    # Only implementation leftovers are removed, after all durable containers
    # have authenticated. No decrypted/temp plaintext survives a successful pass.
    for path in temporary:
        path.unlink(missing_ok=True)
    (session_dir / _ENCRYPT_MARKER).unlink(missing_ok=True)


def resume_encryption(root: Path) -> int:
    """Finish all marker-bearing migrations without exposing decrypted files."""
    from .storage_crypto import ensure_dek
    root = Path(root)
    pending = ([d for d in root.iterdir()
                if d.is_dir() and (d / _ENCRYPT_MARKER).exists()]
               if root.is_dir() else [])
    if not pending:
        return 0
    dek = ensure_dek(root)
    for session_dir in pending:
        encrypt_session(session_dir, dek)
    return len(pending)


def migrate_unencrypted_sessions(root: Path, dek: bytes) -> int:
    """Encrypt completed legacy sessions in place; active sessions are skipped."""
    root = Path(root)
    if not root.is_dir():
        return 0
    migrated = 0
    for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if (session_dir / (_META_NAME + ".enc")).exists():
            continue
        meta = load_meta(session_dir)
        if meta is None or meta.status in (STATUS_RECORDING, STATUS_PROCESSING):
            continue
        encrypt_session(session_dir, dek, status=meta.status)
        migrated += 1
    return migrated


def record_audio_exports(session_dir: Path, exports: dict) -> "MeetingMeta | None":
    """Атомарно записати відносні шляхи готових WAV-блоків у meeting.json."""
    session_dir = Path(session_dir).resolve()
    meta = load_meta(session_dir)
    if meta is None:
        return None
    files = {}
    for track in meta.sources:
        relative = []
        for path in exports.get(track, []) or []:
            try:
                item = Path(path).resolve().relative_to(session_dir)
            except (OSError, ValueError):
                continue
            relative.append(item.as_posix())
        if relative:
            files[str(track)] = relative
    meta.audio_files = files
    _write_meta(session_dir, meta)
    return meta


def update_processing(session_dir: Path, **changes) -> "MeetingMeta | None":
    """Атомарно оновити лише versioned metadata post-meeting обробки."""
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    state = dict(meta.processing or {})
    state.update(changes)
    meta.processing = state
    _write_meta(session_dir, meta)
    return meta


def list_sessions(root: Path) -> list:
    """Усі сесії у сховищі, новіші першими (за created; тай-брейк — id)."""
    root = Path(root)
    if not root.is_dir():
        return []
    metas = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = load_meta(d)
        if meta is not None:
            # Рубіж 1: джерело істини id — назва теки на диску (d.name — рівно
            # один компонент), а НЕ поле "id" з meeting.json. Підкладене
            # "id": "..\\інша-тека" тут перезаписується й ніколи не стає шляхом.
            meta.id = d.name
            metas.append(meta)
    metas.sort(key=lambda m: (m.created, m.id), reverse=True)
    return metas


def find_orphans(root: Path) -> list:
    """Незавершені сесії, що лишились без живого worker-а після краху.

    ``recording`` означає падіння під час захоплення, а ``stopped`` — падіння
    між finalize та завершенням postprocess. Обидві мають потрапити у recovery.
    """
    return [m for m in list_sessions(root)
            if m.status in (STATUS_RECORDING, STATUS_STOPPED)]


def finalize_dir(session_dir: Path, status: str = STATUS_STOPPED) -> "MeetingMeta | None":
    """Фіналізація сесії ЗА ТЕКОЮ (без живого MeetingSession): дорахувати duration
    з файлів на диску, оновити статус у meeting.json. Потрібна відновленню
    осиротілих — після успішної розшифровки Б2 переводить сесію на диску у
    завершений стан (finalize_dir(dir, STATUS_DONE)). None — якщо meeting.json
    нема/битий."""
    session_dir = Path(session_dir)
    meta = load_meta(session_dir)
    if meta is None:
        return None
    meta.status = status
    meta.duration = _measure_duration(session_dir, meta.rate, meta.channels, meta.sources)
    _write_meta(session_dir, meta)
    return meta


def mark_interrupted(session_dir: Path) -> "MeetingMeta | None":
    """Осиротілу сесію перевести в interrupted, дорахувавши duration з наявних
    сегментів на диску. None — якщо meeting.json нема/битий."""
    return finalize_dir(session_dir, STATUS_INTERRUPTED)


def delete_session(session_dir: Path, root: "Path | None" = None) -> bool:
    """Повне незворотне видалення: аудіо + транскрипт + meeting.json РАЗОМ (rmtree
    цілої теки). True — видалено; False — теки вже не було (повторний виклик безпечний)
    АБО шлях не пройшов захист від traversal.

    Захист від підкладеного meeting.json з id="..\\інша-тека" (кнопка видалення
    інакше передала б rmtree шлях ЗА МЕЖІ сховища):
      рубіж 2 — basename цільової теки мусить бути безпечним id (формат create_session);
      рубіж 3 — realpath теки мусить фізично лежати під root (за замовч. — батько
                теки; UI передає справжній корінь сховища). Інакше відмова + лог, БЕЗ rmtree."""
    session_dir = Path(session_dir)
    root = Path(root) if root is not None else session_dir.parent
    name = os.path.basename(os.path.normpath(str(session_dir)))
    if not is_safe_session_id(name):
        logging.warning("delete_session: небезпечний id теки %r — відмова", name)
        return False
    if not _within_root(session_dir, root):
        logging.warning("delete_session: %r поза сховищем %r — відмова",
                        anonymize_path(session_dir), anonymize_path(root))
        return False
    if not session_dir.exists():
        return False
    shutil.rmtree(session_dir)
    return True


def _measure_duration(session_dir: Path, rate: int, channels: int, sources=None) -> float:
    """Тривалість = найдовша доріжка (сумарні кадри її сегментів / rate). Рахуємо
    з розмірів файлів на диску — працює і для щойно фіналізованої, і для осиротілої."""
    bytes_per_frame = channels * _BYTES_PER_SAMPLE
    longest = 0
    for track in (sources or TRACKS):
        tdir = Path(session_dir) / track
        if not tdir.is_dir():
            continue
        total = 0
        for seg in tdir.glob("*.f32"):
            try:
                total += seg.stat().st_size
            except OSError:
                pass
        longest = max(longest, total // bytes_per_frame)
    return round(longest / rate, 3) if rate else 0.0
