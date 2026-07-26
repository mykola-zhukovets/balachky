"""ЗГЕНЕРОВАНО balachky.spec під час збірки — НЕ редагувати вручну."""
COMMIT = "d990427"


def build_commit() -> str:
    return COMMIT


def build_version(version: str) -> str:
    return f"{version} ({COMMIT})"
