"""E3/E5: оркестрація ProtocolGenerator — гейт моделі, повний прогін через
справжній worker-підпроцес із фейк-бекендом, скасування, збереження, помилки."""
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.protocol import ENV_FAKE_BACKEND
from whisper_core.protocol import model_manager as mm
from whisper_core.protocol import service
from whisper_core.protocol.service import (ProtocolGenerator, ProtocolCancelled,
                                           ProtocolModelMissing)
from whisper_core.meeting.postprocess import Utterance

_WORKER = [sys.executable, "-m", "whisper_core.protocol.worker"]


def _make_ready_model(root: Path):
    """Створити фейкову «завантажену» модель fast із маркером READY."""
    d = root / "fast"
    d.mkdir(parents=True)
    (d / mm.MODEL_FILENAME).write_bytes(b"GGUF" + b"\x00" * 500)
    (d / mm._READY_MARKER).write_text("ok", encoding="utf-8")
    return d


class TestServiceHelpers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svc-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_model_available_false_when_missing(self):
        self.assertFalse(service.model_available("fast", self.tmp))

    def test_model_available_true_when_ready(self):
        _make_ready_model(self.tmp)
        self.assertTrue(service.model_available("fast", self.tmp))

    def test_backend_available_matches_llama(self):
        from whisper_core.protocol.worker import llama_available
        self.assertEqual(service.backend_available(), llama_available())

    def test_save_protocol_writes_file(self):
        dest = service.save_protocol(self.tmp, "## Підсумок\nОк")
        self.assertEqual(dest.name, "protocol.md")
        self.assertEqual(dest.read_text(encoding="utf-8"), "## Підсумок\nОк")


class TestGeneratorRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svcrun-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)
        self.us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _gen(self, **kw):
        return ProtocolGenerator("fast", model_root=self.tmp, worker_command=_WORKER,
                                 env={ENV_FAKE_BACKEND: "1"}, generate_timeout=20, **kw)

    def test_missing_model_raises(self):
        with self.assertRaises(ProtocolModelMissing):
            self._gen().run(self.us)

    def test_available_reflects_model(self):
        g = self._gen()
        self.assertFalse(g.available())
        _make_ready_model(self.tmp)
        self.assertTrue(g.available())

    def test_full_run_through_fake_worker_rejects_stub(self):
        """Блокер: без llama worker бере FakeBackend, чий вихід — заглушка.
        run() має відхилити її як невалідний протокол (ProtocolError), а НЕ
        повертати сміття як успіх; сайдкар усе одно прибрано у finally."""
        from whisper_core.protocol.service import ProtocolError
        _make_ready_model(self.tmp)
        g = self._gen()
        with self.assertRaises(ProtocolError) as ctx:
            g.run(self.us)
        self.assertIn("недоступна", str(ctx.exception))   # зрозуміла UA-помилка
        self.assertIsNone(g._sidecar)                     # сайдкар зупинено у finally

    def test_fake_backend_does_not_write_protocol_md(self):
        """Симуляція відсутності llama (FakeBackend): run піднімає помилку і
        protocol.md НЕ створюється (UI зберігає лише після успішного run)."""
        from whisper_core.protocol.service import ProtocolError
        _make_ready_model(self.tmp)
        session = Path(tempfile.mkdtemp(prefix="sess-", dir=self.tmp))
        g = self._gen()
        text = ""
        try:
            text = g.run(self.us)                 # UI зберегло б лише цей результат
        except ProtocolError:
            pass
        if text.strip():
            service.save_protocol(session, text)  # шлях збереження UI (_on_done)
        self.assertFalse((session / service.PROTOCOL_FILENAME).exists())

    def test_empty_utterances_returns_empty(self):
        _make_ready_model(self.tmp)
        self.assertEqual(self._gen().run([]), "")

    def test_cancel_before_run_raises_cancelled(self):
        _make_ready_model(self.tmp)
        g = self._gen()
        g.cancel()
        with self.assertRaises(ProtocolCancelled):
            g.run(self.us)


class TestErrorHandling(unittest.TestCase):
    """E5: краш сайдкара → ProtocolError (зрозуміле повідомлення), не Cancelled."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svcerr-"))
        self._orig = mm.PRESETS.copy()
        mm.PRESETS["fast"] = replace(mm.PRESETS["fast"], min_bytes=100, sha256=None)
        _make_ready_model(self.tmp)
        self.us = [Utterance(0.0, 3.0, "me", "привіт", source="mic")]

    def tearDown(self):
        mm.PRESETS.clear(); mm.PRESETS.update(self._orig)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sidecar_crash_becomes_protocol_error(self):
        from whisper_core.protocol.service import ProtocolError, ProtocolCancelled
        # Воркер, що миттєво завершується (жодної відповіді) → SidecarError у generate
        crashing = [sys.executable, "-c", "raise SystemExit(1)"]
        g = ProtocolGenerator("fast", model_root=self.tmp, worker_command=crashing,
                              generate_timeout=10)
        with self.assertRaises(ProtocolError) as ctx:
            g.run(self.us)
        self.assertNotIsInstance(ctx.exception, ProtocolCancelled)
        self.assertTrue(str(ctx.exception))          # непорожнє зрозуміле повідомлення
        self.assertIsNone(g._sidecar)                # сайдкар прибрано


if __name__ == "__main__":
    unittest.main()
