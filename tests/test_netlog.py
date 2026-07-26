"""Журнал мережевої активності (whisper_core.netlog) — доказова офлайновість.

Ключова гарантія фічі: у нормі журнал порожній; коли користувач сам запускає
завантаження моделі — воно з'являється як дозволене (allowed=True), і жодних
інших з'єднань немає. Записуємо лише факт+хост+тип, без вмісту.
"""
import tempfile
import unittest
import urllib.request
from pathlib import Path

from whisper_core import netlog


class NetlogCore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "network_log.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_by_default(self):
        # Немає файлу → офлайн-норма: нуль з'єднань.
        self.assertEqual(netlog.entries(path=self.path), [])
        s = netlog.summary(path=self.path)
        self.assertEqual(s["total"], 0)
        self.assertEqual(s["allowed"], 0)
        self.assertEqual(s["flagged"], 0)
        self.assertIsNone(s["last_ts"])

    def test_model_download_recorded_as_allowed(self):
        netlog.record("huggingface.co", kind=netlog.MODEL, allowed=True,
                      detail="large", path=self.path)
        rows = netlog.entries(path=self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "huggingface.co")
        self.assertEqual(rows[0]["kind"], netlog.MODEL)
        self.assertTrue(rows[0]["allowed"])

    def test_only_model_download_nothing_else(self):
        # Сценарій приймання: одне завантаження моделі, дозволене; більш нічого.
        netlog.record_url("https://huggingface.co/org/model/resolve/main/x.bin",
                          kind=netlog.MODEL, path=self.path)
        s = netlog.summary(path=self.path)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["allowed"], 1)
        self.assertEqual(s["flagged"], 0, "у журналі має бути ЛИШЕ дозволене завантаження")

    def test_record_url_extracts_host_only(self):
        netlog.record_url("https://api.github.com/repos/x/y/releases/latest",
                          kind=netlog.UPDATE, path=self.path)
        rows = netlog.entries(path=self.path)
        self.assertEqual(rows[0]["host"], "api.github.com")

    def test_unexpected_connection_is_flagged(self):
        # Будь-що поза нашими точками (kind=OTHER) → allowed=False = помітно.
        netlog.record("example.com", path=self.path)
        s = netlog.summary(path=self.path)
        self.assertEqual(s["flagged"], 1)
        self.assertEqual(s["allowed"], 0)

    def test_no_payload_content_stored(self):
        # Приватність: у записі — лише факт+хост+тип, жодного вмісту запиту.
        netlog.record_url("https://huggingface.co/a/b?secret=payload",
                          kind=netlog.MODEL, path=self.path)
        keys = set(netlog.entries(path=self.path)[0].keys())
        self.assertEqual(keys, {"ts", "host", "kind", "allowed", "detail"})
        # host — тільки домен, без шляху/параметрів запиту
        self.assertEqual(netlog.entries(path=self.path)[0]["host"], "huggingface.co")

    def test_no_local_path_leak_on_fileurl(self):
        # file:// чи URL без хоста → "?", НІКОЛИ не сирий локальний шлях у журналі.
        netlog.record_url("file:///C:/Users/secret/model.gguf",
                          kind=netlog.MODEL, path=self.path)
        host = netlog.entries(path=self.path)[0]["host"]
        self.assertEqual(host, "?")
        self.assertNotIn("secret", host)

    def test_clear_empties_log(self):
        netlog.record("huggingface.co", kind=netlog.MODEL, allowed=True, path=self.path)
        netlog.clear(path=self.path)
        self.assertEqual(netlog.entries(path=self.path), [])

    def test_record_never_raises_on_bad_path(self):
        # Логування — best-effort: помилка запису не має зупиняти завантаження.
        bad = Path(self._tmp.name) / "no" / "such" / "file"  # батьк. теки нема — record сам створить
        netlog.record("h", kind=netlog.MODEL, allowed=True, path=bad)  # не кидає
        # шлях у неможливе місце (файл як тека) — теж мовчки
        f = Path(self._tmp.name) / "afile"
        f.write_text("x", encoding="utf-8")
        netlog.record("h", kind=netlog.MODEL, allowed=True, path=f / "child")  # не кидає

    def test_malformed_lines_skipped(self):
        self.path.write_text('{"host":"ok","kind":"model","allowed":true}\n'
                             'not-json\n'
                             '{"no_host":1}\n', encoding="utf-8")
        rows = netlog.entries(path=self.path)
        self.assertEqual([r["host"] for r in rows], ["ok"])


class NetlogWiring(unittest.TestCase):
    """Наскрізна перевірка: реальна точка виходу справді пише в журнал."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "network_log.jsonl"
        self._orig_default = netlog._default_path
        netlog._default_path = lambda: self.path  # інструментація кличе без path=

    def tearDown(self):
        netlog._default_path = self._orig_default
        self._tmp.cleanup()

    def test_autocorrect_download_records_model_connection(self):
        from whisper_core import autocorrect_download

        class _FakeResp:
            def __init__(self):
                self.headers = {"Content-Length": "5"}
                self._sent = False

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, _n=-1):
                if self._sent:
                    return b""
                self._sent = True
                return b"hello"

        orig = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: _FakeResp()
        try:
            dest = Path(self._tmp.name) / "out.txt"
            autocorrect_download._download("https://raw.githubusercontent.com/x/y/z.txt", dest)
        finally:
            urllib.request.urlopen = orig

        rows = netlog.entries()  # дефолтний шлях = наш тимчасовий файл
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["host"], "raw.githubusercontent.com")
        self.assertEqual(rows[0]["kind"], netlog.MODEL)
        self.assertTrue(rows[0]["allowed"])


if __name__ == "__main__":
    unittest.main()
