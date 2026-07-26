"""Контекстні профілі за активним застосунком (feature/context-profiles).

Самодостатній модуль: детект активного вікна (без хуків і фонових потоків),
матчинг профілю за іменем exe, гейт безпеки (блок-лист менеджерів паролів) і
модель профілю з TOML-сховищем. Паралельна гілка feature/cascade-paste теж
додає блок-лист у fronts/desktop/wininput.py — тут СВІЙ, самодостатній; зведе
інтегратор.

Детект — два синхронні виклики за сесію диктування (на старті запису й перед
вставкою). Жодних хуків, таймерів чи фонових потоків: лише GetForegroundWindow →
GetWindowThreadProcessId → ім'я exe, з UWP-фіксом для ApplicationFrameHost.

Qt тут НЕ імпортується: модель/матчер/гейт/сховище тестуються без вікна.
Win32 (ctypes.WinDLL) чіпаємо лише в рантаймі всередині функцій, тож імпорт
модуля не залежить від платформи.
"""
import ctypes
import logging
import os
import re
from ctypes import wintypes
from dataclasses import dataclass, field
from fnmatch import fnmatch

log = logging.getLogger(__name__)

#: exe-хост, під яким Windows показує UWP/Store-застосунки (Калькулятор,
#: Пошта тощо). Справжній процес — у дочірньому вікні CoreWindow.
APPLICATION_FRAME_HOST = "applicationframehost.exe"
#: клас реального вікна UWP-застосунку всередині ApplicationFrameHost
CORE_WINDOW_CLASS = "Windows.UI.Core.CoreWindow"

#: Менеджери паролів: вставку диктування сюди НІКОЛИ не пускаємо. Профілі цей
#: список не редагують (безпека понад налаштування). Порівняння — lower-case
#: точний збіг імені exe. ЄДИНЕ джерело переліку — wininput.PASSWORD_MANAGERS
#: (той самий блок-лист, що й для набору тексту в paste.py); зведено інтегратором
#: wave-2, щоб два переліки не розходились.
from .wininput import PASSWORD_MANAGERS as SECURITY_BLOCKLIST


# ─────────────────────────── модель ───────────────────────────
@dataclass
class WindowContext:
    """Знімок активного вікна на момент виклику."""
    exe: str = ""      # ім'я процесу (basename, напр. "chrome.exe")
    title: str = ""    # заголовок вікна
    hwnd: int = 0      # HWND (0 — не визначено)


@dataclass
class Behavior:
    """Що робити з диктуванням у вікні цього профілю."""
    auto_enter: bool = False           # натиснути Enter після вдалої вставки
    dictionary: "str | None" = None    # назва профілю-словника або None (активний)
    enabled: bool = True               # False → не вставляти, лише картка+нота
    # feature/output-formats: детермінований профіль форматування виводу
    # ("plain"/"markdown"/"code"/"letter"). Невідоме значення трактуємо як plain.
    formatting: str = "plain"


@dataclass
class ContextProfile:
    name: str
    apps: list = field(default_factory=list)  # імена exe (як введено користувачем)
    title_regex: "str | None" = None          # опційне доповнення до збігу за exe
    behavior: Behavior = field(default_factory=Behavior)

    def apps_lower(self) -> set:
        return {a.strip().lower() for a in self.apps if a and a.strip()}


