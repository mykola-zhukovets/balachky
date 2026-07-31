"""Вартові порядку durable-запису для критичних файлових шляхів.

Тести доводять виклик ``fsync`` для дескриптора саме staged/append-файла.
Вони не моделюють фізичний диск: фактична стійкість після втрати живлення
також залежить від ОС, файлової системи, контролера й кешу накопичувача.
"""

import ast
import builtins
import contextlib
import importlib.util
import os
import shutil
import tempfile
import unittest
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np

from whisper_core import config
from whisper_core import history
from whisper_core.meeting import audit_log
from whisper_core.meeting import media
from whisper_core.meeting import postprocess
from whisper_core.meeting import screen_record
from whisper_core.meeting import session as meeting_session
from whisper_core.meeting import storage_crypto


_HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized(path):
    return Path(path).resolve()


@contextlib.contextmanager
def _temporary_directory():
    parent = _REPO_ROOT / "tests"
    path = parent / f".fsync-durability-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield str(path)
    finally:
        if path.parent != parent or not path.name.startswith(
                ".fsync-durability-"):
            raise AssertionError(f"unsafe test cleanup path: {path}")
        shutil.rmtree(path)


@dataclass(frozen=True)
class _Event:
    action: str
    path: "Path | None"
    fd: "int | None" = None
    destination: "Path | None" = None


class _TracedFile:
    def __init__(self, stream, path, trace):
        self._stream = stream
        self._path = _normalized(path)
        self._trace = trace
        self._trace.fd_paths[stream.fileno()] = self._path

    def write(self, data):
        result = self._stream.write(data)
        self._trace.events.append(
            _Event("write", self._path, self._stream.fileno()))
        return result

    def flush(self):
        result = self._stream.flush()
        self._trace.events.append(
            _Event("flush", self._path, self._stream.fileno()))
        return result

    def fileno(self):
        return self._stream.fileno()

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._stream.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _DurabilityTrace:
    def __init__(self):
        self.events = []
        self.fd_paths = {}
        self._real_open = builtins.open
        self._real_path_open = Path.open
        self._real_fdopen = os.fdopen
        self._real_mkstemp = tempfile.mkstemp
        self._real_replace = os.replace

    @staticmethod
    def _writable(mode):
        return any(flag in mode for flag in ("w", "a", "x", "+"))

    def _wrap(self, stream, path, mode):
        if path is None:
            return stream
        path = _normalized(path)
        self.fd_paths[stream.fileno()] = path
        if not self._writable(mode):
            return stream
        return _TracedFile(stream, path, self)

    def open(self, file, mode="r", *args, **kwargs):
        stream = self._real_open(file, mode, *args, **kwargs)
        path = file if isinstance(file, (str, bytes, os.PathLike)) else None
        return self._wrap(stream, path, mode)

    def path_open(self, path, mode="r", *args, **kwargs):
        stream = self._real_path_open(path, mode, *args, **kwargs)
        return self._wrap(stream, path, mode)

    def mkstemp(self, *args, **kwargs):
        fd, name = self._real_mkstemp(*args, **kwargs)
        self.fd_paths[fd] = _normalized(name)
        return fd, name

    def fdopen(self, fd, mode="r", *args, **kwargs):
        stream = self._real_fdopen(fd, mode, *args, **kwargs)
        return self._wrap(stream, self.fd_paths.get(fd), mode)

    def fsync(self, fd):
        self.events.append(_Event("fsync", self.fd_paths.get(fd), fd))

    def replace(self, source, destination):
        source = _normalized(source)
        destination = _normalized(destination)
        self.events.append(
            _Event("replace", source, destination=destination))
        return self._real_replace(source, destination)

    def unlock(self, path):
        self.events.append(_Event("unlock", _normalized(path)))

    @contextlib.contextmanager
    def active(self):
        def traced_path_open(path, mode="r", *args, **kwargs):
            return self.path_open(path, mode, *args, **kwargs)

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(builtins, "open", self.open))
            stack.enter_context(mock.patch.object(Path, "open", traced_path_open))
            stack.enter_context(mock.patch.object(tempfile, "mkstemp", self.mkstemp))
            stack.enter_context(mock.patch.object(os, "fdopen", self.fdopen))
            stack.enter_context(mock.patch.object(os, "fsync", self.fsync))
            stack.enter_context(mock.patch.object(os, "replace", self.replace))
            yield self

    def staged_source_for(self, destination):
        destination = _normalized(destination)
        sources = [
            event.path for event in self.events
            if event.action == "replace" and event.destination == destination
        ]
        if len(sources) != 1:
            raise AssertionError(
                f"expected one replace into {destination}, got {sources!r}")
        return sources[0]

    def assert_order(self, case, path, expected, *, same_descriptor=True):
        path = _normalized(path)
        matching = [event for event in self.events if event.path == path]
        cursor = -1
        selected = []
        for action in expected:
            try:
                cursor = next(
                    index for index in range(cursor + 1, len(matching))
                    if matching[index].action == action)
            except StopIteration:
                case.fail(
                    f"{path}: expected order {expected!r}; actual events "
                    f"{[(event.action, event.fd) for event in matching]!r}")
            selected.append(matching[cursor])
        if same_descriptor:
            file_fds = {
                event.fd for event in selected
                if event.action in {"write", "flush", "fsync"}
            }
            case.assertEqual(
                len(file_fds), 1,
                f"{path}: write/flush/fsync must use one file descriptor; "
                f"events={selected!r}",
            )


