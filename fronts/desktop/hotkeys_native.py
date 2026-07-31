"""feature/native-hotkeys: гарячі клавіші через WinAPI (ctypes, БЕЗ залежностей).

Заміна бібліотеці `keyboard` (покинута з 2020; її WH_KEYBOARD_LL-хук — типова
кейлогер-евристика для антивірусів/EDR, через яку хоткеї «мовчки» вмирають на
корпоративних машинах). Тут ЖОДНОГО глобального хука:

- натиск комбінації → RegisterHotKey + прихований message-loop потік (WM_HOTKEY
  прилітає в чергу потоку, бо hwnd=NULL). Система сама «ковтає» комбінацію —
  еквівалент suppress=True в legacy;
- відпускання (hold-PTT) → RegisterHotKey keyup не дає, тож ПІСЛЯ WM_HOTKEY
  запускаємо легке опитування GetAsyncKeyState (~20 мс) ЛИШЕ доки комбінація
  затиснута; відпускання будь-якого компонента → released (та сама семантика,
  що в legacy-хука). Обрано замість Raw Input свідомо: WM_INPUT слухає ВЕСЬ
  клавіатурний потік постійно (та сама телеметрична поверхня, що й хук, і для
  EDR виглядає так само), а GetAsyncKeyState лише ЧИТАЄ стан пари клавіш і
  працює секунди — доки користувач тримає PTT;
- MOD_NOREPEAT глушить авто-повтор WM_HOTKEY — один pressed на одне фізичне
  затискання (у legacy це робив гейт _active).

VK-коди прив'язані до фізичних клавіш, НЕ до розкладки: укр/англ дають той
самий VK, тож комбінації не ламаються при перемиканні мови.

Фолбек: config.hotkey_backend = "native" (дефолт) | "legacy" — миттєве
повернення на keyboard без ребілду, якщо в користувача щось піде не так.
"""
import ctypes
import itertools
import logging
import sys
import threading
import time
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal

from .hotkey import _DEFAULT_KEY, normalize_name

log = logging.getLogger(__name__)

# --- WinAPI константи ---
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000            # без авто-повтору WM_HOTKEY при утриманні
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
WM_APP_TASK = 0x8000 + 1         # WM_APP+1: «у черзі завдань щось є»
PM_NOREMOVE = 0
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_HOLD_POLL_S = 0.02              # крок опитування GetAsyncKeyState під час hold

# --- мапінг імен → VK (модифікатори) ---
_MOD_FLAGS = {"ctrl": MOD_CONTROL, "shift": MOD_SHIFT,
              "alt": MOD_ALT, "windows": MOD_WIN}
# групи VK для опитування «чи ще затиснуто»: generic-коди VK_CONTROL/VK_SHIFT/
# VK_MENU покривають обидві сторони; для Win generic-коду нема — пара LWIN/RWIN
_MOD_POLL = {"ctrl": (0x11,), "shift": (0x10,),
             "alt": (0x12,), "windows": (0x5B, 0x5C)}

# --- мапінг імен → VK (основні клавіші; канонічні імена — як у legacy/конфігу) ---
_VK_MAIN = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "esc": 0x1B, "escape": 0x1B, "backspace": 0x08,
    "insert": 0x2D, "delete": 0x2E, "del": 0x2E,
    "home": 0x24, "end": 0x23,
    "page up": 0x21, "pageup": 0x21, "page down": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "caps lock": 0x14, "num lock": 0x90, "scroll lock": 0x91,
    "print screen": 0x2C, "pause": 0x13, "menu": 0x5D,
    "-": 0xBD, "minus": 0xBD, "=": 0xBB, "plus": 0xBB,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, ";": 0xBA, "'": 0xDE,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}
for _i in range(26):
    _VK_MAIN[chr(ord("a") + _i)] = 0x41 + _i
for _i in range(10):
    _VK_MAIN[str(_i)] = 0x30 + _i
    _VK_MAIN[f"num {_i}"] = 0x60 + _i
for _i in range(24):                       # f1..f24 (Copilot-клавіша = f23)
    _VK_MAIN[f"f{_i + 1}"] = 0x70 + _i

