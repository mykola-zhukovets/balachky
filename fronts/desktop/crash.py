"""Логи та неперехоплені помилки: файл у %LOCALAPPDATA%\\Balachky\\logs + діалог.

setup_logging() вмикає RotatingFileHandler (5 МБ x 3 файли, utf-8) — викликати
найпершим у main(), щоб жоден стартовий збій не лишився без сліду.
install_excepthooks() перехоплює винятки GUI- та робочих потоків: traceback у
лог і (якщо є QApplication) діалог «Сталася помилка» з деталями.
Будь-яка помилка в самих хуках ковтається — обробник збоїв не має збоїти.
"""
import logging
import logging.handlers
import os
import sys
import threading
import traceback
from pathlib import Path

from whisper_core import __version__
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Balachky" / "logs"
LOG_FILE = LOG_DIR / "balachky.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 2
_EVENT_LOGGER = logging.getLogger("balachky.event")
_LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO, "WARNING": logging.WARNING}

# ВІЙСЬКОВИЙ КОНТЕКСТ / ПРИВАТНІСТЬ: у routine-лог НІКОЛИ не пишемо символів
# розшифровки, аудіо, вміст буфера обміну чи нотатки. diagnostic_event приймає
# лише коди станів, числа, булеві прапорці та короткі технічні enum-значення.
# Не передавайте сюди text/raw/final/transcript/audio/clipboard/note — вони
# свідомо відкидаються ще до форматування запису.
_PRIVATE_FIELDS = {"text", "raw", "final", "transcript", "audio", "clipboard", "note", "content", "message", "password", "token", "secret", "vaultkey"}


def diagnostic_event(code: str, *, level=logging.INFO, **fields) -> None:
    """Записати лише безпечну структуровану подію, без користувацького вмісту.

    Діагностика є суто best-effort: несправний logging handler або несподіване
    значення поля не мають права перервати основний робочий потік.
    """
    try:
        safe = []
        for key, value in fields.items():
            if key.lower() in _PRIVATE_FIELDS:
                continue
            if isinstance(value, bool): safe.append(f"{key}={str(value).lower()}")
            elif isinstance(value, (int, float)): safe.append(f"{key}={value}")
            elif key in {"mode", "destination", "state", "track", "tracks", "device", "model", "compute", "config_path", "reason", "status", "version", "level_name"} and isinstance(value, str): safe.append(f"{key}={value}")
        _EVENT_LOGGER.log(level, "event=%s%s", code, " " + " ".join(safe) if safe else "")
    except Exception:
        try:
            logging.getLogger(__name__).debug("Could not write diagnostic event",
                                              exc_info=True)
        except Exception:
            pass


def _file_handler(path):
    """RotatingFileHandler (1 МБ x 3 бекапи, utf-8) з пробою відкриття ЗАРАЗ.

    delay=True прибирає відкриття файлу з конструктора, а пробуємо відкрити
    самі й одразу: транзієнтний лок (антивірус, індексатор, інший екземпляр)
    має вистрілити тут, де setup_logging його перехопить і дасть фолбек.
    Інакше збій стався б у першому emit, де logging мовчки шле помилку в
    stderr — а у windowed-exe stderr=None, і сесія жила б узагалі без логів.
    """
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8", delay=True)
    handler.stream = handler._open()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
    return handler


