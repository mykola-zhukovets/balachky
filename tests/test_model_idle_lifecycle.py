"""Idle lifecycle важких моделей без реального faster-whisper/Gemma."""
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from whisper_core.config import Config
from whisper_core.model_lifecycle import ModelLifecycle


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class ModelLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        self.busy = False
        self.lifecycle = ModelLifecycle(
            timeout_seconds=600,
            unload=lambda: self.calls.append("unload") or True,
            load=lambda: self.calls.append("load"),
            is_busy=lambda: self.busy,
            clock=self.clock,
        )

    def test_idle_timeout_calls_unload(self):
        self.clock.advance(599)
        self.assertFalse(self.lifecycle.check_idle())
        self.clock.advance(1)
        self.assertTrue(self.lifecycle.check_idle())
        self.assertEqual(self.calls, ["unload"])
        self.assertEqual(self.lifecycle.state, "unloaded")

    def test_activity_after_unload_lazy_reloads_once(self):
        self.clock.advance(600)
        self.lifecycle.check_idle()

        loaded_by_this_call = self.lifecycle.ensure_loaded()

        self.assertTrue(loaded_by_this_call)
        self.assertEqual(self.calls, ["unload", "load"])
        self.assertEqual(self.lifecycle.state, "loaded")

    def test_active_processing_prevents_unload_and_resets_idle_window(self):
        self.busy = True
        self.clock.advance(600)
        self.assertFalse(self.lifecycle.check_idle())
        self.assertEqual(self.calls, [])

        self.busy = False
        self.clock.advance(599)
        self.assertFalse(self.lifecycle.check_idle())
        self.clock.advance(1)
        self.assertTrue(self.lifecycle.check_idle())
        self.assertEqual(self.calls, ["unload"])

    def test_never_disables_idle_unload(self):
        self.lifecycle.set_timeout(0)
        self.clock.advance(24 * 60 * 60)
        self.assertFalse(self.lifecycle.check_idle())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.lifecycle.state, "loaded")

    def test_unload_exception_leaves_state_consistent_not_loaded(self):
        # Відтворення дефекту рецензії: unload частково відпускає ресурси (engine=None),
        # а потім падає на завершенні sidecar. Стан НЕ має лишитись "loaded",
        # інакше наступний ensure_loaded поверне без reload → transcribe по None.
        def failing_unload():
            self.calls.append("unload")
            raise RuntimeError("sidecar shutdown впав")

        self.lifecycle._unload = failing_unload
        self.clock.advance(600)

        with self.assertRaises(RuntimeError):
            self.lifecycle.check_idle()

        # Головна інваріанта: стан не "loaded".
        self.assertNotEqual(self.lifecycle.state, "loaded")
        self.assertEqual(self.lifecycle.state, "unloaded")

        # Наступна робота гарантовано reload-ить модель, а не мовчки падає.
        self.lifecycle._unload = lambda: self.calls.append("unload") or True
        loaded_by_this_call = self.lifecycle.ensure_loaded()
        self.assertTrue(loaded_by_this_call)
        self.assertEqual(self.lifecycle.state, "loaded")
        self.assertEqual(self.calls, ["unload", "load"])

    def test_activity_context_prevents_unload_and_lazy_loads(self):
        # Контекст-менеджер activity() досі не тестувався.
        # 1) Під час активності check_idle НЕ вивантажує, навіть після таймауту.
        self.clock.advance(600)
        with self.lifecycle.activity():
            # модель уже loaded, тож load не викликається повторно
            self.assertNotIn("unload", self.calls)
            unloaded = self.lifecycle.check_idle()
            self.assertFalse(unloaded)
            self.assertEqual(self.lifecycle.state, "loaded")
        # 2) Після виходу — вивантаження знову можливе.
        self.assertNotIn("unload", self.calls)
        self.clock.advance(600)
        self.assertTrue(self.lifecycle.check_idle())
        self.assertEqual(self.calls, ["unload"])

    def test_activity_lazy_reloads_after_unload(self):
        # activity() з load=True має підняти модель, якщо вона вивантажена.
        self.clock.advance(600)
        self.lifecycle.check_idle()
        self.assertEqual(self.lifecycle.state, "unloaded")

        with self.lifecycle.activity():
            self.assertEqual(self.lifecycle.state, "loaded")
        self.assertEqual(self.calls, ["unload", "load"])

    def test_concurrent_ensure_loads_model_once(self):
        self.clock.advance(600)
        self.lifecycle.check_idle()
        started = threading.Event()
        release = threading.Event()

        def slow_load():
            self.calls.append("load")
            started.set()
            release.wait(2)

        self.lifecycle._load = slow_load
        results = []
        first = threading.Thread(target=lambda: results.append(
            self.lifecycle.ensure_loaded()))
        second = threading.Thread(target=lambda: results.append(
            self.lifecycle.ensure_loaded()))
        first.start()
        started.wait(1)
        second.start()
        time.sleep(0.02)
        self.assertEqual(self.lifecycle.state, "loading")
        release.set()
        first.join(1)
        second.join(1)

        self.assertEqual(self.calls, ["unload", "load"])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.lifecycle.state, "loaded")


