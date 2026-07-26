import json
import tempfile
import time
import unittest
from pathlib import Path

from whisper_core.stats import summarize, estimate_saved_minutes, streak_days


class StatsSummaryTests(unittest.TestCase):
    """Чиста функція зведення історії: порожньо / кілька днів / биті рядки."""

    @staticmethod
    def _write(tmp, lines):
        path = Path(tmp) / "history.jsonl"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _day_start(now):
        lt = time.localtime(now)
        return now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)

    def test_empty_history_is_all_zeros(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"      # файлу ще нема
            data = summarize(path)
            for period in ("today", "week", "all"):
                self.assertEqual(data[period], {"records": 0, "words": 0})

    def test_counts_split_by_today_week_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000
            day_start = self._day_start(now)
            recs = [
                {"ts": day_start + 10, "final": "один два три"},    # сьогодні, 3
                {"ts": day_start + 20, "final": "чотири"},           # сьогодні, 1
                {"ts": day_start - 3600, "final": "hello world"},    # вчора, 2
                {"ts": now - 8 * 86400, "final": "старий запис тут"},  # >7 днів, 3
                {"final": "безчасовий"},                             # без ts, 1
            ]
            path = self._write(
                tmp, [json.dumps(r, ensure_ascii=False) for r in recs])
            data = summarize(path, now=now)
            self.assertEqual(data["today"], {"records": 2, "words": 4})
            self.assertEqual(data["week"], {"records": 3, "words": 6})
            self.assertEqual(data["all"], {"records": 5, "words": 10})

    def test_broken_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000
            day_start = self._day_start(now)
            lines = [
                json.dumps({"ts": day_start + 10, "final": "два слова"}),
                "не json зовсім",
                "",
                "{битий",
                # final відсутній → рахуємо raw-фолбек (3 слова)
                json.dumps({"ts": day_start + 20, "raw": "raw фолбек тут"}),
            ]
            path = self._write(tmp, lines)
            data = summarize(path, now=now)
            self.assertEqual(data["today"], {"records": 2, "words": 5})
            self.assertEqual(data["all"], {"records": 2, "words": 5})


class SavedTimeTests(unittest.TestCase):
    """Формула зекономленого часу проти набору (feature/ux-center)."""

    def test_words_over_wpm(self):
        self.assertAlmostEqual(estimate_saved_minutes(400, wpm=40), 10.0)
        self.assertAlmostEqual(estimate_saved_minutes(40), 1.0)   # дефолт 40 wpm

    def test_zero_and_guards(self):
        self.assertEqual(estimate_saved_minutes(0), 0.0)
        self.assertEqual(estimate_saved_minutes(100, wpm=0), 0.0)   # без ділення на 0
        self.assertEqual(estimate_saved_minutes(-5), 0.0)


class StreakTests(unittest.TestCase):
    """Стрік днів поспіль над history.jsonl (feature/ux-center)."""

    @staticmethod
    def _write(tmp, recs):
        path = Path(tmp) / "history.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
            encoding="utf-8")
        return path

    def test_empty_is_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(streak_days(Path(tmp) / "history.jsonl"), 0)

    def test_three_consecutive_days_including_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000
            recs = [{"ts": now - k * 86400, "final": "x"} for k in (0, 1, 2)]
            path = self._write(tmp, recs)
            self.assertEqual(streak_days(path, now=now), 3)

    def test_gap_breaks_streak(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000
            # сьогодні + пропуск (нема вчора) + позавчора
            recs = [{"ts": now, "final": "x"}, {"ts": now - 2 * 86400, "final": "y"}]
            path = self._write(tmp, recs)
            self.assertEqual(streak_days(path, now=now), 1)

    def test_yesterday_only_counts_from_yesterday(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = 1_700_000_000
            recs = [{"ts": now - 86400, "final": "x"},
                    {"ts": now - 2 * 86400, "final": "y"}]
            path = self._write(tmp, recs)
            self.assertEqual(streak_days(path, now=now), 2)


if __name__ == "__main__":
    unittest.main()