def setup_logging():
    """Логи у файл: час РІВЕНЬ повідомлення. Помилка тут не валить застосунок."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fallback_error = None
        try:
            handler = _file_handler(LOG_FILE)
        except OSError as e:            # і PermissionError — його підклас
            # Основний лог зайнятий/недоступний → окремий файл цього процесу,
            # щоб сесія не лишилася без логів узагалі.
            fallback_error = e
            handler = _file_handler(LOG_DIR / f"balachky-{os.getpid()}.log")
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        if fallback_error is not None:
            logging.warning("Основний лог %s недоступний (%s) — пишу в %s",
                            LOG_FILE, fallback_error, handler.baseFilename)
        # балакучі бібліотеки (hf_hub шумить лише під час першої докачки моделі в
        # онбордингу; звичайний старт — офлайн, local_files_only) — лише важливе
        for noisy in ("httpx", "urllib3"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    except Exception:
        traceback.print_exc()   # без файлу логів усе одно стартуємо


#: останній застосований рівень — щоб log_level_changed не логувався двічі підряд
#: (старт кличе apply_log_level, а слідом apply_test_mode повторно застосовує той
#: самий рівень). Подія цікава лише коли рівень реально змінився.
_last_applied_level = None


def apply_log_level(value: str) -> str:
    global _last_applied_level
    normalized = str(value or "INFO").upper()
    normalized = normalized if normalized in _LEVELS else "INFO"
    logging.getLogger().setLevel(_LEVELS[normalized])
    if normalized != _last_applied_level:
        _last_applied_level = normalized
        diagnostic_event("log_level_changed", level=logging.WARNING, level_name=normalized)
    return normalized


# ─────────────────────── РЕЖИМ ТЕСТУВАННЯ (детальний журнал) ───────────────────────
# Дія-логер для живих тестів Миколи: кожен клік nav-кнопки, перемикання сторінки й
# КОЖЕН крок текстового конвеєра з таймінгом мс і ДОВЖИНАМИ текстів. Самі тексти
# розшифровок у журнал НЕ потрапляють, доки окремо не ввімкнено include_text
# (тоді журнал попереджено міститиме продиктований текст). Режим вмикає DEBUG на
# час сесії й НЕ персистить його: вимкнення повертає рівень за налаштуванням.
_TEST_MODE = False
_TEST_MODE_TEXT = False


def set_test_mode(enabled: bool, include_text: bool = False) -> None:
    """Увімкнути/вимкнути дія-логер. include_text діє лише при enabled."""
    global _TEST_MODE, _TEST_MODE_TEXT
    _TEST_MODE = bool(enabled)
    _TEST_MODE_TEXT = bool(enabled and include_text)


def test_mode_active() -> bool:
    return _TEST_MODE


def test_log(event: str, **fields) -> None:
    """Детальна дія-подія режиму тестування → balachky.event (рівень DEBUG).

    No-op, доки режим вимкнено. Ключі з префіксом ``text_`` логуються ДОВЖИНОЮ
    (`{key}_len=`); самі тексти йдуть у журнал лише коли ввімкнено include_text.
    Best-effort: жодна помилка тут не має права перервати робочий потік."""
    if not _TEST_MODE:
        return
    try:
        parts = []
        for key, value in fields.items():
            if key.startswith("text_"):
                s = "" if value is None else str(value)
                parts.append(f"{key}_len={len(s)}")
                if _TEST_MODE_TEXT:
                    parts.append(f"{key}={s!r}")
            elif isinstance(value, bool):
                parts.append(f"{key}={str(value).lower()}")
            else:
                parts.append(f"{key}={value}")
        _EVENT_LOGGER.debug("test event=%s%s", event,
                            (" " + " ".join(parts)) if parts else "")
    except Exception:
        try:
            logging.getLogger(__name__).debug("test_log failed", exc_info=True)
        except Exception:
            pass


def test_log_header() -> None:
    """Шапка тест-журналу: версія+коміт збірки і чи включено тексти."""
    if not _TEST_MODE:
        return
    try:
        from whisper_core import __version__
        from whisper_core._buildinfo import build_version
        test_log("test_mode_enabled", build=build_version(__version__),
                 include_text=_TEST_MODE_TEXT)
    except Exception:
        pass


def apply_test_mode(cfg) -> None:
    """Застосувати стан режиму з конфігу: увімкнено → DEBUG + шапка; вимкнено →
    повернути рівень за налаштуванням лог-рівня (DEBUG не персистить)."""
    enabled = bool(getattr(cfg, "test_mode", False))
    include = bool(getattr(cfg, "test_mode_include_text", False))
    set_test_mode(enabled, include)
    if enabled:
        global _last_applied_level
        logging.getLogger().setLevel(logging.DEBUG)
        _last_applied_level = "DEBUG"   # щоб вимкнення режиму знову лоґнуло зміну
        test_log_header()
    else:
        apply_log_level(getattr(cfg, "log_level", "INFO"))


def _safe_config_summary(cfg) -> str:
    fields = ("model_name", "device", "compute_type", "language", "ui_language", "ptt_mode", "live_transcription", "diarization_enabled", "meeting_screen_enabled", "meeting_sources", "log_level")
    return "\n".join(f"{name}={getattr(cfg, name, None)}" for name in fields)


def copy_diagnostics(cfg, *, lines: int = 80) -> str:
    """Безпечний текст для bug report: версія, whitelist конфігу, хвіст логу."""
    try: tail = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError: tail = []
    tail = [line for line in tail if ".vaultkey" not in line.lower() and not any(word in line.lower() for word in ("password=", "token=", "secret="))]
    return (f"Balachky {__version__}\nConfiguration (safe fields only):\n" + _safe_config_summary(cfg) + "\nRecent routine log:\n" + "\n".join(tail))


def open_log_dir():
    """Відкрити теку логів у Провіднику (кнопка в Налаштуваннях і в діалозі)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)
    except Exception as e:
        logging.error("Не вдалося відкрити теку логів: %s", e)


