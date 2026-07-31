"""Вставка тексту — каскад із політикою за класом активного вікна.

Крок 1 (страхувальна сітка): pyperclip.copy — текст завжди опиняється в буфері,
навіть якщо вставити не вийде. Крок 2: спосіб доставки за вікном:
  • менеджер паролів  → НЕ вставляти й НЕ набирати (повертаємо PASTE_BLOCKED);
  • консоль (cmd/PowerShell/Windows Terminal) → Ctrl+Shift+V (там KEYEVENTF_UNICODE збоїть);
  • RDP (TscShellContainerClass) → Ctrl+V (буфер редиректиться в сесію; набір не шлемо);
  • решта → Ctrl+V, або, коли ввімкнено «вставляти набором», type_unicode —
    для полів, де вставка заборонена, а надійного авто-детекту провалу нема.
Останній рубіж, якщо SendInput нічого не прийняв, — keyboard.send (уже в залежностях).

Відновлення буфера: перед вставкою запам'ятовуємо поточний ТЕКСТОВИЙ вміст і
повертаємо його ~0.4 с ПІСЛЯ вставки. Не-текст (зображення, файли) pyperclip не
читає — не зберігаємо й не відновлюємо.
"""
import threading
import time
import pyperclip

from . import wininput

_RESTORE_DELAY = 0.4   # с; негайне відновлення ламає вставку в повільних застосунках
_PANIC_RESTORE_TIMEOUT = 0.25
_restore_lock = threading.Lock()
_restore_condition = threading.Condition(_restore_lock)
_restore_timer = None
_restore_expected = None
_restore_delay = _RESTORE_DELAY
_active_restore_operations = 0
_session_original = None
_restore_generation = 0
_restore_blocked = False
_restore_panic_barriers = 0
_restore_callbacks_in_flight = 0
_restore_operation_state = threading.local()

# Сентинел: ціль — менеджер паролів, туди свідомо нічого не шлемо.
PASTE_BLOCKED = "blocked"


class _ClipboardRestoreSnapshot(str):
    def __new__(cls, text: str, generation: int):
        value = super().__new__(cls, text)
        value.restore_generation = generation
        return value


def target_changed(pinned_hwnd, current_hwnd) -> bool:
    """Чи змінилось активне вікно між стартом диктування і моментом вставки.

    feature/paste-safety: pinned_hwnd — дескриптор вікна, що було активним на
    момент старту диктування/запиту вставки; current_hwnd — активне вікно ЗАРАЗ.
    Порівнюємо саме за HWND (заголовок вікна може мінятись у тому ж вікні —
    напр. «* Untitled» → «file.txt»). pinned_hwnd = None/0 → ціль не закріплювали
    (напр. «повторити вставку» з трею) → НЕ блокуємо (False).

    СВІДОМИЙ КОМПРОМІС: звіряємо лише HWND. Windows може перевикористати числовий
    HWND після закриття вікна, тож теоретично нове вікно з тим самим HWND пройде
    як «те саме» — крихітне вікно гонки (закрити ціль І встигнути отримати той
    самий HWND за час диктування). Ловити його дорожче, ніж воно варте: довелось
    би тримати ще й клас/PID вікна у піні й звіряти їх (capture_paste_target тоді
    має повертати не (HWND, заголовок), а ширший знімок). Для наявного кейсу
    (диктуєш → фокус перемкнувся у ІНШЕ живе вікно з іншим HWND) звірки за HWND
    досить; якщо цей компроміс стане проблемою — додати сюди звірку класу/PID."""
    if not pinned_hwnd:
        return False
    return pinned_hwnd != current_hwnd


def target_changed_ex(pinned_hwnd, pinned_pid, current_hwnd, current_pid) -> bool:
    """feature/dictation-queue: звірка цілі вставки за HWND І PID.

    Для відкладеної вставки (черга) самого HWND замало: між записом фрази і її
    вставкою вікно-ціль могло закритись, а Windows — перевикористати той самий
    числовий HWND під інше вікно (можливо, іншого процесу). Тож окрім зміни HWND
    (як у ``target_changed``) вважаємо ціль зміненою і коли ОБИДВА PID відомі й
    різні. Якщо PID невідомий (None) — не блокуємо через нього (лишається
    поведінка за HWND). pinned_hwnd = None → ціль не закріплювали → False."""
    if target_changed(pinned_hwnd, current_hwnd):
        return True
    if pinned_pid and current_pid and pinned_pid != current_pid:
        return True
    return False


def snapshot_clipboard() -> str:
    """Поточний ТЕКСТОВИЙ вміст буфера ('' якщо порожньо / не-текст / недоступно)."""
    try:
        return pyperclip.paste()
    except Exception:
        return ""


