"""ЗГЕНЕРОВАНО balachky.spec під час збірки — НЕ редагувати вручну."""
COMMIT = "b4b9b2b"


def build_commit() -> str:
    return COMMIT


def build_version(version: str) -> str:
    return f"{version} ({COMMIT})"