class _FakeFrame:
    def reformat(self, **_kwargs):
        return self


class _FakeVideoFrame:
    @staticmethod
    def from_ndarray(_data, format):
        if format != "bgra":
            raise AssertionError(format)
        return _FakeFrame()


class _FakeAudioFrame:
    @staticmethod
    def from_ndarray(_data, format, layout):
        if (format, layout) != ("fltp", "mono"):
            raise AssertionError((format, layout))
        return SimpleNamespace(sample_rate=None)


class _FakeStream:
    def encode(self, frame=None):
        return [b"packet"] if frame is not None else []


class _WritingContainer:
    def __init__(self, path):
        self._file = open(path, "wb")
        self._file.write(b"header")
        self._stream = _FakeStream()

    def add_stream(self, _codec, rate):
        self._stream.rate = rate
        return self._stream

    def mux(self, _packet):
        self._file.write(b"packet")

    def close(self):
        self._file.write(b"trailer")
        self._file.flush()
        self._file.close()


class _WritingAv:
    VideoFrame = _FakeVideoFrame
    AudioFrame = _FakeAudioFrame

    def open(self, path, mode, format=None):
        if mode != "w":
            raise AssertionError(mode)
        return _WritingContainer(path)


class _ScreenSource:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def bounds(self, _monitor_index):
        return {"width": 8, "height": 6}

    def grab(self, _monitor_index):
        return np.zeros((6, 8, 4), dtype=np.uint8)


