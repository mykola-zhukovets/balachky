"""README screenshot coverage and language pairing."""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FEATURES = ("01-dictation", "02-files")


def _image_sources(readme_name: str) -> set[str]:
    text = (ROOT / readme_name).read_text(encoding="utf-8")
    return set(re.findall(r'<img[^>]+src="([^"]+)"', text))


def test_readme_screenshot_files_exist():
    for readme_name in ("README.md", "README.uk.md"):
        for source in _image_sources(readme_name):
            if source.startswith(("http://", "https://")):
                continue
            assert (ROOT / source).is_file(), f"{readme_name}: missing {source}"


def test_feature_screenshots_match_readme_language():
    english = _image_sources("README.md")
    ukrainian = _image_sources("README.uk.md")

    for feature in FEATURES:
        assert f"docs/screenshots/{feature}-en.png" in english
        assert f"docs/screenshots/{feature}.png" in ukrainian
        assert f"docs/screenshots/{feature}.png" not in english
        assert f"docs/screenshots/{feature}-en.png" not in ukrainian
