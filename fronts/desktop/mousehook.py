"""feature/mouse-ptt: бічна кнопка миші (X1/X2) як альтернативна кнопка запису.

Низькорівневий хук WH_MOUSE_LL через ctypes — БЕЗ нових залежностей. Окремий
потік із власним message loop (GetMessage) ловить WM_XBUTTONDOWN/UP, витягає
номер бічної кнопки з high-word поля mouseData і, коли натиснута саме
налаштована кнопка, викликає ТІ САМІ on_press/on_release, що й клавіатурний хук
(режими hold/toggle успадковуються самі). Клік НІКОЛИ не ковтається (завжди
CallNextHookEx) — кнопка й далі виконує свою звичну дію (напр. «назад» у
браузері).

Тонкощі, які легко зламати:
- HOOKPROC-трамплін тримаємо у ЖИВІЙ змінній (self._proc), інакше GC його
  забере і callback із C-боку впаде.
- restype/argtypes прописані явно: на x64 хендли й LRESULT — вказівникового
  розміру, дефолтний int їх ріже.
- перед тим як звітувати «готово», примусово створюємо чергу повідомлень потоку
  (PeekMessage), щоб PostThreadMessage(WM_QUIT) зі stop() гарантовано мав куди
  лягти й не загубився у вікні до входу в GetMessage.
"""
import ctypes
import logging
import sys
import threading
from ctypes import wintypes

WH_MOUSE_LL = 14
WM_XBUTTONDOWN = 0x020B
WM_XBUTTONUP = 0x020C
WM_QUIT = 0x0012
HC_ACTION = 0
PM_NOREMOVE = 0
XBUTTON1 = 0x0001
XBUTTON2 = 0x0002

# high-word mouseData → канонічне ім'я бічної кнопки (як у config.ptt_mouse_button)
_BUTTON_NAMES = {XBUTTON1: "x1", XBUTTON2: "x2"}

# LRESULT / LONG_PTR — вказівникового розміру (на x64 = 8 байт зі знаком)
LRESULT = ctypes.c_ssize_t


class MSLLHOOKSTRUCT(ctypes.Structure):
    """lParam WH_MOUSE_LL-події (учасники: mouseData несе номер X-кнопки)."""
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def button_from_mouse_data(mouse_data: int) -> "str | None":
    """High-word поля mouseData → "x1"/"x2"; для звичайних кнопок X-біта нема → None."""
    high = (mouse_data >> 16) & 0xFFFF
    return _BUTTON_NAMES.get(high)


def route_event(msg: int, button: "str | None", target: str,
                on_press, on_release) -> bool:
    """Чиста маршрутизація події: якщо кнопка збігається з target — down→on_press,
    up→on_release. Повертає True, якщо подію оброблено (для тестів). Ковтати клік
    чи ні — не наша справа (хук ЗАВЖДИ викликає CallNextHookEx окремо)."""
    if button is None or button != target:
        return False
    if msg == WM_XBUTTONDOWN:
        on_press()
        return True
    if msg == WM_XBUTTONUP:
        on_release()
        return True
    return False