class FsyncDurabilityBehaviorTests(unittest.TestCase):
    def _assert_destination_absent_at_fsync(self, trace, destination):
        states = []
        traced_fsync = trace.fsync

        def checking_fsync(fd):
            states.append(destination.exists())
            traced_fsync(fd)

        return states, mock.patch.object(trace, "fsync", checking_fsync)

    def _audit_trace(self, session_dir):
        trace = _DurabilityTrace()
        real_history_lock = audit_log.history_lock

        @contextlib.contextmanager
        def traced_history_lock(path, *args, **kwargs):
            with real_history_lock(path, *args, **kwargs):
                yield
            trace.unlock(path)

        with trace.active(), mock.patch.object(
                audit_log, "history_lock", traced_history_lock):
            audit_log.append_event(
                session_dir, audit_log.EVENT_CREATED, ts=1.0)
        return trace

    def test_audit_append_fsyncs_journal_before_releasing_its_lock(self):
        with _temporary_directory() as tmp:
            session_dir = Path(tmp) / "session"
            trace = self._audit_trace(session_dir)
            trace.assert_order(
                self,
                session_dir / "audit.jsonl",
                ("write", "flush", "fsync", "unlock"),
            )

    def test_audit_head_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            session_dir = Path(tmp) / "session"
            trace = self._audit_trace(session_dir)
            head = session_dir / ".audit.head"
            staged = trace.staged_source_for(head)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    @unittest.skipUnless(_HAS_CRYPTO, "cryptography unavailable")
    def test_encrypted_container_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "transcript.txt.enc"
            trace = _DurabilityTrace()
            with trace.active():
                storage_crypto.encrypt_bytes(
                    b"durable transcript",
                    destination,
                    os.urandom(32),
                    context=b"session/transcript.txt",
                )
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    def test_config_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "config.toml"
            trace = _DurabilityTrace()
            with trace.active():
                config._atomic_write_text(destination, "theme = \"dark\"\n")
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    def test_plain_artifact_file_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            source = Path(tmp) / "capture.wav"
            source.write_bytes(b"meeting recording")
            session_dir = Path(tmp) / "session"
            destination = session_dir / "audio" / "mic.wav"
            trace = _DurabilityTrace()
            with trace.active():
                result = meeting_session.write_artifact_file(
                    session_dir, Path("audio") / "mic.wav", source)

            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    def test_dictation_history_append_fsyncs_before_releasing_lock(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "history.jsonl"
            trace = _DurabilityTrace()
            real_history_lock = history.history_lock

            @contextlib.contextmanager
            def traced_history_lock(path, *args, **kwargs):
                with real_history_lock(path, *args, **kwargs):
                    yield
                trace.unlock(path)

            with trace.active(), mock.patch.object(
                    history, "history_lock", traced_history_lock):
                record = history.log_history(
                    destination, "сирий текст", "готовий текст")

            self.assertIsNotNone(record)
            trace.assert_order(
                self,
                destination,
                ("write", "flush", "fsync", "unlock"),
            )

    def test_dictation_history_rewrite_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "history.jsonl"
            record = history.log_history(
                destination, "сирий текст", "готовий текст")
            self.assertIsNotNone(record)
            trace = _DurabilityTrace()
            with trace.active():
                updated = history.update_final_by_id(
                    destination, record["id"], "виправлений текст")
            self.assertTrue(updated)
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    def test_screen_record_fsyncs_finalized_stage_before_replace(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "screen.webm"
            trace = _DurabilityTrace()
            states, check_fsync = self._assert_destination_absent_at_fsync(
                trace, destination)
            recorder = screen_record.ScreenRecorder(
                source_factory=_ScreenSource, av_module=_WritingAv())
            with check_fsync, trace.active():
                self.assertTrue(recorder.start(1, destination, fps=15))
                self.assertTrue(recorder.wait_started(1.0))
                recorder.request_stop()
                self.assertTrue(recorder.wait_finished(1.0))

            self.assertTrue(recorder.finished_ok)
            self.assertEqual(states, [False])
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self,
                staged,
                ("write", "flush", "fsync", "replace"),
                same_descriptor=False,
            )

    def test_wav_export_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            destination = Path(tmp) / "clip.wav"
            trace = _DurabilityTrace()
            states, check_fsync = self._assert_destination_absent_at_fsync(
                trace, destination)
            with check_fsync, trace.active():
                media.write_wav(
                    destination,
                    np.array([0.0, 0.25, -0.25], dtype=np.float32),
                    16000,
                )

            self.assertEqual(states, [False])
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self,
                staged,
                ("write", "flush", "fsync", "replace"),
                same_descriptor=False,
            )

    def test_meeting_raw_segment_fsyncs_on_rotation_and_on_finalize(self):
        """§1: живі *.f32-сегменти наради. Ротація трапляється кожні
        segment_seconds (~45с у продакшні) — саме там fsync, без окремого
        виклику на кожен вхідний блок PCM (це дало б затримку живого захоплення).
        Хвостовий сегмент коротший за повний період і fsync'ається окремо
        у finalize()."""
        with _temporary_directory() as tmp:
            trace = _DurabilityTrace()
            with trace.active():
                rec = meeting_session.MeetingSession(
                    Path(tmp) / "meetings", ["mic"], rate=1, channels=1,
                    segment_seconds=1)
                rec.mic_sink(b"\x00\x00\x00\x00")  # 1 frame == segment_frames -> rotate
                rec.mic_sink(b"\x00\x00\x00\x00")  # tail segment, closed by finalize
                rec.finalize()
            rotated = rec.dir / "mic" / "0000.f32"
            tail = rec.dir / "mic" / "0001.f32"
            trace.assert_order(self, rotated, ("write", "flush", "fsync"))
            trace.assert_order(self, tail, ("write", "flush", "fsync"))

    def test_meeting_flat_wav_fsyncs_staged_file_before_replace(self):
        """§2: плаский mic.wav/sys.wav (build_wav → _stream_wav)."""
        with _temporary_directory() as tmp:
            session_dir = Path(tmp) / "session"
            track_dir = session_dir / "mic"
            track_dir.mkdir(parents=True)
            samples = np.array([0.1, -0.2, 0.3, -0.1], dtype=np.float32)
            (track_dir / "0000.f32").write_bytes(samples.tobytes())
            (session_dir / "meeting.json").write_text(
                '{"rate": 4, "channels": 1}', encoding="utf-8")
            destination = session_dir / "mic.wav"
            trace = _DurabilityTrace()
            with trace.active():
                result = postprocess.build_wav(session_dir, "mic", out_rate=4)
            self.assertEqual(result, destination)
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self, staged, ("write", "flush", "fsync", "replace"))

    def test_meeting_segmented_wav_fsyncs_staged_file_before_replace(self):
        """§3: сегментовані доріжки для розшифровки (build_segmented_wavs)."""
        with _temporary_directory() as tmp:
            session_dir = Path(tmp) / "session"
            track_dir = session_dir / "mic"
            track_dir.mkdir(parents=True)
            samples = np.array([0.2] * 8, dtype=np.float32)
            (track_dir / "0000.f32").write_bytes(samples.tobytes())
            trace = _DurabilityTrace()
            with trace.active():
                paths = postprocess._segmented_track_wavs(
                    session_dir, "mic", rate=4, channels=1,
                    segment_seconds=1, out_rate=4)[0]
            self.assertTrue(paths)
            for destination in paths:
                staged = trace.staged_source_for(destination)
                trace.assert_order(
                    self, staged, ("write", "flush", "fsync", "replace"))

    def test_encoded_audio_export_fsyncs_staged_file_before_replace(self):
        with _temporary_directory() as tmp:
            root = Path(tmp)
            source = media.write_wav(
                root / "source.wav",
                np.array([0.0, 0.25, -0.25], dtype=np.float32),
                16000,
            )
            destination = root / "clip.mp3"
            trace = _DurabilityTrace()
            states, check_fsync = self._assert_destination_absent_at_fsync(
                trace, destination)
            with (
                mock.patch.object(
                    media, "available_formats",
                    return_value={"mp3": "libmp3lame"}),
                mock.patch.dict("sys.modules", {"av": _WritingAv()}),
                check_fsync,
                trace.active(),
            ):
                media.export_audio([source], destination, "mp3")

            self.assertEqual(states, [False])
            staged = trace.staged_source_for(destination)
            trace.assert_order(
                self,
                staged,
                ("write", "flush", "fsync", "replace"),
                same_descriptor=False,
            )