def begin_clipboard_restore() -> str:
    """Почати paste-операцію, не прив'язуючи первинний оригінал до timer."""
    global _restore_timer, _restore_expected
    global _active_restore_operations, _session_original
    while True:
        with _restore_condition:
            while _restore_blocked:
                _restore_condition.wait()
            timer = _restore_timer
            active = _active_restore_operations
            generation = _restore_generation
        current = snapshot_clipboard()
        with _restore_condition:
            if (timer is not _restore_timer
                    or active != _active_restore_operations
                    or generation != _restore_generation
                    or _restore_blocked):
                continue
            if active == 0:
                if (timer is None
                        or current != _restore_expected
                        or _session_original is None):
                    _session_original = current
            elif _session_original is None:
                _session_original = current
            _active_restore_operations += 1
            _restore_timer = None
            _restore_expected = None
            _restore_operation_state.generation = generation
        if timer is not None:
            timer.cancel()
        return _ClipboardRestoreSnapshot(current, generation)


def end_clipboard_restore() -> None:
    """Завершити paste-операцію, яка не плануватиме відновлення буфера."""
    global _restore_timer, _restore_expected
    global _active_restore_operations, _session_original
    timer = None
    generation = getattr(
        _restore_operation_state, "generation", _restore_generation)
    with _restore_condition:
        if generation != _restore_generation or _restore_blocked:
            if hasattr(_restore_operation_state, "generation"):
                del _restore_operation_state.generation
            return
        if _active_restore_operations:
            _active_restore_operations -= 1
            if _active_restore_operations == 0:
                timer = _restore_timer
                _restore_timer = None
                _session_original = None
                _restore_expected = None
    if hasattr(_restore_operation_state, "generation"):
        del _restore_operation_state.generation
    if timer is not None:
        timer.cancel()


def cancel_clipboard_restore() -> None:
    """Скасувати pending-відновлення, щоб timer не пережив нову вставку/вихід."""
    with _restore_condition:
        timer = _reset_restore_session_locked(invalidate=True)
        _restore_condition.notify_all()
    if hasattr(_restore_operation_state, "generation"):
        del _restore_operation_state.generation
    if timer is not None:
        timer.cancel()


def _reset_restore_session_locked(*, invalidate: bool):
    global _restore_timer, _restore_expected
    global _active_restore_operations, _session_original, _restore_generation
    timer = _restore_timer
    _restore_timer = None
    _restore_expected = None
    _active_restore_operations = 0
    _session_original = None
    if invalidate:
        _restore_generation += 1
    return timer


def panic_clear_clipboard(clear_clipboard,
                          timeout: float = _PANIC_RESTORE_TIMEOUT) -> bool:
    """Інвалідувати restore-сесію, дочекатися callback і очистити буфер.

    Нові restore-операції чекають, доки panic завершить очищення. False означає,
    що або callback не завершився за timeout, або саме очищення не підтвердилось.
    """
    global _restore_blocked, _restore_panic_barriers
    deadline = time.monotonic() + max(0.0, timeout)
    with _restore_condition:
        _restore_panic_barriers += 1
        _restore_blocked = True
        timer = _reset_restore_session_locked(invalidate=True)
    if timer is not None:
        timer.cancel()

    barrier_complete = True
    try:
        with _restore_condition:
            while _restore_callbacks_in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    barrier_complete = False
                    break
                _restore_condition.wait(remaining)
        if not barrier_complete:
            return False
        cleared = clear_clipboard() is not False
        return cleared
    finally:
        with _restore_condition:
            _reset_restore_session_locked(invalidate=False)
            _restore_panic_barriers -= 1
            _restore_blocked = bool(_restore_panic_barriers)
            _restore_condition.notify_all()


# Command Mode: сентинел, який кладемо в буфер ПЕРЕД Ctrl+C, щоб відрізнити
# «нічого не виділено» (буфер лишився сентинелом) від реального виділеного тексту.
_CAPTURE_SENTINEL = "\x00__balachky_capture_probe__\x00"