class MouseHook:
    """WH_MOUSE_LL-хук у власному потоці. start()/stop() ідемпотентні.

    on_press/on_release викликаються з ПОТОКУ хука — приймач має бути потоко-
    безпечним. У застосунку це Qt-сигнали (Signal.emit): крос-thread emit кладе
    подію в чергу GUI-потоку, тож логіка hold/toggle виконується там само, де й
    для клавіатури."""

    # Lifecycle start()/stop() та цей cross-instance guard викликаються лише
    # з GUI-потоку; hook-thread лише обробляє WinAPI message loop.
    _unconfirmed_stop = None

    def __init__(self, button: str, on_press, on_release):
        self._button = button            # "x1" | "x2"
        self._on_press = on_press
        self._on_release = on_release
        self._thread = None
        self._thread_id = None
        self._hook = None                # HHOOK (істинний → хук стоїть)
        self._proc = None                # ЖИВЕ посилання на HOOKPROC (GC-guard)
        self._ready = threading.Event()  # виставляється після спроби SetWindowsHookEx
        self._ok = False                 # хук справді встановлено
        self._stop_unconfirmed = False

    def start(self) -> bool:
        """Підняти потік із хуком; блокує до результату SetWindowsHookEx.
        True — хук стоїть; False — не Windows / не вдалося встановити."""
        if sys.platform != "win32":
            return False
        if self._thread is not None:
            if not self._stop_unconfirmed:
                return self._ok
            if self._thread.is_alive():
                logging.warning("mouse-ptt: повторний запуск відхилено — "
                                "зупинку попереднього хука не підтверджено")
                return False
            self._clear_stopped_state()
        unconfirmed = type(self)._unconfirmed_stop
        if unconfirmed is not None:
            guarded_thread = unconfirmed._thread
            if guarded_thread is not None and guarded_thread.is_alive():
                logging.warning("mouse-ptt: новий хук відхилено — зупинку "
                                "попереднього хука не підтверджено")
                return False
            unconfirmed._clear_stopped_state()
        self._ready.clear()
        self._ok = False
        self._stop_unconfirmed = False
        self._thread = threading.Thread(target=self._run, name="mouse-ptt-hook",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(3.0)
        return self._ok

    def _run(self):
        # use_last_error=True → ctypes зберігає приватну копію LastError на кожен
        # виклик, тож ctypes.get_last_error() нижче поверне РЕАЛЬНИЙ код збою
        # SetWindowsHookExW, а не завжди 0 (плаский windll.user32 її не оновлює).
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.argtypes = ()
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._thread_id = kernel32.GetCurrentThreadId()

        HOOKPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        def _callback(nCode, wParam, lParam):
            if nCode == HC_ACTION:
                try:
                    info = ctypes.cast(
                        lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    button = button_from_mouse_data(info.mouseData)
                    route_event(wParam, button, self._button,
                                self._on_press, self._on_release)
                except Exception:
                    logging.exception("mouse-ptt: збій обробки події хука")
            # ЗАВЖДИ передати подію далі — бічну кнопку не ковтаємо
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc = HOOKPROC(_callback)      # тримати живим, інакше крах GC

        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
        user32.UnhookWindowsHookEx.restype = wintypes.BOOL
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.PeekMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT,
            wintypes.UINT, wintypes.UINT]
        user32.PeekMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = LRESULT
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        # примусово створити чергу повідомлень цього потоку ДО звіту «готово»,
        # щоб stop()->PostThreadMessage(WM_QUIT) не загубив повідомлення
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, WM_QUIT, WM_QUIT, PM_NOREMOVE)

        hmod = kernel32.GetModuleHandleW(None)
        self._hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._proc, hmod, 0)
        self._ok = bool(self._hook)
        self._ready.set()
        if not self._ok:
            logging.warning("mouse-ptt: SetWindowsHookExW не встановив хук "
                            "(err=%s)", ctypes.get_last_error())
            return
        # message loop обов'язковий для low-level хука; WM_QUIT (зі stop) → вихід
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None

    def stop(self) -> bool:
        """Зупинити хук: WM_QUIT у чергу потоку → GetMessage поверне 0 → цикл
        вийде і зніме хук; чекаємо потік із таймаутом (daemon — не блокуємо
        вихід). True — зупинку підтверджено, False — потік іще живий."""
        t = self._thread
        if t is None:
            return True
        tid = self._thread_id
        if tid is not None:
            # use_last_error=True — щоб ctypes.get_last_error() нижче дав реальний
            # код збою PostThreadMessageW, а не 0 (як плаский windll.user32).
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = [
                wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostThreadMessageW.restype = wintypes.BOOL
            if not user32.PostThreadMessageW(tid, WM_QUIT, 0, 0):
                logging.warning("mouse-ptt: PostThreadMessage(WM_QUIT) не вдався "
                                "(err=%s)", ctypes.get_last_error())
        t.join(3.0)
        if t.is_alive():
            self._stop_unconfirmed = True
            type(self)._unconfirmed_stop = self
            logging.warning("mouse-ptt: потік хука не завершився за 3 с; "
                            "стан і GC-guard збережено")
            return False
        self._clear_stopped_state()
        return True

    def _clear_stopped_state(self):
        """Очистити lifecycle-стан лише після підтвердженої смерті потоку."""
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc = None
        self._ok = False
        self._stop_unconfirmed = False
        if type(self)._unconfirmed_stop is self:
            type(self)._unconfirmed_stop = None
