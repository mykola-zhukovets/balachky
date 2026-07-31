"""Guard the effective installer and frozen-distribution contract."""

import ast
import os
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "balachky.spec"
ISS_PATH = ROOT / "installer" / "balachky.iss"
APP_PATH = ROOT / "fronts" / "desktop" / "app.py"
SHERPA_TTS_PATH = ROOT / "whisper_core" / "tts" / "engines" / "sherpa.py"

FORBIDDEN_MODULES = {
    "_pytest",
    "pytest",
    "unittest",
    "matplotlib",
    "tkinter",
    "_tkinter",
    "aiogram",
}
ESPEAK_PATTERN = re.compile(
    r"(?i)(?<![a-z])e-?speak(?:-ng)?(?![a-z])"
)


def _section(source: str, name: str) -> str:
    match = re.search(
        rf"(?ims)^\[{re.escape(name)}\]\s*$"
        rf"(.*?)(?=^\[[^\]]+\]\s*$|\Z)",
        source,
    )
    assert match is not None, f"missing [{name}]"
    return match.group(1)


def _directives(source: str, name: str) -> list[str]:
    return [
        line.strip()
        for line in _section(source, name).splitlines()
        if line.strip() and not line.lstrip().startswith(";")
    ]


def _assigned_nodes(tree: ast.AST) -> dict[str, ast.AST]:
    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            result[target.id] = node.value
    return result


def _string_values(node: ast.AST, assigned: dict[str, ast.AST]) -> set[str]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = set()
        for item in node.elts:
            values.update(_string_values(item, assigned))
        return values
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return _string_values(assigned[node.id], assigned)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return (
            _string_values(node.left, assigned)
            | _string_values(node.right, assigned)
        )
    raise AssertionError(f"unsupported excludes expression: {ast.dump(node)}")


def _analysis_excludes() -> list[set[str]]:
    tree = ast.parse(SPEC_PATH.read_text(encoding="utf-8"))
    assigned = _assigned_nodes(tree)
    result = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Analysis"
        ):
            continue
        keyword = next(
            (item for item in node.keywords if item.arg == "excludes"),
            None,
        )
        assert keyword is not None, "every Analysis must declare excludes"
        result.append(_string_values(keyword.value, assigned))
    return result


def _audit_dist() -> Path | None:
    raw = os.environ.get("BALACHKY_AUDIT_DIST")
    if not raw:
        return None
    path = Path(raw)
    assert path.is_dir(), f"BALACHKY_AUDIT_DIST is not a directory: {path}"
    return path


def _archived_modules(executable: Path) -> set[str]:
    pytest.importorskip("PyInstaller")
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(str(executable))
    pyz = archive.open_embedded_archive("PYZ.pyz")
    return set(pyz.toc)


def test_every_analysis_excludes_forbidden_product_modules():
    analyses = _analysis_excludes()
    assert len(analyses) == 3, "GUI, TTS worker, and protocol worker expected"
    for index, excludes in enumerate(analyses, start=1):
        assert FORBIDDEN_MODULES <= excludes, (
            f"Analysis #{index} misses "
            f"{sorted(FORBIDDEN_MODULES - excludes)}"
        )


def test_product_sources_do_not_import_test_frameworks():
    roots = [ROOT / "whisper_core", ROOT / "fronts"]
    sources = [path for root in roots for path in root.rglob("*.py")]
    sources.extend(ROOT.glob("run*.py"))
    forbidden = {"pytest", "_pytest", "unittest"}
    found = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {item.name.partition(".")[0] for item in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").partition(".")[0]}
            else:
                continue
            overlap = forbidden & names
            if overlap:
                found.append((path.relative_to(ROOT), node.lineno, overlap))
    assert found == []


def test_existing_dist_has_no_forbidden_files_or_archived_modules():
    dist = _audit_dist()
    if dist is None:
        pytest.skip("set BALACHKY_AUDIT_DIST to inspect an existing build")

    internal = dist / "_internal"
    assert internal.is_dir()
    forbidden_on_disk = []
    for path in internal.rglob("*"):
        lowered_parts = {
            part.lower()
            for part in path.relative_to(internal).parts
        }
        lowered_parts.add(path.name.lower().partition(".")[0])
        if lowered_parts & FORBIDDEN_MODULES:
            forbidden_on_disk.append(path.relative_to(dist))
    assert forbidden_on_disk == []

    archived = {}
    for executable in sorted(dist.glob("*.exe")):
        roots = {name.partition(".")[0] for name in _archived_modules(executable)}
        leaked = sorted(roots & FORBIDDEN_MODULES)
        if leaked:
            archived[executable.name] = leaked
    assert archived == {}


def test_espeak_is_voice_pack_data_not_an_application_binary():
    source = SHERPA_TTS_PATH.read_text(encoding="utf-8")
    assert source.count(
        'os.path.join(model_dir, files.get("data_dir", "espeak-ng-data"))'
    ) == 2
    assert not re.search(
        r"(?i)(subprocess|createprocess|popen).{0,120}"
        r"(?<![a-z])e-?speak(?:-ng)?(?![a-z])",
        source,
    )
    product_references = []
    for root in (ROOT / "whisper_core", ROOT / "fronts"):
        for path in root.rglob("*.py"):
            if ESPEAK_PATTERN.search(path.read_text(encoding="utf-8")):
                product_references.append(path)
    assert product_references == [SHERPA_TTS_PATH]

    dist = _audit_dist()
    if dist is not None:
        found = [
            path.relative_to(dist)
            for path in dist.rglob("*")
            if ESPEAK_PATTERN.search(path.name)
        ]
        assert found == []


def test_files_wildcard_installs_the_complete_onedir_tree():
    source = ISS_PATH.read_text(encoding="utf-8")
    assert _directives(source, "Files") == [
        'Source: "..\\dist\\Balachky\\*"; DestDir: "{app}"; '
        "Flags: ignoreversion recursesubdirs createallsubdirs"
    ]


def test_uninstall_delete_has_only_the_documented_extra_cleanup():
    source = ISS_PATH.read_text(encoding="utf-8")
    assert _directives(source, "UninstallDelete") == [
        'Type: filesandordirs; Name: "{localappdata}\\Balachky"; '
        "Check: RemoveUserDataChecked",
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-meeting-*"',
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-meeting-media-*"',
        'Type: filesandordirs; Name: "{%TEMP}\\balachky-tts-plain-*"',
    ]


def test_restart_manager_contract_is_explicit_and_mutex_claim_is_honest():
    source = ISS_PATH.read_text(encoding="utf-8")
    setup = _section(source, "Setup")
    assert re.search(r"(?im)^CloseApplications=yes\s*$", setup)
    assert re.search(r"(?im)^RestartApplications=yes\s*$", setup)
    assert not re.search(r"(?im)^AppMutex=", setup)

    app_source = APP_PATH.read_text(encoding="utf-8")
    # Продуктове ім'я каналу лишається літералом "balachky-single" (лише з
    # тестовим суфіксом BALACHKY_INSTANCE_SUFFIX offscreen) — випадкове
    # перейменування зламало б single-instance при оновленні поверх старої
    # версії.
    assert 'return "balachky-single" + os.environ.get("BALACHKY_INSTANCE_SUFFIX", "")' in app_source
    assert 'server.listen(channel)' in app_source
    assert "CreateMutex" not in app_source
