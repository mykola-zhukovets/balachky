import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "balachky-settings.json"
MANIFEST_KIND = "balachky-settings-export"
FORMAT_VERSION = 1

#: що з теки профілю ПЕРЕНОСИМО (приватну історію history.jsonl та бекапи *.bak
#: — НІ; це вимога «БЕЗ історії»). macros.toml — пер-профільні голосові макроси,
#: phrases.toml, self-learning.jsonl, terms.learned.toml, phrases.learned.toml.
_PROFILE_INCLUDE = (
    "terms.toml",
    "profile.json",
    "ignore.txt",
    "macros.toml",
    "phrases.toml",
    "self-learning.jsonl",
    "terms.learned.toml",
    "phrases.learned.toml",
)

#: цілі верхнього рівня, які приймаємо при імпорті (усе інше ігноруємо —
#: захист від zip-slip та сторонніх файлів у чужому архіві).
_TOP_LEVEL = ("config.toml", "snippets.toml", "context_profiles.toml")


class SettingsArchiveError(Exception):
    """Файл не є валідним експортом Балачок (чужий або пошкоджений zip)."""


def _add_file(zf, src: Path, arcname: str) -> None:
    if src.exists() and src.is_file():
        zf.write(src, arcname)


def export_settings(zip_path, *, config_path, snippets_path=None,
                    context_profiles_path=None, profiles_root=None,
                    version: str = "") -> Path:
    """Зібрати переносний .zip. profiles_root — тека, що МІСТИТЬ profiles/
    (як whisper_core.paths.profiles_root()). Повертає шлях створеного архіву."""
    zip_path = Path(zip_path)
    contents = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if Path(config_path).exists():
            contents.append("config.toml")
            _add_file(zf, Path(config_path), "config.toml")
        if snippets_path and Path(snippets_path).exists():
            contents.append("snippets.toml")
            _add_file(zf, Path(snippets_path), "snippets.toml")
        if context_profiles_path and Path(context_profiles_path).exists():
            contents.append("context_profiles.toml")
            _add_file(zf, Path(context_profiles_path), "context_profiles.toml")
        if profiles_root:
            proot = Path(profiles_root) / "profiles"
            if proot.is_dir():
                if (proot / "state.json").exists():
                    contents.append("profiles/state.json")
                    _add_file(zf, proot / "state.json", "profiles/state.json")
                for pdir in sorted(proot.iterdir()):
                    if not pdir.is_dir():
                        continue
                    for name in _PROFILE_INCLUDE:
                        fpath = pdir / name
                        if fpath.exists():
                            arcname = f"profiles/{pdir.name}/{name}"
                            contents.append(arcname)
                            _add_file(zf, fpath, arcname)

        manifest = {
            "kind": MANIFEST_KIND,
            "format": FORMAT_VERSION,
            "app_version": version,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "contents": contents,
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        zf.writestr(MANIFEST_NAME, manifest_bytes)
        zf.writestr("manifest.json", manifest_bytes)
    return zip_path


def read_manifest(zip_path) -> dict:
    """Прочитати маніфест з архіву (manifest.json або balachky-settings.json)."""
    with zipfile.ZipFile(zip_path) as zf:
        for mname in ("manifest.json", MANIFEST_NAME):
            if mname in zf.namelist():
                data = json.loads(zf.read(mname).decode("utf-8"))
                if isinstance(data, dict) and data.get("kind") == MANIFEST_KIND:
                    return data
    raise SettingsArchiveError("Manifest invalid or missing")


def is_valid_archive(zip_path) -> bool:
    """True — це наш експорт (цілий zip із валідним маніфестом)."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            if zf.testzip() is not None:
                return False
            read_manifest(zip_path)
            return True
    except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError,
            UnicodeError, SettingsArchiveError):
        return False


def inspect_archive(zip_path, current_version: str = "") -> dict:
    """Отримати метадані архіву: версія, список файлів, відсутні елементи."""
    if not is_valid_archive(zip_path):
        raise SettingsArchiveError("invalid archive")
    manifest = read_manifest(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()

    app_ver = manifest.get("app_version", "")
    created_at = manifest.get("created_at", "")

    is_newer = False
    if app_ver and current_version:
        try:
            def _vtuple(v):
                return tuple(int(x) for x in re.findall(r"\d+", str(v)))
            is_newer = _vtuple(app_ver) > _vtuple(current_version)
        except Exception:
            is_newer = False

    expected = ["config.toml", "context_profiles.toml", "profiles/state.json"]
    missing = [item for item in expected if item not in members]

    return {
        "app_version": app_ver,
        "created_at": created_at,
        "files": members,
        "missing_components": missing,
        "is_newer_version": is_newer,
    }


def _safe_members(zf) -> list:
    """Імена записів, які МОЖНА розпаковувати. Відкидаємо маніфест, абсолютні
    шляхи, вихід із теки (zip-slip) та все, що не є відомою ціллю."""
    out = []
    for name in zf.namelist():
        if name in (MANIFEST_NAME, "manifest.json") or name.endswith("/"):
            continue
        p = Path(name)
        if p.is_absolute() or ".." in p.parts:
            continue
        if name in _TOP_LEVEL or name.startswith("profiles/"):
            out.append(name)
    return out


def import_settings(zip_path, *, user_dir, profiles_root=None,
                    backup_path=None, version: str = "") -> "Path | None":
    """Розпакувати архів у теку користувача. ПЕРЕД перезаписом — бекап поточного
    стану (export) у backup_path. Повертає шлях бекапа (або None, якщо його не
    просили/не вдалось). Кидає SettingsArchiveError на невалідному архіві —
    поточний стан при цьому не чіпається."""
    if not is_valid_archive(zip_path):
        raise SettingsArchiveError("invalid archive")
    user_dir = Path(user_dir)
    proot = Path(profiles_root or user_dir)
    made_backup = None
    if backup_path is not None:
        try:
            export_settings(
                backup_path,
                config_path=user_dir / "config.toml",
                snippets_path=user_dir / "snippets.toml",
                context_profiles_path=user_dir / "context_profiles.toml",
                profiles_root=proot, version=version)
            made_backup = Path(backup_path)
        except OSError:
            made_backup = None
    with zipfile.ZipFile(zip_path) as zf:
        for name in _safe_members(zf):
            target = user_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())
    return made_backup


def import_settings_with_dir_backup(zip_path, *, user_dir, profiles_root=None, version: str = "") -> Path:
    """Імпорт з автоматичним створення теки backup-YYYY-MM-DD у теці даних користувача."""
    if not is_valid_archive(zip_path):
        raise SettingsArchiveError("invalid archive")

    user_dir = Path(user_dir)
    proot = Path(profiles_root or user_dir)

    today_str = datetime.now().strftime("%Y-%m-%d")
    backup_dir = user_dir / f"backup-{today_str}"
    if backup_dir.exists():
        idx = 1
        while (user_dir / f"backup-{today_str}_{idx}").exists():
            idx += 1
        backup_dir = user_dir / f"backup-{today_str}_{idx}"

    backup_dir.mkdir(parents=True, exist_ok=True)

    # Зберегти поточний стан у теку бекапу
    if (user_dir / "config.toml").exists():
        shutil.copy2(user_dir / "config.toml", backup_dir / "config.toml")
    if (user_dir / "snippets.toml").exists():
        shutil.copy2(user_dir / "snippets.toml", backup_dir / "snippets.toml")
    if (user_dir / "context_profiles.toml").exists():
        shutil.copy2(user_dir / "context_profiles.toml", backup_dir / "context_profiles.toml")

    src_proot = proot / "profiles"
    if src_proot.is_dir():
        dst_proot = backup_dir / "profiles"
        dst_proot.mkdir(parents=True, exist_ok=True)
        if (src_proot / "state.json").exists():
            shutil.copy2(src_proot / "state.json", dst_proot / "state.json")
        for pdir in src_proot.iterdir():
            if not pdir.is_dir():
                continue
            bak_pdir = dst_proot / pdir.name
            bak_pdir.mkdir(parents=True, exist_ok=True)
            for name in _PROFILE_INCLUDE:
                fpath = pdir / name
                if fpath.exists():
                    shutil.copy2(fpath, bak_pdir / name)

    # Розпакувати новий стан з архіву
    with zipfile.ZipFile(zip_path) as zf:
        for name in _safe_members(zf):
            target = user_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(target, "wb") as dst:
                dst.write(src.read())

    return backup_dir