# ─────────────────────────── детект вікна ───────────────────────────
class ContextResolver:
    """Синхронний резолвер активного вікна. Кожен низькорівневий Win32-крок —
    окремий метод, щоб тести мокали їх без реального Win32. Будь-який збій дає
    порожній WindowContext: детект НІКОЛИ не має ламати диктування."""

    def get_window_context(self) -> WindowContext:
        try:
            hwnd = self._foreground_hwnd()
            if not hwnd:
                return WindowContext()
            pid = self._pid_for_hwnd(hwnd)
            exe = self._exe_for_pid(pid) if pid else ""
            # UWP-фікс: реальний процес — у дочірньому CoreWindow
            if exe.lower() == APPLICATION_FRAME_HOST:
                real_pid = self._uwp_real_pid(hwnd)
                if real_pid and real_pid != pid:
                    exe = self._exe_for_pid(real_pid) or exe
            title = self._title_for_hwnd(hwnd)
            return WindowContext(exe=exe, title=title, hwnd=hwnd)
        except Exception:
            log.debug("Не вдалося визначити активне вікно", exc_info=True)
            return WindowContext()

    # --- низькорівневі Win32-кроки (мокаються в тестах) ---
    def _foreground_hwnd(self) -> int:
        return int(ctypes.WinDLL("user32", use_last_error=True).GetForegroundWindow())

    def _pid_for_hwnd(self, hwnd) -> int:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)

    def _exe_for_pid(self, pid) -> str:
        """Ім'я exe через QueryFullProcessImageNameW (ctypes; без залежності від
        psutil — його немає в requirements.txt)."""
        if not pid:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            ok = kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            return os.path.basename(buf.value) if ok else ""
        finally:
            kernel32.CloseHandle(handle)

    def _title_for_hwnd(self, hwnd) -> str:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
        return buf.value

    def _uwp_real_pid(self, hwnd) -> int:
        """Пройтись дочірніми вікнами ApplicationFrameHost і знайти PID вікна
        класу Windows.UI.Core.CoreWindow — це справжній UWP-процес."""
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found = {"pid": 0}

        def _cb(child, _lparam):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child, cls, 256)
            if cls.value == CORE_WINDOW_CLASS:
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
                found["pid"] = int(pid.value)
                return False   # знайшли — зупинити перебір
            return True

        user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(_cb), 0)
        return found["pid"]


# ─────────────────────────── матчинг ───────────────────────────
class ProfileMatcher:
    """Первинний ключ — ім'я exe (lower-case, точний збіг); title_regex —
    опційне доповнення. Порядок профілів = пріоритет: перший збіг виграє.
    Без збігу — окремий дефолт-профіль."""

    def __init__(self, profiles, default: ContextProfile):
        self.profiles = list(profiles)
        self.default = default

    def match(self, ctx: WindowContext) -> ContextProfile:
        exe = (ctx.exe or "").strip().lower()
        if exe:
            for p in self.profiles:
                if exe not in p.apps_lower():
                    continue
                if p.title_regex:
                    try:
                        if not re.search(p.title_regex, ctx.title or ""):
                            continue          # заголовок не збігся — далі за списком
                    except re.error:
                        pass                  # битий regex → матч лише за exe
                return p
        return self.default


# ────────── авто-вибір профілю за вікном (feature/auto-profile) ──────────
@dataclass
class AutoProfileRule:
    """Правило «активне вікно → профіль».

    ``process`` і ``title`` — wildcard-патерни (fnmatch, регістронезалежні):
    ``WINWORD.EXE``, ``chrome.exe``, ``*.exe``. Порожній ``title`` — будь-який
    заголовок; голий фрагмент (без ``*``/``?``) трактуємо як підрядок ``*frag*``,
    тож ``Gmail`` збігається з ``… — Gmail — Chrome``. Правило без жодного
    патерну не матчить нічого (щоб не хапати всі вікна). ``profile`` — назва
    профілю-словника (whisper_core.profiles)."""
    process: str = ""
    title: str = ""
    profile: str = ""

    def matches(self, ctx: "WindowContext") -> bool:
        proc = (self.process or "").strip().lower()
        ttl = (self.title or "").strip().lower()
        if not proc and not ttl:
            return False
        if proc and not fnmatch((ctx.exe or "").lower(), proc):
            return False
        if ttl:
            pat = ttl if ("*" in ttl or "?" in ttl) else f"*{ttl}*"
            if not fnmatch((ctx.title or "").lower(), pat):
                return False
        return True

    def specificity(self) -> int:
        """Точність правила для пріоритету: є фрагмент заголовка (+2) точніше за
        лише-процес; точний процес без wildcard (+1) точніше за маску."""
        score = 0
        if (self.title or "").strip():
            score += 2
        proc = (self.process or "").strip()
        if proc and "*" not in proc and "?" not in proc:
            score += 1
        return score


class AutoProfileMatcher:
    """Вибір профілю-словника за активним вікном. Серед усіх правил, що збіглися,
    виграє найточніше; за рівної точності — перше у списку (порядок = пріоритет).
    Без збігу — None. ``enabled=False`` → matcher мовчить (None), тобто диктування
    йде як завжди."""

    def __init__(self, rules, enabled: bool = True):
        self.rules = [r for r in rules if r.profile and r.profile.strip()]
        self.enabled = bool(enabled)

    def match(self, ctx: "WindowContext"):
        if not self.enabled:
            return None
        best = None
        best_score = -1
        for r in self.rules:
            if r.matches(ctx) and r.specificity() > best_score:
                best, best_score = r, r.specificity()
        return best.profile if best is not None else None


