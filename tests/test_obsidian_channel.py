# feature/obsidian-channel
"""Тести чистого каналу доставки нарад у Obsidian (whisper_core.obsidian, без Qt):
підстановка імені за шаблоном із плейсхолдерами, розвʼязання колізій імен без
перезапису, безпечний запис ЛИШЕ в межах вибраної папки (safe_under), поле
type: meeting у frontmatter та URI obsidian://open. Стиль — як test_markdown_export.py."""
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from whisper_core import export, obsidian


class RenderFilenameTests(unittest.TestCase):
    def test_default_template_date_name(self):
        out = obsidian.render_filename("{дата}-{назва}",
                                       date="2026-07-18", name="Планерка")
        self.assertEqual(out, "2026-07-18-Планерка.md")

    def test_english_placeholder_synonyms(self):
        out = obsidian.render_filename("{date}-{name}-{time}",
                                       date="2026-07-18", name="Standup", time="14-30")
        self.assertEqual(out, "2026-07-18-Standup-14-30.md")

    def test_time_placeholder(self):
        out = obsidian.render_filename("{назва}-{час}", name="Нарада", time="09-05")
        self.assertEqual(out, "Нарада-09-05.md")

    def test_md_extension_not_doubled(self):
        out = obsidian.render_filename("{назва}.md", name="Звіт")
        self.assertEqual(out, "Звіт.md")

    def test_illegal_chars_and_separators_sanitized(self):
        # роздільники шляху й заборонені символи → «-»; за межі папки не вийде
        out = obsidian.render_filename("{назва}", name=r'a/b\c:d?*e')
        self.assertNotIn("/", out)
        self.assertNotIn("\\", out)
        self.assertNotIn(":", out)
        self.assertTrue(out.endswith(".md"))

    def test_empty_placeholders_fall_back(self):
        out = obsidian.render_filename("{дата}-{назва}", date="", name="")
        self.assertEqual(out, "нарада.md")

    def test_none_template_uses_default(self):
        out = obsidian.render_filename(None, date="2026-07-18", name="X")
        self.assertEqual(out, "2026-07-18-X.md")

    def test_double_dashes_collapsed(self):
        # порожня назва між дефісами не лишає «--»
        out = obsidian.render_filename("{дата}-{назва}-{час}",
                                       date="2026-07-18", name="", time="12-00")
        self.assertNotIn("--", out)
        self.assertEqual(out, "2026-07-18-12-00.md")


class CollisionTests(unittest.TestCase):
    def test_no_collision_returns_plain(self):
        with tempfile.TemporaryDirectory() as d:
            p = obsidian.resolve_collision(d, "нота.md")
            self.assertEqual(p, Path(d) / "нота.md")

    def test_collision_appends_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "нота.md").write_text("x", encoding="utf-8")
            p = obsidian.resolve_collision(d, "нота.md")
            self.assertEqual(p, Path(d) / "нота-2.md")

    def test_collision_increments_until_free(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "нота.md").write_text("x", encoding="utf-8")
            (Path(d) / "нота-2.md").write_text("x", encoding="utf-8")
            p = obsidian.resolve_collision(d, "нота.md")
            self.assertEqual(p, Path(d) / "нота-3.md")


class WriteMarkdownTests(unittest.TestCase):
    def test_writes_file_and_returns_path(self):
        with tempfile.TemporaryDirectory() as d:
            p = obsidian.write_markdown(d, "нота.md", "---\nx\n---\n")
            self.assertTrue(p.is_file())
            self.assertEqual(p.read_text(encoding="utf-8"), "---\nx\n---\n")

    def test_does_not_overwrite_original(self):
        with tempfile.TemporaryDirectory() as d:
            first = obsidian.write_markdown(d, "нота.md", "перший")
            second = obsidian.write_markdown(d, "нота.md", "другий")
            self.assertNotEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), "перший")
            self.assertEqual(second.read_text(encoding="utf-8"), "другий")
            self.assertEqual(second.name, "нота-2.md")

    def test_lf_newlines_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            p = obsidian.write_markdown(d, "н.md", "a\nb\n")
            self.assertEqual(p.read_bytes(), b"a\nb\n")   # без CRLF

    def test_traversal_outside_vault_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d) / "vault"
            sub.mkdir()
            with self.assertRaises(ValueError):
                obsidian.write_markdown(sub, "../escape.md", "x")
            self.assertFalse((Path(d) / "escape.md").exists())


class FrontmatterTypeTests(unittest.TestCase):
    def test_type_meeting_in_frontmatter(self):
        fm = export.build_frontmatter(
            {"date": "2026-07-18", "type": "meeting", "source": "Планерка"})
        self.assertEqual(fm.splitlines(), [
            "---",
            "date: 2026-07-18",
            "type: meeting",
            'source: "Планерка"',
            "tags: [балачки]",
            "---",
        ])

    def test_no_type_when_absent(self):
        fm = export.build_frontmatter({"date": "2026-07-18"})
        self.assertNotIn("type:", fm)


class OpenUriTests(unittest.TestCase):
    def test_uri_scheme_and_encoded_path(self):
        uri = obsidian.open_uri(r"C:\Vault\нота.md")
        self.assertTrue(uri.startswith("obsidian://open?path="))
        # шлях url-кодований (пробіли/кирилиця/зворотні слеші не сирі)
        encoded = uri.split("path=", 1)[1]
        self.assertNotIn(" ", encoded)
        self.assertEqual(urllib.parse.unquote(encoded),
                         str(Path(r"C:\Vault\нота.md").resolve()))


if __name__ == "__main__":
    unittest.main()