# зворотний мапінг VK → канонічне ім'я (для Qt-захоплення в Налаштуваннях);
# перше (канонічне) ім'я перемагає аліаси ("enter", не "return")
NAME_BY_VK = {}
for _name, _vk in _VK_MAIN.items():
    NAME_BY_VK.setdefault(_vk, _name)
# модифікатори: generic і сторонні VK → канонічне ім'я
for _vk in (0x10, 0xA0, 0xA1):
    NAME_BY_VK[_vk] = "shift"
for _vk in (0x11, 0xA2, 0xA3):
    NAME_BY_VK[_vk] = "ctrl"
for _vk in (0x12, 0xA4, 0xA5):
    NAME_BY_VK[_vk] = "alt"
for _vk in (0x5B, 0x5C):
    NAME_BY_VK[_vk] = "windows"


class HotkeyError(Exception):
    """Помилка реєстрації гарячої клавіші. conflict=True — комбінацію вже
    тримає хтось інший (система чи інший застосунок)."""

    def __init__(self, message: str, conflict: bool = False):
        super().__init__(message)
        self.conflict = conflict


def parse_combo(key: str):
    """Рядок комбінації ("ctrl+shift+space") → (fsModifiers, VK основної,
    групи VK для hold-опитування). Імена — канон legacy (normalize_name зводить
    ліві/праві модифікатори). Невідома клавіша / дві основні / нема основної →
    HotkeyError із зрозумілим текстом."""
    parts = [normalize_name(p.strip()) for p in (key or "").split("+") if p.strip()]
    if not parts:
        raise HotkeyError("порожня комбінація клавіш")
    mods, main, poll = 0, None, []
    for p in parts:
        if p in _MOD_FLAGS:
            if not mods & _MOD_FLAGS[p]:
                poll.append(_MOD_POLL[p])
            mods |= _MOD_FLAGS[p]
        elif p in _VK_MAIN:
            if main is not None:
                raise HotkeyError(
                    f"у комбінації «{key}» дві основні клавіші — має бути одна")
            main = _VK_MAIN[p]
        else:
            raise HotkeyError(f"невідома клавіша «{p}» у комбінації «{key}»")
    if main is None:
        raise HotkeyError(f"у комбінації «{key}» нема основної клавіші")
    poll.append((main,))
    return mods, main, tuple(poll)


def _combo_released(groups, is_down) -> bool:
    """Чи відпущено комбінацію: хоч одна група (компонент) без жодної
    затиснутої клавіші → так. groups — кортежі альтернативних VK
    (windows = LWIN|RWIN), is_down(vk) — предикат «клавіша затиснута»."""
    return any(not any(is_down(vk) for vk in g) for g in groups)


class _Entry:
    __slots__ = ("id", "mods", "vk", "poll_groups",
                 "on_press", "on_release", "hold", "active")

    def __init__(self, hid, mods, vk, poll_groups, on_press, on_release, hold):
        self.id = hid
        self.mods = mods
        self.vk = vk
        self.poll_groups = poll_groups
        self.on_press = on_press
        self.on_release = on_release
        self.hold = hold
        self.active = False     # hold: комбінація зараз фізично затиснута


