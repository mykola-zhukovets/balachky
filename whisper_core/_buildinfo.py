"""Відомості про коміт для dev-режиму й frozen-збірки."""
import subprocess


COMMIT = None


def build_commit() -> str:
    if COMMIT is not None:
        return COMMIT
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return "dev"
    commit = result.stdout.strip()
    if not commit:
        raise RuntimeError("git rev-parse returned an empty commit")
    return commit


def build_version(version: str) -> str:
    return f"{version} ({build_commit()})"