# ─────────────────────────── гейт безпеки ───────────────────────────
class SecurityGate:
    """Блок-лист менеджерів паролів. Незалежний від профілів (їх редагування
    його НЕ чіпає). extra — додаткові imena exe (для тестів/розширення)."""

    def __init__(self, extra=None):
        self.blocked = set(SECURITY_BLOCKLIST)
        if extra:
            self.blocked |= {e.strip().lower() for e in extra if e and e.strip()}

    def is_blocked(self, exe: str) -> bool:
        return bool(exe) and exe.strip().lower() in self.blocked


# ─────────────────────────── auto-enter ───────────────────────────
ULONG_PTR = wintypes.WPARAM          # покажчико-розмірний (32/64-біт)


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    # union мусить бути повного розміру (найбільший член — MOUSEINPUT), інакше
    # SendInput відкине структуру як невалідну за cbSize.
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def press_enter() -> bool:
    """Натиснути Enter через SendInput (VK_RETURN down+up). Тихо повертає False
    при будь-якому збої — auto_enter не має валити конвеєр."""
    VK_RETURN = 0x0D
    KEYEVENTF_KEYUP = 0x0002
    INPUT_KEYBOARD = 1
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)

        def _mk(flags):
            inp = _INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = _KEYBDINPUT(wVk=VK_RETURN, wScan=0, dwFlags=flags,
                                   time=0, dwExtraInfo=0)
            return inp

        arr = (_INPUT * 2)(_mk(0), _mk(KEYEVENTF_KEYUP))
        sent = user32.SendInput(2, ctypes.byref(arr), ctypes.sizeof(_INPUT))
        return sent == 2
    except Exception:
        log.debug("SendInput Enter не вдався", exc_info=True)
        return False


# ─────────────────────────── сховище (TOML) ───────────────────────────
# Окремий файл context_profiles.toml поруч із config.toml: серіалізатор
# config.py скалярний (bool/str/число) і не пише масиви таблиць [[profile]];
# тримати профілі там означало б переписати його. Окремий TOML-файл лишається
# редагованим руками (принцип «людське — TOML» з whisper_core.profiles) і
# природно лягає на кнопку «Відкрити файл профілів».
import tomllib


#: допустимі значення форматування (дублює whisper_core.textformat.MODES, але
#: context.py навмисно не тягне whisper_core — тримаємо межу «фронт без ядра»).
_FORMATTING_MODES = ("plain", "markdown", "code", "letter")


def _norm_formatting(val) -> str:
    v = str(val or "plain").strip().lower()
    return v if v in _FORMATTING_MODES else "plain"


def _behavior_from(d: dict) -> Behavior:
    dic = d.get("dictionary")
    if isinstance(dic, str) and not dic.strip():
        dic = None                         # порожній рядок → активний словник
    return Behavior(
        auto_enter=bool(d.get("auto_enter", False)),
        dictionary=dic,
        enabled=bool(d.get("enabled", True)),
        formatting=_norm_formatting(d.get("formatting")),
    )


def _default_profile(name: str = "default") -> ContextProfile:
    return ContextProfile(name=name, apps=[], title_regex=None,
                          behavior=Behavior())


def load_profiles(path):
    """Прочитати context_profiles.toml → (список ContextProfile, дефолт).
    Порядок [[profile]] у файлі = пріоритет. Нема файлу / битий TOML → порожній
    список і дефолт зі стандартною поведінкою (диктування працює як завжди)."""
    try:
        raw = tomllib.loads(_read_text(path))
    except FileNotFoundError:
        # Відсутній файл — очікувано (перший запуск): профілі опційні. Не шумимо
        # WARNING на кожному старті; диктування працює на дефолтах.
        log.debug("Файл профілів %s відсутній — беру дефолти", path)
        return [], _default_profile()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as e:
        log.warning("Не вдалося прочитати профілі %s: %s — беру дефолти", path, e)
        return [], _default_profile()

    default = _default_profile()
    if isinstance(raw.get("default"), dict):
        default.behavior = _behavior_from(raw["default"])

    profiles = []
    for entry in raw.get("profile", []):
        if not isinstance(entry, dict):
            continue
        apps = entry.get("apps", [])
        if isinstance(apps, str):
            apps = [apps]
        title_regex = entry.get("title_regex") or None
        profiles.append(ContextProfile(
            name=str(entry.get("name", "")),
            apps=[str(a) for a in apps],
            title_regex=title_regex,
            behavior=_behavior_from(entry),
        ))
    return profiles, default


