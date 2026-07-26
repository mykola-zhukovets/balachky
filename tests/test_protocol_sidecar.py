"""E1: sidecar ↔ worker IPC. Ганяємо СПРАВЖНІЙ worker-підпроцес
(`python -m whisper_core.protocol.worker`) з фейковим бекендом (ENV_FAKE_BACKEND) —
покрито реальний шлях запуску, JSON stdin/stdout, ping/pong, generate, shutdown,
таймаут і краш процесу."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import ENV_FAKE_BACKEND
from whisper_core.protocol.sidecar import (Sidecar, SidecarError,
                                           SidecarTimeout, any_running,
                                           default_worker_command)
from whisper_core.protocol import worker


def _fake_sidecar(**kw):
    return Sidecar(env={ENV_FAKE_BACKEND: "1"}, **kw)


class TestWorkerHandlers(unittest.TestCase):
    """Чиста логіка обробника повідомлень — без підпроцесу."""

    def test_ping_returns_pong(self):
        self.assertEqual(worker._handle({"type": "ping"}, worker.FakeBackend()),
                         {"type": "pong"})

    def test_shutdown_returns_none(self):
        self.assertIsNone(worker._handle({"type": "shutdown"}, worker.FakeBackend()))

    def test_generate_returns_response_with_id(self):
        reply = worker._handle(
            {"type": "generate", "id": "abc", "prompt": "привіт"}, worker.FakeBackend())
        self.assertEqual(reply["type"], "response")
        self.assertEqual(reply["id"], "abc")
        self.assertIn("text", reply)

    def test_unknown_type_is_error(self):
        reply = worker._handle({"type": "zzz", "id": "1"}, worker.FakeBackend())
        self.assertEqual(reply["type"], "error")

    def test_generate_backend_exception_becomes_error(self):
        class Boom:
            def generate(self, *a, **k):
                raise RuntimeError("вибух")
        reply = worker._handle({"type": "generate", "id": "9", "prompt": "x"}, Boom())
        self.assertEqual(reply["type"], "error")
        self.assertEqual(reply["id"], "9")
        self.assertIn("вибух", reply["message"])


class TestSidecarLive(unittest.TestCase):
    """Живий підпроцес worker-а з фейковим бекендом."""

    def test_default_command_targets_worker_module(self):
        cmd = default_worker_command()
        self.assertIn("whisper_core.protocol.worker", cmd)

    def test_ping_pong(self):
        with _fake_sidecar() as s:
            self.assertTrue(s.ping(timeout=15))

    def test_generate_round_trip(self):
        with _fake_sidecar() as s:
            text = s.generate("склади протокол", model_path="", timeout=15)
            self.assertIsInstance(text, str)
            self.assertTrue(text)

    def test_two_generates_reuse_process(self):
        with _fake_sidecar() as s:
            s.generate("a", model_path="", timeout=15)
            s.generate("b", model_path="", timeout=15)
            self.assertTrue(s.running)

    def test_shutdown_stops_process(self):
        s = _fake_sidecar()
        s.start()
        self.assertTrue(any_running())
        self.assertTrue(s.ping(timeout=15))
        s.shutdown(timeout=15)
        self.assertFalse(s.running)
        self.assertFalse(any_running())

    def test_generate_after_shutdown_raises(self):
        s = _fake_sidecar()
        s.start()
        s.shutdown(timeout=15)
        with self.assertRaises(SidecarError):
            s.generate("x", model_path="", timeout=5)

    def test_bad_command_raises_sidecar_error(self):
        s = Sidecar(command=["definitely-not-a-real-binary-xyz"])
        with self.assertRaises(SidecarError):
            s.start()


class TestSidecarShutdownResilience(unittest.TestCase):
    """Завислий процес: другий w() після kill() не має пробивати виняток наскрізь."""

    def test_shutdown_survives_double_wait_timeout(self):
        import subprocess
        from unittest.mock import MagicMock
        from whisper_core.protocol import sidecar as sc

        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        # Обидва wait() кидають TimeoutExpired — навіть після kill() процес висить.
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="worker", timeout=5)

        s = Sidecar(command=["dummy"])
        s._proc = proc
        s._closed = False
        with sc._registry_lock:
            sc._running_sidecars.add(s)

        # Не має підняти TimeoutExpired; має прибрати процес і реєстр.
        s.shutdown(timeout=0.01)

        proc.kill.assert_called_once_with()
        self.assertIsNone(s._proc)
        self.assertNotIn(s, sc._running_sidecars)


class TestSidecarTimeout(unittest.TestCase):
    def test_await_times_out_when_no_reply(self):
        # Воркер, що нічого не пише у відповідь (cat у нікуди): читає stdin, мовчить.
        s = Sidecar(command=[sys.executable, "-c",
                             "import sys; [None for _ in sys.stdin]"])
        s.start()
        try:
            with self.assertRaises(SidecarTimeout):
                s.generate("x", model_path="", timeout=1.0)
        finally:
            s.shutdown(timeout=5)


if __name__ == "__main__":
    unittest.main()
