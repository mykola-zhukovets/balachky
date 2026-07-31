"""Static and optional built-dist license traceability guards."""

import hashlib
import os
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "balachky.spec"
NOTICES_PATH = ROOT / "THIRD-PARTY-NOTICES.txt"
LICENSES_DIR = ROOT / "licenses"
BINARY_MANIFEST_PATH = LICENSES_DIR / "BINARY-NOTICES.txt"


def _audit_dist() -> Path | None:
    raw = os.environ.get("BALACHKY_AUDIT_DIST")
    if not raw:
        return None
    path = Path(raw)
    assert path.is_dir(), f"BALACHKY_AUDIT_DIST is not a directory: {path}"
    return path


def _binary_manifest_rows() -> dict[str, dict[str, str]]:
    source = BINARY_MANIFEST_PATH.read_text(encoding="utf-8")
    assert not re.search(
        r"(?i)\b[A-Z]:\\",
        source,
    ), "packaged notice must not expose a build-machine absolute path"
    rows = {}
    in_table = False
    for line in source.splitlines():
        if line == "BINARY TRACE BEGIN":
            in_table = True
            continue
        if line == "BINARY TRACE END":
            in_table = False
            break
        if not in_table or not line or line.startswith("file\t"):
            continue
        fields = line.split("\t")
        assert len(fields) == 5, line
        path, version, upstream, license_id, sha256 = fields
        assert path not in rows
        assert version
        assert upstream.startswith("https://")
        assert license_id
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)
        rows[path] = {
            "version": version,
            "upstream": upstream,
            "license": license_id,
            "sha256": sha256,
        }
    assert not in_table, "missing BINARY TRACE END"
    assert rows
    declared_count = re.search(r"(?m)^Binary count: (\d+)$", source)
    assert declared_count is not None
    assert int(declared_count.group(1)) == len(rows)
    return rows


def test_application_license_files_are_packaged_beside_notices():
    spec = SPEC_PATH.read_text(encoding="utf-8")
    for filename in (
        "THIRD-PARTY-NOTICES.txt",
        "LICENSE",
        "COMMERCIAL-LICENSE.md",
    ):
        assert f'("{filename}", ".")' in spec

    notices = NOTICES_PATH.read_text(encoding="utf-8")
    lowered_notices = notices.lower()
    assert "application root directory" not in lowered_notices
    assert "application root" not in lowered_notices
    assert "_internal directory" in lowered_notices
    consolidated = (LICENSES_DIR / "PERMISSIVE-LICENSES.txt").read_text(
        encoding="utf-8"
    )
    assert "application root" not in consolidated.lower()


def test_every_notice_license_path_exists_and_is_packaged():
    notices = NOTICES_PATH.read_text(encoding="utf-8")
    referenced = {
        match.replace("\\", "/")
        for match in re.findall(
            r"(?i)\blicenses\\[A-Za-z0-9_.-]+\.txt\b",
            notices,
        )
    }
    assert referenced
    for relative in sorted(referenced):
        assert (ROOT / relative).is_file(), relative
    assert '("licenses", "licenses")' in SPEC_PATH.read_text(encoding="utf-8")


def test_dictionary_data_has_official_cc_by_sa_3_text():
    license_path = LICENSES_DIR / "CC-BY-SA-3.0.txt"
    text = license_path.read_text(encoding="utf-8")
    assert "Attribution-ShareAlike 3.0 Unported" in text
    assert "Creative Commons Legal Code" in text
    assert hashlib.sha256(text.encode("utf-8")).hexdigest() == (
        "500f4501315ad94875d7e3bdae735984c97f6bf02f0aa1f3677f31cf69221cf6"
    )
    assert "https://creativecommons.org/licenses/by-sa/3.0/legalcode.txt" in (
        NOTICES_PATH.read_text(encoding="utf-8")
    )


def test_native_extension_owners_have_verbatim_permissive_notices():
    text = (LICENSES_DIR / "PERMISSIVE-LICENSES.txt").read_text(
        encoding="utf-8"
    )
    for owner in ("MarkupSafe 3.0.3", "pydantic-core 2.46.4",
                  "llama-cpp-python 0.3.34"):
        assert owner in text