def capture_selection(*, copy_delay: float = 0.12,
                      copy_fn=None, paste_fn=None, send_copy=None, sleep_fn=None) -> str:
    """Command Mode: зчитати ВИДІЛЕНИЙ текст з активного вікна через Ctrl+C, БЕЗ
    втрати буфера обміну користувача.

    Кроки: (1) запам'ятати поточний текст буфера; (2) покласти сентинел (щоб
    відрізнити «нічого не виділено» від реального тексту); (3) Ctrl+C; (4) прочитати
    буфер — якщо там сентинел, виділення НЕ було → ''; (5) ВІДНОВИТИ початковий
    текст буфера синхронно. → виділений текст ('' якщо нічого не виділено /
    недоступно).

    copy_fn/paste_fn/send_copy/sleep_fn — інжекція для юніт-тестів (без реального
    буфера й клавіатури). Не-текст у буфері pyperclip не читає — тоді previous='' і
    відновлювати нічого (як у restore_clipboard)."""
    copy_fn = copy_fn or pyperclip.copy
    paste_fn = paste_fn or pyperclip.paste
    send_copy = send_copy or wininput.send_ctrl_c
    sleep_fn = sleep_fn or time.sleep

    try:
        previous = paste_fn()
    except Exception:
        previous = ""
    try:
        copy_fn(_CAPTURE_SENTINEL)
    except Exception:
        pass
    try:
        send_copy()
    except Exception:
        pass
    sleep_fn(copy_delay)                # дати активному вікну час покласти виділене
    try:
        current = paste_fn()
    except Exception:
        current = ""
    # Відновлюємо початковий буфер СИНХРОННО (перезаписуємо сентинел/виділення);
    # порожній previous не відновлюємо — нема чого / був не-текст.
    if previous:
        try:
            copy_fn(previous)
        except Exception:
            pass
    if current == _CAPTURE_SENTINEL:
        return ""                       # Ctrl+C нічого не скопіював — виділення нема
    return current or ""


def paste_text(text: str, typing_fallback: bool = False,
               owner_hwnd: int | None = None):
    """Кладе текст у буфер і доставляє його в активне вікно за політикою класу.

    → назва способу ('ctrl_v' | 'ctrl_shift_v' | 'type_unicode' | 'keyboard'),
      PASTE_BLOCKED (ціль — менеджер паролів), або None (жоден спосіб не вкинувся).
    """
    from whisper_core.win_hardening import set_clipboard_text_excluded
    if not set_clipboard_text_excluded(text, owner_hwnd):
        pyperclip.copy(text)   # страхувальна сітка — робимо ПЕРШИМ, до будь-чого

    window_class, exe = wininput.get_foreground_info()
    if wininput.is_password_manager(exe):
        return PASTE_BLOCKED

    time.sleep(0.3)  # дати фокусу повернутись у ціль

    if wininput.is_console_class(window_class):
        method, ok = "ctrl_shift_v", wininput.send_ctrl_shift_v()
    elif wininput.is_rdp_class(window_class):
        method, ok = "ctrl_v", wininput.send_ctrl_v()
    elif typing_fallback:
        method, ok = "type_unicode", wininput.type_unicode(text)
    else:
        method, ok = "ctrl_v", wininput.send_ctrl_v()

    if ok:
        return method

    # SendInput нічого не прийняв — останній рубіж давньою бібліотекою.
    # keyboard лениво: send не ставить хуків, а в native-режимі без цього
    # фолбека бібліотека взагалі не вантажиться.
    try:
        import keyboard
        keyboard.send("ctrl+v")
        return "keyboard"
    except Exception:
        return None


# feature/office-voice-nav: пауза між Ctrl+G та набором адреси — щоб діалог
# «Перейти» встиг відкритись і прийняти фокус (як 0.3с перед вставкою вище).
_GOTO_DIALOG_DELAY = 0.25

# Сентинели результату навігації (окремі від рядків-методів, щоб gate у app
# розрізняв «оброблено успішно» / «заблоковано» / «збій»).
NAV_OK = "ok"
NAV_FAILED = None


def send_nav(action, target_hwnd=None):
    """feature/office-voice-nav: доставити навігаційну дію у активне вікно.

    action — NavAction із navcommands: ("key", <ім'я>) або ("goto", "<АДРЕСА>").
    target_hwnd — закріплена ціль навігації (HWND вікна, де почалося диктування).
    Безпека — ТІ САМІ гейти, що й вставка, плюс власні для навігації:
      • менеджер паролів  → PASTE_BLOCKED (нічого не шлемо);
      • власне вікно Балачок → PASTE_BLOCKED (не навігуємо власний UI);
      • target_hwnd задано, але активне вікно вже ІНШЕ → PASTE_BLOCKED
        (pinned-target: фокус пішов з документа — не тиснемо клавіші наосліп у
        чужий застосунок). target_hwnd=None → перевірку пропускаємо (сумісно з
        master, де закріплення цілі ще не вмикається).
    → NAV_OK ('ok') | PASTE_BLOCKED | NAV_FAILED (None, жодна подія не вкинулась).

    На відміну від тексту, навігацію в буфер не кладемо (клавіша — не текст).
    """
    kind, arg = action
    window_class, exe = wininput.get_foreground_info()
    if wininput.is_password_manager(exe):
        return PASTE_BLOCKED
    if wininput.is_own_process(exe):
        return PASTE_BLOCKED
    if target_hwnd is not None and wininput.get_foreground_window() != target_hwnd:
        return PASTE_BLOCKED

    if kind == "goto":
        # Excel/Word «Перейти»: Ctrl+G → адреса → Enter. Детермінований шлях без
        # прямого доступу до Name Box (той не має власного хоткея).
        if not wininput.send_ctrl_g():
            return NAV_FAILED
        time.sleep(_GOTO_DIALOG_DELAY)
        wininput.type_unicode(arg)
        ok = wininput.send_nav_key("enter")
        return NAV_OK if ok else NAV_FAILED

    # kind == "key"
    ok = wininput.send_nav_key(arg)
    return NAV_OK if ok else NAV_FAILED


