# feature/watch-folder
"""Тести чистих хелперів watch-теки (без Qt): фільтр розширень, «не обробляти
вже оброблене», стабільність файлу при копіюванні (tempfile + mock таймерів)."""
import os
import tempfile
import unittest
from pathlib import Path

from fronts.desktop import watch


class ExtensionFilterTests(unittest.TestCase):
    def test_supported_regardless_of_case(self):
        self.assertTrue(watch.is_supported_audio("a.mp3"))
        self.assertTrue(watch.is_supported_audio("a.OGG"))
        self.assertTrue(watch.is_supported_audio(Path("dir/b.M4a")))

    def test_unsupported_extensions_rejected(self):
        for name in ("note.txt", "clip.mkv", "photo.png", "archive.zip", "noext"):
            with self.subTest(name=name):
                self.assertFalse(watch.is_supported_audio(name))

    def test_shared_constant_matches_files_page(self):
        # Той самий список, що приймає drag&drop — джерело правди одне (watch.py).
        from fronts.desktop.main_window import FilesPage
        self.assertIs(FilesPage.AUDIO_EXT, watch.AUDIO_EXT)


class NewAudioFilesTests(unittest.TestCase):
    def test_keeps_only_new_supported_files(self):
        files = ["/d/a.mp3", "/d/b.txt", "/d/c.wav", "/d/d.mp3"]
        seen = {"/d/d.mp3"}                 # вже оброблений раніше
        got = watch.new_audio_files(files, seen)
        self.assertEqual(got, ["/d/a.mp3", "/d/c.wav"])  # .txt і вже-бачений відпали

    def test_already_processed_never_returned_again(self):
        # «не обробляти вже оброблене»: імітуємо два проходи спостерігача.
        files = ["/d/rec.ogg"]
        seen = set()
        first = watch.new_audio_files(files, seen)
        self.assertEqual(first, ["/d/rec.ogg"])
        seen.update(str(f) for f in first)   # так робить оркестратор після постановки
        second = watch.new_audio_files(files, seen)
        self.assertEqual(second, [])         # той самий файл удруге не береться

    def test_empty_when_nothing_new(self):
        self.assertEqual(watch.new_audio_files([], set()), [])


class SizeStableTests(unittest.TestCase):
    def test_two_equal_nonzero_reads_are_stable(self):
        self.assertTrue(watch.size_is_stable(1024, 1024))

    def test_first_read_never_stable(self):
        self.assertFalse(watch.size_is_stable(-1, 1024))

    def test_growing_file_not_stable(self):
        self.assertFalse(watch.size_is_stable(512, 1024))

    def test_empty_file_not_stable(self):
        self.assertFalse(watch.size_is_stable(0, 0))


class WaitUntilStableTests(unittest.TestCase):
    """Файл ще копіюється: розмір читається доти, доки два послідовні заміри не
    збіжаться. Таймери мокаємо (sleep не спить) — тест миттєвий."""

    def test_stable_file_returns_true_without_real_sleep(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"x" * 2048)
            path = f.name
        try:
            slept = []
            ok = watch.wait_until_stable(
                path, sleep=slept.append, interval=1.0)
            self.assertTrue(ok)
            # перший замір ще не має з чим порівняти → рівно одна «пауза» до збігу
            self.assertEqual(slept, [1.0])
        finally:
            os.unlink(path)

    def test_growing_then_settling_file(self):
        # getsize мокаємо: файл росте 1000 → 2000 → 3000 → 3000 (дописано).
        sizes = iter([1000, 2000, 3000, 3000])
        slept = []
        ok = watch.wait_until_stable(
            "any", getsize=lambda _p: next(sizes),
            sleep=slept.append, interval=1.0)
        self.assertTrue(ok)
        self.assertEqual(len(slept), 3)      # три паузи до другого однакового заміру

    def test_missing_file_returns_false(self):
        def _boom(_p):
            raise OSError("зник під час копіювання")
        ok = watch.wait_until_stable(
            "gone", getsize=_boom, sleep=lambda _s: None)
        self.assertFalse(ok)

    def test_forever_growing_file_times_out(self):
        counter = iter(range(10_000))
        slept = []
        ok = watch.wait_until_stable(
            "grow", getsize=lambda _p: 100 + next(counter),
            sleep=slept.append, interval=1.0, timeout=5.0)
        self.assertFalse(ok)                 # так і не стабілізувався → таймаут


if __name__ == "__main__":
    unittest.main()
