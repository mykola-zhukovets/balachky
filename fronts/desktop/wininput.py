"""Низькорівневий ввід у Windows через SendInput (чистий ctypes, без залежностей).

feature/cascade-paste — потрібен, бо pyautogui.typewrite / keyboard.write НЕ
вміють кирилицю надійно. SendInput із KEYEVENTF_UNICODE набирає будь-який
Unicode (wVk=0, код символу в wScan), а сурогатні пари покривають символи
поза BMP (емодзі тощо). Увесь текст вкидаємо ОДНИМ викликом SendInput з масивом
INPUT — атомарно; окремий режим із паузою між символами — для RDP.
"""
import ctypes
import os
import time
from ctypes import wintypes

# ULONG_PTR (dwExtraInfo) — вказівникового розміру; WPARAM саме такий на обох ОС.
ULONG_PTR = wintypes.WPARAM

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_V = 0x56
VK_C = 0x43             # C — копіювання виділеного (Command Mode: зчитати виділення)
VK_BACK = 0x08          # Backspace — скасування останньої вставки (feature/qol-pack)

# feature/office-voice-nav: клавіші голосової навігації полями зовнішніх
# документів (Tab/стрілки/Enter) і Ctrl+G для переходу в комірку Excel (Go To).
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_G = 0x47
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
# ім'я навігаційної клавіші (як у navcommands NavAction) → VK одиночної клавіші.
# "shift_tab" опрацьовується окремо (акорд), тут його нема.
_NAV_VK = {
    "tab": VK_TAB, "enter": VK_RETURN,
    "up": VK_UP, "down": VK_DOWN, "left": VK_LEFT, "right": VK_RIGHT,
}

# Менеджери паролів: у їхні вікна НЕ вставляти й НЕ набирати (див. paste.py).
# CredentialUIBroker — системний діалог введення облікових даних Windows.
PASSWORD_MANAGERS = frozenset({
    "keepass.exe",
    "keepassxc.exe",
    "bitwarden.exe",
    "1password.exe",
    "credentialuibroker.exe",
})

_RDP_CLASS = "TscShellContainerClass"


class MOUSEINPUT(ctypes.Structure):
    # Не використовуємо, але має бути в union: це найбільший його член, тож без
    # нього sizeof(INPUT) вийде замалим і SendInput відхилить cbSize.
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# argtypes/restype ОБОВʼЯЗКОВІ: на 64-бітній Windows дескриптори — вказівники,
# і без явного restype ctypes візьме c_int (32 біти) й обріже HWND/HANDLE.
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.SendInput.argtypes = (
    wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_user32.SendInput.restype = wintypes.UINT

_kernel32.OpenProcess.argtypes = (
    wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = (
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD))
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL


def _call_sendinput(inputs) -> int:
    """Вкинути список INPUT одним викликом. → скільки подій прийняла система.
    Винесено окремо, щоб тести могли підмінити реальний SendInput моком."""
    n = len(inputs)
    if n == 0:
        return 0
    arr = (INPUT * n)(*inputs)
    return _user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _vk_input(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _unicode_input(code_unit: int, up: bool) -> INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    ki = KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, u=_INPUTunion(ki=ki))


def _char_to_units(ch: str):
    """Символ → кортеж 16-бітних UTF-16 одиниць. Поза BMP → сурогатна пара."""
    cp = ord(ch)
    if cp > 0xFFFF:
        cp -= 0x10000
        return (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))
    return (cp,)


def _char_inputs(ch: str):
    seq = []
    for unit in _char_to_units(ch):
        seq.append(_unicode_input(unit, up=False))
        seq.append(_unicode_input(unit, up=True))
    return seq


def send_ctrl_v() -> bool:
    """Симулювати Ctrl+V. → True, якщо систему прийняла всі події."""
    seq = [_vk_input(VK_CONTROL, False), _vk_input(VK_V, False),
           _vk_input(VK_V, True), _vk_input(VK_CONTROL, True)]
    return _call_sendinput(seq) == len(seq)


def send_ctrl_c() -> bool:
    """Симулювати Ctrl+C (Command Mode: скопіювати виділене в активному вікні,
    щоб зчитати його з буфера). → True, якщо систему прийняла всі події."""
    seq = [_vk_input(VK_CONTROL, False), _vk_input(VK_C, False),
           _vk_input(VK_C, True), _vk_input(VK_CONTROL, True)]
    return _call_sendinput(seq) == len(seq)


def send_ctrl_shift_v() -> bool:
    """Симулювати Ctrl+Shift+V (вставка в консолях — там KEYEVENTF_UNICODE збоїть)."""
    seq = [_vk_input(VK_CONTROL, False), _vk_input(VK_SHIFT, False),
           _vk_input(VK_V, False), _vk_input(VK_V, True),
           _vk_input(VK_SHIFT, True), _vk_input(VK_CONTROL, True)]
    return _call_sendinput(seq) == len(seq)


def send_backspaces(count: int) -> bool:
    """Надіслати `count` натисків Backspace одним викликом SendInput (атомарно).
    Скасовує останню вставку простим стиранням символів. → True, якщо систему
    прийняла всі події. count <= 0 → no-op (True).

    ОБМЕЖЕННЯ: припускає, що курсор лишився там, де завершилась вставка, і що
    один вставлений символ = один Backspace. Для звичайних текстових полів це
    надійно; у полях з автозаміною/автодоповненням чи після ручного руху курсора
    результат може відрізнятись (тому дію робимо одразу після вставки)."""
    if count <= 0:
        return True
    seq = []
    for _ in range(count):
        seq.append(_vk_input(VK_BACK, False))
        seq.append(_vk_input(VK_BACK, True))
    return _call_sendinput(seq) == len(seq)


