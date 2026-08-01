"""Підготовка незмінних вхідних даних для релізної збірки."""
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class InputSnapshot:
    head: str
    hashes: dict[str, str]


def _git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True, text=text,
    )


def _tracked_hashes(root: Path) -> dict[str, str]:
    result = _git(root, "ls-files", "-z", text=False)
    hashes = {}
    for name in result.stdout.split(b"\0"):
        if not name:
            continue
        relative = name.decode("utf-8")
        hashes[relative] = hashlib.sha256((root / relative).read_bytes()).hexdigest()
    return hashes


def snapshot_tracked_inputs(root: Path) -> InputSnapshot:
    root = Path(root).resolve()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    return InputSnapshot(head=head, hashes=_tracked_hashes(root))


def assert_tracked_inputs_unchanged(root: Path, snapshot: InputSnapshot) -> None:
    root = Path(root).resolve()
    head = _git(root, "rev-parse", "HEAD").stdout.strip()
    if head != snapshot.head:
        raise RuntimeError("HEAD changed after build inputs were snapshotted")
    if _tracked_hashes(root) != snapshot.hashes:
        raise RuntimeError("tracked input changed after build inputs were snapshotted")


def _frozen_buildinfo(commit: str) -> str:
    return (
        '"""Build metadata generated in an isolated staging tree."""\n'
        f'COMMIT = "{commit}"\n\n\n'
        "def build_commit() -> str:\n"
        "    return COMMIT\n\n\n"
        "def build_version(version: str) -> str:\n"
        "    return f\"{version} ({COMMIT})\"\n"
    )


def stage_exact_head(root: Path, commit: str, output: Path) -> None:
    root = Path(root).resolve()
    output = Path(output).resolve()
    if output.exists():
        raise RuntimeError(f"staging directory already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as archive:
        archive_path = Path(archive.name)
    try:
        _git(root, "archive", "--format=zip", "--output", str(archive_path), commit)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(output)
    finally:
        archive_path.unlink(missing_ok=True)
    buildinfo = output / "whisper_core" / "_buildinfo.py"
    if not buildinfo.is_file():
        shutil.rmtree(output, ignore_errors=True)
        raise RuntimeError("archived HEAD has no whisper_core/_buildinfo.py")
    buildinfo.write_text(_frozen_buildinfo(commit), encoding="utf-8", newline="\n")


def _snapshot_json(snapshot: InputSnapshot) -> dict:
    return {"head": snapshot.head, "hashes": snapshot.hashes}


def _read_snapshot(path: Path) -> InputSnapshot:
    data = json.loads(path.read_text(encoding="utf-8"))
    return InputSnapshot(head=data["head"], hashes=data["hashes"])


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
        command.add_argument("--snapshot", required=True, type=Path)
    stage = commands.add_parser("stage")
    stage.add_argument("--root", required=True, type=Path)
    stage.add_argument("--commit", required=True)
    stage.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        args.snapshot.write_text(
            json.dumps(_snapshot_json(snapshot_tracked_inputs(args.root)), sort_keys=True),
            encoding="utf-8",
        )
    elif args.command == "verify":
        assert_tracked_inputs_unchanged(args.root, _read_snapshot(args.snapshot))
    else:
        stage_exact_head(args.root, args.commit, args.output)


if __name__ == "__main__":
    main()
