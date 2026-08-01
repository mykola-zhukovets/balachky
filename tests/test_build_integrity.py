"""Контракт походження й незмінності вхідних даних релізної збірки."""
import importlib.util
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDINFO = ROOT / "whisper_core" / "_buildinfo.py"
SPEC = ROOT / "balachky.spec"
BUILD_SCRIPT = ROOT / "installer" / "build.ps1"
HELPER = ROOT / "scripts" / "build_integrity.py"


def _load_buildinfo():
    spec = importlib.util.spec_from_file_location("buildinfo_under_test", BUILDINFO)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_helper():
    spec = importlib.util.spec_from_file_location("build_integrity_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _git_repo(tmp_path):
    tmp_path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Build Integrity Test"], cwd=tmp_path, check=True)
    buildinfo = tmp_path / "whisper_core" / "_buildinfo.py"
    buildinfo.parent.mkdir()
    buildinfo.write_text("COMMIT = None\n", encoding="utf-8")
    (tmp_path / "payload.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "test fixture"], cwd=tmp_path, check=True)
    return tmp_path


def test_source_buildinfo_uses_runtime_git_fallback_not_baked_commit():
    source = BUILDINFO.read_text(encoding="utf-8")

    assert "COMMIT = None" in source
    assert "git" in source


def test_dev_buildinfo_reads_short_git_head(monkeypatch):
    module = _load_buildinfo()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "abc1234\n", ""),
    )

    assert module.build_commit() == "abc1234"


def test_dev_buildinfo_uses_dev_only_when_git_executable_is_missing(monkeypatch):
    module = _load_buildinfo()

    def no_git(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(module.subprocess, "run", no_git)

    assert module.build_commit() == "dev"


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.CalledProcessError(1, ["git", "rev-parse"]),
        subprocess.TimeoutExpired(["git", "rev-parse"], 5),
    ],
)
def test_dev_buildinfo_propagates_git_command_failures(monkeypatch, failure):
    module = _load_buildinfo()

    def failed_git(*args, **kwargs):
        raise failure

    monkeypatch.setattr(module.subprocess, "run", failed_git)

    with pytest.raises(type(failure)):
        module.build_commit()


def test_dev_buildinfo_rejects_empty_git_output(monkeypatch):
    module = _load_buildinfo()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "\n", ""),
    )

    with pytest.raises(RuntimeError, match="empty commit"):
        module.build_commit()


def test_spec_never_writes_tracked_buildinfo():
    assert '_buildinfo.py").write_text' not in SPEC.read_text(encoding="utf-8")


def test_build_script_never_checks_out_tracked_buildinfo():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "git checkout" not in source
    assert "build_integrity.py" in source


def test_build_script_packages_only_an_isolated_staged_head():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "stage --root $Root --commit $commit --output $stageRoot" in source
    assert "--distpath $stageDist" in source
    assert "$stageSpecPath" in source
    assert source.count("verify --root $Root --snapshot $inputSnapshot") >= 3
    assert "$env:TEMP" in source


def test_build_script_runs_pyinstaller_from_staged_source():
    source = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "Push-Location $stageRoot" in source
    assert "Pop-Location" in source


def test_build_integrity_helper_exists_for_staging_and_snapshots():
    assert HELPER.is_file()


def test_staging_exact_head_bakes_literal_commit_without_touching_source(tmp_path):
    helper = _load_helper()
    repo = _git_repo(tmp_path / "repo")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    source_before = (repo / "whisper_core" / "_buildinfo.py").read_bytes()
    stage = tmp_path / "stage"

    helper.stage_exact_head(repo, commit, stage)

    staged_buildinfo = (stage / "whisper_core" / "_buildinfo.py").read_text(encoding="utf-8")
    assert f'COMMIT = "{commit}"' in staged_buildinfo
    staged_spec = importlib.util.spec_from_file_location(
        "frozen_buildinfo_under_test", stage / "whisper_core" / "_buildinfo.py",
    )
    staged_module = importlib.util.module_from_spec(staged_spec)
    assert staged_spec.loader is not None
    staged_spec.loader.exec_module(staged_module)
    assert staged_module.build_commit() == commit
    assert (repo / "whisper_core" / "_buildinfo.py").read_bytes() == source_before


def test_snapshot_rejects_tracked_byte_change_before_finalization(tmp_path):
    helper = _load_helper()
    repo = _git_repo(tmp_path / "repo")
    snapshot = helper.snapshot_tracked_inputs(repo)
    (repo / "payload.txt").write_text("mutated\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="tracked input changed"):
        helper.assert_tracked_inputs_unchanged(repo, snapshot)


def test_snapshot_rejects_head_change_before_finalization(tmp_path):
    helper = _load_helper()
    repo = _git_repo(tmp_path / "repo")
    snapshot = helper.snapshot_tracked_inputs(repo)
    (repo / "payload.txt").write_text("next head\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "next head", "-q"], cwd=repo, check=True)

    with pytest.raises(RuntimeError, match="HEAD changed"):
        helper.assert_tracked_inputs_unchanged(repo, snapshot)