def send_nav_key(name: str) -> bool:
    """feature/office-voice-nav: надіслати одну навігаційну клавішу/акорд у активне
    вікно. name ∈ {tab, shift_tab, enter, up, down, left, right}. → True, якщо
    систему прийняла всі події; невідома назва → False (нічого не шлемо)."""
    if name == "shift_tab":
        seq = [_vk_input(VK_SHIFT, False), _vk_input(VK_TAB, False),
               _vk_input(VK_TAB, True), _vk_input(VK_SHIFT, True)]
        return _call_sendinput(seq) == len(seq)
    vk = _NAV_VK.get(name)
    if vk is None:
        return False
    seq = [_vk_input(vk, False), _vk_input(vk, True)]
    return _call_sendinput(seq) == len(seq)


def send_ctrl_g() -> bool:
    """feature/office-voice-nav: Ctrl+G — відкрити «Перейти» (Go To) у Excel/Word.
    Далі набирається адреса комірки й тиснеться Enter (див. paste.send_nav)."""
    seq = [_vk_input(VK_CONTROL, False), _vk_input(VK_G, False),
           _vk_input(VK_G, True), _vk_input(VK_CONTROL, True)]
    return _call_sendinput(seq) == len(seq)


def type_unicode(text: str, char_delay_ms: int = 0) -> bool:
    """Набрати text через KEYEVENTF_UNICODE. → True, якщо всі події прийнято.

    char_delay_ms == 0 → увесь текст одним SendInput (атомарно, найшвидше).
    char_delay_ms > 0  → посимвольно з паузою (для RDP, де пакетний вкид губиться).
    """
    if not text:
        return True
    if char_delay_ms <= 0:
        inputs = []
        for ch in text:
            inputs.extend(_char_inputs(ch))
        return _call_sendinput(inputs) == len(inputs)
    ok = True
    for ch in text:
        seq = _char_inputs(ch)
        ok = (_call_sendinput(seq) == len(seq)) and ok
        time.sleep(char_delay_ms / 1000.0)
    return ok


def is_password_manager(exe_name: str) -> bool:
    return bool(exe_name) and exe_name.lower() in PASSWORD_MANAGERS


def own_exe_name() -> str:
    """Ім'я власного процесу застосунку (нижній регістр): frozen — Balachky.exe,
    dev — python.exe/pythonw.exe. Для guard'а голосової навігації, щоб не слати
    Tab/Ctrl+G у власне вікно Балачок (див. is_own_process)."""
    import sys
    return os.path.basename(sys.executable or "").lower()


def is_own_process(exe_name: str) -> bool:
    """feature/office-voice-nav: активне вікно належить самим Балачкам? Навігаційні
    клавіші свідомо НЕ шлемо у власний UI (курсор диктанту має бути в чужому
    документі). Порожнє ім'я → False (не знаємо — не блокуємо цим guard'ом;
    інші запобіжники лишаються)."""
    return bool(exe_name) and exe_name.lower() == own_exe_name()


def is_console_class(window_class: str) -> bool:
    """Класична консоль (ConsoleWindowClass) чи Windows Terminal (CASCADIA_*)."""
    if not window_class:
        return False
    return (window_class == "ConsoleWindowClass"
            or window_class.upper().startswith("CASCADIA"))


def is_rdp_class(window_class: str) -> bool:
    return window_class == _RDP_CLASS


def _get_class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_window_exe(hwnd) -> str:
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _kernel32.CloseHandle(handle)


def get_foreground_info():
    """→ (клас активного вікна, ім'я exe у нижньому регістрі).
    Будь-яке зі значень може бути '', якщо WinAPI не відповів."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return "", ""
        return _get_class_name(hwnd), _get_window_exe(hwnd)
    except Exception:
        return "", ""


def get_foreground_window():
    """Дескриптор активного вікна (HWND) або None. feature/paste-preview:
    запам'ятовуємо ціль на момент показу картки, щоб повернути їй фокус перед
    вставкою (картка/її кнопки могли стати активним вікном під час редагування)."""
    try:
        hwnd = _user32.GetForegroundWindow()
        return hwnd or None
    except Exception:
        return None


def _get_window_title(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(512)
    _user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def capture_paste_target():
    """feature/paste-safety: знімок цілі вставки — (HWND активного вікна, його
    заголовок). Робимо на СТАРТІ диктування/запиту вставки, щоб перед фактичною
    вставкою звірити, що фокус лишився там само. → (None, '') якщо WinAPI мовчить."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return None, ""
        return hwnd, _get_window_title(hwnd)
    except Exception:
        return None, ""


def get_window_pid(hwnd):
    """PID процесу-власника вікна hwnd (або None). feature/dictation-queue:
    у відкладеній вставці (черга) звіряємо ще й PID — Windows може перевикористати
    числовий HWND після закриття вікна, тож самого HWND для черги замало."""
    if not hwnd:
        return None
    try:
        pid = ctypes.c_ulong(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value or None
    except Exception:
        return None


def set_foreground_window(hwnd) -> bool:
    """Повернути фокус вікну hwnd (best-effort — Windows обмежує зміну активного
    вікна). → True, якщо WinAPI підтвердив. None/0 → нічого не робимо."""
    if not hwnd:
        return False
    try:
        return bool(_user32.SetForegroundWindow(hwnd))
    except Exception:
        return False
