"""Хвиля 1: володіння plaintext-temp озвучення (§8.9, §11.2).

crash → startup-cleanup прибирає balachky-tts-plain-*; stop/cancel/error видаляють
файли; у temp-імені немає вмісту."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import PLAINTEXT_TEMP_PREFIX
from whisper_core.tts import plaintext_temp as PT


class TestPlaintextAudioDir(unittest.TestCase):
    def test_context_cleanup(self):
        with PT.PlaintextAudioDir() as d:
            path = d.path
            self.assertTrue(os.path.isdir(path))
            (Path(path) / "s0.wav").write_bytes(b"\x00")
        self.assertFalse(os.path.isdir(path))    # прибрано на виході

    def test_name_has_no_content(self):
        with PT.PlaintextAudioDir() as d:
            base = os.path.basename(d.path)
            # у імені лише префікс + випадковий суфікс, ЖОДНОГО тексту наради
            self.assertTrue(base.startswith(PLAINTEXT_TEMP_PREFIX))
            self.assertNotIn("нарада", base.lower())

    def test_explicit_cleanup_idempotent(self):
        d = PT.PlaintextAudioDir()
        p = d.path
        d.cleanup()
        d.cleanup()                              # повторний — без винятку
        self.assertFalse(os.path.isdir(p))


class TestStaleCleanup(unittest.TestCase):
    def test_startup_removes_old(self):
        old = tempfile.mkdtemp(prefix=PLAINTEXT_TEMP_PREFIX)
        # зістарити mtime на 2 години
        past = time.time() - 7200
        os.utime(old, (past, past))
        removed = PT.cleanup_stale(max_age_seconds=3600)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(os.path.isdir(old))

    def test_young_dir_kept(self):
        fresh = tempfile.mkdtemp(prefix=PLAINTEXT_TEMP_PREFIX)
        PT.cleanup_stale(max_age_seconds=3600)
        self.assertTrue(os.path.isdir(fresh))    # свіжу (можливо активну) не чіпаємо
        PT.PlaintextAudioDir  # noqa
        import shutil
        shutil.rmtree(fresh, ignore_errors=True)


class TestProdCleanupPath(unittest.TestCase):
    """РЕАЛЬНИЙ prod-шлях app.py (не мертвий модуль): startup викликає
    app._cleanup_stale_tts_temps(), яке делегує plaintext_temp.cleanup_stale().
    Мутація (прибрати делегат/зламати префікс) має ЧЕРВОНИТИ (зразок test_meeting_ui)."""

    def test_app_cleanup_removes_stale_tts_plain(self):
        import os as _os
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from fronts.desktop import app as app_module
        stale = tempfile.mkdtemp(prefix=PLAINTEXT_TEMP_PREFIX)
        past = time.time() - 7200
        _os.utime(stale, (past, past))
        app_module._cleanup_stale_tts_temps()    # саме те, що кличе __init__
        self.assertFalse(os.path.isdir(stale))

    def test_app_cleanup_removes_FRESH_temp(self):
        # СУД п.6 §8.9: startup-очищення (max_age=0) прибирає навіть СВІЖУ теку
        # (молодшу за годину) — на старті активної озвучки немає, тож не лишаємо
        # конфіденційне аудіо на годину. Мутація (дефолтний поріг 3600) → червонить.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from fronts.desktop import app as app_module
        fresh = tempfile.mkdtemp(prefix=PLAINTEXT_TEMP_PREFIX)   # щойно створена
        app_module._cleanup_stale_tts_temps()
        self.assertFalse(os.path.isdir(fresh), "свіжа crash-temp не прибрана на старті")

    def test_startup_wires_tts_cleanup(self):
        # ФАКТ виклику саме в ТІЛІ DesktopApp.__init__ (не греп — той ловив і `def
        # _cleanup_stale_tts_temps()`, тож видалення виклику з __init__ пережило б).
        # AST: знаходимо __init__ і шукаємо Call на _cleanup_stale_tts_temps у ньому.
        import ast
        src = (Path(__file__).resolve().parent.parent / "fronts" / "desktop"
               / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        init = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.ClassDef) and node.name == "DesktopApp"):
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        init = item
                        break
        self.assertIsNotNone(init, "DesktopApp.__init__ не знайдено")
        called = False
        for n in ast.walk(init):
            if isinstance(n, ast.Call):
                fn = n.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name == "_cleanup_stale_tts_temps":
                    called = True
                    break
        self.assertTrue(called,
                        "__init__ НЕ викликає _cleanup_stale_tts_temps (мертвий шлях)")


if __name__ == "__main__":
    unittest.main()
