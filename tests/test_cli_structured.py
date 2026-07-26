"""feature/cli-structured — argparse-підкоманди CLI зі структурованим виводом.

Ядро кожної підкоманди тестуємо БЕЗ реального Engine (мок транскрипції) на
тимчасовому корені профілів. Перевіряємо і людський, і --json вивід, парсинг
argparse, невідому команду (помилка+код) і зворотну сумісність старої форми.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from fronts import cli
from whisper_core import profiles


# Мок рушія: детермінована «розшифровка» без моделі. Сигнатура — як у
# Engine.transcribe: (raw, final, duration, words, segments).
def _fake_transcribe(cfg, terms, path):
    segs = [(0.0, 1.5, "перше речення"), (1.5, 3.0, "друге речення")]
    return ("сире", "перше речення друге речення", 3.0, [], segs)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # створити активний профіль default
        self.prof = profiles.get_active(self.root)

    def parse(self, argv):
        return cli.build_parser().parse_args(argv)

    def run_cmd(self, fn, argv, **kw):
        args = self.parse(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = fn(args, root=self.root, **kw)
        return rc, out.getvalue(), err.getvalue()


class TranscribeTests(_Base):
    def test_json_output(self):
        f = self.root / "a.wav"
        f.write_bytes(b"x")
        rc, out, err = self.run_cmd(
            cli.cmd_transcribe, ["transcribe", str(f), "--json"],
            transcribe_fn=_fake_transcribe)
        self.assertEqual(rc, 0)
        obj = json.loads(out)
        self.assertEqual(obj["text"], "перше речення друге речення")
        self.assertEqual(len(obj["segments"]), 2)
        self.assertEqual(obj["segments"][0], {"start": 0.0, "end": 1.5,
                                              "text": "перше речення"})
        self.assertIn("model", obj)
        self.assertEqual(obj["duration"], 3.0)

    def test_human_output(self):
        f = self.root / "a.wav"
        f.write_bytes(b"x")
        rc, out, err = self.run_cmd(
            cli.cmd_transcribe, ["transcribe", str(f)],
            transcribe_fn=_fake_transcribe)
        self.assertEqual(rc, 0)
        self.assertIn("перше речення друге речення", out)
        self.assertNotIn("{", out)          # не JSON

    def test_model_override_in_json(self):
        f = self.root / "a.wav"
        f.write_bytes(b"x")
        rc, out, _ = self.run_cmd(
            cli.cmd_transcribe,
            ["transcribe", str(f), "--model", "tiny", "--json"],
            transcribe_fn=_fake_transcribe)
        self.assertEqual(json.loads(out)["model"], "tiny")

    def test_missing_file_errors(self):
        rc, out, err = self.run_cmd(
            cli.cmd_transcribe, ["transcribe", "нема.wav", "--json"],
            transcribe_fn=_fake_transcribe)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")           # stdout чистий
        self.assertIn("Нема файлу", err)


class DictionaryTests(_Base):
    def test_add_then_list_json(self):
        rc, _, _ = self.run_cmd(
            cli.cmd_dictionary, ["dictionary", "add", "кар'єра", "карʼєра"])
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cmd(
            cli.cmd_dictionary, ["dictionary", "list", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        canons = [t["canon"] for t in data["terms"]]
        self.assertIn("кар'єра", canons)

    def test_list_human(self):
        self.run_cmd(cli.cmd_dictionary, ["dictionary", "add", "слово"])
        rc, out, _ = self.run_cmd(cli.cmd_dictionary, ["dictionary", "list"])
        self.assertEqual(rc, 0)
        self.assertIn("слово", out)
        self.assertNotIn("{", out)

    def test_remove(self):
        self.run_cmd(cli.cmd_dictionary, ["dictionary", "add", "тимчасове"])
        rc, out, _ = self.run_cmd(
            cli.cmd_dictionary, ["dictionary", "remove", "тимчасове", "--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        # повторне видалення → ok=False, код 1
        rc2, out2, _ = self.run_cmd(
            cli.cmd_dictionary, ["dictionary", "remove", "тимчасове", "--json"])
        self.assertEqual(rc2, 1)
        self.assertFalse(json.loads(out2)["ok"])

    def test_add_without_canon_errors(self):
        # argparse дозволяє відсутній canon (nargs="?") — валідуємо в коді
        rc, out, err = self.run_cmd(cli.cmd_dictionary, ["dictionary", "add"])
        self.assertEqual(rc, 1)
        self.assertIn("Вжиток", err)


class HistoryTests(_Base):
    def _seed(self, *finals):
        from whisper_core.history import log_history
        for fin in finals:
            log_history(self.prof.history_path, fin, fin, source="cli")

    def test_search_json(self):
        self._seed("привіт світе", "кава з молоком", "світлий день")
        rc, out, _ = self.run_cmd(
            cli.cmd_history, ["history", "search", "світ", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["query"], "світ")
        self.assertGreaterEqual(len(data["results"]), 2)
        for r in data["results"]:
            self.assertIn("світ", r["snippet"].lower())

    def test_search_human(self):
        self._seed("унікальнеслово тут")
        rc, out, _ = self.run_cmd(
            cli.cmd_history, ["history", "search", "унікальнеслово"])
        self.assertEqual(rc, 0)
        self.assertIn("унікальнеслово", out)
        self.assertNotIn("{", out)

    def test_empty_query_errors(self):
        rc, out, err = self.run_cmd(
            cli.cmd_history, ["history", "search", " "])
        self.assertEqual(rc, 1)
        self.assertIn("Порожній запит", err)


class ExportTests(_Base):
    def _session(self, sid="session-1"):
        meetings = self.root / "meetings"
        d = meetings / sid
        d.mkdir(parents=True)
        (d / "transcript.json").write_text(json.dumps([
            {"start": 0.0, "end": 1.2, "speaker": "s1", "text": "Вітаю всіх"},
            {"start": 1.2, "end": 2.5, "speaker": "s2", "text": "Починаємо нараду"},
        ], ensure_ascii=False), encoding="utf-8")
        return meetings, sid

    def test_export_session_srt_stdout(self):
        meetings, sid = self._session()
        rc, out, _ = self.run_cmd(
            cli.cmd_export, ["export", sid, "--format", "srt"],
            meetings_root=meetings)
        self.assertEqual(rc, 0)
        self.assertIn("-->", out)           # таймкод SRT
        self.assertIn("Вітаю всіх", out)

    def test_export_session_txt_to_file(self):
        meetings, sid = self._session()
        out_path = self.root / "out.txt"
        rc, out, _ = self.run_cmd(
            cli.cmd_export,
            ["export", sid, "--format", "txt", "--out", str(out_path)],
            meetings_root=meetings)
        self.assertEqual(rc, 0)
        body = out_path.read_text(encoding="utf-8")
        self.assertIn("Вітаю всіх", body)
        self.assertIn("Починаємо нараду", body)

    def test_export_file_via_engine_mock(self):
        meetings = self.root / "meetings"
        meetings.mkdir()
        f = self.root / "audio.wav"
        f.write_bytes(b"x")
        rc, out, _ = self.run_cmd(
            cli.cmd_export, ["export", str(f), "--format", "md"],
            meetings_root=meetings, transcribe_fn=_fake_transcribe)
        self.assertEqual(rc, 0)
        self.assertIn("перше речення", out)
        self.assertIn("---", out)           # frontmatter Markdown

    def test_export_session_traversal_does_not_leak(self):
        # Traversal-id не має прочитати transcript.json ПОЗА teky нарад.
        meetings = self.root / "meetings"
        meetings.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "transcript.json").write_text(json.dumps(
            [{"start": 0.0, "end": 1.0, "speaker": "x", "text": "СЕКРЕТ ВИТІК"}],
            ensure_ascii=False), encoding="utf-8")
        rc, out, _ = self.run_cmd(
            cli.cmd_export, ["export", "../outside", "--format", "txt"],
            meetings_root=meetings, transcribe_fn=_fake_transcribe)
        # session-гілку відкинуто (за межами meetings) → у вивід не потрапив секрет.
        self.assertNotIn("СЕКРЕТ ВИТІК", out)

    def test_export_confine_root_rejects_outside_file(self):
        # MCP-режим (confine_root заданий): файл-джерело поза межами → відмова.
        meetings = self.root / "meetings"
        meetings.mkdir()
        secret = self.root.parent / f"secret_{self.root.name}.txt"
        secret.write_text("СЕКРЕТ", encoding="utf-8")
        self.addCleanup(lambda: secret.unlink(missing_ok=True))
        rc, out, err = self.run_cmd(
            cli.cmd_export, ["export", str(secret), "--format", "txt"],
            meetings_root=meetings, transcribe_fn=_fake_transcribe,
            confine_root=self.root)
        self.assertEqual(rc, 1)
        self.assertIn("поза межами", err.lower())

    def test_export_unknown_target_errors(self):
        meetings = self.root / "meetings"
        meetings.mkdir()
        rc, out, err = self.run_cmd(
            cli.cmd_export, ["export", "нема", "--format", "txt"],
            meetings_root=meetings, transcribe_fn=_fake_transcribe)
        self.assertEqual(rc, 1)
        self.assertIn("Нема сесії чи файлу", err)


class ParserAndCompatTests(unittest.TestCase):
    def test_unknown_command_errors(self):
        # argparse при невідомій підкоманді завершує SystemExit із кодом 2
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            cli.build_parser().parse_args(["bogus"])
        self.assertNotEqual(cm.exception.code, 0)

    def test_export_requires_format(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["export", "x"])

    def test_legacy_form_still_works(self):
        # стара форма «<файл>» без підкоманди → розшифровка + підказка в stderr
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            profiles.get_active(root)
            f = root / "a.wav"
            f.write_bytes(b"x")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli._legacy_main([str(f)], root=root,
                                      transcribe_fn=_fake_transcribe)
            self.assertEqual(rc, 0)
            self.assertIn("перше речення друге речення", out.getvalue())
            self.assertIn("нова форма", err.getvalue())

    def test_main_routes_legacy_for_nonsubcommand(self):
        # main() з першим токеном-файлом (не підкоманда) іде у legacy-гілку
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cli._legacy_main(["--profile"], root=root)
            self.assertEqual(rc, 1)   # --profile без значення й без файлів


if __name__ == "__main__":
    unittest.main()