# --- діалог «Сталася помилка» ---
# Винятки робочих потоків не можуть малювати віджети самі: сигнал _Notifier
# доправляє текст у GUI-потік (queued connection), там і показуємо діалог.
_notifier = None
_dialog_open = False

_ISSUE_URL = "https://github.com/mykola-zhukovets/balachky/issues/new"


def _open_github_issue(details: str):
    """Відкрити форму нового issue на GitHub із безпечною заготовкою.

    Повні деталі кладемо лише в локальний буфер. У URL їх не передаємо: traceback
    може містити ім'я користувача та локальні шляхи, які GitHub отримав би вже під
    час відкриття сторінки. Користувач сам переглядає й вставляє безпечний фрагмент.
    """
    import urllib.parse
    from .i18n import tr
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QApplication.clipboard().setText(details)
        url = _ISSUE_URL + "?" + urllib.parse.urlencode(
            {"title": tr("crash_report_title"),
             "body": tr("crash_report_body")})
        QDesktopServices.openUrl(QUrl(url))
    except Exception:
        logging.exception("Не вдалося відкрити GitHub issue")


def _build_crash_dialog(details: str):
    """Побудувати діалог краху (без exec) — щоб тест міряв ширини кнопок.

    Кнопки — ВЕРТИКАЛЬНИЙ стек на всю ширину діалогу: 5 кнопок дій (+ авто-
    «Показати деталі» від QMessageBox) в один ряд не вміщались і різались
    («оказати детал», «рвати технічні»…). Стек гарантує, що кожен підпис
    читається повністю в обох мовах. Деталі (traceback) — у read-only полі,
    тож авто-кнопка «Показати деталі» більше не потрібна.

    → (dlg, [кнопки-дій], result) — result["action"] у {copy|logs|report|None}."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QStyle,
        QVBoxLayout,
    )
    from .i18n import tr
    dlg = QDialog()
    dlg.setWindowTitle(tr("app_title"))
    dlg.setMinimumWidth(460)
    root = QVBoxLayout(dlg)
    root.setSpacing(12)
    head = QHBoxLayout()
    head.setSpacing(12)
    icon = QLabel()
    icon.setPixmap(dlg.style().standardIcon(
        QStyle.SP_MessageBoxWarning).pixmap(32, 32))
    icon.setAlignment(Qt.AlignTop)
    head.addWidget(icon)
    title = QLabel(tr("crash_error_title"))
    title.setWordWrap(True)
    f = title.font(); f.setBold(True); title.setFont(f)
    head.addWidget(title, 1)
    root.addLayout(head)
    info = QLabel(tr("crash_error_info"))
    info.setWordWrap(True)
    root.addWidget(info)
    det = QPlainTextEdit(details)
    det.setReadOnly(True)
    det.setMaximumHeight(120)
    det.setAccessibleName(tr("crash_error_title"))
    root.addWidget(det)
    result = {"action": None}
    buttons = []

    def _act(key, action):
        b = QPushButton(tr(key))
        b.setAccessibleName(tr(key))
        b.clicked.connect(
            lambda _=False, a=action: (result.__setitem__("action", a),
                                       dlg.accept()))
        root.addWidget(b)
        buttons.append(b)
        return b

    _act("crash_copy", "copy")
    _act("crash_open_logs", "logs")
    _act("crash_report_github", "report")
    close_btn = QPushButton(tr("crash_close"))
    close_btn.setAccessibleName(tr("crash_close"))
    close_btn.clicked.connect(dlg.reject)
    root.addWidget(close_btn)
    buttons.append(close_btn)
    return dlg, buttons, result


def _show_dialog(details: str):
    global _dialog_open
    if _dialog_open:        # помилка сипеться в циклі — не плодити стос діалогів
        return
    try:
        _dialog_open = True
        from PySide6.QtWidgets import QApplication
        dlg, _buttons, result = _build_crash_dialog(details)
        dlg.exec()
        action = result["action"]
        if action == "copy":
            QApplication.clipboard().setText(details)
        elif action == "logs":
            open_log_dir()
        elif action == "report":
            _open_github_issue(details)
    except Exception:
        pass
    finally:
        _dialog_open = False


def _worker_cancellation_active():
    """Чи є зараз живий від'єднаний воркер докачки моделі. _active_workers
    непорожній РІВНО у вікні життя від'єднаного воркера (від _reap_worker до
    повернення run()) — саме коли стрей non-KeyboardInterrupt виняток із
    внутрішнього потоку hf/xet може вистрілити повз наш try/except. Вибрано
    set-вікно, а не скоуп за іменем потоку: імена внутрішніх hf-потоків нам
    непідконтрольні."""
    try:
        from .onboarding import _active_workers
        return bool(_active_workers)
    except Exception:
        return False


def install_excepthooks(app=None):
    """sys.excepthook + threading.excepthook → лог + діалог (якщо є GUI)."""
    global _notifier
    try:
        from PySide6.QtCore import QObject, Signal

        class _Notifier(QObject):
            crashed = Signal(str)

        _notifier = _Notifier(app)
        _notifier.crashed.connect(_show_dialog)
    except Exception:
        _notifier = None

    def _handle(exc_type, exc, tb):
        # Скасування докачки (onboarding._Cancelled — підклас KeyboardInterrupt)
        # може спливти на внутрішньому потоці huggingface_hub, поза нашим
        # try/except. Це штатне переривання, НЕ «неперехоплена помилка» —
        # мовчки ігноруємо, щоб не лякати користувача діалогом краху.
        if exc_type is SystemExit or (
                isinstance(exc_type, type)
                and issubclass(exc_type, KeyboardInterrupt)):
            return
        try:
            text = "".join(traceback.format_exception(exc_type, exc, tb))
            logging.error("Неперехоплена помилка:\n%s", text)
            try:
                sys.__excepthook__(exc_type, exc, tb)   # звичний вивід у консоль
            except Exception:
                pass
            # НЕ-KeyboardInterrupt виняток зі скасованої/від'єднаної докачки
            # (напр. xet ковтає _Cancelled → RuntimeError) міг спливти на
            # внутрішньому потоці hf повз ранній return. Лог лишаємо (нічого не
            # втрачаємо), але діалог краху НЕ піднімаємо — це штатне переривання.
            if _worker_cancellation_active():
                return
            from PySide6.QtWidgets import QApplication
            # саме QApplication (не QCoreApplication): без GUI діалог неможливий
            if isinstance(QApplication.instance(), QApplication) and _notifier:
                _notifier.crashed.emit(text)
        except Exception:
            pass    # виняток у самому хуку — ковтаємо

    def _thread_handle(args):
        if args.exc_type is SystemExit or issubclass(args.exc_type,
                                                     KeyboardInterrupt):
            return
        _handle(args.exc_type, args.exc_value, args.exc_traceback)

    sys.excepthook = _handle
    threading.excepthook = _thread_handle
