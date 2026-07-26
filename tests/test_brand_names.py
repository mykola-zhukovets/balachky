"""Brand-name canon for Ukrainian and English surfaces."""
from pathlib import Path

from fronts.desktop.i18n import STRINGS


ROOT = Path(__file__).resolve().parents[1]


def test_english_brand_is_balachky_only():
    en = STRINGS["en"]

    assert en["app_title"] == "Balachky"
    assert en["brand_top"] == "Balachky"
    assert en["brand_bottom"] == ""
    assert en["set_about_lead"] == "“Balachky” turns your voice into text."

    legacy = ("Balachky u Korosteni", "Chats in Korosten")
    hits = {
        key: value for key, value in en.items()
        if any(name in str(value) for name in legacy)
    }
    assert hits == {}


def test_ukrainian_brand_keeps_korosten_name():
    uk = STRINGS["uk"]

    assert uk["app_title"] == "Балачки у Коростені"
    assert uk["brand_top"] == "Балачки"
    assert uk["brand_bottom"] == "у Коростені"


def test_english_readme_heading_is_balachky_only():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert '<h1 align="center">Balachky</h1>' in readme
    assert "Balachky — Chats in Korosten" not in readme


def test_installer_localizes_product_name_by_language():
    script = (ROOT / "installer" / "balachky.iss").read_text(encoding="utf-8-sig")

    assert '#define AppDisplayName "Balachky"' in script
    assert "AppName={cm:AppDisplayName}" in script
    assert "AppVerName={cm:AppDisplayName} {#AppVersion}" in script
    assert "ukrainian.AppDisplayName=Балачки у Коростені" in script
    assert "english.AppDisplayName=Balachky" in script
    assert "english.UninstTitle=Uninstall Balachky" in script
    assert "Balachky u Korosteni" not in script


def test_english_license_notices_use_balachky_brand():
    paths = (
        ROOT / "THIRD-PARTY-NOTICES.txt",
        ROOT / "licenses" / "PERMISSIVE-LICENSES.txt",
    )

    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        assert "Balachky u Korosteni" not in text
