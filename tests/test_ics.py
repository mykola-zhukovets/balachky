"""Юніти мінімального .ics-парсера (feature/diary-calendar): whisper_core.ics.
Тільки stdlib, диск — tempfile. Перевіряємо розбір VEVENT, часові зони UTC/локальні,
folding довгих рядків RFC 5545, порожній файл; та suggest_meeting_name за часовим
перекриттям."""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from whisper_core import ics


TWO_EVENTS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Планірка бригади
DTSTART:20260717T090000Z
DTEND:20260717T100000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Синхронізація з командою
DTSTART:20260717T140000Z
DTEND:20260717T150000Z
END:VEVENT
END:VCALENDAR
"""


class ParseTests(unittest.TestCase):
    def test_parses_multiple_events(self):
        events = ics.parse_ics(TWO_EVENTS)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["summary"], "Планірка бригади")
        self.assertEqual(events[1]["summary"], "Синхронізація з командою")

    def test_utc_datetime_is_aware(self):
        ev = ics.parse_ics(TWO_EVENTS)[0]
        self.assertEqual(ev["start"].tzinfo, timezone.utc)
        self.assertEqual(ev["start"], datetime(2026, 7, 17, 9, 0,
                                               tzinfo=timezone.utc))

    def test_local_naive_datetime(self):
        text = (
            "BEGIN:VEVENT\r\n"
            "SUMMARY:Локальна\r\n"
            "DTSTART:20260717T090000\r\n"
            "DTEND:20260717T100000\r\n"
            "END:VEVENT\r\n"
        )
        ev = ics.parse_ics(text)[0]
        self.assertIsNone(ev["start"].tzinfo)
        self.assertEqual(ev["start"], datetime(2026, 7, 17, 9, 0))

    def test_folded_long_summary(self):
        # RFC 5545: продовження рядка починається з пробілу — має склеїтись
        text = (
            "BEGIN:VEVENT\r\n"
            "SUMMARY:Дуже довга назва наради яка не влазить\r\n"
            "  в один рядок\r\n"
            "DTSTART:20260717T090000Z\r\n"
            "DTEND:20260717T100000Z\r\n"
            "END:VEVENT\r\n"
        )
        ev = ics.parse_ics(text)[0]
        self.assertEqual(ev["summary"],
                         "Дуже довга назва наради яка не влазить в один рядок")

    def test_empty_returns_no_events(self):
        self.assertEqual(ics.parse_ics(""), [])
        self.assertEqual(ics.parse_ics("BEGIN:VCALENDAR\nEND:VCALENDAR\n"), [])

    def test_event_without_summary_skipped(self):
        text = ("BEGIN:VEVENT\nDTSTART:20260717T090000Z\n"
                "DTEND:20260717T100000Z\nEND:VEVENT\n")
        self.assertEqual(ics.parse_ics(text), [])


class SuggestTests(unittest.TestCase):
    def _write(self, text):
        d = tempfile.mkdtemp()
        p = Path(d) / "cal.ics"
        p.write_text(text, encoding="utf-8")
        return p

    def test_picks_event_covering_time(self):
        p = self._write(TWO_EVENTS)
        at = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
        self.assertEqual(ics.suggest_meeting_name(p, at), "Планірка бригади")

    def test_picks_second_event(self):
        p = self._write(TWO_EVENTS)
        at = datetime(2026, 7, 17, 14, 15, tzinfo=timezone.utc)
        self.assertEqual(ics.suggest_meeting_name(p, at),
                         "Синхронізація з командою")

    def test_no_event_returns_none(self):
        p = self._write(TWO_EVENTS)
        at = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(ics.suggest_meeting_name(p, at))

    def test_start_inclusive_end_exclusive(self):
        p = self._write(TWO_EVENTS)
        start = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)
        end = datetime(2026, 7, 17, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(ics.suggest_meeting_name(p, start), "Планірка бригади")
        self.assertIsNone(ics.suggest_meeting_name(p, end))

    def test_missing_file_returns_none(self):
        at = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
        self.assertIsNone(ics.suggest_meeting_name("nonexistent.ics", at))


if __name__ == "__main__":
    unittest.main()
