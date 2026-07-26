"""Профілі пам'яті: у кожного користувача/контексту — свій словник та історія.

    profiles/
      state.json            ← активний профіль (керує програма)
      default/
        terms.toml          ← словник термінів профілю (редагує людина)
        history.jsonl       ← пам'ять (журнал транскрипцій)
        ignore.txt          ← слова, які learn.py більше не пропонує
        profile.json        ← прапорець пам'яті (керує програма)

Принцип: людське — TOML, машинне — JSON (tomllib не вміє писати).
Скидання пам'яті — БЕЗ втрати даних: history перейменовується у бекап.

CLI:  python -m whisper_core.profiles list | new <назва> | use <назва>
"""
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from . import processing
from .history import history_lock

# База профілів: dev — корінь репо, frozen — %LOCALAPPDATA%\Balachky (writable)
_ROOT = paths.profiles_root()

#: захардкоджений дефолтний профіль — його не можна ні видалити, ні перейменувати
#: (get_active падає саме на нього, ensure_migrated завжди його відтворює)
DEFAULT_PROFILE = "default"


class ProfileValidationError(ValueError):
    """Стабільна машинна помилка назви профілю для локалізації у фронтенді."""

    def __init__(self, code: str, name: str = ""):
        self.code = code
        self.name = name
        super().__init__(code)


