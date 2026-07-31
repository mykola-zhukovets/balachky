"""Парсер CHANGELOG.md для картки «Що нового» (whisper_core.changelog)."""
import tempfile
import unittest
from pathlib import Path

from whisper_core import changelog

ROOT = Path(__file__).resolve().parent.parent

_FIXTURE = """\
# Changelog

## [Unreleased]

## [1.2.3-beta] - 2026-07-25

### Added

**Українською:**

- **Нова фіча.** Опис нової фічі.

**In English:**

- **New feature.** Feature description.

### Fixed

**Українською:**

- Дрібний фікс без жирного заголовка.

**In English:**

- Small fix without bold heading.

## [1.2.2] - 2026-07-20

### Changed

**Українською:**

- Стара зміна.

**In English:**

- Old change.

## [1.0.0] - 2026-07-12

### Added

**Українською:**

- Найдавніша зміна.

**In English:**

- Oldest change.
"""


class ChangelogParserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        self.tmp.write(_FIXTURE)
        self.tmp.close()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_unreleased_section_is_skipped(self):
        entries = changelog.latest_entries(self.path, lang="uk", max_versions=10)
        versions = [e["version"] for e in entries]
        self.assertNotIn("Unreleased", versions)

    def test_max_versions_limits_result_and_keeps_order(self):
        entries = changelog.latest_entries(self.path, lang="uk", max_versions=2)
        self.assertEqual([e["version"] for e in entries],
                         ["1.2.3-beta", "1.2.2"])

    def test_language_filter_picks_only_requested_language(self):
        entries = changelog.latest_entries(self.path, lang="uk", max_versions=1)
        self.assertEqual(entries[0]["items"],
                         ["<b>Нова фіча.</b> Опис нової фічі.",
                          "Дрібний фікс без жирного заголовка."])

        entries_en = changelog.latest_entries(self.path, lang="en", max_versions=1)
        self.assertEqual(entries_en[0]["items"],
                         ["<b>New feature.</b> Feature description.",
                          "Small fix without bold heading."])

    def test_date_is_captured(self):
        entries = changelog.latest_entries(self.path, lang="uk", max_versions=1)
        self.assertEqual(entries[0]["date"], "2026-07-25")

    def test_missing_file_returns_empty_list_not_error(self):
        entries = changelog.latest_entries(
            self.path.parent / "does-not-exist.md", lang="uk")
        self.assertEqual(entries, [])


class RealChangelogTests(unittest.TestCase):
    """Той самий парсер проти справжнього CHANGELOG.md репозиторію."""

    def test_real_file_yields_current_version_entry(self):
        """Верхній запис журналу мусить описувати САМЕ ту версію, яку збирає
        застосунок. Раніше тут стояв жорсткий рядок і його доводилось правити
        руками щорелізу (31.07 забули — тест почервонів уже після коміта);
        звірка з канонічною константою версії ловить і протилежне: підняли
        версію, а запис у журналі не додали."""
        from whisper_core.version import DISPLAY_VERSION

        entries = changelog.latest_entries(
            ROOT / "CHANGELOG.md", lang="uk", max_versions=1)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["version"], DISPLAY_VERSION)
        self.assertTrue(entries[0]["items"], "нема пунктів для поточної версії")


if __name__ == "__main__":
    unittest.main()