class HotkeyManager:
    """Прихований message-loop потік + RegisterHotKey (hwnd=NULL → WM_HOTKEY
    у чергу потоку). Реєстрація/дереєстрація МУСЯТЬ виконуватись на потоці
    циклу (вимога WinAPI) — прокидаємо їх туди чергою завдань + WM_APP_TASK.

    Callbacks (on_press/on_release) стріляють НЕ в GUI-потоці — приймач має
    бути потокобезпечним; у застосунку це Qt Signal.emit (queued у GUI-потік),
    та сама модель, що була з keyboard-потоком і з mousehook."""

    def __init__(self):
        self._thread = None
        self._thread_id = None
        self._ready = threading.Event()
        self._ok = False
        self._lock = threading.Lock()
        self._tasks = []             # (fn, done_event, box) — виконати на потоці циклу
        self._entries = {}           # id → _Entry
        self._ids = itertools.count(1)
        self._suspended = False
        self._user32 = None
        self._stop_unconfirmed = False

    # --- публічний API (будь-який потік) ---
    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        if self._thread is not None:
            if self._stop_unconfirmed:
                if not self._thread.is_alive():
                    self._clear_stopped_state()
                else:
                    log.warning("hotkeys: повторний запуск відхилено — зупинку "
                                "попереднього message-loop не підтверджено")
                    return False
            else:
                return self._ok
        self._ready.clear()
        self._stop_unconfirmed = False
        self._thread = threading.Thread(target=self._run, name="hotkey-loop",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self._ok

    def thread_id(self):
        return self._thread_id

    def register(self, key: str, on_press, on_release=None,
                 hold: bool = False) -> int:
        """Зареєструвати комбінацію. → id. HotkeyError: невідома клавіша,
        зайнята комбінація (conflict=True) чи менеджер не запущено."""
        mods, vk, groups = parse_combo(key)     # валідація ДО походу в потік
        if not self.start():
            raise HotkeyError("менеджер гарячих клавіш не запустився")
        return self._call(lambda: self._do_register(
            key, mods, vk, groups, on_press, on_release, hold))

    def unregister(self, hid: int) -> None:
        """Зняти комбінацію за id; невідомий id — тихий no-op (уже знято)."""
        if self._thread is None:
            return
        try:
            self._call(lambda: self._do_unregister(hid))
        except HotkeyError:
            pass

    def suspend(self) -> None:
        """Тимчасово відпустити всі комбінації (діалог захоплення нової клавіші:
        інакше RegisterHotKey «з'їв» би натиск і Qt його не побачив)."""
        if self._thread is not None and not self._suspended:
            self._call(self._do_suspend)

    def resume(self) -> None:
        if self._thread is not None and self._suspended:
            self._call(self._do_resume)

    def stop(self) -> bool:
        """True — message-loop підтверджено зупинився; False — потік іще живий."""
        t = self._thread
        if t is None:
            return True
        tid = self._thread_id
        if tid is not None and self._user32 is not None:
            if not self._user32.PostThreadMessageW(tid, WM_QUIT, 0, 0):
                log.warning("hotkeys: PostThreadMessage(WM_QUIT) не вдався (err=%s)",
                            ctypes.get_last_error())
        t.join(3.0)
        if t.is_alive():
            self._stop_unconfirmed = True
            log.warning("hotkeys: message-loop не завершився за 3 с; "
                        "стан і записи реєстрацій збережено")
            return False
        self._clear_stopped_state()
        return True

    def _clear_stopped_state(self) -> None:
        """Очистити lifecycle-стан лише після підтвердженої смерті потоку."""
        self._thread = None
        self._thread_id = None
        self._ok = False
        self._suspended = False
        self._stop_unconfirmed = False
        with self._lock:
            self._entries.clear()
            self._tasks.clear()

    # --- виконання завдання на потоці циклу ---
    def _call(self, fn):
        done = threading.Event()
        box = {}
        with self._lock:
            self._tasks.append((fn, done, box))
        if not self._user32.PostThreadMessageW(self._thread_id, WM_APP_TASK, 0, 0):
            raise HotkeyError("потік гарячих клавіш недоступний")
        if not done.wait(3.0):
            raise HotkeyError("потік гарячих клавіш не відповів")
        if "exc" in box:
            raise box["exc"]
        return box.get("result")

    def _drain_tasks(self):
        while True:
            with self._lock:
                if not self._tasks:
                    return
                fn, done, box = self._tasks.pop(0)
            try:
                box["result"] = fn()
            except Exception as e:           # прокидаємо в потік-замовник
                box["exc"] = e
            done.set()

    # --- операції НА потоці циклу ---
    def _do_register(self, key, mods, vk, groups, on_press, on_release, hold):
        hid = next(self._ids)
        if not self._user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
            err = ctypes.get_last_error()
            if err == ERROR_HOTKEY_ALREADY_REGISTERED:
                raise HotkeyError(
                    f"комбінація «{key}» вже зайнята іншим застосунком або "
                    "системою — оберіть іншу", conflict=True)
            raise HotkeyError(
                f"не вдалося зареєструвати «{key}» (код Windows {err})")
        entry = _Entry(hid, mods, vk, groups, on_press, on_release, hold)
        with self._lock:
            self._entries[hid] = entry
        return hid

    def _do_unregister(self, hid):
        with self._lock:
            entry = self._entries.pop(hid, None)
        if entry is None:
            return
        entry.active = False              # зупинити hold-poll, якщо крутиться
        if not self._user32.UnregisterHotKey(None, hid):
            log.warning("hotkeys: UnregisterHotKey(%s) не вдався (err=%s)",
                        hid, ctypes.get_last_error())

    def _do_suspend(self):
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
            e.active = False
            self._user32.UnregisterHotKey(None, e.id)
        self._suspended = True

    def _do_resume(self):
        with self._lock:
            entries = list(self._entries.values())
        for e in entries:
            if not self._user32.RegisterHotKey(None, e.id,
                                               e.mods | MOD_NOREPEAT, e.vk):
                log.warning("hotkeys: не вдалося повернути комбінацію id=%s "
                            "після захоплення (err=%s)", e.id,
                            ctypes.get_last_error())
        self._suspended = False

    # --- message loop ---
    def _run(self):
        # use_last_error=True → get_last_error() віддає РЕАЛЬНИЙ код збою
        # (плаский windll.user32 його не оновлює) — грабля з mousehook
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [
            wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostThreadMessageW.restype = wintypes.BOOL
        user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32 = user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.argtypes = ()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = kernel32.GetCurrentThreadId()

        # примусово створити чергу повідомлень ДО звіту «готово», щоб перший
        # PostThreadMessage не загубився (та сама грабля, що в mousehook)
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_QUIT, WM_QUIT, PM_NOREMOVE)
        self._ok = True
        self._ready.set()

        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self._on_hotkey(int(msg.wParam))
            elif msg.message == WM_APP_TASK:
                self._drain_tasks()
        # вихід (WM_QUIT): зняти всі реєстрації — без витоків
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for e in entries:
            e.active = False
            user32.UnregisterHotKey(None, e.id)
        self._drain_tasks()      # не лишати замовників висіти на wait()

    def _on_hotkey(self, hid: int):
        with self._lock:
            entry = self._entries.get(hid)
        if entry is None:
            return
        if entry.hold:
            if entry.active:      # страхування (MOD_NOREPEAT і так глушить повтор)
                return
            entry.active = True
        try:
            entry.on_press()
        except Exception:
            log.exception("hotkeys: збій обробника натиску id=%s", hid)
        if entry.hold:
            threading.Thread(target=self._hold_poll, args=(entry,),
                             name="hotkey-hold-poll", daemon=True).start()

    def _hold_poll(self, entry: _Entry):
        """Опитування «чи ще затиснуто» ~кожні 20 мс — ЛИШЕ доки триває
        утримання. Відпускання будь-якого компонента → released (семантика
        legacy: on release of ANY component)."""
        is_down = lambda vk: bool(self._user32.GetAsyncKeyState(vk) & 0x8000)
        while entry.active:
            if _combo_released(entry.poll_groups, is_down):
                entry.active = False
                if entry.on_release is not None:
                    try:
                        entry.on_release()
                    except Exception:
                        log.exception("hotkeys: збій обробника відпускання id=%s",
                                      entry.id)
                return
            time.sleep(_HOLD_POLL_S)


# --- спільний менеджер процесу ---
_manager = None
_manager_lock = threading.Lock()


def get_manager() -> HotkeyManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = HotkeyManager()
        return _manager


# --- Qt-обгортки з API legacy-класів (drop-in) ---
class NativeHotkey(QObject):
    """PTT: pressed/released — той самий інтерфейс, що hotkey.Hotkey.
    hold=True завжди: released потрібен режиму утримання, а toggle/double_tap
    його просто ігнорують (як і з legacy-хуком)."""

    pressed = Signal()
    released = Signal()

    def __init__(self, key: str):
        super().__init__()
        self._key = key
        self._id = None
        # битий/зайнятий ключ із config.toml НЕ має валити старт — відкат
        # на дефолт-комбінацію (та сама поведінка, що в legacy)
        if not self._register(key) and key != _DEFAULT_KEY:
            self._key = _DEFAULT_KEY
            self._register(_DEFAULT_KEY)

    def _register(self, key: str) -> bool:
        try:
            self._id = get_manager().register(
                key, self.pressed.emit, self.released.emit, hold=True)
            return True
        except HotkeyError as e:
            log.warning("Не вдалося повісити клавішу запису «%s»: %s", key, e)
            self._id = None
            return False
        except Exception:
            log.exception("Не вдалося повісити клавішу запису «%s»", key)
            self._id = None
            return False

    def rebind(self, key: str) -> bool:
        """Перепризначити на льоту. Новий ключ не зачепився → повертаємо
        попередній робочий (не лишаємо застосунок без клавіші запису).
        Повертає True лише коли НОВИЙ ключ реально зареєстровано — щоб UI
        не стверджував успіх при мовчазному відкаті (звірка №8 18.07)."""
        if self._id is not None:
            get_manager().unregister(self._id)
            self._id = None
        if self._register(key):
            self._key = key
            return True
        self._register(self._key)
        return False


class NativeActionHotkeys:
    """Хоткеї простих дій (натиск): API дзеркалить actionhotkey.ActionHotkeys.
    На відміну від legacy, reapply після rebind — справжній no-op-перевіс
    (нативний rebind НЕ зносить чужі реєстрації), але лишаємо для симетрії."""

    def __init__(self, on_undo, on_insert):
        self._on_undo = on_undo
        self._on_insert = on_insert
        self._undo_key = ""
        self._insert_key = ""
        self._ids = []

    def apply(self, undo_key: str, insert_key: str) -> None:
        self._undo_key = undo_key or ""
        self._insert_key = insert_key or ""
        self.reapply()

    def reapply(self) -> None:
        self._clear()
        for key, cb in ((self._undo_key, self._on_undo),
                        (self._insert_key, self._on_insert)):
            if not key:
                continue
            try:
                self._ids.append(get_manager().register(key, cb))
            except Exception:
                log.exception("Не вдалося повісити хоткей дії «%s»", key)

    def _clear(self) -> None:
        for hid in self._ids:
            get_manager().unregister(hid)
        self._ids = []


class NativeNoteHotkey(QObject):
    """Комбінація відкриття нотатки: API дзеркалить note.NoteHotkey."""

    triggered = Signal()

    def __init__(self, key: str):
        super().__init__()
        self._key = key
        self._id = None

    def start(self) -> bool:
        try:
            self._id = get_manager().register(self._key, self.triggered.emit)
            return True
        except Exception:
            log.exception("Не вдалося повісити хук нотатки на «%s»", self._key)
            self._id = None
            return False

    def stop(self):
        if self._id is not None:
            get_manager().unregister(self._id)
            self._id = None


# --- вибір бекенда (config.hotkey_backend: "native" дефолт | "legacy") ---
def backend_is_native(cfg) -> bool:
    raw = (getattr(cfg, "hotkey_backend", "native") or "native").strip().lower()
    return raw != "legacy"


def make_ptt_hotkey(cfg, key: str):
    if backend_is_native(cfg):
        return NativeHotkey(key)
    from . import hotkey
    return hotkey.Hotkey(key)


def make_action_hotkeys(cfg, on_undo, on_insert):
    if backend_is_native(cfg):
        return NativeActionHotkeys(on_undo, on_insert)
    from . import actionhotkey
    return actionhotkey.ActionHotkeys(on_undo, on_insert)


def make_note_hotkey(cfg, key: str):
    if backend_is_native(cfg):
        return NativeNoteHotkey(key)
    from . import note
    return note.NoteHotkey(key)      # атрибут модуля — тести його патчать


def capture_suspend() -> None:
    """Перед діалогом захоплення нової комбінації: відпустити нативні
    реєстрації, інакше RegisterHotKey «з'їсть» натиск і діалог його не побачить.
    Legacy-бекенд менеджера не створює → no-op (його хук бачить і
    suppressed-події, тож там нічого призупиняти й не треба)."""
    if _manager is not None:
        try:
            _manager.suspend()
        except Exception:
            log.exception("hotkeys: suspend перед захопленням не вдався")


def capture_resume() -> None:
    if _manager is not None:
        try:
            _manager.resume()
        except Exception:
            log.exception("hotkeys: resume після захоплення не вдався")


def shutdown(cfg) -> None:
    """Вихід застосунку: нативний менеджер — зупинити (сам зніме реєстрації);
    legacy — keyboard.unhook_all, як і раніше."""
    if backend_is_native(cfg):
        if _manager is not None:
            if not _manager.stop():
                log.warning("hotkeys: shutdown не підтвердив зупинку message-loop")
        return
    try:
        import keyboard
        keyboard.unhook_all()
    except Exception:
        pass