class IdleUnloadConfigTests(unittest.TestCase):
    def test_default_is_ten_minutes_and_round_trips(self):
        self.assertEqual(Config().model_idle_unload_seconds, 600)
        path = Path("config-test.toml")
        written = {}
        # save пише через _atomic_write_text (temp + os.replace) — перехоплюємо його
        with patch("whisper_core.config._atomic_write_text",
                   side_effect=lambda _path, text: written.update(text=text)):
            Config(model_idle_unload_seconds=3600).save(path)
        with patch.object(Path, "exists", return_value=True), \
                patch.object(Path, "read_text", return_value=written["text"]):
            loaded = Config.load(path)
        self.assertEqual(loaded.model_idle_unload_seconds, 3600)


class DesktopUnloadIntegrationTests(unittest.TestCase):
    def test_unload_closes_stt_and_requests_gemma_shutdown(self):
        from fronts.desktop.app import DesktopApp
        engine = SimpleNamespace(close=MagicMock())
        app = SimpleNamespace(
            engine=engine, _engine_lock=threading.Lock(),
            _models_busy=lambda: False)
        with patch("whisper_core.protocol.sidecar.idle_transition",
                   return_value=nullcontext()), \
                patch("whisper_core.protocol.sidecar.shutdown_all") as shutdown, \
                patch("fronts.desktop.app.diagnostic_event"):
            unloaded = DesktopApp._unload_idle_models(app)

        self.assertTrue(unloaded)
        engine.close.assert_called_once_with()
        shutdown.assert_called_once_with()
        self.assertIsNone(app.engine)

    def test_unload_rechecks_activity_and_keeps_engine(self):
        from fronts.desktop.app import DesktopApp
        engine = SimpleNamespace(close=MagicMock())
        app = SimpleNamespace(
            engine=engine, _engine_lock=threading.Lock(),
            _models_busy=lambda: True)
        with patch("whisper_core.protocol.sidecar.idle_transition",
                   return_value=nullcontext()), \
                patch("whisper_core.protocol.sidecar.shutdown_all") as shutdown:
            unloaded = DesktopApp._unload_idle_models(app)

        self.assertFalse(unloaded)
        engine.close.assert_not_called()
        shutdown.assert_not_called()
        self.assertIs(app.engine, engine)

    def test_engine_close_releases_ctranslate2_and_torch_cache(self):
        from whisper_core.engine import Engine
        inner = SimpleNamespace(unload_model=MagicMock())
        engine = Engine.__new__(Engine)
        engine.model = SimpleNamespace(model=inner)
        torch = SimpleNamespace(cuda=SimpleNamespace(empty_cache=MagicMock()))

        with patch.dict("sys.modules", {"torch": torch}), \
                patch("whisper_core.engine.gc.collect") as collect:
            engine.close()

        inner.unload_model.assert_called_once_with()
        torch.cuda.empty_cache.assert_called_once_with()
        collect.assert_called_once_with()
        self.assertIsNone(engine.model)


if __name__ == "__main__":
    unittest.main()