class _DirectCallCollector(ast.NodeVisitor):
    def __init__(self):
        self.calls = []

    def visit_Call(self, node):
        self.calls.append((node.lineno, self._call_name(node.func)))
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        return

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        return

    @staticmethod
    def _call_name(node):
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return ".".join(reversed(parts))

    @classmethod
    def collect(cls, function):
        visitor = cls()
        for statement in function.body:
            visitor.visit(statement)
        return visitor.calls


class StagedWriteAstGuardTests(unittest.TestCase):
    """Підстраховка: новий staged-write не може тихо оминути fsync."""

    _KNOWN_WITHOUT_FSYNC = {
        "fronts/desktop/onboarding.py:resumable_download_file",
        "whisper_core/autocorrect_download.py:download_and_install",
        "whisper_core/meeting/diarization_models.py:download_and_install",
        "whisper_core/models.py:dereference_snapshot",
        "whisper_core/protocol/model_manager.py:_install_from_url",
        "whisper_core/punctuator.py:download_and_install",
        "whisper_core/tts/voices.py:download_and_install",
        "whisper_core/tts/worker.py:synthesize_stream",
        "whisper_core/updater.py:download_installer",
    }

    @staticmethod
    def _looks_like_staged_write(call_names):
        direct_writes = {
            "av.open",
            "json.dump",
            "shutil.copyfile",
            "self._av.open",
            "shutil.copyfileobj",
            "wave.open",
            "write_wav",
        }
        return any(
            name in direct_writes
            or name.endswith((".write", ".write_bytes", ".write_text"))
            or "download" in name.lower()
            for name in call_names
        )

    def test_every_new_file_staged_write_has_fsync_before_replace(self):
        missing = set()
        wrong_order = set()
        for source_root in ("whisper_core", "fronts"):
            for path in (_REPO_ROOT / source_root).rglob("*.py"):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                relative = path.relative_to(_REPO_ROOT).as_posix()
                for function in ast.walk(tree):
                    if not isinstance(
                            function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    calls = _DirectCallCollector.collect(function)
                    names = [name for _, name in calls]
                    if ("os.replace" not in names
                            or not self._looks_like_staged_write(names)):
                        continue
                    key = f"{relative}:{function.name}"
                    fsync_lines = [
                        line for line, name in calls if name == "os.fsync"]
                    replace_lines = [
                        line for line, name in calls if name == "os.replace"]
                    if not fsync_lines:
                        missing.add(key)
                    elif min(fsync_lines) > min(replace_lines):
                        wrong_order.add(key)

        self.assertEqual(
            missing,
            self._KNOWN_WITHOUT_FSYNC,
            "Classify every staged-write without file fsync explicitly; "
            "new paths must add durability or a reviewed exception.",
        )
        self.assertEqual(
            wrong_order,
            set(),
            "File fsync must occur before the first os.replace.",
        )


if __name__ == "__main__":
    unittest.main()