def test_ffmpeg_lgpl_references_match_embedded_library_license_strings():
    notices = NOTICES_PATH.read_text(encoding="utf-8")
    ffmpeg = notices.split(
        "1. FFmpeg (LGPL build inside PyAV) — no GPL codecs",
        1,
    )[1].split(
        "2. LGPL-3.0 COMPONENTS — Qt / PySide6",
        1,
    )[0]
    assert "SPDX: LGPL-3.0-or-later" in ffmpeg
    assert r"licenses\LGPL-3.0.txt" in ffmpeg
    assert r"licenses\LGPL-2.1.txt" not in ffmpeg
    assert "LGPL-2.1-or-later" not in ffmpeg
    qt = notices.split(
        "2. LGPL-3.0 COMPONENTS — Qt / PySide6",
        1,
    )[1].split(
        "3. FONTS (bundled via QtAwesome 1.4.2)",
        1,
    )[0]
    assert "FFmpeg 7.1.3" in qt
    assert "LGPL-2.1-or-later" in qt
    assert r"licenses\LGPL-2.1.txt" in qt
    assert (LICENSES_DIR / "LGPL-2.1.txt").is_file()
    assert (LICENSES_DIR / "LGPL-3.0.txt").is_file()
    consolidated = (LICENSES_DIR / "PERMISSIVE-LICENSES.txt").read_text(
        encoding="utf-8"
    )
    pyav_note = consolidated.split(
        "PyAV (python wrapper only) 18.0.0",
        1,
    )[1].split(
        "Copyright retained by original committers.",
        1,
    )[0]
    assert "LGPL-3.0-or-later" in pyav_note
    assert " is GPL" not in pyav_note

    rows = _binary_manifest_rows()
    pyav_ffmpeg = {
        path: row
        for path, row in rows.items()
        if re.fullmatch(
            r"_internal/av/"
            r"(?:avcodec|avdevice|avfilter|avformat|avutil|swresample|swscale)"
            r"-\d+\.dll",
            path,
        )
    }
    qt_ffmpeg = {
        path: row
        for path, row in rows.items()
        if re.fullmatch(
            r"_internal/PySide6/"
            r"(?:avcodec|avformat|avutil|swresample|swscale)-\d+\.dll",
            path,
        )
    }
    assert len(pyav_ffmpeg) == 7
    assert {row["license"] for row in pyav_ffmpeg.values()} == {
        "LGPL-3.0-or-later"
    }
    assert len(qt_ffmpeg) == 5
    assert {row["license"] for row in qt_ffmpeg.values()} == {
        "LGPL-2.1-or-later"
    }

    dist = _audit_dist()
    if dist is not None:
        assert b"LGPL version 3 or later" in (
            dist / "_internal" / "av" / "avutil-61.dll"
        ).read_bytes()
        assert b"LGPL version 2.1 or later" in (
            dist / "_internal" / "PySide6" / "avutil-59.dll"
        ).read_bytes()


def test_notices_reference_complete_binary_trace_manifest():
    notices = NOTICES_PATH.read_text(encoding="utf-8")
    assert r"licenses\BINARY-NOTICES.txt" in notices
    rows = _binary_manifest_rows()
    assert len(rows) >= 2
    assert all("unavailable" not in row["version"] for row in rows.values())


def test_existing_dist_binary_manifest_matches_paths_and_sha256():
    dist = _audit_dist()
    if dist is None:
        pytest.skip("set BALACHKY_AUDIT_DIST to inspect an existing build")

    rows = _binary_manifest_rows()
    actual = {
        path.relative_to(dist).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in dist.rglob("*")
        if path.is_file() and path.suffix.lower() in {".dll", ".pyd", ".exe"}
    }
    assert set(rows) == set(actual)
    # Наші власні виконувані файли несуть у собі мітку коміта збірки, тож їхній
    # хеш різний у КОЖНІЙ збірці — звіряти його з комітнутим маніфестом
    # неможливо в принципі. Присутність і ліцензійний рядок для них перевіряє
    # рівність множин вище; сам хеш лишається довідковим (від останньої збірки).
    # Для УСІХ сторонніх бінарників звірка хешів лишається суворою — саме вона
    # 31.07 спіймала підміну LGPL-збірки ffmpeg на GPL-варіант.
    own_build_stamped = {"Balachky.exe", "balachky-protocol-worker.exe"}
    mismatches = {
        path: (rows[path]["sha256"], digest)
        for path, digest in actual.items()
        if path not in own_build_stamped and rows[path]["sha256"] != digest
    }
    assert mismatches == {}


def test_existing_dist_puts_claimed_files_beside_notices():
    dist = _audit_dist()
    if dist is None:
        pytest.skip("set BALACHKY_AUDIT_DIST to inspect an existing build")

    internal = dist / "_internal"
    for filename in (
        "THIRD-PARTY-NOTICES.txt",
        "LICENSE",
        "COMMERCIAL-LICENSE.md",
    ):
        assert (internal / filename).is_file(), filename