def undo_paste(count: int) -> bool:
    """feature/qol-pack: скасувати останню вставку — стерти `count` символів
    Backspace-ами в активному вікні. Буфер обміну НЕ чіпаємо. → True/False.
    Обмеження — див. wininput.send_backspaces (припускаємо, що курсор лишився
    на місці вставки)."""
    if count <= 0:
        return False
    return wininput.send_backspaces(count)


def restore_clipboard(previous: str, delay: float = _RESTORE_DELAY,
                      *, expected: str | None = None) -> None:
    """Повернути попередній ТЕКСТОВИЙ вміст буфера через delay с (щоб ціль устигла
    забрати вставку). Порожній previous не відновлюємо (нема чого / був не-текст).
    Відновлюємо лише якщо буфер досі містить expected: нове користувацьке
    копіювання не затираємо. У процесі може бути лише один активний timer."""
    global _restore_timer, _restore_expected, _restore_delay
    global _active_restore_operations, _session_original
    generation = getattr(
        previous, "restore_generation", _restore_generation)
    while True:
        with _restore_condition:
            if generation != _restore_generation or _restore_blocked:
                return
            replaced = _restore_timer
        current = snapshot_clipboard() if replaced is not None else None
        with _restore_condition:
            if (replaced is not _restore_timer
                    or generation != _restore_generation
                    or _restore_blocked):
                if generation != _restore_generation or _restore_blocked:
                    return
                continue
            if _active_restore_operations:
                _active_restore_operations -= 1
            if _session_original is None:
                _session_original = previous
            original = _session_original
            if replaced is not None and current == _restore_expected:
                expected = _restore_expected
            if not original:
                _restore_timer = None
                _restore_expected = None
                if _active_restore_operations == 0:
                    _session_original = None
                timer = None
                break
            timer = _make_restore_timer(delay, generation)
            _restore_timer = timer
            _restore_expected = expected
            _restore_delay = delay
            break
    if (hasattr(_restore_operation_state, "generation")
            and _restore_operation_state.generation == generation):
        del _restore_operation_state.generation
    if replaced is not None:
        replaced.cancel()
    if timer is not None:
        timer.start()


def _make_restore_timer(delay: float, generation: int):
    timer = None

    def finish():
        _finish_clipboard_restore(timer, generation)

    timer = threading.Timer(delay, finish)
    return timer


def _finish_clipboard_restore(timer, generation: int) -> None:
    global _restore_timer, _restore_expected, _session_original
    global _restore_callbacks_in_flight
    with _restore_condition:
        if (timer is not _restore_timer
                or generation != _restore_generation
                or _restore_blocked):
            return
        expected = _restore_expected
    current = snapshot_clipboard() if expected is not None else None
    with _restore_condition:
        if (timer is not _restore_timer
                or generation != _restore_generation
                or _restore_blocked):
            return
        if _active_restore_operations:
            relay = _make_restore_timer(_restore_delay, generation)
            _restore_timer = relay
            _restore_expected = current if expected is not None else None
            original = None
        elif expected is not None and current != expected:
            _restore_timer = None
            _restore_expected = None
            _session_original = None
            relay = None
            original = None
        else:
            relay = None
            original = _session_original
            if original is not None:
                _restore_callbacks_in_flight += 1
    if relay is not None:
        relay.start()
        return
    if original is None:
        return
    try:
        if not _restore_copy_is_current(timer, generation):
            return
        copied = _safe_copy(original)
        retry = None
        with _restore_condition:
            if (timer is _restore_timer
                    and generation == _restore_generation
                    and not _restore_blocked):
                if copied:
                    _restore_timer = None
                    _restore_expected = None
                    _session_original = None
                    retry = None
                else:
                    retry = _make_restore_timer(_restore_delay, generation)
                    _restore_timer = retry
        if retry is not None:
            retry.start()
    finally:
        with _restore_condition:
            _restore_callbacks_in_flight -= 1
            _restore_condition.notify_all()


def _restore_copy_is_current(timer, generation: int) -> bool:
    """Остання generation-перевірка безпосередньо перед clipboard write."""
    with _restore_condition:
        return (timer is _restore_timer
                and generation == _restore_generation
                and not _restore_blocked)


def _safe_copy(text: str) -> bool:
    try:
        pyperclip.copy(text)
        return True
    except Exception:
        return False