@dataclass
class Profile:
    name: str
    dir: Path
    _meta_corrupt: bool = field(default=False, init=False, repr=False)

    @property
    def terms_path(self) -> Path:
        return self.dir / "terms.toml"

    @property
    def macros_path(self) -> Path:
        # feature/voice-macros: голосові макроси — пер-профільні, поруч зі словником
        return self.dir / "macros.toml"

    @property
    def phrases_path(self) -> Path:
        # feature/bilingual-memory: білінгвальна пам'ять фраз — пер-профільна,
        # поруч зі словником термінів (окреме сховище, свій тумблер)
        return self.dir / "phrases.toml"

    @property
    def learning_journal_path(self) -> Path:
        # feature/selflearn-dict: append-only журнал самонавчання словника —
        # пер-профільний, поруч зі словником (ізоляція = межа каталогу профілю)
        return self.dir / "self-learning.jsonl"

    @property
    def learned_terms_path(self) -> Path:
        # згенерована проєкція вивчених термінів (терміни-заміни + bias)
        return self.dir / "terms.learned.toml"

    @property
    def learned_phrases_path(self) -> Path:
        # згенерована проєкція вивчених пар-фраз
        return self.dir / "phrases.learned.toml"

    @property
    def history_path(self) -> Path:
        return self.dir / "history.jsonl"

    @property
    def ignore_path(self) -> Path:
        return self.dir / "ignore.txt"

    @property
    def voice_memory_path(self) -> Path:
        # feature/voice-memory (Т41): персистентне сховище центроїдів голосів
        return self.dir / "voices.json"

    @property
    def _meta_path(self) -> Path:
        return self.dir / "profile.json"

    def _meta(self) -> dict:
        if not self._meta_path.exists():
            self._meta_corrupt = False
            return {}
        try:
            meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise TypeError("profile metadata must be an object")
            self._meta_corrupt = False
            return meta
        except (json.JSONDecodeError, UnicodeError, OSError, TypeError):
            self._meta_corrupt = True
            return {}

    @property
    def meta_corrupt(self) -> bool:
        self._meta()
        return self._meta_corrupt

    @property
    def memory_enabled(self) -> bool:
        meta = self._meta()
        return False if self._meta_corrupt else bool(meta.get("memory", True))

    def set_memory(self, on: bool) -> None:
        meta = self._meta()
        meta["memory"] = bool(on)
        self._write_meta(meta)
        self._meta_corrupt = False

    def _write_meta(self, meta: dict) -> None:
        """Атомарно перезаписати profile.json (temp + os.replace), щоб крах між
        записами не лишив напівписаного JSON (спека §5: atomic profile-meta write)."""
        tmp = self._meta_path.with_name(self._meta_path.name + ".tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(meta, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self._meta_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # feature/processing-slider: рівень обробки тексту — пер-профільний, окремо для
    # кожної поверхні (диктування / нарада), щоб протокол наради не змінював вставку.
    def has_processing(self) -> bool:
        """Чи вже записаний блок processing (для одноразової міграції зі старих
        глобальних прапорців — щоб не перевиводити його щоразу)."""
        meta = self._meta()
        return self._meta_corrupt or isinstance(meta.get("processing"), dict)

    def processing_mode(self, surface: str) -> str:
        """Режим обробки поверхні (dictation|meeting). Немає запису → DEFAULT_MODE."""
        data = self._meta().get("processing")
        if isinstance(data, dict) and isinstance(data.get(surface), str):
            return processing.normalize_mode(data[surface]).value
        return processing.DEFAULT_MODE.value

    def set_processing_mode(self, surface: str, mode) -> None:
        """Зберегти режим поверхні, зберігши memory та невідомі ключі (merge)."""
        if surface not in processing.SURFACES:
            return
        meta = self._meta()
        if self._meta_corrupt:
            return
        proc = meta.get("processing")
        if not isinstance(proc, dict):
            proc = {}
        proc[surface] = processing.normalize_mode(mode).value
        meta["processing"] = proc
        self._write_meta(meta)

    def reset_memory(self):
        """Перезапис пам'яті: history → history.<ts>.bak.jsonl. → шлях бекапа або None."""
        # Той самий міжпроцесний lock, що й log_history/delete_line: append не
        # може писати у вже перейменований backup, а Windows не бачить open file.
        with history_lock(self.history_path):
            if not self.history_path.exists():
                return None
            stamp = time.strftime("%Y%m%d-%H%M%S")
            bak = self.dir / f"history.{stamp}.bak.jsonl"
            n = 1
            while bak.exists():  # Windows rename не перезаписує: два кліки за секунду
                bak = self.dir / f"history.{stamp}-{n}.bak.jsonl"
                n += 1
            self.history_path.rename(bak)
            return bak

    def ignored_words(self) -> set:
        if not self.ignore_path.exists():
            return set()
        return {w.strip().lower() for w in
                self.ignore_path.read_text(encoding="utf-8").splitlines() if w.strip()}

    def add_ignored(self, words) -> None:
        have = self.ignored_words()
        new = [w.strip().lower() for w in words if w.strip() and w.strip().lower() not in have]
        if new:
            with self.ignore_path.open("a", encoding="utf-8") as f:
                f.write("\n".join(new) + "\n")


def _proot(root) -> Path:
    return Path(root) / "profiles"


def _state_path(root) -> Path:
    return _proot(root) / "state.json"


def ensure_migrated(root=_ROOT) -> None:
    """Міграція кореневих terms.toml/history.jsonl → profiles/default/.
    Ідемпотентна й покрокова: безпечна при одночасному старті кількох процесів
    і долікує частково виконану міграцію. terms копіюється (корінний лишається
    прикладом), history — переїжджає (приватне)."""
    root = Path(root)
    default = _proot(root) / "default"
    default.mkdir(parents=True, exist_ok=True)
    src_terms, dst_terms = root / "terms.toml", default / "terms.toml"
    if src_terms.exists() and not dst_terms.exists():
        shutil.copy2(src_terms, dst_terms)
    src_hist, dst_hist = root / "history.jsonl", default / "history.jsonl"
    if src_hist.exists() and not dst_hist.exists():
        try:
            src_hist.rename(dst_hist)
        except OSError:
            pass  # паралельний процес уже переніс
    # frozen, перший запуск: словника ще нема — сідимо прикладом зі збірки
    # (dev сюди не потрапляє: там сід — кореневий terms.toml, блок вище)
    if not dst_terms.exists():
        example = paths.bundled_terms_example()
        if example is not None:
            shutil.copy2(example, dst_terms)


def list_profiles(root=_ROOT) -> list:
    ensure_migrated(root)
    return [Profile(d.name, d) for d in sorted(_proot(root).iterdir())
            if d.is_dir()]


def get(root=_ROOT, name: str = "") -> "Profile | None":
    ensure_migrated(root)
    proot = _proot(root)
    d = proot / name
    # Захист від traversal: назва-параметр приходить і від MCP-агента під
    # недовіреним контекстом. "../evil" чи абсолютний шлях вислизнув би за
    # межі profiles/ і читав/писав чужий terms.toml — резолв має лишитись
    # РІВНО одним компонентом усередині profiles/ (не сам корінь).
    if not name or d.resolve() == proot.resolve() or not paths.safe_under(proot, d):
        return None
    return Profile(name, d) if d.is_dir() else None


def get_active(root=_ROOT) -> Profile:
    ensure_migrated(root)
    try:
        name = json.loads(_state_path(root).read_text(encoding="utf-8"))["active"]
    except Exception:
        name = "default"
    return get(root, name) or get(root, "default")


def set_active(root=_ROOT, name: str = "default") -> None:
    if get(root, name) is None:
        raise ValueError(f"Профілю «{name}» не існує")
    _state_path(root).write_text(json.dumps({"active": name}, ensure_ascii=False),
                                 encoding="utf-8")


def _validate_name(name: str) -> None:
    """Назва профілю: літери (в т.ч. кирилиця — \\w Unicode), цифри, дефіс,
    підкреслення; без пробілів. Спільна для create/rename, щоб правила збігались."""
    if not re.fullmatch(r"[\w\-]+", name):
        raise ProfileValidationError("invalid_name", name)


def create_profile(root=_ROOT, name: str = "") -> Profile:
    """Новий профіль; словник сідиться з default (спільна база, далі розходяться)."""
    _validate_name(name)
    ensure_migrated(root)
    d = _proot(root) / name
    if d.exists():
        raise ProfileValidationError("already_exists", name)
    d.mkdir()
    seed = _proot(root) / "default" / "terms.toml"
    if seed.exists():
        shutil.copy2(seed, d / "terms.toml")
    return Profile(name, d)


def delete_profile(root=_ROOT, name: str = "") -> None:
    """Видалити профіль з диска разом із його словником та історією.
    Дефолтний профіль недоторканний. Якщо видаляємо активний — активним
    стає дефолтний (state.json перемикаємо ДО rmtree, щоб не лишити
    «висячого» активного)."""
    if name == DEFAULT_PROFILE:
        raise ProfileValidationError("is_default", name)
    p = get(root, name)
    if p is None:
        raise ProfileValidationError("not_found", name)
    if get_active(root).name == name:
        set_active(root, DEFAULT_PROFILE)
    shutil.rmtree(p.dir)


def rename_profile(root=_ROOT, old: str = "", new: str = "") -> Profile:
    """Перейменувати профіль (тека на диску). Дефолтний недоторканний, нове
    ім'я валідується як при створенні. Якщо перейменовуємо активний — активним
    лишається він (під новим ім'ям)."""
    if old == DEFAULT_PROFILE:
        raise ProfileValidationError("is_default", old)
    _validate_name(new)
    ensure_migrated(root)
    src = get(root, old)
    if src is None:
        raise ProfileValidationError("not_found", old)
    if new == old:
        return src
    dst = _proot(root) / new
    # .samefile: на регістронезалежній ФС (Windows) «Foo»→«foo» — це та сама тека,
    # а не колізія; лишаємо такий перейменунок дозволеним
    if dst.exists() and not dst.samefile(src.dir):
        raise ProfileValidationError("already_exists", new)
    was_active = get_active(root).name == old
    src.dir.rename(dst)
    if was_active:
        set_active(root, new)
    return Profile(new, dst)


def _main(argv) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        active = get_active().name
        for p in list_profiles():
            mark = "→" if p.name == active else " "
            mem = "пам'ять увімкнена" if p.memory_enabled else "пам'ять ВИМКНЕНА"
            n = 0
            if p.history_path.exists():
                n = sum(1 for line in
                        p.history_path.read_text(encoding="utf-8").splitlines() if line.strip())
            print(f" {mark} {p.name:<12} {mem}, записів: {n}")
    elif cmd == "new" and len(argv) > 1:
        p = create_profile(name=argv[1])
        print(f"Створено профіль «{p.name}» ({p.dir}). Активувати: use {p.name}")
    elif cmd == "use" and len(argv) > 1:
        set_active(name=argv[1])
        print(f"Активний профіль: {argv[1]}")
    else:
        print("Вжиток: python -m whisper_core.profiles list | new <назва> | use <назва>")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