def _read_text(path) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _toml_str(val: str) -> str:
    """Екранувати рядок для нашого простого TOML (як у config.py)."""
    return '"' + str(val).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_list(vals) -> str:
    return "[" + ", ".join(_toml_str(v) for v in vals) + "]"


def load_auto_rules(path):
    """Прочитати ``[auto]`` + ``[[auto_rule]]`` з context_profiles.toml.

    → ``(list[AutoProfileRule], enabled)``, де ``enabled`` — ``bool`` або ``None``
    (прапорець не заданий: UI/контролер трактують як «увімкнено, лише якщо правила
    є»). Нема файлу / битий TOML → ``([], None)`` (диктування без змін)."""
    try:
        raw = tomllib.loads(_read_text(path))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return [], None
    rules = []
    for entry in raw.get("auto_rule", []):
        if not isinstance(entry, dict):
            continue
        rules.append(AutoProfileRule(
            process=str(entry.get("process", "") or ""),
            title=str(entry.get("title", "") or ""),
            profile=str(entry.get("profile", "") or ""),
        ))
    enabled = None
    auto = raw.get("auto")
    if isinstance(auto, dict) and "enabled" in auto:
        enabled = bool(auto["enabled"])
    return rules, enabled


def save_profiles(path, profiles, default: "ContextProfile | None" = None,
                  auto_rules=None, auto_enabled=None) -> None:
    """Записати профілі у TOML. Порядок списку зберігається (= пріоритет).
    Пишемо власним серіалізатором (як config.py.save) — залежність від
    tomli-w не потрібна.

    feature/auto-profile: правила «вікно → профіль» живуть у ТОМУ Ж файлі. Якщо
    викликач не передав ``auto_rules`` — наявні правила зчитуються й переписуються
    без змін (правка профілів не стирає правила, і навпаки)."""
    default = default or _default_profile()
    if auto_rules is None:
        auto_rules, existing_enabled = load_auto_rules(path)
        if auto_enabled is None:
            auto_enabled = existing_enabled
    lines = [
        "# Балачки — контекстні профілі застосунків. # feature/context-profiles",
        "# Порядок [[profile]] = пріоритет: перший збіг за іменем exe виграє.",
        "# dictionary: назва профілю-словника або \"\" (активний словник).",
        "",
        "[default]",
        f"auto_enter = {_b(default.behavior.auto_enter)}",
        f"dictionary = {_toml_str(default.behavior.dictionary or '')}",
        f"enabled = {_b(default.behavior.enabled)}",
        f"formatting = {_toml_str(default.behavior.formatting)}",
    ]
    for p in profiles:
        lines += [
            "",
            "[[profile]]",
            f"name = {_toml_str(p.name)}",
            f"apps = {_toml_list(p.apps)}",
            f"title_regex = {_toml_str(p.title_regex or '')}",
            f"auto_enter = {_b(p.behavior.auto_enter)}",
            f"dictionary = {_toml_str(p.behavior.dictionary or '')}",
            f"enabled = {_b(p.behavior.enabled)}",
            f"formatting = {_toml_str(p.behavior.formatting)}",
        ]
    # feature/auto-profile: секція правил «вікно → профіль» у тому ж файлі.
    if auto_rules or auto_enabled is not None:
        lines += [
            "",
            "# Авто-вибір профілю за активним вікном (feature/auto-profile).",
            "# process/title — wildcard; серед збігів виграє найточніше правило.",
            "[auto]",
            f"enabled = {_b(bool(auto_enabled))}",
        ]
        for r in auto_rules:
            lines += [
                "",
                "[[auto_rule]]",
                f"process = {_toml_str(r.process)}",
                f"title = {_toml_str(r.title)}",
                f"profile = {_toml_str(r.profile)}",
            ]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.error("Не вдалося зберегти профілі %s: %s", path, e)


def _b(v: bool) -> str:
    return "true" if v else "false"
