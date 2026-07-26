"""Суд BLOCK: тривке зʼєднання RAD-TTS — три блокери, покриті ОФЛАЙН (без torch).

  1) synthesize() не пише нічого в stdout (кожен виклик synthesis() друкує
     "Inferencing take N" — псує JSON-канал IPC воркера);
  2) load() не лишає permanent chdir (CWD відновлюється навіть при винятку);
  3) повторний load() ІНШОЇ теки в тому самому процесі → чесна EngineLoadError
     (import закешовано в sys.modules), той САМИЙ шлях → дозволений no-op.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts.engines import radtts
from whisper_core.tts.engines.base import EngineLoadError
from whisper_core.tts.engines.radtts import RadTtsEngine


class _FakeWave:
    """Мінімальний tensor-подібний вихід: .squeeze().cpu().numpy()."""
    def squeeze(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        import numpy as np
        return np.zeros(8, dtype=np.float32)


class _FakeTi:
    """Підробка tts_uk.inference: synthesis() ДРУКУЄ у stdout, як справжня."""
    def __init__(self):
        self.calls = 0
        self.radtts = types.SimpleNamespace(infer=lambda *a, **k: {"dur": None})

    def synthesis(self, **kwargs):
        self.calls += 1
        print(f"Inferencing take {self.calls}")   # має піти в stderr, не в stdout
        return [None, _FakeWave(), {}]


class TestSynthesizeStdoutClean(unittest.TestCase):
    """Блокер 1: жоден synthesize не пише в stdout (IPC-канал недоторканий)."""

    def test_synthesize_writes_nothing_to_stdout(self):
        eng = RadTtsEngine()
        eng._ti = _FakeTi()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = eng.synthesize("текст один два", speed=1.0, want_timings=False)
        self.assertEqual(out.getvalue(), "")           # stdout ЧИСТИЙ
        self.assertIn("Inferencing take", err.getvalue())  # print пішов у stderr
        self.assertEqual(res.sample_rate, 44100)

    def test_stdout_clean_across_multiple_calls(self):
        # блокер: обгортка має бути на КОЖНОМУ виклику, не лише на першому
        eng = RadTtsEngine()
        eng._ti = _FakeTi()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            for _ in range(3):
                eng.synthesize("речення", speed=1.0, want_timings=False)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(eng._ti.calls, 3)


class TestCwdRestore(unittest.TestCase):
    """Блокер 2: chdir лише на час import, CWD завжди відновлюється."""

    def test_restore_cwd_context_restores_on_exception(self):
        start = os.getcwd()
        d = tempfile.mkdtemp(prefix="radtts-cwd-cm-")
        with self.assertRaises(RuntimeError):
            with radtts._restore_cwd():
                os.chdir(d)
                self.assertEqual(os.getcwd(), os.path.realpath(d))
                raise RuntimeError("boom усередині контексту")
        self.assertEqual(os.getcwd(), start)           # відновлено попри виняток

    def test_load_restores_cwd_on_import_failure(self):
        # без torch load() падає EngineLoadError на import tts_uk — CWD має відновитись,
        # а не лишитись у теці голосу (доведено регресом: styletts2→radtts→styletts2)
        if importlib.util.find_spec("torch") is not None:
            self.skipTest("torch присутній — гілка перевіряє відновлення на error-шляху")
        if "tts_uk.inference" in sys.modules:
            self.skipTest("tts_uk уже імпортовано в цьому процесі")
        start = os.getcwd()
        d = tempfile.mkdtemp(prefix="radtts-cwd-load-")
        saved_env = os.environ.get("HF_HUB_CACHE")
        try:
            with self.assertRaises(EngineLoadError):
                RadTtsEngine().load(d)
            self.assertEqual(os.getcwd(), start)       # CWD НЕ лишився в теці голосу
        finally:
            if saved_env is not None:
                os.environ["HF_HUB_CACHE"] = saved_env
            else:
                os.environ.pop("HF_HUB_CACHE", None)


class TestHotSwapRejected(unittest.TestCase):
    """Блокер 3: гаряча заміна голосу в одному процесі — чесна помилка, не тихий
    no-op зі старими вагами. Той самий шлях — дозволений no-op."""

    def setUp(self):
        # зберігаємо процес-стан (import закешовано в sys.modules на весь процес)
        self._saved_mod = sys.modules.get("tts_uk.inference")
        self._saved_pkg = sys.modules.get("tts_uk")
        self._saved_path = radtts._LOADED_MODEL_PATH
        self._saved_orig = radtts._ORIG_INFER

    def tearDown(self):
        for key, val in (("tts_uk.inference", self._saved_mod),
                         ("tts_uk", self._saved_pkg)):
            if val is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = val
        radtts._LOADED_MODEL_PATH = self._saved_path
        radtts._ORIG_INFER = self._saved_orig

    def _inject_loaded(self, loaded_path):
        fake = _FakeTi()
        sys.modules["tts_uk.inference"] = fake
        radtts._LOADED_MODEL_PATH = radtts._norm_path(loaded_path)
        radtts._ORIG_INFER = None                      # свіжий original для обгортки
        return fake

    def test_different_path_raises(self):
        self._inject_loaded("C:/voices/voiceA")
        with self.assertRaises(EngineLoadError) as ctx:
            RadTtsEngine().load("C:/voices/voiceB")
        self.assertIn("гарячу заміну", str(ctx.exception))

    def test_same_path_is_noop(self):
        fake = self._inject_loaded("C:/voices/voiceA")
        eng = RadTtsEngine()
        eng.load("C:/voices/voiceA")                   # той самий шлях → без помилки
        self.assertIs(eng._ti, fake)                   # підʼєднано кешований модуль
        # обгортка тривалостей встановлена на цей екземпляр (не нашарована)
        self.assertTrue(callable(fake.radtts.infer))

    def test_same_path_case_insensitive(self):
        # Windows: шлях порівнюється через normcase (регістр не має ламати no-op)
        fake = self._inject_loaded("C:/Voices/VoiceA")
        eng = RadTtsEngine()
        eng.load("C:/voices/voicea")                   # той самий шлях (інший регістр)
        self.assertIs(eng._ti, fake)


if __name__ == "__main__":
    unittest.main()
