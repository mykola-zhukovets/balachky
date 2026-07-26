"""Хвиля 1: frozen TTS-worker (§11.1, §12.1).

Каркас: збірка НЕ потрібна для зелені — dev-режимний selftest перевіряється завжди;
frozen-exe тест скіпається, якщо exe відсутній (є білд → spawn+ping реального exe)."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whisper_core.tts import ENV_FAKE_BACKEND
from whisper_core.tts.sidecar import TtsSidecar
from whisper_core.tts.worker import _WARMUP_MODULES, _warm_engine_imports

_FROZEN_WORKER = ROOT / "dist" / "Balachky" / "balachky-tts-worker.exe"


class TestWarmEngineImports(unittest.TestCase):
    """Прогрів важких імпортів ДО reader-потоку (фікс frozen import-lock deadlock).

    Тут мокаємо importer (реальний torch у тест-venv відсутній), перевіряючи логіку
    циклу: що прогрівається послідовно ДО reader, які модулі, tolerance до ImportError."""

    def test_warms_all_when_import_succeeds(self):
        calls = []
        warmed = _warm_engine_imports(import_module=calls.append)
        # усі модулі спробувані рівно раз, У ПОРЯДКУ, і всі повернені як прогріті
        self.assertEqual(calls, list(_WARMUP_MODULES))
        self.assertEqual(warmed, list(_WARMUP_MODULES))

    def test_torch_first_styletts2_before_radtts_stack(self):
        # torch мусить грітись ДО styletts2_inference/tts_uk (найважче спільне першим)
        self.assertEqual(_WARMUP_MODULES[0], "numpy")
        self.assertEqual(_WARMUP_MODULES[1], "torch")
        i_st = _WARMUP_MODULES.index("styletts2_inference.models")
        i_rad = _WARMUP_MODULES.index("tts_uk.radtts")
        self.assertLess(_WARMUP_MODULES.index("torch"), i_st)
        self.assertLess(i_st, i_rad)

    def test_never_warms_tts_uk_inference(self):
        # tts_uk.inference вантажить моделі з CWD на імпорті — його прогрів заборонено
        self.assertNotIn("tts_uk.inference", _WARMUP_MODULES)

    def test_tolerant_missing_module_is_skipped(self):
        # відсутній модуль (ImportError) → пропуск, решта гріється, воркер не падає
        def imp(name):
            if name == "torch":
                raise ImportError("No module named 'torch'")
            return None
        warmed = _warm_engine_imports(import_module=imp)
        self.assertNotIn("torch", warmed)
        self.assertIn("numpy", warmed)
        self.assertIn("tts_uk.radtts", warmed)
        self.assertEqual(len(warmed), len(_WARMUP_MODULES) - 1)

    def test_tolerant_arbitrary_exception_is_skipped(self):
        # не лише ImportError: будь-яка помилка імпорту не валить прогрів
        def imp(name):
            if name == "styletts2_inference.models":
                raise RuntimeError("DLL load failed")
            return None
        warmed = _warm_engine_imports(import_module=imp)
        self.assertNotIn("styletts2_inference.models", warmed)
        self.assertEqual(len(warmed), len(_WARMUP_MODULES) - 1)

    def test_log_callback_receives_lines(self):
        logs = []
        _warm_engine_imports(import_module=lambda n: None, log=logs.append)
        self.assertTrue(any("numpy" in ln for ln in logs))


class TestDevSelftest(unittest.TestCase):
    """dev-режим: run_tts_worker.py --selftest робить РЕАЛЬНИЙ synth (FakeBackend)."""

    def test_dev_selftest_passes(self):
        env = dict(os.environ, PYTHONUTF8="1")
        out = subprocess.run(
            [sys.executable, str(ROOT / "run_tts_worker.py"), "--selftest"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("SELFTEST PASS", out.stdout)


class TestFrozenWorkerExe(unittest.TestCase):
    """frozen: реальний balachky-tts-worker.exe spawn+ping (скіп, якщо exe нема)."""

    def setUp(self):
        if not _FROZEN_WORKER.is_file():
            self.skipTest("frozen balachky-tts-worker.exe відсутній (зроби білд)")

    def test_frozen_worker_ping(self):
        s = TtsSidecar(command=[str(_FROZEN_WORKER)],
                       env={ENV_FAKE_BACKEND: "1"})
        try:
            s.start()
            # Прогрів важких імпортів іде ДО reader-потоку (фікс дедлоку a1751d3):
            # на холодному диск-кеші перший ping приходить за 4-52с — бюджет як у
            # продуктового load_voice (120с), продакшн ping до прогріву не кличе.
            self.assertTrue(s.ping(timeout=120))
        finally:
            s.shutdown()

    def test_frozen_worker_selftest(self):
        out = subprocess.run([str(_FROZEN_WORKER), "--selftest"],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0)
        self.assertIn("SELFTEST PASS", out.stdout)


if __name__ == "__main__":
    unittest.main()
