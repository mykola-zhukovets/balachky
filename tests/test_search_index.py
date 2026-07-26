"""Тести ядра глобального пошуку (whisper_core.search_index): індексація трьох
джерел, ранжування, пошук по даті, порожній індекс. БЕЗ Qt."""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from whisper_core.search_index import (
    KIND_DICTATION, KIND_FILE, KIND_MEETING, SearchIndex,
)


def _write_history(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8")


def _write_meeting(root: Path, session_id: str, *, title: str, created: float,
                   utterances: list) -> Path:
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "meeting.json").write_text(
        json.dumps({"id": session_id, "title": title, "created": created,
                    "status": "done"}, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.json").write_text(
        json.dumps(utterances, ensure_ascii=False), encoding="utf-8")
    return d


class HistoryIndexTests(unittest.TestCase):
    def test_dictation_and_file_records_indexed_with_kind(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": 1000, "raw": "r1", "final": "купити молоко", "source": "desktop"},
                {"ts": 2000, "raw": "r2", "final": "звіт по кварталу", "source": "file"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(len(idx.docs), 2)
            milk = idx.search("молоко")
            self.assertEqual(len(milk), 1)
            self.assertEqual(milk[0].kind, KIND_DICTATION)
            report = idx.search("квартал")
            self.assertEqual(report[0].kind, KIND_FILE)

    def test_empty_final_falls_back_to_raw_and_blank_skipped(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": 1, "raw": "сире слово", "final": "", "source": "desktop"},
                {"ts": 2, "raw": "", "final": "", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(len(idx.docs), 1)
            self.assertTrue(idx.search("сире"))

    def test_profile_name_propagated(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [{"ts": 1, "raw": "", "final": "текст", "source": "desktop"}])
            prof = type("P", (), {"history_path": hp, "name": "робота"})()
            idx = SearchIndex.build(history_paths=[prof])
            self.assertEqual(idx.search("текст")[0].profile, "робота")


class MeetingIndexTests(unittest.TestCase):
    def test_utterances_indexed_with_timecode(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_meeting(root, "2026-07-17_10-00-00", title="Планерка",
                           created=1000.0, utterances=[
                               {"start": 3.5, "end": 5.0, "speaker": "me",
                                "text": "домовились про бюджет"},
                               {"start": 12.0, "end": 14.0, "speaker": "others",
                                "text": "надішлю документи"}])
            idx = SearchIndex.build(meetings_root=root)
            res = idx.search("бюджет")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].kind, KIND_MEETING)
            self.assertEqual(res[0].ref, "2026-07-17_10-00-00")
            self.assertEqual(res[0].title, "Планерка")
            self.assertEqual(res[0].timecode, 3.5)

    def test_transcript_txt_fallback_without_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "2026-07-17_11-00-00"
            d.mkdir()
            (d / "meeting.json").write_text(
                json.dumps({"id": d.name, "created": 5.0}), encoding="utf-8")
            (d / "transcript.txt").write_text("суцільний текст наради без json",
                                              encoding="utf-8")
            idx = SearchIndex.build(meetings_root=root)
            res = idx.search("суцільний")
            self.assertEqual(len(res), 1)
            self.assertIsNone(res[0].timecode)

    def test_broken_meeting_json_skipped(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "2026-07-17_12-00-00"
            d.mkdir()
            (d / "meeting.json").write_text("{ битий", encoding="utf-8")
            idx = SearchIndex.build(meetings_root=root)
            self.assertEqual(idx.docs, [])


class RankingTests(unittest.TestCase):
    def test_more_matches_ranks_higher(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": 1, "raw": "", "final": "кава", "source": "desktop"},
                {"ts": 2, "raw": "", "final": "кава кава кава", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            res = idx.search("кава")
            self.assertEqual(res[0].score, 3)     # три збіги — вище
            self.assertGreater(res[0].score, res[1].score)

    def test_recency_tiebreak_when_equal_score(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": 100, "raw": "", "final": "нота стара", "source": "desktop"},
                {"ts": 900, "raw": "", "final": "нота нова", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            res = idx.search("нота")
            self.assertEqual(res[0].date, 900)

    def test_all_terms_required(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": 1, "raw": "", "final": "синій кит", "source": "desktop"},
                {"ts": 2, "raw": "", "final": "синій птах", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(len(idx.search("синій кит")), 1)


class DateSearchTests(unittest.TestCase):
    def test_search_by_dotted_date(self):
        ts = time.mktime(time.strptime("2026-07-17 09:30", "%Y-%m-%d %H:%M"))
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": ts, "raw": "", "final": "запис того дня", "source": "desktop"},
                {"ts": ts - 86400 * 3, "raw": "", "final": "інший день", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(len(idx.search("17.07.2026")), 1)
            self.assertEqual(len(idx.search("17.07")), 1)
            self.assertEqual(len(idx.search("2026-07-17")), 1)

    def test_date_plus_term_combined(self):
        ts = time.mktime(time.strptime("2026-07-17 09:30", "%Y-%m-%d %H:%M"))
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [
                {"ts": ts, "raw": "", "final": "бюджет затверджено", "source": "desktop"},
                {"ts": ts, "raw": "", "final": "кава", "source": "desktop"},
            ])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(len(idx.search("17.07 бюджет")), 1)


class EmptyIndexTests(unittest.TestCase):
    def test_empty_index_returns_nothing(self):
        idx = SearchIndex.build()
        self.assertEqual(idx.docs, [])
        self.assertEqual(idx.search("будь-що"), [])

    def test_empty_query_returns_nothing(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            _write_history(hp, [{"ts": 1, "raw": "", "final": "текст", "source": "desktop"}])
            idx = SearchIndex.build(history_paths=[hp])
            self.assertEqual(idx.search(""), [])
            self.assertEqual(idx.search("   "), [])

    def test_missing_history_file_skipped(self):
        idx = SearchIndex.build(history_paths=[Path("nope/history.jsonl")])
        self.assertEqual(idx.docs, [])

    def test_snippet_has_context(self):
        with TemporaryDirectory() as tmp:
            hp = Path(tmp) / "history.jsonl"
            long = "початок довгого тексту де десь всередині є слово ключ і продовження далі"
            _write_history(hp, [{"ts": 1, "raw": "", "final": long, "source": "desktop"}])
            idx = SearchIndex.build(history_paths=[hp])
            snip = idx.search("ключ")[0].snippet
            self.assertIn("ключ", snip)
            self.assertLess(len(snip), len(long) + 10)


if __name__ == "__main__":
    unittest.main()
