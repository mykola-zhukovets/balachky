import logging
import logging.handlers
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from whisper_core import updates
from whisper_core.config import Config
from whisper_core.engine import (
    Engine, MODEL_REVISIONS, ModelRevisionUnavailable,
    cuda_runtime_available, is_cuda_runtime_error,
)
from whisper_core.models import resolve_cache_dir


MODEL_NAME = "large-v3"
REPO_ID = "Systran/faster-whisper-large-v3"
REVISION = MODEL_REVISIONS[MODEL_NAME]


def _make_snapshot(hub: Path) -> Path:
    snap = (hub / "models--Systran--faster-whisper-large-v3"
            / "snapshots" / REVISION)
    snap.mkdir(parents=True)
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        (snap / name).write_bytes(b"x")
    return snap


def _cfg(model_dir):
    return SimpleNamespace(
        model_name=MODEL_NAME,
        device="cuda",
        compute_type="float16",
        model_dir=str(model_dir),
    )


class CachePathTests(unittest.TestCase):
    def test_every_hf_cache_level_resolves_to_hub_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            hf_home = Path(tmp) / "huggingface"
            hub = hf_home / "hub"
            snap = _make_snapshot(hub)
            repo_dir = snap.parent.parent
            snapshots = snap.parent

            for selected in (hf_home, hub, repo_dir, snapshots, snap):
                with self.subTest(selected=selected):
                    self.assertEqual(resolve_cache_dir(selected), os.path.normpath(str(hub)))

    def test_config_saves_and_loads_canonical_hub_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hf" / "hub"
            snap = _make_snapshot(hub)
            config_path = Path(tmp) / "config.toml"

            cfg = Config(model_dir=str(snap))
            cfg.save(config_path)
            self.assertEqual(cfg.model_dir, os.path.normpath(str(hub)))

            loaded = Config.load(config_path)
            self.assertEqual(loaded.model_dir, os.path.normpath(str(hub)))

    def test_engine_passes_canonical_hub_root_to_faster_whisper(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hf" / "hub"
            snap = _make_snapshot(hub)
            with patch(
                    "whisper_core.models.model_snapshot_integrity",
                    return_value=True), \
                    patch("whisper_core.engine.WhisperModel") as model:
                Engine(_cfg(snap))
            self.assertEqual(model.call_args.kwargs["download_root"],
                             os.path.normpath(str(hub)))


class EngineErrorTests(unittest.TestCase):
    def test_file_not_found_is_typed_even_when_main_snapshot_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            _make_snapshot(hub)
            with patch("whisper_core.engine.WhisperModel",
                       side_effect=FileNotFoundError("missing model asset")):
                with self.assertRaises(ModelRevisionUnavailable):
                    Engine(_cfg(hub))

    def test_local_entry_missing_is_typed_even_when_main_snapshot_files_exist(self):
        from huggingface_hub.errors import LocalEntryNotFoundError

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            _make_snapshot(hub)
            with patch("whisper_core.engine.WhisperModel",
                       side_effect=LocalEntryNotFoundError("missing cache entry")):
                with self.assertRaises(ModelRevisionUnavailable):
                    Engine(_cfg(hub))

    def test_cuda_runtime_error_is_not_masked_when_snapshot_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            _make_snapshot(hub)
            with patch(
                    "whisper_core.models.model_snapshot_integrity",
                    return_value=True), \
                    patch("whisper_core.engine.WhisperModel",
                          side_effect=RuntimeError("CUDA out of memory")):
                with self.assertRaisesRegex(RuntimeError, "CUDA out of memory"):
                    Engine(_cfg(hub))

    def test_driver_oserror_is_not_masked_when_snapshot_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            _make_snapshot(hub)
            with patch(
                    "whisper_core.models.model_snapshot_integrity",
                    return_value=True), \
                    patch("whisper_core.engine.WhisperModel",
                          side_effect=OSError("CUDA driver DLL is missing")):
                with self.assertRaisesRegex(OSError, "driver DLL"):
                    Engine(_cfg(hub))

    def test_corrupt_managed_snapshot_is_rejected_before_model_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            _make_snapshot(hub)
            with patch(
                    "whisper_core.models.model_snapshot_integrity",
                    return_value=False), \
                    patch("whisper_core.engine.WhisperModel") as model:
                with self.assertRaises(ModelRevisionUnavailable):
                    Engine(_cfg(hub))
            model.assert_not_called()

    def test_unpinned_model_runtime_error_is_not_masked_when_snapshot_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            # власна (немапована) модель — без піна в MODEL_REVISIONS: вантажиться
            # як main, тож перевірка usable іде через local_snapshot_revision
            snap = (hub / "models--owner--custom-model"
                    / "snapshots" / "local-main")
            snap.mkdir(parents=True)
            for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
                (snap / name).write_bytes(b"x")
            cfg = SimpleNamespace(
                model_name="owner/custom-model", device="cuda",
                compute_type="float16", model_dir=str(hub),
            )
            with patch("whisper_core.engine.WhisperModel",
                       side_effect=RuntimeError("CUDA out of memory")):
                with self.assertRaisesRegex(RuntimeError, "CUDA out of memory"):
                    Engine(cfg)

    def test_model_error_is_typed_when_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            hub.mkdir()
            with patch("whisper_core.engine.WhisperModel",
                       side_effect=RuntimeError("Unable to open file model.bin")):
                with self.assertRaises(ModelRevisionUnavailable):
                    Engine(_cfg(hub))


class ConfigFallbackTests(unittest.TestCase):
    def test_invalid_utf8_uses_defaults_without_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_bytes(b"\xff\xfe")
            with patch("builtins.print") as output, self.assertLogs(
                    "whisper_core.config", level="WARNING"):
                cfg = Config.load(config_path)
            output.assert_not_called()
            self.assertEqual(cfg.model_name, Config().model_name)

    def test_invalid_toml_uses_defaults_without_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model_name = "unterminated', encoding="utf-8")
            with patch("builtins.print") as output, self.assertLogs(
                    "whisper_core.config", level="WARNING"):
                cfg = Config.load(config_path)
            output.assert_not_called()
            self.assertEqual(cfg.model_name, Config().model_name)

    def test_read_oserror_uses_defaults_without_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.touch()
            with patch.object(Path, "read_text", side_effect=OSError("denied")), \
                    patch("builtins.print") as output, self.assertLogs(
                        "whisper_core.config", level="WARNING"):
                cfg = Config.load(config_path)
            output.assert_not_called()
            self.assertEqual(cfg.model_name, Config().model_name)

    def test_save_oserror_is_logged_without_print(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            # save пише через _atomic_write_text (temp + os.replace), а не
            # Path.write_text — мокаємо саме той шлях, поведінка перевіряється та сама
            with patch("whisper_core.config._atomic_write_text",
                       side_effect=OSError("denied")), \
                    patch("builtins.print") as output, self.assertLogs(
                        "whisper_core.config", level="ERROR"):
                Config().save(config_path)
            output.assert_not_called()


class UpdateTests(unittest.TestCase):
    def test_http_404_is_unavailable_not_up_to_date(self):
        error = urllib.error.HTTPError(
            updates.API_URL, 404, "Not Found", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=error):
            result = updates.check_latest("1.0.0", etag='"old"')
        self.assertEqual(result.status, updates.OFFLINE)
        self.assertNotEqual(result.status, updates.UP_TO_DATE)
        self.assertEqual(result.etag, '"old"')

    def test_only_canonical_github_release_urls_are_displayable(self):
        self.assertTrue(updates.is_release_url(
            "https://github.com/mykola-zhukovets/balachky/releases/tag/v1.1.0"))
        self.assertFalse(updates.is_release_url("https://example.invalid/release"))
        self.assertFalse(updates.is_release_url(
            "http://github.com/mykola-zhukovets/balachky/releases/tag/v1.1.0"))
        self.assertFalse(updates.is_release_url(None))


class CudaRuntimeTests(unittest.TestCase):
    # feature/gpu: коли докачаного рантайму нема (runtime_ready=False), доступність
    # визначається системним пошуком DLL. Набір скоротився до двох cuBLAS
    # (cudnn для ct2 4.7.2 не потрібен).
    @patch("whisper_core.engine.cuda_runtime.runtime_ready", return_value=False)
    @patch("whisper_core.engine.sys.platform", "win32")
    @patch("whisper_core.engine._load_windows_dll")
    @patch("whisper_core.engine.ctranslate2.get_cuda_device_count", return_value=1)
    def test_windows_gpu_requires_every_runtime_dll(self, _count, load_dll, _ready):
        load_dll.side_effect = [object(), OSError("missing cublasLt")]
        self.assertFalse(cuda_runtime_available())

    @patch("whisper_core.engine.cuda_runtime.runtime_ready", return_value=False)
    @patch("whisper_core.engine.sys.platform", "win32")
    @patch("whisper_core.engine._load_windows_dll", return_value=object())
    @patch("whisper_core.engine.ctranslate2.get_cuda_device_count", return_value=1)
    def test_windows_gpu_with_complete_runtime_is_available(
            self, _count, load_dll, _ready):
        self.assertTrue(cuda_runtime_available())
        self.assertEqual(load_dll.call_count, 2)   # cublas + cublasLt (без cudnn)

    @patch("whisper_core.engine.cuda_runtime.runtime_ready", return_value=True)
    @patch("whisper_core.engine.sys.platform", "win32")
    @patch("whisper_core.engine._load_windows_dll")
    @patch("whisper_core.engine.ctranslate2.get_cuda_device_count", return_value=1)
    def test_downloaded_runtime_short_circuits_dll_probe(
            self, _count, load_dll, _ready):
        # докачаний рантайм готовий → доступно без системного пошуку DLL
        self.assertTrue(cuda_runtime_available())
        load_dll.assert_not_called()

    @patch("whisper_core.engine._load_windows_dll")
    @patch("whisper_core.engine.ctranslate2.get_cuda_device_count", return_value=0)
    def test_no_gpu_short_circuits_runtime_probe(self, _count, load_dll):
        self.assertFalse(cuda_runtime_available())
        load_dll.assert_not_called()

    def test_cuda_error_classifier_is_narrow(self):
        self.assertTrue(is_cuda_runtime_error(
            RuntimeError("Library cublas64_12.dll is not found")))
        self.assertTrue(is_cuda_runtime_error(RuntimeError("CUDA out of memory")))
        self.assertFalse(is_cuda_runtime_error(RuntimeError("model.bin is missing")))


class DesktopMigrationsTests(unittest.TestCase):
    def test_cpu_config_normalizes_cuda_only_compute_type(self):
        from fronts.desktop.app import _prepare_cpu_config

        cfg = SimpleNamespace(device="cuda", compute_type="float16")
        self.assertIs(_prepare_cpu_config(cfg), cfg)
        self.assertEqual((cfg.device, cfg.compute_type), ("cpu", "int8"))

    def test_startup_cpu_float16_is_normalized_and_saved(self):
        from fronts.desktop.app import _normalize_startup_config

        class Cfg:
            device = "cpu"
            compute_type = "float16"
            saved = False

            def save(self):
                self.saved = True

        cfg = Cfg()
        self.assertFalse(_normalize_startup_config(
            cfg, frozen=False, cuda_available=True))
        self.assertEqual((cfg.device, cfg.compute_type), ("cpu", "int8"))
        self.assertTrue(cfg.saved)

    def test_legacy_update_cache_is_cleared_without_network(self):
        from fronts.desktop import app as desktop_app

        class FakeSettings:
            data = {
                "update_latest": "1.1.0",
                "update_url": "https://example.invalid/release",
                "update_etag": '"test"',
                "update_last_check": 123,
            }

            def __init__(self, *_args):
                pass

            def value(self, key, default=None, type=None):
                value = self.data.get(key, default)
                return type(value) if type is not None else value

            def remove(self, key):
                self.data.pop(key, None)

            def setValue(self, key, value):
                self.data[key] = value

        with patch.object(desktop_app, "QSettings", FakeSettings):
            desktop_app._migrate_update_cache()
        self.assertNotIn("update_latest", FakeSettings.data)
        self.assertNotIn("update_url", FakeSettings.data)
        self.assertEqual(FakeSettings.data["update_cache_schema"], 1)

    def test_existing_autostart_file_is_rewritten_with_mode_flag(self):
        from fronts.desktop import autostart

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(autostart.paths, "FROZEN", True), \
                patch.object(autostart.sys, "executable", r"C:\\Balachky\\Balachky.exe"):
            target = Path(tmp) / autostart.BAT_NAME
            target.write_text("@echo off\nold command\n", encoding="utf-8")
            self.assertTrue(autostart.refresh_if_enabled(tmp))
            self.assertIn(b"--autostart", target.read_bytes())

    def test_unicode_autostart_migration_error_does_not_block_startup(self):
        from fronts.desktop import app as desktop_app

        error = UnicodeEncodeError("mbcs", "λ", 0, 1, "not representable")
        with patch.object(desktop_app.paths, "FROZEN", True), \
                patch("fronts.desktop.autostart.refresh_if_enabled",
                      side_effect=error), \
                self.assertLogs(level="ERROR"):
            self.assertFalse(desktop_app._refresh_frozen_autostart())

    def test_runtime_cuda_failure_retries_same_audio_on_cpu(self):
        from fronts.desktop.app import DesktopApp

        expected = ("raw", "final", 1.0, [], [])

        class Cfg:
            device = "cuda"
            compute_type = "float16"
            saved = False

            def save(self):
                self.saved = True

        class GpuEngine:
            def transcribe(self, _audio, _terms):
                raise RuntimeError("cublas64_12.dll is not found")

        class CpuEngine:
            cfg = None

            def transcribe(self, audio, _terms):
                self.audio = audio
                return expected

        class Signal:
            def __init__(self):
                self.count = 0

            def emit(self):
                self.count += 1

        class Settings:
            device = None

            def sync_device(self, device):
                self.device = device

        class Tray:
            messages = []

            def notify(self, message):
                self.messages.append(message)

        cfg = Cfg()
        cpu = CpuEngine()
        fallback_signal = Signal()
        controller = SimpleNamespace(
            _engine_lock=threading.Lock(), engine=GpuEngine(), cfg=cfg,
            cpu_fallback=fallback_signal,
            window=SimpleNamespace(settings=Settings()), tray=Tray(),
        )
        def build_cpu(cpu_cfg):
            self.assertEqual((cpu_cfg.device, cpu_cfg.compute_type), ("cpu", "int8"))
            return cpu

        with patch("fronts.desktop.app.Engine", side_effect=build_cpu):
            result = DesktopApp._transcribe_with_fallback(
                controller, "same-audio", object())
        self.assertEqual(result, expected)
        self.assertEqual(cpu.audio, "same-audio")
        # Worker не мутує і не пише shared cfg; queued GUI-slot робить це далі.
        self.assertEqual(cfg.device, "cuda")
        self.assertFalse(cfg.saved)
        self.assertEqual(fallback_signal.count, 1)
        DesktopApp._apply_runtime_cpu_fallback(controller)
        self.assertEqual(cfg.device, "cpu")
        self.assertEqual(cfg.compute_type, "int8")
        self.assertTrue(cfg.saved)
        self.assertEqual(controller.window.settings.device, "cpu")
        self.assertTrue(controller.tray.messages)


class DictationSilenceTests(unittest.TestCase):
    """PTT-конвеєр: порожній запис і порожнє розпізнавання (заглушений мікрофон)
    мусять давати явний сигнал transcription_error, а не мовчазний return."""

    @staticmethod
    def _controller(to_audio, transcribe):
        class ErrorSignal:
            def __init__(self):
                self.messages = []

            def emit(self, message):
                self.messages.append(message)

        class FinishedSignal:
            def __init__(self):
                self.count = 0

            def emit(self):
                self.count += 1

        return SimpleNamespace(
            recorder=SimpleNamespace(to_audio=to_audio),
            _transcribe_with_fallback=transcribe,
            transcription_error=ErrorSignal(),
            finished=FinishedSignal(),
            _busy=True,
        )

    def test_none_audio_emits_silence_error(self):
        from fronts.desktop.app import DesktopApp
        from fronts.desktop.i18n import tr

        transcribe_calls = []
        controller = self._controller(
            to_audio=lambda chunks: None,
            transcribe=lambda *a: transcribe_calls.append(a))
        with self.assertLogs(level="WARNING"):
            DesktopApp._work(controller, [], object(), object())
        self.assertEqual(controller.transcription_error.messages,
                         [tr("app_dictation_silence")])
        self.assertEqual(transcribe_calls, [])   # до рушія справа не доходить
        self.assertFalse(controller._busy)
        self.assertEqual(controller.finished.count, 1)

    def test_empty_final_emits_silence_error(self):
        from fronts.desktop.app import DesktopApp
        from fronts.desktop.i18n import tr

        controller = self._controller(
            to_audio=lambda chunks: "audio",
            transcribe=lambda audio, terms, **_kw: ("raw", "", 1.0, [], []))
        with self.assertLogs(level="WARNING"):
            DesktopApp._work(controller, ["chunk"], object(), object())
        self.assertEqual(controller.transcription_error.messages,
                         [tr("app_dictation_silence")])
        self.assertFalse(controller._busy)
        self.assertEqual(controller.finished.count, 1)


class PasteFailureTests(unittest.TestCase):
    """PTT-конвеєр: якщо режим виводу передбачає вставку, а paste_text не зміг
    вставити жодним зі способів (повернув None), користувач має отримати трей-
    повідомлення. Текст уже в буфері й у стрічці — тихо втрачати вставку не можна.
    """

    @staticmethod
    def _controller(output_mode="paste"):
        class ErrorSignal:
            def __init__(self):
                self.messages = []

            def emit(self, message):
                self.messages.append(message)

        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *args):
                self.count += 1

        return SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: (
                "raw", "final", 1.0, [], []),
            output_mode=output_mode,
            transcription_error=ErrorSignal(),
            transcribed=Counter(),
            finished=Counter(),
            # restore_clipboard вимкнено: ці тести — про повідомлення вставки,
            # а не про буфер (відновлення покрито ClipboardRestoreWorkTests)
            cfg=SimpleNamespace(sounds=False, restore_clipboard=False),
            _busy=True,
        )

    def test_failed_paste_emits_tray_message(self):
        from fronts.desktop import app as desktop_app
        from fronts.desktop.i18n import tr

        controller = self._controller(output_mode="paste")
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text", return_value=None), \
                patch.object(desktop_app, "log_history", return_value=None), \
                self.assertLogs(level="WARNING"):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(controller.transcription_error.messages,
                         [tr("app_paste_failed")])
        self.assertEqual(controller.transcribed.count, 1)   # картку все одно видно
        self.assertFalse(controller._busy)
        self.assertEqual(controller.finished.count, 1)

    def test_successful_paste_stays_silent(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(output_mode="paste")
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text", return_value="pyautogui"), \
                patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        self.assertEqual(controller.transcription_error.messages, [])
        self.assertEqual(controller.transcribed.count, 1)
        self.assertFalse(controller._busy)

    def test_show_only_mode_never_pastes(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(output_mode="show")
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text") as paste, \
                patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        paste.assert_not_called()
        self.assertEqual(controller.transcription_error.messages, [])


class ClipboardRestoreHelperTests(unittest.TestCase):
    """paste.py: знімок і відновлення буфера — з mock замість реального буфера.
    КОНСТРЕЙНТ: не-текст (зображення/файли) → pyperclip дає '' → не відновлюємо.
    """

    def test_snapshot_returns_current_text(self):
        from fronts.desktop import paste

        with patch.object(paste.pyperclip, "paste", return_value="старе"):
            self.assertEqual(paste.snapshot_clipboard(), "старе")

    def test_snapshot_returns_empty_when_clipboard_unreadable(self):
        from fronts.desktop import paste

        with patch.object(paste.pyperclip, "paste",
                          side_effect=Exception("буфер недоступний")):
            self.assertEqual(paste.snapshot_clipboard(), "")

    def test_restore_puts_back_previous_text_after_delay(self):
        from fronts.desktop import paste

        started = []

        class FakeTimer:
            def __init__(self, delay, fn, args=()):
                self.delay, self.fn, self.args = delay, fn, args

            def start(self):
                started.append(self.delay)
                self.fn(*self.args)          # у тесті виконуємо негайно

        with patch.object(paste.threading, "Timer", FakeTimer), \
                patch.object(paste.pyperclip, "copy") as copy:
            paste.restore_clipboard("старе", delay=0.4)
        copy.assert_called_once_with("старе")
        self.assertEqual(started, [0.4])     # відкладено, а не миттєво

    def test_non_text_clipboard_is_not_restored(self):
        # Windows: pyperclip.paste() дає '' для зображення/файлів → не відновлюємо,
        # розшифрований текст лишається в буфері.
        from fronts.desktop import paste

        with patch.object(paste.pyperclip, "paste", return_value=""):
            previous = paste.snapshot_clipboard()
        self.assertEqual(previous, "")
        with patch.object(paste.threading, "Timer") as timer, \
                patch.object(paste.pyperclip, "copy") as copy:
            paste.restore_clipboard(previous)
        timer.assert_not_called()
        copy.assert_not_called()


class ClipboardRestoreWorkTests(unittest.TestCase):
    """PTT-конвеєр (_work): рішення про відновлення буфера навколо автовставки.
    Знімок робиться ДО paste_text; відновлюємо лише при вдалій вставці й лише
    коли ввімкнено конфіг. Провал вставки / режим «показати» / вимкнений конфіг —
    буфер не чіпаємо.
    """

    @staticmethod
    def _controller(output_mode="paste", restore_clipboard=True):
        class Counter:
            def __init__(self):
                self.count = 0

            def emit(self, *args):
                self.count += 1

        return SimpleNamespace(
            recorder=SimpleNamespace(to_audio=lambda chunks: "audio"),
            _transcribe_with_fallback=lambda audio, terms, **_kw: (
                "raw", "final", 1.0, [], []),
            output_mode=output_mode,
            transcription_error=Counter(),
            transcribed=Counter(),
            finished=Counter(),
            cfg=SimpleNamespace(sounds=False, restore_clipboard=restore_clipboard),
            _busy=True,
        )

    def test_restore_scheduled_after_successful_paste(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(restore_clipboard=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text", return_value="pyautogui"), \
                patch.object(desktop_app, "begin_clipboard_restore", return_value="old"), \
                patch.object(desktop_app, "restore_clipboard") as restore, \
                patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        restore.assert_called_once_with("old", expected="final")

    def test_failed_paste_does_not_restore(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(restore_clipboard=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text", return_value=None), \
                patch.object(desktop_app, "begin_clipboard_restore", return_value="old"), \
                patch.object(desktop_app, "end_clipboard_restore") as end, \
                patch.object(desktop_app, "restore_clipboard") as restore, \
                patch.object(desktop_app, "log_history", return_value=None), \
                self.assertLogs(level="WARNING"):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        restore.assert_not_called()
        end.assert_called_once_with()

    def test_disabled_config_skips_snapshot_and_restore(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(restore_clipboard=False)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text", return_value="pyautogui"), \
                patch.object(desktop_app, "begin_clipboard_restore") as begin, \
                patch.object(desktop_app, "cancel_clipboard_restore") as cancel, \
                patch.object(desktop_app, "restore_clipboard") as restore, \
                patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        begin.assert_not_called()
        cancel.assert_called_once_with()
        restore.assert_not_called()

    def test_show_mode_never_touches_clipboard(self):
        from fronts.desktop import app as desktop_app

        controller = self._controller(output_mode="show", restore_clipboard=True)
        profile = SimpleNamespace(history_path="hp", memory_enabled=False)
        with patch.object(desktop_app, "paste_text") as paste_fn, \
                patch.object(desktop_app, "begin_clipboard_restore") as begin, \
                patch.object(desktop_app, "restore_clipboard") as restore, \
                patch.object(desktop_app, "log_history", return_value=None):
            desktop_app.DesktopApp._work(controller, ["chunk"], profile, object())
        paste_fn.assert_not_called()
        begin.assert_not_called()
        restore.assert_not_called()

    def test_config_defaults_to_enabled(self):
        self.assertIs(Config().restore_clipboard, True)


class CrashLoggingFallbackTests(unittest.TestCase):
    def test_locked_main_log_falls_back_to_pid_file(self):
        from fronts.desktop import crash

        real_handler = logging.handlers.RotatingFileHandler
        attempts = []

        def flaky_handler(filename, *args, **kwargs):
            attempts.append(str(filename))
            if len(attempts) == 1:
                raise PermissionError("файл зайнято іншим процесом")
            return real_handler(filename, *args, **kwargs)

        root = logging.getLogger()
        before = list(root.handlers)
        old_level = root.level
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            try:
                with patch.object(logging.handlers, "RotatingFileHandler",
                                  flaky_handler), \
                        patch.object(crash, "LOG_DIR", log_dir), \
                        patch.object(crash, "LOG_FILE",
                                     log_dir / "balachky.log"):
                    crash.setup_logging()
                added = [h for h in root.handlers if h not in before]
                self.assertEqual(len(added), 1)
                pid_name = f"balachky-{os.getpid()}.log"
                self.assertTrue(attempts[0].endswith("balachky.log"))
                self.assertTrue(added[0].baseFilename.endswith(pid_name))
                self.assertTrue((log_dir / pid_name).exists())
            finally:
                for h in root.handlers:
                    if h not in before:
                        root.removeHandler(h)
                        h.close()
                root.setLevel(old_level)


class DeleteModelTests(unittest.TestCase):
    """feature/delete-model: безпечне видалення теки моделі з кешу HuggingFace.
    Ядро (whisper_core.models) — без Qt, тому тестується напряму на tempfile-структурах."""

    def _link_dir(self, link: Path, target: Path):
        """Створити теку-лінк link → target. symlink потребує прав; на Windows
        фолбек на junction (mklink /J, без адмін-прав). Не вдалось — skip."""
        try:
            os.symlink(target, link, target_is_directory=True)
            return
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            import subprocess
            res = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                 capture_output=True)
            if res.returncode == 0:
                return
        self.skipTest("не вдалося створити symlink/junction для тесту")

    @staticmethod
    def _make_repo(hub: Path, *, blobs=(("model.bin", 2048), ("config.json", 8),
                                        ("tokenizer.json", 8), ("vocabulary.json", 8))):
        """Мінімальна тека моделі у форматі HF-кешу: реальні blob-и (несуть розмір)
        + порожній каталог знімка."""
        repo = hub / "models--Systran--faster-whisper-large-v3"
        (repo / "snapshots" / REVISION).mkdir(parents=True)
        blob_dir = repo / "blobs"
        blob_dir.mkdir()
        for name, size in blobs:
            (blob_dir / name).write_bytes(b"x" * size)
        return repo

    def test_deletes_correct_repo_folder(self):
        from whisper_core.models import delete_model

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            hub.mkdir()
            repo = self._make_repo(hub)
            other = hub / "models--other--keepme"    # чужа модель у спільному кеші
            other.mkdir()
            (other / "model.bin").write_bytes(b"x" * 16)

            freed = delete_model(hub, "large-v3")

            self.assertFalse(repo.exists())          # цільову теку прибрано
            self.assertTrue(other.exists())          # чужу — не чіпали
            self.assertGreater(freed, 0)

    def test_refuses_repo_that_resolves_outside_model_dir(self):
        from whisper_core.models import delete_model

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            hub.mkdir()
            outside = (Path(tmp) / "outside"
                       / "models--Systran--faster-whisper-large-v3")
            outside.mkdir(parents=True)
            (outside / "model.bin").write_bytes(b"x" * 32)
            # у кеші тека моделі — лінк, що веде НАЗОВНІ (symlink-вихід)
            self._link_dir(hub / "models--Systran--faster-whisper-large-v3", outside)

            with self.assertRaises(ValueError):
                delete_model(hub, "large-v3")

            self.assertTrue(outside.exists())        # нічого поза кешем не видалено
            self.assertTrue((outside / "model.bin").exists())

    def test_refuses_unknown_repo(self):
        from whisper_core.models import delete_model

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            repo = (hub / "models--owner--foreign-model"
                    / "snapshots" / "local-main")
            repo.mkdir(parents=True)
            (repo / "model.bin").write_bytes(b"x" * 16)

            with self.assertRaises(ValueError):
                # власна/чужа модель поза MODEL_REVISIONS — не керована застосунком
                delete_model(hub, "owner/foreign-model")

            self.assertTrue(repo.exists())           # невідому теку не чіпали

    def test_counts_real_size_without_following_symlinks(self):
        from whisper_core.models import model_snapshot_size

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            hub.mkdir()
            self._make_repo(hub, blobs=(("model.bin", 4096), ("config.json", 100)))

            self.assertEqual(model_snapshot_size(hub, REPO_ID), 4196)
            self.assertEqual(                        # немапованого repo нема → 0
                model_snapshot_size(hub, "Systran/faster-whisper-small"), 0)

    def test_dereferenced_snapshot_not_double_counted(self):
        """Регрес 31.07 (живий тест власника): майстер обіцяв ~1,6 ГБ, а Центр
        моделей після завершення показував «Завантажено (5,8 ГБ)» — рівно
        удвічі більше. Причина: dereference_snapshot() (WinError 448 self-heal)
        підмінює symlink у snapshots/<rev>/ РЕАЛЬНОЮ копією байтів blob-а, і
        стара _dir_size рахувала обидві копії (blobs/ + дереференсований
        snapshots/) як окремі дані. Модель має важити стільки ж, скільки і
        обіцяно ДО завантаження, а не вдвічі більше після нього."""
        from whisper_core.models import model_snapshot_size

        with tempfile.TemporaryDirectory() as tmp:
            hub = Path(tmp) / "hub"
            hub.mkdir()
            repo = self._make_repo(hub, blobs=(("model.bin", 4096),
                                                ("config.json", 100)))
            # symlink-стан кешу: розмір рахує тільки blobs/ (як і мав раніше)
            self.assertEqual(model_snapshot_size(hub, REPO_ID), 4196)

            # dereference_snapshot(): symlink підмінюється РЕАЛЬНОЮ копією
            # тих самих байтів (точно те, що робить self-heal у onboarding.py)
            snap = repo / "snapshots" / REVISION
            (snap / "model.bin").write_bytes((repo / "blobs" / "model.bin").read_bytes())
            (snap / "config.json").write_bytes((repo / "blobs" / "config.json").read_bytes())

            # та сама модель, ті самі байти — обіцяний розмір не має подвоїтись
            self.assertEqual(model_snapshot_size(hub, REPO_ID), 4196)


# feature/bulk-import
class BulkImportParserTests(unittest.TestCase):
    def test_bare_word_is_canon_only_with_no_variant(self):
        from whisper_core.terms import parse_bulk_terms

        new_terms, skipped = parse_bulk_terms("докер")
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)

    def test_heard_equals_printed_format_with_flexible_spacing(self):
        from whisper_core.terms import parse_bulk_terms

        new_terms, skipped = parse_bulk_terms("доку тер=докер\nко  ворк   =   коворк")
        self.assertEqual(
            new_terms, [("докер", "доку тер"), ("коворк", "ко  ворк")])
        self.assertEqual(skipped, 0)

    def test_blank_and_comment_lines_are_ignored_without_affecting_skipped(self):
        from whisper_core.terms import parse_bulk_terms

        text = "\n  \n# коментар\nдокер\n   # інший коментар   \n"
        new_terms, skipped = parse_bulk_terms(text)
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)

    def test_garbage_line_without_canon_is_dropped_silently(self):
        from whisper_core.terms import parse_bulk_terms

        new_terms, skipped = parse_bulk_terms("=\n   =   \nдокер")
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)

    def test_duplicate_within_pasted_text_is_skipped_case_insensitively(self):
        from whisper_core.terms import parse_bulk_terms

        new_terms, skipped = parse_bulk_terms("докер\nДОКЕР\nдоку тер = докер\n"
                                              "ДОКУ ТЕР = ДОКЕР")
        self.assertEqual(new_terms, [("докер", ""), ("докер", "доку тер")])
        self.assertEqual(skipped, 2)

    def test_duplicate_against_existing_bare_canon_is_skipped(self):
        from whisper_core.terms import parse_bulk_terms

        existing = {"Докер": ["доку тер"]}
        new_terms, skipped = parse_bulk_terms("докер", existing)
        self.assertEqual(new_terms, [])
        self.assertEqual(skipped, 1)

    def test_duplicate_against_existing_variant_is_skipped_new_variant_kept(self):
        from whisper_core.terms import parse_bulk_terms

        existing = {"Докер": ["доку тер"]}
        new_terms, skipped = parse_bulk_terms(
            "доку тер = докер\nдок ер = докер", existing)
        self.assertEqual(new_terms, [("докер", "док ер")])
        self.assertEqual(skipped, 1)

    def test_no_existing_dictionary_treated_as_empty(self):
        from whisper_core.terms import parse_bulk_terms

        new_terms, skipped = parse_bulk_terms("докер", None)
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)


# CSV-імпорт словника з файлу: «термін;вимова1;вимова2»
class CsvImportParserTests(unittest.TestCase):
    def test_term_with_two_pronunciations(self):
        from whisper_core.terms import parse_csv_terms

        new_terms, skipped = parse_csv_terms("докер;доку тер;док ер")
        self.assertEqual(new_terms, [("докер", "доку тер"), ("докер", "док ер")])
        self.assertEqual(skipped, 0)

    def test_bare_term_is_canon_only(self):
        from whisper_core.terms import parse_csv_terms

        new_terms, skipped = parse_csv_terms("докер")
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)

    def test_blank_and_comment_and_empty_cells_ignored(self):
        from whisper_core.terms import parse_csv_terms

        text = "\n # коментар \nдокер;;доку тер;\n"
        new_terms, skipped = parse_csv_terms(text)
        self.assertEqual(new_terms, [("докер", "доку тер")])
        self.assertEqual(skipped, 0)

    def test_row_without_canon_dropped_silently(self):
        from whisper_core.terms import parse_csv_terms

        new_terms, skipped = parse_csv_terms(";доку тер\nдокер")
        self.assertEqual(new_terms, [("докер", "")])
        self.assertEqual(skipped, 0)

    def test_duplicates_within_file_skipped_case_insensitively(self):
        from whisper_core.terms import parse_csv_terms

        new_terms, skipped = parse_csv_terms("докер;доку тер\nДОКЕР;ДОКУ ТЕР")
        self.assertEqual(new_terms, [("докер", "доку тер")])
        self.assertEqual(skipped, 1)

    def test_duplicates_against_existing_skipped(self):
        from whisper_core.terms import parse_csv_terms

        existing = {"Докер": ["доку тер"]}
        new_terms, skipped = parse_csv_terms("докер;доку тер;док ер", existing)
        self.assertEqual(new_terms, [("докер", "док ер")])
        self.assertEqual(skipped, 1)

    def test_no_existing_dictionary_treated_as_empty(self):
        from whisper_core.terms import parse_csv_terms

        new_terms, skipped = parse_csv_terms("докер;доку тер", None)
        self.assertEqual(new_terms, [("докер", "доку тер")])
        self.assertEqual(skipped, 0)


class BulkImportWriteTests(unittest.TestCase):
    """Bulk-імпорт пише через ТОЙ САМИЙ add_term, що й діалог виправлення слова."""

    def test_parsed_terms_round_trip_through_add_term(self):
        from whisper_core.terms import add_term, parse_bulk_terms, read_terms_dict

        with tempfile.TemporaryDirectory() as tmp:
            terms_path = Path(tmp) / "terms.toml"
            text = "докер\nдоку тер = докер\nко ворк = коворк"
            new_terms, skipped = parse_bulk_terms(
                text, read_terms_dict(terms_path))
            self.assertEqual(skipped, 0)
            for canon, variant in new_terms:
                add_term(terms_path, canon, variant)

            result = read_terms_dict(terms_path)
            self.assertEqual(sorted(result["докер"]), ["доку тер"])
            self.assertEqual(result["коворк"], ["ко ворк"])

    def test_reimporting_same_list_skips_everything(self):
        from whisper_core.terms import add_term, parse_bulk_terms, read_terms_dict

        with tempfile.TemporaryDirectory() as tmp:
            terms_path = Path(tmp) / "terms.toml"
            add_term(terms_path, "докер", "доку тер")

            new_terms, skipped = parse_bulk_terms(
                "докер\nдоку тер = докер", read_terms_dict(terms_path))
            self.assertEqual(new_terms, [])
            self.assertEqual(skipped, 2)


if __name__ == "__main__":
    unittest.main()
