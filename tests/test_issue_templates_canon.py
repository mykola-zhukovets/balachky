"""Лінт КАНОНу лапок для GitHub issue-шаблону функції.

House-style: у видимих текстах — лише “ ”, НЕ «ялинки» «» чи „лапки-низом“
(джерело: канон типографіки — робочий документ поза репозиторієм).

Стереже feature_request.yml — НОВИЙ файл гілки feature/release-docs. Регрес до
«Балачки» тут уже ловили на рецензії, тож фіксуємо канон тестом.

bug_report.yml і config.yml — легасі на master (не змінювані цією гілкою), тож
за правилом хірургічності їх не чіпаємо й не стережемо цим тестом.
"""
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml"
_FORBIDDEN = "«»„"


class IssueTemplateQuotesCanon(unittest.TestCase):
    def test_no_guillemets_in_feature_request(self):
        text = _TEMPLATE.read_text(encoding="utf-8")
        hits = sorted({ch for ch in _FORBIDDEN if ch in text})
        self.assertEqual(
            hits, [],
            f"лише “ ”, не {hits} у {_TEMPLATE.name}",
        )


if __name__ == "__main__":
    unittest.main()
