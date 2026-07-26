"""E6: history append і rewrite користуються спільним файловим lock."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from whisper_core.history import history_lock, log_history, read_recent, update_final
from whisper_core.profiles import Profile


class HistoryLockingTests(unittest.TestCase):
    def test_two_concurrent_appends_lose_no_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            gate = threading.Barrier(3)

            def writer(text):
                gate.wait()
                log_history(path, text, text, enabled=True)

            workers = [threading.Thread(target=writer, args=(text,))
                       for text in ("first", "second")]
            for worker in workers:
                worker.start()
            gate.wait()
            for worker in workers:
                worker.join()
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual({row["final"] for row in rows}, {"first", "second"})
            self.assertEqual(len(read_recent(path)), 2)


    def test_update_final_waits_for_history_lock(self):
        """update_final (виправлення розшифровки у вкладці «Файли») мусить брати
        той самий history_lock, що append/rewrite. Інакше конкурентний писар у
        вікно read→write губить дописані рядки. Доказ: поки lock тримає головний
        потік, update_final у воркері БЛОКУЄТЬСЯ."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                json.dumps({"id": "1", "raw": "r", "final": "old",
                            "source": "desktop"}) + "\n", encoding="utf-8")
            done = []

            with history_lock(path):
                worker = threading.Thread(
                    target=lambda: done.append(update_final(path, "old", "new")))
                worker.start()
                worker.join(0.05)
                self.assertTrue(worker.is_alive())   # заблокований на lock
            worker.join()

            self.assertEqual(done, [True])
            rows = [json.loads(line) for line in
                    path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["final"], "new")
            self.assertEqual(rows[0]["raw"], "r")     # verbatim не чіпаємо

    def test_update_final_keeps_concurrent_append(self):
        """Дописаний під час read→write update_final рядок НЕ має зникнути:
        серіалізація через lock зберігає обидва записи."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(
                json.dumps({"id": "1", "raw": "r", "final": "old",
                            "source": "desktop"}) + "\n", encoding="utf-8")

            # append відбувається, поки update_final ще тримає lock; коли lock
            # звільниться — append дописує ПОВЕРХ переписаного файлу, нічого не гублячи.
            with history_lock(path):
                appender = threading.Thread(
                    target=lambda: log_history(path, "x", "appended", enabled=True))
                updater = threading.Thread(
                    target=lambda: update_final(path, "old", "new"))
                appender.start(); updater.start()
                appender.join(0.05); updater.join(0.05)
                self.assertTrue(appender.is_alive() and updater.is_alive())
            appender.join(); updater.join()

            finals = {json.loads(line)["final"] for line in
                      path.read_text(encoding="utf-8").splitlines()}
            self.assertEqual(finals, {"new", "appended"})

    def test_reset_memory_waits_for_history_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = Path(tmp) / "profile"
            profile_dir.mkdir()
            profile = Profile("profile", profile_dir)
            profile.history_path.write_text('{"final":"before"}\n', encoding="utf-8")
            result = []

            with history_lock(profile.history_path):
                worker = threading.Thread(target=lambda: result.append(profile.reset_memory()))
                worker.start()
                worker.join(0.05)
                self.assertTrue(worker.is_alive())
            worker.join()

            self.assertEqual(len(result), 1)
            self.assertFalse(profile.history_path.exists())
            self.assertIn('"before"', result[0].read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
