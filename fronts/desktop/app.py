"""Оркестратор desktop-фронту: hotkey → recorder → engine → paste → tray.

Етап 1: engine викликається прямо in-process у робочому потоці (без HTTP — то Етап 2).
Етап 3: словник та пам'ять беруться з активного профілю (whisper_core.profiles);
перемикання профілю/пам'яті — з трей-меню. Фраза завжди пишеться у профіль,
що був активним на момент диктування (знімок у on_release).
"""
import datetime
import errno
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tempfile
import traceback
from collections import OrderedDict
from contextlib import nullcontext
from copy import copy

import numpy as np
from pathlib import Path

# HF-кеш РЕАЛЬНИМИ файлами, не символічними лінками. Це має бути встановлено
# ДО імпорту whisper_core.engine -> faster_whisper -> huggingface_hub: constants
# читає змінну середовища один раз під час імпорту.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import (
    QFileSystemWatcher, QObject, QSettings, QThread, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QDesktopServices

from whisper_core import paths, profiles, updates, cuda_runtime, export
from whisper_core import recordings          # feature/player-recordings
from whisper_core import corpus               # feature/accuracy-corpus
from whisper_core import audioedit           # feature/audio-editor
from whisper_core import DISPLAY_VERSION, PEP440_VERSION
from whisper_core.config import Config
from whisper_core.live import LiveTranscriber
from whisper_core.engine import (
    Engine, ModelRevisionUnavailable, TranscriptionCancelled,
    NullEngine, ModelAbsentError,
    cuda_runtime_available, is_cuda_runtime_error,
)
from whisper_core.model_lifecycle import ModelLifecycle, LOADED, LOADING, UNLOADED
from whisper_core.terms import build_terms, read_terms_dict, merge_terms_data
from whisper_core import phrasebook           # feature/bilingual-memory
from whisper_core.punctuation import apply_voice_punctuation  # feature/voice-punctuation
from whisper_core.macros import load_macros, apply_macro, migrate_snippets  # feature/voice-macros
from whisper_core.fillers import apply_filler_cleanup  # feature/filler-cleanup
from whisper_core.config import cleanup_level_for_cfg  # feature/clean-mix
from whisper_core import processing                     # feature/processing-slider
from whisper_core.processing import policy_for_mode, DICTATION  # feature/processing-slider
from whisper_core.textformat import apply_format as apply_output_format  # feature/output-formats
from whisper_core import autocorrect, punctuator         # feature/punctuation-plus
from whisper_core.history import (
    log_history, read_recent, update_record, update_final_by_id)
from whisper_core import self_learning        # feature/selflearn-dict
from whisper_core.qol import (                          # feature/qol-pack
    UndoBuffer, AutostopMonitor, duration_status, sounds_muted_now,
    PasteHistory,                                       # feature/paste-safety
)

from . import i18n
from . import watch                    # feature/watch-folder: чисті хелпери спостереження
from . import context                  # feature/context-profiles: детект вікна + профілі
from .context import ContextResolver, SecurityGate, press_enter
from .i18n import tr
from .tray import Tray
from .hotkey import combos_equal
from . import hotkeys_native           # feature/native-hotkeys: RegisterHotKey-бекенд
from .recorder import (
    Recorder, MIC_TEST_SECONDS, classify_mic_level, play_audio,  # feature/audio-qol
)
from .paste import (
    paste_text, begin_clipboard_restore, end_clipboard_restore,
    cancel_clipboard_restore, panic_clear_clipboard,
    restore_clipboard, undo_paste, PASTE_BLOCKED,
    capture_selection,             # feature/voice-edit-selection: зчитати виділене
    send_nav,   # feature/office-voice-nav
    target_changed,                                     # feature/paste-safety
    target_changed_ex,                                  # feature/dictation-queue
)
from whisper_core import navcommands  # feature/office-voice-nav
from whisper_core.dictation_queue import DictationQueue  # feature/dictation-queue
from whisper_core.meeting.audit_log import (  # блокер Т56 + delete-barrier
    AuditLogCorrupt,
    AuditLogDeleted,
)
from .main_window import MainWindow, FileStatus, app_icon
from . import sounds
from .crash import diagnostic_event, apply_log_level, apply_test_mode, test_log, anonymize_path

# База профілів (dev — корінь репо, frozen — %LOCALAPPDATA%\Balachky);
# централізовано у whisper_core.paths, тут лише псевдонім для читабельності
ROOT = paths.profiles_root()

_UPDATE_INTERVAL = 7 * 24 * 3600     # не частіше ніж раз на тиждень
_UPDATE_CACHE_SCHEMA = 1             # 1: очистити тестовий кеш до першого релізу


def _within_root(target: Path, root: Path) -> bool:
    """target ФІЗИЧНО під root (realpath резолвить "..", симлінки, регістр
    тому) — той самий трирубіжний traversal-захист, що в whisper_core.recordings
    (RecordActionBar: rename/delete запису екрана)."""
    try:
        rt = os.path.realpath(target)
        rr = os.path.realpath(root)
        return rt != rr and os.path.commonpath([rt, rr]) == rr
    except (ValueError, OSError):
        return False


def _migrate_update_cache() -> None:
    """Одноразово прибрати тестовий update-кеш ранніх збірок 1.0.

    Міграція локальна й не залежить від мережі/opt-in. Саме в старому кеші
    лишились вигадані ``1.1.0`` та ``example.invalid``, тому 404 GitHub не треба
    помилково трактувати як доказ актуальності.
    """
    settings = QSettings("Balachky", "Balachky")
    schema = settings.value("update_cache_schema", 0, type=int)
    if schema >= _UPDATE_CACHE_SCHEMA:
        return
    for key in ("update_latest", "update_url", "update_etag", "update_last_check"):
        settings.remove(key)
    settings.setValue("update_cache_schema", _UPDATE_CACHE_SCHEMA)


def _should_show_onboarding(settings, config_exists, models_env, current_version=None):
    """Чи показувати майстер першого запуску (два незалежні сигнали + версія).

    Баг «чистої деінсталяції»: реєстровий прапорець ``onboarded`` міг пережити
    видалення програми (користувач не позначив «стерти дані»), тож сам по собі
    він — ненадійний доказ, що налаштування вже пройдене. Тому майстер показуємо,
    коли БУДЬ-ЯКИЙ сигнал каже «ще не налаштовано»:
      • прапорця ``onboarded`` у реєстрі немає,  АБО
      • конфіг-файла (config.toml) немає.

    Третій сигнал (скарга власника 25.07): версія, на якій майстер востаннє
    пройдено, відрізняється від поточної ``DISPLAY_VERSION`` — новий
    крок майстра (наприклад, «Додаткові можливості») інакше лишається
    невидимим для людини, що оновилась поверх наявної інсталяції. Порожнє чи
    відсутнє збережене значення теж трактуємо як «давня версія» (показати).
    ``current_version=None`` вимикає цю перевірку — старі виклики без
    четвертого аргументу поводяться так само, як і раніше.

    Dev-виняток: ``WHISPER_TYPER_MODELS`` означає «моделі вже у дев-кеші» —
    майстер не потрібен.
    """
    if models_env:
        return False
    onboarded = settings.value("onboarded") is not None
    if (not onboarded) or (not config_exists):
        return True
    if current_version:
        saved_version = settings.value("onboarded_version") or ""
        if saved_version != current_version:
            return True
    return False


def _is_onboarding_repeat(settings, config_exists) -> bool:
    """Показ майстра ЦЬОГО разу — повторний (після оновлення), а не перший
    запуск: ``onboarded`` уже стоїть і config.toml є, тож єдина причина, чому
    ``_should_show_onboarding`` повернув True — версійний сигнал. Повторний
    режим має інші тексти на вітальному кроці й передзаповнюється з наявного
    cfg (ГОЛОВНА ПАСТКА: без передзаповнення майстер стартує з хардкод-
    дефолтів і мовчки стирає налаштування людини на _apply_onboarding_result)."""
    return bool(config_exists) and settings.value("onboarded") is not None


def _build_onboarding_wizard(is_repeat: bool, prior_cfg):
    """Створити майстер першого запуску — те саме місце для обох гілок
    main(), щоб гілку «повторний показ» неможливо було випадково лишити без
    передзаповнення (ГОЛОВНА ПАСТКА завдання: непередзаповнений майстер стартує
    з хардкод-дефолтів і _apply_onboarding_result тихо стирає ними мову/хоткей/
    модель людини). ``prior_cfg`` обов'язковий, коли ``is_repeat=True``."""
    from .onboarding import FirstRunWizard
    if is_repeat:
        return FirstRunWizard(
            model_name=prior_cfg.model_name, model_dir=prior_cfg.model_dir,
            language=prior_cfg.ui_language or prior_cfg.language,
            ptt_key=prior_cfg.ptt_key, repeat=True)
    return FirstRunWizard()


def _apply_onboarding_result(cfg, wizard, settings, current_version=None):
    """Зберегти вибір майстра першого запуску і позначити «onboarded».

    Спільно для обох шляхів: звичайного (модель завантажена) і «Пропустити»
    (слабкий інтернет). Ключове для другого — налаштування зберігаються ТАК
    САМО, тож наступний запуск не починає майстер з нуля.

    Кожне поле пишемо в cfg ЛИШЕ коли значення майстра відрізняється від уже
    наявного — у повторному режимі майстер передзаповнений з cfg (main()), тож
    непорушений крок ніколи не затре чуже налаштування дефолтом майстра
    (ГОЛОВНА ПАСТКА завдання: оновлення не має стирати мову/хоткей/модель).

    ``current_version`` (`DISPLAY_VERSION`), якщо задано, зберігається
    поруч з «onboarded» — щоб наступний запуск ТІЄЇ САМОЇ версії не показував
    майстер знову (сигнал версії в _should_show_onboarding).

    Повертає True, коли завантаження моделі пропущено (рушій не стартувати).
    """
    if wizard.model_name != cfg.model_name:
        cfg.model_name = wizard.model_name
    if wizard.model_dir != cfg.model_dir:
        cfg.model_dir = wizard.model_dir
    if wizard.language != cfg.language:
        cfg.language = wizard.language
    if wizard.language != cfg.ui_language:
        cfg.ui_language = wizard.language  # мова інтерфейсу = та, що обрав користувач
    if wizard.ptt_key != cfg.ptt_key:
        cfg.ptt_key = wizard.ptt_key      # обрана (або стандартна) комбінація запису
    if getattr(wizard, "use_gpu", False):
        # користувач докачав прискорення в майстрі → стартуємо на GPU
        # (normalize_startup_config активує рантайм і задасть compute_type)
        cfg.device = "cuda"
        cfg.compute_type = "int8_float16"
    cfg.save()
    settings.setValue("onboarded", 1)
    if current_version:
        settings.setValue("onboarded_version", current_version)
    return bool(getattr(wizard, "model_skipped", False))


def _handle_onboarding_dismissed(wizard, settings, current_version):
    """Повторний майстер закрито БЕЗ проходу кроків (X/Esc/«Скасувати»/кнопка
    «Закрити» на вітальному кроці). Нічого в cfg не чіпаємо. Якщо людина
    позначила «більше не показувати» — запам'ятати поточну версію, щоб
    наступний запуск не пропонував майстер знову для НЕЇ; інакше версію не
    чіпаємо, і наступний старт цієї самої версії знову покаже майстер."""
    if getattr(wizard, "dont_show_again", False) and current_version:
        settings.setValue("onboarded_version", current_version)


def _paste_context(app):
    """feature/context-profiles, виклик (б): свіжий знімок вікна ПЕРЕД вставкою
    (фокус міг повернутись у ціль). → (behavior | None, blocked). Повністю
    getattr-захищений: контролери тестів без контекст-полів дають (None, False)
    — тобто стару поведінку без змін."""
    resolver = getattr(app, "_ctx_resolver", None)
    if resolver is None:
        return None, False
    try:
        ctx = resolver.get_window_context()
    except Exception:
        return None, False
    gate = getattr(app, "_ctx_gate", None)
    blocked = bool(gate.is_blocked(ctx.exe)) if gate is not None else False
    matcher = getattr(app, "_ctx_matcher", None)
    profile = matcher.match(ctx) if matcher is not None else None
    behavior = profile.behavior if profile is not None else None
    # test_log: рішення авто-профілю (яке правило збіглось за вікном). exe/profile —
    # імена процесу й профілю, НЕ текст розшифровки (безпечні для журналу).
    test_log("ctx_match", exe=ctx.exe or "-",
             profile=(profile.name if profile is not None else "-"),
             blocked=blocked,
             enabled=(behavior.enabled if behavior is not None else True),
             auto_enter=(behavior.auto_enter if behavior is not None else False))
    return behavior, blocked


def _snapshot_processing_mode(app) -> str:
    """feature/processing-slider: режим обробки активного профілю на старті запису.
    getattr-захищено — старі мок-контролери тестів без .profile дають DEFAULT."""
    prof = getattr(app, "profile", None)
    getter = getattr(prof, "processing_mode", None)
    if callable(getter):
        return getter(DICTATION)
    return processing.DEFAULT_MODE.value


def _token_diff_count(before: str, after: str) -> int:
    """Груба к-сть змінених токенів (для test_log автокорекції): позиційні
    розбіжності + різниця в кількості токенів."""
    a, b = (before or "").split(), (after or "").split()
    return sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))


def _deliver_paste(app, text, auto_enter, pinned_target=None, pinned_pid=None):
    """Доставити text у активне вікно наявним paste-шляхом: буфер+тости+auto_enter.

    Спільний код негайної вставки (_work) і вставки після перегляду (картка
    feature/paste-preview). ``app`` — DesktopApp або сумісний контролер (потрібні
    лише cfg і transcription_error). Знімок буфера робимо ДО paste_text (він
    затирає його своїм copy); відновлюємо ЛИШЕ при вдалій вставці — на провалі
    текст мусить лишитись у буфері (як обіцяє app_paste_failed).

    feature/paste-safety: pinned_target — (HWND, заголовок) вікна на момент старту
    диктування/запиту. Якщо ввімкнено «підтверджувати при зміні вікна» і активне
    вікно ЗАРАЗ інше — НЕ вставляємо наосліп (військовий кейс: краще не вставити,
    ніж вставити у чужий чат): текст лишаємо в буфері й повідомляємо, щоб вставити
    вручну Ctrl+V. pinned_target=None → ціль не закріплювали, гейт не діє.

    feature/dictation-queue (рекомендація рецензії): для джоба ЧЕРГИ (pinned_pid
    відомий → відкладена вставка) звірку вікна робимо ЗАВЖДИ, незалежно від
    тумблера. Між записом і вставкою з черги минає значно більше часу (фонова
    розшифровка попередніх фраз), тож фокус устигає піти в чуже вікно набагато
    ймовірніше — відкладеність робить захист ВАЖЛИВІШИМ, не менш. Негайне
    диктування (pinned_pid=None) — як раніше, суто за тумблером."""
    _queue_job = pinned_pid is not None
    _confirm = getattr(app.cfg, "paste_confirm_on_window_change", True)
    if pinned_target is not None and (_confirm or _queue_job):
        from . import wininput
        import pyperclip
        cur_hwnd, cur_title = wininput.capture_paste_target()
        # для відкладеної вставки (черга) звіряємо ще й PID — pinned_pid=None →
        # лишається стара звірка за HWND (негайний шлях).
        cur_pid = wininput.get_window_pid(cur_hwnd) if _queue_job else None
        if target_changed_ex(pinned_target[0], pinned_pid, cur_hwnd, cur_pid):
            cancel_clipboard_restore()
            pyperclip.copy(text)   # гарантуємо текст у буфері (paste_text не звали)
            logging.info("Диктування: активне вікно змінилось від старту — "
                         "вставку не роблю, текст лишається в буфері")
            app.transcription_error.emit(
                tr("app_paste_window_changed", window=(cur_title or "?")))
            return
    restore = app.cfg.restore_clipboard
    if restore:
        previous = begin_clipboard_restore()
    else:
        cancel_clipboard_restore()
        previous = ""
    typing = getattr(app.cfg, "paste_typing_fallback", False)
    owner_hwnd = getattr(app, "_clipboard_owner_hwnd", None)
    try:
        if owner_hwnd:
            result = paste_text(text, typing_fallback=typing, owner_hwnd=owner_hwnd)
        else:
            result = paste_text(text, typing_fallback=typing)
    except BaseException:
        if restore:
            end_clipboard_restore()
        raise
    if result == PASTE_BLOCKED:
        if restore:
            end_clipboard_restore()
        logging.info("Диктування: активне вікно — менеджер паролів; "
                     "текст лишився лише в буфері")
        app.transcription_error.emit(tr("app_paste_blocked"))
    elif result is None:
        if restore:
            end_clipboard_restore()
        logging.warning("Диктування: не вдалося вставити текст жодним зі "
                        "способів — лишається в буфері")
        app.transcription_error.emit(tr("app_paste_failed"))
    else:
        if restore:
            restore_clipboard(previous, expected=text)
        # feature/qol-pack: запам'ятати вставку (скасувати/повторити) + опційний
        # звук підтвердження — ПІСЛЯ фактичної вставки. Тут спільна точка для
        # негайного шляху й вставки після перегляду (feature/paste-preview), тож
        # у буфер undo лягає саме доставлений (можливо, відредагований у картці)
        # текст. getattr — сумісність зі старими мок-контролерами тестів.
        undo = getattr(app, "_undo", None)
        if undo is not None:
            undo.record(text)
        history = getattr(app, "_paste_history", None)   # feature/paste-safety
        if history is not None and getattr(app.cfg, "paste_history_enabled", True):
            history.record(text)   # тумблер off → буфер вставок не поповнюємо
        if getattr(app.cfg, "paste_confirm_sound", False):
            _play_chime(app.cfg, "paste")
        if auto_enter:                  # Enter лише ПІСЛЯ вдалої вставки
            press_enter()


def _deliver_nav(app, action):
    """feature/office-voice-nav: доставити навігаційну дію у закріплену ціль і
    повідомити користувача про блок/збій. Ціль (target_hwnd) — вікно старту
    диктанту; send_nav звірить, що фокус лишився там (pinned-target). ``app`` —
    DesktopApp або сумісний контролер (потрібні лише cfg і transcription_error).
    → результат send_nav (NAV_OK | PASTE_BLOCKED | None)."""
    target = getattr(app, "_nav_target_hwnd", None)
    result = send_nav(action, target_hwnd=target)
    if result == PASTE_BLOCKED:
        app.transcription_error.emit(tr("nav_blocked"))
    elif result is None:
        app.transcription_error.emit(tr("nav_failed"))
    return result


# --- feature/dictation-queue (запит Миколи №10): гейти черги ------------------
# Модульні функції (не методи), беруть app і getattr-захищені — щоб гейти
# лишались викликабельними на легких стабах тестів (як _paste_context/_deliver_*).
def _queue_enabled(app) -> bool:
    return bool(getattr(getattr(app, "cfg", None), "dictation_queue_enabled", True))


def _should_queue(app) -> bool:
    """Чи ставити нову фразу в чергу. Черга має існувати (реальний контролер);
    v1 НЕ діє в режимах перегляду перед вставкою / заповнення полів / голосової
    навігації — там кожна фраза інтерактивна (стара блокувальна поведінка)."""
    if getattr(app, "_queue", None) is None:
        return False
    if not _queue_enabled(app):
        return False
    cfg = getattr(app, "cfg", None)
    if getattr(cfg, "paste_preview", False):
        return False
    if getattr(app, "formfill_capturing", False):
        return False
    if getattr(cfg, "voice_nav_enabled", False):
        return False
    return True


def _is_queue_full(app) -> bool:
    q = getattr(app, "_queue", None)
    return bool(_queue_enabled(app) and q is not None and q.is_full())


def _dictation_start_blocked(app) -> bool:
    """Чи блокувати старт НОВОГО запису (спільно клавіша+кнопка). У режимі черги
    фонова розшифровка НЕ блокує — лише активний запис або повна черга. Поза
    чергою (або поки в польоті стара блокувальна фраза) — прапорець _busy."""
    if getattr(app.recorder, "recording", False) or getattr(app, "_busy", False):
        return True
    q = getattr(app, "_queue", None)
    if _queue_enabled(app) and q is not None:
        return q.is_full()
    return False


def _pipeline_busy(app) -> bool:
    """Диктування зайняте: стара блокувальна фраза (_busy) АБО в черзі є фраза
    (активна/очікує). Нарада/нотатка/диктофон/тест-мік не стартують, поки триває
    будь-яка розшифровка (спільні мікрофон і модель)."""
    if getattr(app, "_busy", False):
        return True
    q = getattr(app, "_queue", None)
    return bool(q is not None and q.busy())


def _queue_full_toast(app):
    now = time.time()
    if now - getattr(app, "_queue_full_toast_ts", 0.0) > 3.0:
        app._queue_full_toast_ts = now
        app.tray.notify(tr("dictation_queue_full"))


def _finalize_meeting_status(sess, session_dir, status):
    """Підсумковий статус сесії наради НА ДИСКУ (done/error) [integration wave-2].

    MeetingSession.finalize (Б1) ідемпотентна: після meeting_stop вона вже
    викликана зі 'stopped' і другий виклик статусу НЕ міняє — тож підсумок
    пишемо session.finalize_dir (Б1 раунд 2, працює за текою). Живий
    sess.finalize(status) лишається першим кроком: закриває файли, якщо стопу
    не було, і є єдиним шляхом на старому ядрі без finalize_dir (guard).
    Повертає свіжу meta (або стару/None на старому ядрі)."""
    from whisper_core.meeting import session as msession
    meta = None
    if sess is not None:
        meta = sess.finalize(status)
    finalize_dir = getattr(msession, "finalize_dir", None)
    if callable(finalize_dir):
        fresh = finalize_dir(session_dir, status)
        if fresh is not None:
            meta = fresh
    return meta


def _secure_meeting_finish(app, sess, session_dir, status):
    """Finalize then seal every durable artifact when encryption is enabled."""
    from whisper_core.meeting import session as msession
    meta = _finalize_meeting_status(sess, session_dir, status)
    if not getattr(app.cfg, "meeting_encrypt", False):
        return meta
    from whisper_core.meeting.storage_crypto import VaultPasswordRequired, ensure_dek
    try:
        msession.encrypt_session(session_dir, ensure_dek(app._meetings_root()), status=status)
        return msession.load_meta(session_dir)
    except VaultPasswordRequired:
        msession.mark_encryption_pending(session_dir, status)
        logging.warning("Meeting vault is locked; encryption is queued for %s",
                        anonymize_path(session_dir))
        raise
    except Exception:
        # Never trade confidentiality failure for silent data loss. encrypt_session
        # is resumable and leaves its marker plus the last verified source files.
        logging.exception("Meeting encryption failed; resumable data retained at %s",
                          anonymize_path(session_dir))
        raise


def _resume_meeting_encryption(app):
    from whisper_core.meeting import session as msession
    root = app._meetings_root()
    try:
        if getattr(app.cfg, "meeting_encrypt", False):
            msession.queue_plaintext_encryption(root)
        count = msession.resume_encryption(root)
        if count:
            logging.info("Resumed encryption for %d meeting(s)", count)
    except Exception:
        logging.warning("Pending meeting encryption could not resume; vault may be locked")
    pending = msession.count_pending_encryption(
        root, include_plaintext=bool(getattr(app.cfg, "meeting_encrypt", False)))
    app._meeting_plaintext_count = pending
    signal = getattr(app, "meeting_plaintext_pending", None)
    if signal is not None:
        signal.emit(pending)
    if pending:
        app.tray.notify(tr("meeting_pending_plaintext", count=pending))


def _cleanup_stale_meeting_temps(max_age_seconds=3600):
    """Best-effort removal of plaintext media/worker dirs left by a hard crash."""
    import shutil
    now = time.time()
    root = Path(tempfile.gettempdir())
    for prefix in ("balachky-meeting-", "balachky-meeting-media-"):
        for path in root.glob(prefix + "*"):
            try:
                if path.is_dir() and now - path.stat().st_mtime >= max_age_seconds:
                    shutil.rmtree(path)
            except OSError:
                logging.warning("Could not remove stale meeting plaintext temp: %s",
                                anonymize_path(path))


def _cleanup_panic_plaintext_temps() -> bool:
    """Remove all known plaintext temp artifacts and report whether all disappeared."""
    import shutil

    root = Path(tempfile.gettempdir())
    success = True
    for prefix in (
            "balachky-meeting-",
            "balachky-meeting-media-",
            "balachky-tts-plain-"):
        try:
            targets = tuple(root.glob(prefix + "*"))
        except OSError:
            logging.exception("Could not enumerate panic plaintext temps for %s", prefix)
            success = False
            continue
        for path in targets:
            try:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError:
                logging.warning("Could not remove panic plaintext temp: %s",
                                anonymize_path(path))
                success = False
                continue
            try:
                still_exists = path.exists()
            except OSError:
                still_exists = True
            if still_exists:
                success = False
    return success


# Попередження про втрату події журналу цілісності показуємо ОДИН раз на нараду
# для кожної причини наявним механізмом тостів. Нотифікатор реєструє DesktopApp
# у __init__ (tray.notify); _audit_event — модульна функція без self, тож бере
# його звідси, а не з екземпляра. Історична назва лишається для сумісності тестів.
_audit_corrupt_notifier = None
_audit_corrupt_warned_sessions: set[str] = set()
_audit_deleted_warned_sessions: set[str] = set()
_audit_timeout_warned_sessions: set[str] = set()
_audit_unavailable_warned_sessions: set[str] = set()
_AUDIT_DESKTOP_LOCK_TIMEOUT_SECONDS = 1.0


def _set_audit_corrupt_notifier(notify) -> None:
    """DesktopApp реєструє свій tray.notify — щоб модульний _audit_event міг
    показати тост про недописаний журнал (він не має доступу до self)."""
    global _audit_corrupt_notifier
    _audit_corrupt_notifier = notify


def _warn_audit_corrupt(session_dir_or_id=None) -> None:
    """Блокер Т56: чесно попередити, що журнал цілісності наради пошкоджено й нові
    події більше не записуються (доказовий пакет покаже BROKEN). Попередження (тост)
    показується один раз НА КОЖНУ нараду; logging.warning — завжди (без обмеження)."""
    session_key = Path(session_dir_or_id).name if session_dir_or_id else "unknown"
    logging.warning(
        "Журнал цілісності наради %s пошкоджено — нові події більше не записуються",
        session_key)
    if session_key in _audit_corrupt_warned_sessions:
        return
    _audit_corrupt_warned_sessions.add(session_key)
    notify = _audit_corrupt_notifier
    if notify is not None:
        try:
            notify(tr("meeting_audit_corrupt_warn"))
        except Exception:
            logging.exception("Не вдалося показати попередження про пошкоджений журнал")


def _warn_audit_deleted(session_dir_or_id=None) -> None:
    """Чесно попередити про відмову append через tombstone видаленої наради."""
    session_key = Path(session_dir_or_id).name if session_dir_or_id else "unknown"
    logging.warning(
        "Подію журналу цілісності наради %s не дописано: "
        "нараду вже позначено видаленою",
        session_key,
    )
    if session_key in _audit_deleted_warned_sessions:
        return
    _audit_deleted_warned_sessions.add(session_key)
    notify = _audit_corrupt_notifier
    if notify is not None:
        try:
            notify(tr("meeting_audit_deleted_warn"))
        except Exception:
            logging.exception(
                "Не вдалося показати попередження про видалений журнал")


def _warn_audit_timeout(session_dir_or_id=None) -> None:
    """Чесно попередити про втрачену подію через зайнятий журнал."""
    session_key = Path(session_dir_or_id).name if session_dir_or_id else "unknown"
    logging.warning(
        "Подію журналу цілісності наради %s не дописано: журнал зайнятий",
        session_key,
    )
    if session_key in _audit_timeout_warned_sessions:
        return
    _audit_timeout_warned_sessions.add(session_key)
    notify = _audit_corrupt_notifier
    if notify is not None:
        try:
            notify(tr("meeting_audit_timeout_warn"))
        except Exception:
            logging.exception(
                "Не вдалося показати попередження про зайнятий журнал")


def _warn_audit_unavailable(session_dir_or_id, error: OSError) -> None:
    """Чесно попередити про втрачену подію через недоступний audit-файл."""
    session_key = Path(session_dir_or_id).name if session_dir_or_id else "unknown"
    logging.warning(
        "Подію журналу цілісності наради %s не дописано: журнал недоступний: %s",
        session_key,
        error,
        exc_info=True,
    )
    if session_key in _audit_unavailable_warned_sessions:
        return
    _audit_unavailable_warned_sessions.add(session_key)
    notify = _audit_corrupt_notifier
    if notify is not None:
        try:
            notify(tr("meeting_audit_unavailable_warn"))
        except Exception:
            logging.exception(
                "Не вдалося показати попередження про недоступний журнал")


def _cleanup_stale_tts_temps() -> int:
    """feature/tts-listen (§8.9): crash-recovery plaintext-аудіо озвучення на старті.
    На старті активної озвучки НЕМАЄ, тож прибираємо ВСІ balachky-tts-plain-* (age=0)
    — не лишаємо свіжий crash-temp конфіденційного аудіо на годину (рецензія).
    ЄДИНЕ джерело логіки — plaintext_temp.cleanup_stale."""
    from whisper_core.tts import plaintext_temp
    return plaintext_temp.cleanup_stale(max_age_seconds=0)


def _audit_event(session_dir, event_type: str, **kwargs) -> None:
    """feature/chain-of-custody: дописати подію у журнал цілісності наради, ніколи
    не валячи основний потік. Журнал — допоміжна гарантія: його збій (тека лише для
    читання, повний диск) не має зривати запис/експорт/фіналізацію наради.

    Пошкодження журналу (AuditLogCorrupt) ловимо ОКРЕМО від широкого except:
    мовчки ковтати його не можна — інакше всі наступні події наради тихо
    зникають (блокер Т56). Показуємо чесне попередження раз на нараду."""
    try:
        from whisper_core.meeting import audit_log
        audit_log.append_event(
            session_dir,
            event_type,
            lock_timeout=_AUDIT_DESKTOP_LOCK_TIMEOUT_SECONDS,
            **kwargs,
        )
    except AuditLogDeleted:
        _warn_audit_deleted(session_dir)
    except AuditLogCorrupt:
        _warn_audit_corrupt(session_dir)
    except TimeoutError:
        _warn_audit_timeout(session_dir)
    except OSError as exc:
        _warn_audit_unavailable(session_dir, exc)
    except Exception:
        logging.exception("Не вдалося дописати подію журналу цілісності: %s", event_type)


def _play_chime(cfg, kind: str) -> None:
    """feature/qol-pack: відтворити сигнал, якщо відповідний звук ввімкнено й зараз
    не «тихі години»."""
    if kind not in sounds._TONES:
        return
    if kind == "paste" and not getattr(cfg, "paste_confirm_sound", True):
        return
    now = datetime.datetime.now()
    if sounds_muted_now(cfg, now.hour * 60 + now.minute):
        return
    sounds.play(kind)


def _profile_protected_words(terms) -> frozenset:
    """feature/punctuation-plus: слова з активного профілю користувача, які
    автокорекція НЕ має «виправляти» (рідкісні власні назви/терміни, яких немає
    в частотному словнику). Джерело — Terms: канони (біасинг-hotwords) і почуті
    варіанти замін. Кожен канон/варіант розбиваємо на окремі слова в нижньому
    регістрі. Функція (не метод) — щоб мок-контролери тестів працювали."""
    words = set()
    if terms is None:
        return frozenset()
    for chunk in (getattr(terms, "hotwords", "") or "").split(","):
        words.update(w for w in chunk.lower().split() if w)
    for variant in getattr(terms, "variant_map", {}) or {}:
        words.update(w for w in variant.lower().split() if w)
    return frozenset(words)


def _apply_capture_dsp(cfg, audio):
    """feature/audio-center: опційні gate → AGC перед розпізнаванням (обидва
    вимкнені за замовчуванням). Лише «немодельний» DSP: гейт ріже тишу/шум, AGC
    масштабує гучність — мовний сигнал не спотворюється (RESEARCH §2, тому НЕ
    додаємо сюди ML-шумозаглушення). Застосовується ТІЛЬКИ до диктування; файли
    черги — зовнішні записи, нарада має власний пайплайн, тож там не чіпаємо.
    getattr — сумісність зі старими мок-конфігами тестів (усі прапорці → off)."""
    if audio is None or cfg is None:
        return audio
    from whisper_core import audiodsp
    if getattr(cfg, "noise_gate_enabled", False):
        audio = audiodsp.noise_gate(
            audio, cfg.sample_rate,
            getattr(cfg, "noise_gate_threshold_db",
                    audiodsp.NOISE_GATE_THRESHOLD_DB_DEFAULT))
    if getattr(cfg, "agc_enabled", False):
        audio = audiodsp.agc(
            audio, getattr(cfg, "agc_target_db", audiodsp.AGC_TARGET_DB_DEFAULT))
    return audio


def _prepare_cpu_config(cfg):
    """Нормалізувати конфіг для гарантовано підтримуваного CPU-режиму."""
    cfg.device = "cpu"
    # float16/int8_float16 придатні для CUDA, але не для CPU CTranslate2.
    cfg.compute_type = "int8"
    return cfg


def _normalize_startup_config(cfg, *, frozen=None, cuda_available=None) -> bool:
    """Зберегти безпечний startup-конфіг; повернути ознаку CUDA-fallback.

    feature/gpu: «frozen → завжди CPU» знято. device=cuda дозволяється, коли
    рантайм справді доступний (докачаний cuBLAS або системний) — тоді активуємо
    його ДО створення рушія й ставимо дефолтний для GPU compute_type
    int8_float16 (пік VRAM удвічі менший за float16). Інакше — тихий відкат на
    cpu/int8, як і раніше."""
    if frozen is None:
        frozen = paths.FROZEN

    if cfg.device == "cuda":
        if cuda_available is None:
            cuda_available = cuda_runtime_available()
        if not cuda_available:
            logging.warning("CUDA runtime неповний — автоматично перемикаюся на CPU")
            _prepare_cpu_config(cfg)
            cfg.save()
            return True
        # рантайм є: активувати (PATH + preload DLL) ДО створення Engine(cuda)
        cuda_runtime.activate()
        if cfg.compute_type not in ("int8", "int8_float16", "float16"):
            # порожній/несумісний → дефолт для GPU. int8 тепер — валідний ЯВНИЙ
            # вибір (Налаштування→Точність, «Економна»): зберігаємо як є, не
            # підвищуємо до int8_float16.
            cfg.compute_type = "int8_float16"
            cfg.save()
    elif cfg.device == "cpu" and cfg.compute_type != "int8":
        # Попередня версія могла зберегти CPU разом із CUDA-only float16.
        logging.warning("CPU-конфіг має несумісний compute type — нормалізую до int8")
        _prepare_cpu_config(cfg)
        cfg.save()

    return False


def _refresh_frozen_autostart() -> bool:
    """Best-effort міграція старого BAT; помилка кодування не блокує запуск."""
    if not paths.FROZEN:
        return False
    try:
        from .autostart import refresh_if_enabled
        return refresh_if_enabled()
    except (OSError, UnicodeError):
        logging.exception("Не вдалося оновити файл автозапуску")
        return False


class _UpdateThread(QThread):
    """Фонова перевірка оновлень: мережа поза GUI-потоком. Результат — сигналом
    у GUI (QueuedConnection), сама нитка QSettings не чіпає."""
    result = Signal(object)          # updates.UpdateResult

    def __init__(self, current_version, etag, parent=None):
        super().__init__(parent)
        self._version = current_version
        self._etag = etag

    def run(self):
        self.result.emit(updates.check_latest(self._version, self._etag))


class _DownloadThread(QThread):
    """Фонове завантаження інсталятора (feature/auto-update): мережа й перевірка
    SHA-256 поза GUI-потоком. Прогрес/результат — сигналами у GUI. Ядро
    (whisper_core.updater) без Qt; тут лише обгортка-нитка."""
    progress = Signal(int, int)      # завантажено, усього (-1 якщо невідомо)
    done = Signal(str)               # шлях до готового інсталятора
    failed = Signal(str)             # текст помилки (SHA/мережа/https)

    def __init__(self, url, sha256, parent=None):
        super().__init__(parent)
        self._url = url
        self._sha = sha256

    def run(self):
        from whisper_core import updater
        try:
            path = updater.download_installer(
                self._url, self._sha,
                progress=lambda d, t: self.progress.emit(d, t if t else -1),
                should_cancel=self.isInterruptionRequested)
            self.done.emit(str(path))
        except updater.UpdateError as e:
            self.failed.emit(str(e))


class _EngineLoadThread(QThread):
    """Фонове завантаження рушія (~3 ГБ) поза GUI-потоком, щоб splash рухався.

    Результат — РІВНО одним сигналом у GUI (QueuedConnection). КРИТИЧНО: гілку
    ModelRevisionUnavailable НЕ обробляємо тут (RecoveryDialog — модальний
    GUI-обʼєкт, у робочому потоці його створювати не можна) — лише емітимо
    needs_recovery, а само-лікування+діалог лишаються на GUI-потоці (main()).

    КРИТИЧНО (регресія hang): БУДЬ-ЯКИЙ інший виняток рушія (сирий RuntimeError/
    OSError з ctranslate2 — CUDA OOM, несумісний compute_type, DLL/драйвер,
    «Unable to open file model.bin») МУСИТЬ емітнути failed. Інакше run() тихо
    вмирає без сигналу → петля очікування в main() зависає ВІЧНО, splash не
    гасне, застосунок не виходить. На master сирий виняток давав краш-діалог +
    вихід — тут failed веде до того самого (фатальний вихід, не зависання)."""
    ready = Signal(object)            # whisper_core.engine.Engine
    needs_recovery = Signal(object)   # ModelRevisionUnavailable
    failed = Signal(object)           # будь-який інший виняток → фатальний вихід

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        started = time.perf_counter()
        try:
            engine = Engine(self._cfg)          # блокує ~кілька сек (модель)
        except ModelRevisionUnavailable as e:
            self.needs_recovery.emit(e)
        except Exception as e:                  # сирий виняток → НЕ мовчазний hang
            self.failed.emit(e)
        else:
            cfg = _diagnostic_attr(self, "_cfg", None)
            diagnostic_event("model_loaded", model=_diagnostic_attr(cfg, "model_name"),
                             device=_diagnostic_attr(cfg, "device"),
                             compute=_diagnostic_attr(cfg, "compute_type"),
                             duration_s=round(time.perf_counter() - started, 3))
            self.ready.emit(engine)


_SPLASH_MIN_MS = 800    # мінімальний час показу заставки (щоб теплий кеш не блимав)


def _splash_min_remaining(elapsed_ms, min_ms=_SPLASH_MIN_MS):
    """Скільки ще мс тримати splash до мінімального часу показу (0 — вже досить).
    Padding, НЕ sleep: ніколи не повертає більше за (min_ms − elapsed), тож
    затримки понад реальне завантаження не буває."""
    return max(0, min_ms - int(elapsed_ms))


def _diagnostic_attr(obj, name, default="unknown"):
    """Best-effort поле для routine-логу; часткові стаби не є контрактом UI."""
    try:
        value = getattr(obj, name, default)
    except Exception:
        return default
    return default if value is None else value


def _diagnostic_elapsed(started, *, clock=time.time):
    """Тривалість для логу або None, коли початковий час недоступний."""
    try:
        if not isinstance(started, (int, float)):
            return None
        return round(max(0.0, clock() - started), 3)
    except Exception:
        return None


def _meeting_diagnostic_fields(sess):
    """Мінімальні поля stop/cancel без залежності від внутрішньої meta сесії."""
    try:
        meta = _diagnostic_attr(sess, "_meta", None)
        sources = _diagnostic_attr(meta, "sources", ()) or ()
        tracks = "+".join(str(source) for source in sources) or "unknown"
        return {
            "tracks": tracks,
            "duration_s": _diagnostic_elapsed(
                _diagnostic_attr(meta, "created", None)),
        }
    except Exception:
        return {}


def _fatal_engine_error(err):
    """Рушій кинув сирий виняток (не ModelRevisionUnavailable) АБО потік
    завершився без результату: чесний фатальний вихід — лог + діалог краху на
    GUI-потоці + sys.exit(1), як на master. НЕ мовчазне зависання splash."""
    if err is not None:
        text = "".join(traceback.format_exception(
            type(err), err, err.__traceback__))
    else:
        text = "Потік завантаження рушія завершився без результату."
    logging.error("Рушій не завантажився — фатальний вихід:\n%s", text)
    try:
        from .crash import _show_dialog
        _show_dialog(text)      # QMessageBox «Сталася помилка» (GUI-потік)
    except Exception:
        pass                    # обробник збоїв не має збоїти
    sys.exit(1)


def _recover_engine_on_gui(cfg, err, splash=None):
    """GUI-потік: само-лікування symlink-ів HF-кешу + цикл RecoveryDialog.

    Викликається, коли _EngineLoadThread не зміг завантажити піновану ревізію.
    Дослівно перенесено з колишнього DesktopApp.__init__ — RecoveryDialog
    (модальний GUI-обʼєкт) будується ЛИШЕ тут, на GUI-потоці. Повертає готовий
    Engine, а при свідомій відмові користувача — NullEngine (feature/no-model-
    state: застосунок стартує без мовного пакета замість sys.exit(0))."""
    from .recovery import RecoveryDialog
    from whisper_core.models import (
        resolve_model_state, resolve_cache_dir, repo_for,
        dereference_snapshot, ABSENT,
    )
    engine = None
    # САМО-ЛІКУВАННЯ ДО діалогу: модель Є на диску (детекція каже не ABSENT),
    # але не завантажилась — типова причина на встановленому (без підпису) exe:
    # файли HF-кешу є символьними лінками, за якими Windows не дає ходити
    # (WinError 448 «untrusted mount point») → ctranslate2 «Unable to open file
    # model.bin». Замінюємо лінки реальними копіями (без мережі, без діалогу) й
    # пробуємо завантажити ще раз.
    state = resolve_model_state(cfg)
    if state.state != ABSENT:
        if splash is not None:
            splash.set_status(tr("app_preparing_model"))
            QApplication.processEvents()   # показати «Готую модель…» ДО блокуючого дереференсу
        healed = False
        try:
            healed = dereference_snapshot(
                resolve_cache_dir(cfg.model_dir),
                repo_for(cfg.model_name), state.revision)
        except Exception:
            # само-лікування НІКОЛИ не має валити старт — падаємо у діалог
            logging.exception("Само-лікування моделі (дереференс) впало")
        if healed:
            try:
                engine = Engine(cfg, revision_override=state.revision)
                logging.info("Модель само-полагоджено (дереференс лінків "
                             "HF-кешу) — старт без вікна відновлення")
            except ModelRevisionUnavailable as retry_err:
                err = retry_err            # не допомогло — у звичайне відновлення
    while engine is None:
        dlg = RecoveryDialog(cfg, err)
        if not dlg.exec():                 # відмовився — стартуємо без мовного пакета
            logging.info("Відновлення моделі скасовано — старт без мовного пакета")
            return NullEngine(cfg)
        try:
            engine = Engine(cfg, revision_override=dlg.revision_override)
        except ModelRevisionUnavailable as retry_err:
            err = retry_err
            logging.warning("Модель усе ще недоступна — повтор відновлення")
    return engine


# Редакція транскрипту пишеться В ОКРЕМИЙ файл із цим стемом — оригінальні
# transcript.txt/transcript.json сесії лишаються байт-у-байт незмінними
# (доказовість: недоторканні і аудіо, і текст оригіналу).
REDACTED_TRANSCRIPT_STEM = "transcript-redacted"

# feature/dictation-queue: сентинел «ціль вставки не передавали» — щоб відрізнити
# «явно None» від «беремо self._paste_target» у _work (стара блокувальна поведінка).
_PASTE_TARGET_UNSET = object()


def _aggregate_screen_protection_state(results, *, supported: bool) -> str:
    """Звести фактичні результати живих вікон до одного чесного UI-стану."""
    results = tuple(results)
    if not supported or any(
            not getattr(result, "supported", False) for result in results):
        return "unsupported"
    if results and all(
            getattr(result, "succeeded", False) for result in results):
        return "active"
    return "failed"


class DesktopApp(QObject):
    finished = Signal()               # транскрипція завершена (робочий → GUI-потік)
    transcribed = Signal(str, str, object, object)  # (raw, final, words, ts) → стрічка у вікні
    formfill_text = Signal(str)       # feature/voice-form-fill: диктант → активне поле шаблону
    file_status = Signal(int, str)    # (job_id, статус) → вкладка Файли
    file_done = Signal(int, str, str, object, object)  # (job_id, текст, мета,
                                      # сегменти, words). сегменти:
                                      # [(start,end,text),...] або [] — для експорту
                                      # в субтитри/docx на картці. words:
                                      # [(слово,ймовірність),...] або [] —
                                      # feature/model-bottlenecks (під-хвиля 2):
                                      # підсвітка непевних слів у картці файлу
    key_captured = Signal(str)        # нова клавіша запису (застосована; оновити напис)
    note_key_captured = Signal(str)   # feature/scratchpad-note: нова (чи знята "")
    meeting_bookmark_key_captured = Signal(str)
                                      # комбінація нотатки → оновити напис у Налаштуваннях
    command_edit_key_captured = Signal(str)  # feature/voice-edit-selection: нова
                                      # (чи знята "") комбінація Command Mode → Налаштування
    panic_lock_key_captured = Signal(str)  # feature/mil-hardening: нова (чи знята "")
                                      # комбінація panic-lock → напис у Налаштуваннях
    screen_protection_state_changed = Signal(str)  # active|unsupported|failed|""
                                      # фактичний результат, не cfg-намір
    rec_state = Signal(str)           # recording | busy | loading | idle → UI
                                      # (синхронно з треєм: і PTT, і кнопки вікна)
    model_lifecycle_state = Signal(str)  # loading | loaded | error (worker → GUI)
    update_result = Signal(object)    # updates.UpdateResult → рядок у Налаштуваннях
    download_progress = Signal(int, int)  # feature/auto-update: завантажено, усього
    update_downloaded = Signal(str)       # feature/auto-update: інсталятор готовий (шлях)
    download_failed = Signal(str)         # feature/auto-update: помилка завантаження
    transcription_error = Signal(str) # (message) → повідомлення про помилку в трей
    cpu_fallback = Signal()            # worker → GUI: зберегти CPU-конфіг і синхронізувати UI
    watch_ready = Signal(str)          # feature/watch-folder: файл дописано (worker → GUI)
    mic_test_result = Signal(str)      # feature/audio-qol: вердикт тесту мікрофона
    live_dictation_segment = Signal(object)  # LiveSegment → GUI
    live_meeting_segment = Signal(object)    # LiveSegment → GUI
    live_error = Signal(str)                  # нефатальний toast
    live_disable_requested = Signal(object)   # worker → GUI: вимкнути конфіг і active live
                                       # ('good'|'quiet'|'silence'|'error') → Налаштування
    dictation_audio_state = Signal(str)  # reconnecting | reconnected | fallback | failed
    undo_requested = Signal()          # feature/qol-pack: хоткей «скасувати вставку»
                                       # (з keyboard-потоку → GUI, QueuedConnection)
    insert_requested = Signal()        # feature/qol-pack: хоткей «вставити ще раз»
    preview_ready = Signal(str, bool)  # feature/paste-preview: (текст, auto_enter)
                                       # worker → GUI: показати картку перегляду
    queue_state = Signal(int, bool)    # feature/dictation-queue: (скільки ще чекає,
                                       # чи йде розшифровка) — потік черги → GUI
                                       # замість негайної вставки
    note_state = Signal(str)           # feature/scratchpad-note: стан диктування у
                                       # нотатку (idle|recording|busy) → вікно нотатки
    note_appended = Signal(str)        # feature/scratchpad-note: розшифрований рядок
                                       # (worker → GUI-слот, що дописує в буфер)
    test_mode_changed = Signal(bool)   # Режим тестування → сайдбар (позначка активності)
    command_edit_cmd = Signal(str)     # feature/voice-edit-selection: розшифрована
                                       # голосова команда (worker → GUI → діалог)
    command_edit_cmd_error = Signal(str)  # помилка розшифровки голосової команди

    # feature/meeting-ui: режим «Нарада» (вкладка + трей). Ядро (capture/session/
    # postprocess) підвантажується лениво в методах — модуль наради існує лише
    # після злиття Б1/Б3, а старт/смоук/тести мусять працювати й без нього.
    meeting_state = Signal(str)                     # recording | processing | idle
    meeting_track_done = Signal(str, str, str, object)  # (session_id, track, text, segments)
    meeting_session_done = Signal(str, object)      # (session_id, MeetingMeta) → фінал картки
    meeting_error = Signal(str, str)                # (session_id, message) → картка + tray
    meeting_audio_ready = Signal(str)               # (session_id) — WAV зібрано (диктофон-режим)
    meeting_vault_needed = Signal()                 # encrypted vault needs password/key-file
    meeting_storage_warning = Signal(str, float, str)  # session_id, sec, видимий текст
    meeting_plaintext_pending = Signal(int)         # незашифровані сесії після краху
    meeting_audio_state = Signal(str, str)          # (track, reconnecting|reconnected|failed)
    meeting_screen_error = Signal(str)              # нефатальна помилка відео → трей
    meeting_processing_progress = Signal(str, object)  # session_id + snapshot
    meeting_processing_done = Signal(str, object)      # session_id + ProcessingResult
    screen_record_state = Signal(str)               # idle | recording
    screen_record_error = Signal(str)
    screen_record_finished = Signal(str, bool)
    bookmark_requested = Signal()                    # keyboard-потік → GUI
    command_edit_requested = Signal()                # feature/voice-edit-selection:
                                                     # хоткей Command Mode (hook → GUI)
    tts_listen_requested = Signal()                  # feature/tts-listen: глобальний
                                                     # хоткей «Прослухати виділене»
                                                     # (hook-потік → GUI, thread-affinity)
    tts_playable = Signal(str)                       # combined-WAV готовий (worker →
                                                     # GUI-потік; QMediaPlayer лише тут)
    tts_timings = Signal(object, object)             # караоке (Хв.2): наскрізні
                                                     # word_timings + старти речень
    tts_chunk = Signal(str, object, bool, object)    # СТРІМІНГ (§3.2 TTFS): готовий
                                                     # чанк (wav, timings, is_first, generation)
    tts_synth_dropped = Signal(object)               # рецензія 5.3: playback-генерація впала/
                                                     # скасована ДО першого чанка → disarm панелі

    def __init__(self, app: QApplication, engine, cfg=None,
                 cuda_fallback=False):
        super().__init__()
        _migrate_update_cache()
        self.app = app
        # cfg і рушій готуються у main() ДО splash: cfg нормалізується й рушій
        # вантажиться у _EngineLoadThread (поза GUI-потоком), сюди приходять уже
        # готові. Fallback Config.load() — лише страхувальний (напр. у тестах).
        self.cfg = cfg if cfg is not None else Config.load()
        # Рушій уже завантажено у _EngineLoadThread (поза GUI-потоком) і, за
        # потреби, відновлено через _recover_engine_on_gui — сюди приходить
        # готовим. Це і є винос ~3 ГБ завантаження зі старту в потік.
        self.engine = engine
        self._stt_model_installed = bool(
            engine and getattr(engine, "is_available", True))
        from whisper_core.win_hardening import (
            exclude_process_from_wer, set_capture_protection_enabled)
        exclude_process_from_wer()
        # Початковий стан захисту від захоплення екрана — щоб вікна нарад, відкриті
        # ДО першого перемикання тумблера, вже брали правильний стан.
        set_capture_protection_enabled(getattr(self.cfg, "screen_protection", False))
        # авто-відкат плеєра після паузи: віддати значення в модуль плеєра (він без cfg)
        from .player import set_resume_backstep_ms
        set_resume_backstep_ms(int(getattr(self.cfg, "player_resume_backstep_s", 1.5) * 1000))
        self._cuda_fallback_at_start = cuda_fallback
        # Старий autostart.bat не мав --autostart і після оновлення помилково
        # показував вікно при вході у Windows. Якщо автозапуск уже ввімкнено,
        # переписуємо його в актуальному форматі без зміни вибору користувача.
        _refresh_frozen_autostart()
        # мова UI має бути задана ДО побудови трею/вікна (вони беруть tr при старті)
        i18n.set_language(getattr(self.cfg, "ui_language", "uk"))
        self.output_mode = "paste"    # paste | show | both (перемикач у вікні)
        self.formfill_capturing = False   # feature/voice-form-fill: активне поле шаблону ловить диктант
        self.profile = profiles.get_active(ROOT)
        self._migrate_processing_mode(self.profile)  # feature/processing-slider
        self.terms = self._profile_terms(self.profile)
        # feature/context-profiles: детект активного вікна + профілі поведінки.
        # Резолвер і гейт самодостатні (fronts.desktop.context), матчер — з файлу
        # context_profiles.toml. Два синхронні виклики за фразу: словник на старті
        # запису (_capture_context_dictionary) і поведінка вставки в _work.
        self._ctx_resolver = ContextResolver()
        self._ctx_gate = SecurityGate()
        self._ctx_profiles_path = paths.context_profiles_path()
        self._context_terms = None         # словник-override поточної фрази або None
        # feature/auto-profile: щойно користувач сам перемкнув профіль — авто-вибір
        # за вікном мовчить до кінця сесії (явний вибір у пріоритеті).
        self._profile_manual = False
        self.reload_context_profiles()
        # feature/voice-macros: голосові макроси — ПЕР-профільні (поруч зі
        # словником), тож шлях залежить від активного профілю й оновлюється при
        # перемиканні. Лінивий mtime-кеш: файл редагується руками, тож перечитуємо
        # його перед кожним диктуванням лише коли він змінився (дешевий os.stat).
        # Так ручні правки й записи з вікна підхоплюються без перезапуску.
        # Одноразова міграція злиття: колишні глобальні сніпети → macros.toml
        # профілю default (файл сніпетів після переносу зникає — маркер «мігровано»).
        try:
            migrate_snippets(paths.snippets_path(),
                             profiles.get(ROOT, profiles.DEFAULT_PROFILE).macros_path)
        except Exception:
            logging.debug("Міграція сніпетів у макроси пропущена", exc_info=True)
        self._macros_mtime = None
        self.macros = {}
        self._reload_macros()
        # feature/office-voice-nav: користувацькі аліаси команд навігації
        # (navcommands.toml поруч із config.toml). Глобальні, не пер-профільні;
        # вантажимо раз на старті. Битий/відсутній файл → {} (лише вбудовані команди).
        self._nav_aliases = navcommands.load_aliases(navcommands.aliases_path())
        self._nav_target_hwnd = None   # закріплена ціль навігації (вікно старту диктанту)
        self.tray = Tray(
            profile_names=[p.name for p in profiles.list_profiles(ROOT)],
            active=self.profile.name,
            memory_on=self.profile.memory_enabled,
            on_switch_profile=self.switch_profile,
            on_toggle_memory=self.toggle_memory,
            on_reset_memory=self.reset_memory,
            on_reload_terms=self.reload_terms,
            on_quit=self.request_quit,
            on_open_window=self.show_window,
            on_recent=self._recent_history,
            on_undo_paste=self.undo_last_paste,      # feature/qol-pack
            on_insert_last=self.insert_last_again,   # feature/qol-pack
            on_cheat_sheet=self.show_cheat_sheet,   # feature/ux-center
            on_open_note=self.show_note,     # feature/scratchpad-note
            on_command_edit=self.command_edit_from_clipboard,  # feature/voice-edit-selection
            on_listen_selection=self.listen_selection_from_hotkey,  # feature/tts-listen
            on_open_voices=self.open_voice_manager,  # feature/tts-listen: Хвиля 3
            on_pron_dict=lambda: self.open_pronunciation_dialog(),  # Хвиля 4
            on_recent_pastes=self._recent_pastes,   # feature/paste-safety
            on_paste_recent=self.paste_recent,      # feature/paste-safety
            on_help=self.open_help,          # довідка з трею
        )
        # Модульний _audit_event показує попередження про недописаний журнал
        # цілісності через цей нотифікатор (self йому недоступний).
        _set_audit_corrupt_notifier(self.tray.notify)
        # feature/qol-pack: пам'ять останньої вставки (скасувати / вставити ще раз)
        self._undo = UndoBuffer()
        # feature/paste-safety: буфер останніх вставок (повторити / перегляд N)
        self._paste_history = PasteHistory()
        self._paste_target = None       # (HWND, заголовок) цілі на старт диктування
        self.window = MainWindow(self)
        self._clipboard_owner_hwnd = int(self.window.winId())
        # feature/ux-center: плаваючий індикатор диктування (drag + збережена
        # позиція). Створення не має валити старт — guard.
        self.pill = None
        self._cheat_sheet = None
        try:
            from .pill import FloatingPill
            self.pill = FloatingPill(on_moved=self.set_pill_position,
                                     on_reset=self.reset_pill_position)
            self.pill.apply_saved_position(
                (getattr(self.cfg, "pill_x", None),
                 getattr(self.cfg, "pill_y", None)))
            self.rec_state.connect(self.pill.set_state)
        except Exception:
            logging.exception("Не вдалося створити плаваючий індикатор")
            self.pill = None
        self._preview_card = None          # feature/paste-preview: жива картка або None
        self.preview_ready.connect(self._on_preview_ready)
        self.transcribed.connect(self.window.dictation.add_entry)
        self.live_dictation_segment.connect(self.window.dictation.add_live_segment)
        self.live_meeting_segment.connect(self.window.meeting.add_live_segment)
        self.live_error.connect(self.tray.notify)
        self.live_disable_requested.connect(self._disable_live_transcription_from_gui)
        self.transcription_error.connect(self.tray.notify)
        self.cpu_fallback.connect(self._apply_runtime_cpu_fallback)
        # Модель/device у Settings позначені “після перезапуску”. Idle-reload не
        # повинен непомітно застосувати pending-вибір раніше; динамічні language/
        # VAD після побудови рушія все одно читаються зі спільного self.cfg.
        self._engine_load_cfg = copy(self.cfg)
        self.recorder = Recorder(
            self.cfg.sample_rate, self.cfg.input_device,
            on_audio_state=lambda state: self.dictation_audio_state.emit(state))
        self.dictation_audio_state.connect(self._on_dictation_audio_state)
        cfg_for_log = _diagnostic_attr(self, "cfg", None)
        diagnostic_event("app_ready", version=DISPLAY_VERSION,
                         device=_diagnostic_attr(cfg_for_log, "input_device", "system") or "system",
                         model=_diagnostic_attr(cfg_for_log, "model_name"),
                         compute=_diagnostic_attr(cfg_for_log, "compute_type"))
        # feature/native-hotkeys: бекенд за cfg.hotkey_backend (native | legacy)
        self.hotkey = hotkeys_native.make_ptt_hotkey(self.cfg, self.cfg.ptt_key)
        self.hotkey.pressed.connect(self.on_press)
        self.hotkey.released.connect(self.on_release)
        # feature/mouse-ptt: бічна кнопка миші (opt-in) через ті самі сигнали
        # hotkey — режим hold/toggle і маршалінг у GUI-потік успадковуються самі
        self.mouse_hook = None
        self._apply_mouse_ptt()
        # feature/qol-pack: глобальні хоткеї дій (скасувати вставку / вставити ще
        # раз). Callback стріляє у keyboard-потоці → лише emit сигналу в GUI-потік.
        self.undo_requested.connect(self.undo_last_paste)
        self.insert_requested.connect(self.insert_last_again)
        self.action_hotkeys = hotkeys_native.make_action_hotkeys(
            self.cfg,
            on_undo=self.undo_requested.emit,
            on_insert=self.insert_requested.emit)
        self.action_hotkeys.apply(
            getattr(self.cfg, "undo_paste_key", "") or "",
            getattr(self.cfg, "insert_last_key", "") or "")
        self.bookmark_requested.connect(
            lambda: self.add_meeting_bookmark(source="live_hotkey"))
        self.bookmark_hotkey = hotkeys_native.make_action_hotkeys(
            self.cfg, self.bookmark_requested.emit, lambda: None)
        self.bookmark_hotkey.apply(getattr(self.cfg, "meeting_bookmark_hotkey", "") or "", "")
        # feature/voice-edit-selection: глобальний хоткей Command Mode (opt-in).
        # Порожній рядок = вимкнено; трей-пункт діє завжди. Callback стріляє у
        # hook-потоці → .emit маршалить у GUI (QueuedConnection).
        self.command_edit_requested.connect(self.command_edit_from_clipboard)
        self.command_edit_hotkey = hotkeys_native.make_action_hotkeys(
            self.cfg, self.command_edit_requested.emit, lambda: None)
        self.command_edit_hotkey.apply(
            getattr(self.cfg, "command_edit_hotkey", "") or "", "")
        # feature/tts-listen: глобальний хоткей «Прослухати виділене» (§8.8). Callback
        # приходить у hook-потоці → лише .emit сигналу (QueuedConnection у GUI-слот);
        # прямий виклик плеєра з чужого потоку = thread-affinity краш Qt. Порожній
        # рядок = вимкнено; трей-пункт діє завжди.
        self.tts_listen_requested.connect(self.listen_selection_from_hotkey)
        self.tts_playable.connect(self._on_tts_playable)
        self.tts_timings.connect(self._on_tts_timings)
        self.tts_chunk.connect(self._on_tts_chunk)
        self.tts_synth_dropped.connect(self._on_tts_synth_dropped)
        self.tts_listen_hotkey = hotkeys_native.make_action_hotkeys(
            self.cfg, self.tts_listen_requested.emit, lambda: None)
        self.tts_listen_hotkey.apply(getattr(self.cfg, "tts_hotkey", "") or "", "")
        # feature/qol-pack: автостоп по тиші + ліміт тривалості диктування.
        # Один GUI-таймер крутиться ЛИШЕ під час диктування (старт/стоп у
        # _start_recording/_stop_and_transcribe). AutostopMonitor — чистий стан.
        self._autostop = AutostopMonitor(
            getattr(self.cfg, "dictation_autostop_silence_s", 0) or 0)
        self._duration_warned = False   # попередили про наближення ліміту (раз/запис)
        self._dict_watch = QTimer(self)
        self._dict_watch.setInterval(250)
        self._dict_watch.timeout.connect(self._on_dictation_watch)
        # feature/scratchpad-note: плаваюча нотатка. Буфер живе тут (переживає
        # закриття вікна в межах сесії; НЕ перезапуск). Диктування в нотатку
        # взаємно виключне з PTT через _note_dictating (гейти в on_press/record_start).
        self._note_buffer = ""
        self._note_window = None
        self._note_dictating = False
        self._note_state = "idle"
        self._note_hotkey = None
        self.note_appended.connect(self._on_note_appended)
        self._apply_note_hotkey()
        self._panic_hotkey = None
        self._apply_panic_hotkey()
        # feature/voice-edit-selection: Command Mode — диктування команди для
        # редагування виділеного. Ділить той самий recorder (гейтимо PTT через
        # _command_dictating, як нотатку). _command_dialog — активний модальний діалог.
        self._command_dictating = False
        self._command_dialog = None
        self._tts_panel_fix_active = False   # рецензія 5.2 Б2: діалог правки панелі відкритий
        self.command_edit_cmd.connect(self._on_command_cmd_ready)
        self.command_edit_cmd_error.connect(self._on_command_cmd_error)
        self.finished.connect(lambda: self.tray.set_state("idle"))
        self.finished.connect(lambda: self.rec_state.emit("idle"))
        # feature/dictation-queue (запит Миколи №10): черга диктувань. Споживач —
        # один фоновий потік (обробка строго по черзі); аудіо лише в памʼяті.
        # on_state стріляє з потоку черги → маршалимо в GUI сигналом queue_state.
        self._queue = DictationQueue(
            self._process_queue_job, max_items=4, max_seconds=90.0,
            on_state=lambda pending, active: self.queue_state.emit(pending, active))
        self._queue_full_toast_ts = 0.0   # анти-спам тосту «черга повна»
        self.queue_state.connect(self._apply_queue_state)
        self._busy = False
        self._mic_testing = False      # feature/audio-qol: триває тест мікрофона
                                       # (той самий recorder — гейтимо PTT, щоб
                                       # натиск клавіші не вкрав запис тесту)
        self._mic_test_thread = None   # живий потік тесту — _cleanup його джойнить
        self._capturing = False        # триває захоплення нової клавіші запису
        self._rec_started = 0.0
        self._last_tap = 0.0           # feature/double-tap: час попереднього тапу PTT
                                       # (для детекції подвійного натиску)
        self._key_down = False         # PTT-клавіша фізично затиснута зараз
        self._cancel_guard = False     # скасовано мишею при затиснутій клавіші:
                                       # ігнорувати авто-повтори press до release
        self._mic_warned = False       # вже попередили про відсутній мікрофон
                                       # (щоб авто-повтор клавіші не спамив)
        self._export_warned = False    # feature/auto-export: вже попередили про
                                       # збій автозбереження (без спаму на кожен запис)
        # одна модель на всіх: PTT і файли транскрибуються строго по черзі
        self._engine_lock = threading.Lock()
        # Live preview ділить ТОЙ самий Engine+lock; фінальна транскрипція після stop лишається точнішою.
        self._live_dictation = None
        self._live_meeting = None
        self._file_jobs = queue.Queue()
        self._job_seq = 0
        # Скасовані задачі черги файлів (fix/cancel-transcription). Множину читає
        # робочий потік, дописує GUI — тому під замком. Задача, позначена до
        # старту, не стартує взагалі; активна уривається на найближчому сегменті.
        self._file_cancelled = set()
        self._file_cancel_lock = threading.Lock()
        threading.Thread(target=self._file_worker, daemon=True).start()
        # feature/meeting-ui: стан наради (взаємно заблокована з PTT — розділ 3.2)
        self._meeting_active = False       # запис наради йде зараз
        # тумблер довіри: користувач може вимкнути ЖИВУ обробку (розшифровку/
        # діаризацію) поточної наради; сам запис аудіо триває. Скидається на кожній
        # новій нараді (meeting_start).
        self._meeting_live_disabled = False
        self._meeting_session = None       # живий MeetingSession поточного запису
        self._meeting_streams = {}         # track -> CaptureStream (1 або 2 за пресетом)
        self._meeting_screen_recorder = None  # ScreenRecorder або None
        self._screen_recorder = None          # незалежний ScreenRecorder
        self._meeting_pending = {}         # session_id -> {session, dir, expected, tracks}
        self._last_trashed_meeting = None  # шлях останньої видаленої наради в
                                            # кошику — для «Повернути» у тості
        self._meeting_plaintext_count = 0  # стійкий fail-closed банер на вкладці
        # id від старту postprocess до фіналу: stopped у цей проміжок не є orphan.
        self._meeting_postprocessing = set()
        # Явно запущені post-meeting jobs. Під час запису цей словник порожній:
        # важка ASR-обробка не конкурує із захопленням.
        self._meeting_processing_jobs = {}
        # feature/player-recordings: диктофон (простий запис без негайної
        # розшифровки). Ділить той самий recorder із диктуванням — гейтимо
        # взаємно (один мікрофон). Запис синхронний (sounddevice-callback),
        # окремого воркера не треба; смужка рівня бере recorder.take_meter().
        self._dictaphone_active = False
        self._dictaphone_started = 0.0
        self._dictaphone_writer = None     # recordings.RecordingWriter активного запису
        # feature/accuracy-corpus: буфер аудіо ОСТАННІХ диктувань (ts → (audio, sr))
        # для збирача корпусу. WAV диктування на диск не персиститься — тримаємо
        # float32-кліп у пам'яті, поки картка на екрані, щоб «Розпізнано погано…»
        # мала що зберегти. Обмежений — старі витісняються (RAM під контролем).
        self._corpus_audio = OrderedDict()
        # feature/model-idle-unload: єдиний lifecycle для STT + усіх Gemma
        # sidecar-ів. QTimer лише планує перевірку; close/GC працюють у фоні.
        self._model_lifecycle = ModelLifecycle(
            timeout_seconds=getattr(self.cfg, "model_idle_unload_seconds", 600),
            unload=self._unload_idle_models,
            load=self._load_stt_model,
            is_busy=self._models_busy)
        self._model_idle_worker = None
        self._model_idle_timer = QTimer(self)
        self._model_idle_timer.setInterval(30_000)
        self._model_idle_timer.timeout.connect(self._check_model_idle)
        # feature/no-model-state: без мовного пакета нема чого вивантажувати/
        # перезавантажувати — таймер не запускаємо (інакше unload підмінить
        # NullEngine на голий None, а reload спробує будувати Engine без моделі).
        if self._model_lifecycle.timeout_seconds and self.has_model:
            self._model_idle_timer.start()
        self.model_lifecycle_state.connect(self._on_model_lifecycle_state)
        self.rec_state.connect(lambda _state: self._model_lifecycle.touch())
        self.meeting_state.connect(lambda _state: self._model_lifecycle.touch())
        from whisper_core.protocol import sidecar as protocol_sidecar
        protocol_sidecar.add_activity_listener(self._model_lifecycle.touch)
        # feature/tts-listen: арбітр RAM важких моделей (§3.3) + стан пакета
        # «Прослухати». TTS-приладдя (sidecar/плеєр) вмикається панеллю; тут —
        # координатор, що витісняє STT через lifecycle-стан і глушить TTS перед
        # мікрофоном. Реєстр TTS-sidecar-ів теж торкає idle-lifecycle.
        from whisper_core.heavy_models import HeavyModelCoordinator
        from whisper_core.tts import sidecar as tts_sidecar
        self._tts_sidecar = None
        self._tts_player = None
        self._tts_active_req = None
        self._heavy_coordinator = HeavyModelCoordinator(
            stt_force_unload=self._model_lifecycle.force_unload,
            stt_is_busy=self._models_busy,
            tts_cancel=self._tts_cancel_active,
            tts_shutdown=self._tts_shutdown)
        tts_sidecar.add_activity_listener(self._model_lifecycle.touch)
        # feature/tts-listen (§3.3): окремий TTS grace-таймер — завершує TTS-sidecar
        # через 60 c після активності НЕЗАЛЕЖНО від STT-idle-таймера (навіть коли
        # model_idle_unload_seconds=0, TTS не тримається резидентно вічно).
        self._tts_grace_timer = QTimer(self)
        self._tts_grace_timer.setSingleShot(True)
        self._tts_grace_timer.setInterval(60_000)
        self._tts_grace_timer.timeout.connect(self._tts_grace_fire)
        self.meeting_track_done.connect(self._on_meeting_track_done)
        self.meeting_error.connect(self._on_meeting_error)
        self.meeting_audio_state.connect(self._on_meeting_audio_state)
        self.meeting_screen_error.connect(self._on_meeting_screen_error)
        # Трей чіпає QTimer → ЛИШЕ GUI-потік. Емітери meeting_state бувають у
        # worker-потоках (_finish_meeting), тож трей оновлює цей слот: авто-конект
        # для чужого потоку стає QueuedConnection (DesktopApp живе у GUI-потоці),
        # для GUI-емітерів — DirectConnection; обидва шляхи безпечні.
        self.meeting_state.connect(self._on_meeting_state_tray)
        # акуратний вихід: зняти хуки/мікрофон ДО руйнування Qt-об'єктів,
        # інакше keyboard-потік може смикнути мертвий об'єкт (крах 0xC000041D)
        app.aboutToQuit.connect(self._cleanup)
        # feature/watch-folder: авто-черга нових аудіо з вибраної теки
        self.watch_ready.connect(self._watch_enqueue)
        self._watch_seen = set()          # шляхи вже враховані (оброблені/у роботі)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_watch_dir_changed)
        self._apply_watch_config()
        # Краш під час наради лишає розшифровані media/worker-теки у %TEMP%. DPAPI-
        # сховище розблоковується без діалогу (ensure_dek авто), тож прибирання при
        # unlock-діалозі його не зачепить — чистимо на СТАРТІ безумовно.
        _cleanup_stale_meeting_temps()
        _cleanup_stale_tts_temps()             # feature/tts-listen: prod crash-recovery §8.9
        _resume_meeting_encryption(self)
        self.tray.set_state("idle")
        if self._cuda_fallback_at_start:
            self.tray.notify(tr("app_cuda_fallback_start"))

        # тиха перевірка оновлень: через кілька секунд після старту (щоб не гальмувати
        # запуск), у фоновому потоці, не частіше ніж раз на тиждень. Прапорець opt-out.
        self._update_thread = None
        self._download_thread = None      # feature/auto-update
        if self.cfg.check_updates:
            QTimer.singleShot(5000, self._maybe_check_updates)

    # --- feature/no-model-state --------------------------------------------
    @property
    def has_model(self) -> bool:
        """Чи встановлений мовний пакет розпізнавання (не NullEngine)."""
        engine = getattr(self, "engine", None)
        if engine is not None:
            return bool(getattr(engine, "is_available", True))
        lifecycle = getattr(self, "_model_lifecycle", None)
        return bool(getattr(self, "_stt_model_installed", False)
                    and getattr(lifecycle, "state", None) in (UNLOADED, LOADING))

    # --- feature/model-idle-unload ----------------------------------------
    def _models_busy(self) -> bool:
        """Останній гейт перед unload: жодного запису/обробки/AI-sidecar."""
        from whisper_core.protocol import sidecar as protocol_sidecar
        recorder = getattr(self, "recorder", None)
        file_jobs = getattr(self, "_file_jobs", None)
        return bool(
            getattr(self, "_busy", False)
            or getattr(recorder, "recording", False)
            or getattr(self, "_meeting_active", False)
            or getattr(self, "_meeting_postprocessing", set())
            or getattr(self, "_note_dictating", False)
            or getattr(self, "_command_dictating", False)
            or getattr(self, "_mic_testing", False)
            or getattr(self, "_dictaphone_active", False)
            or getattr(self, "_live_dictation", None) is not None
            or getattr(self, "_live_meeting", None) is not None
            or (file_jobs is not None and not file_jobs.empty())
            or protocol_sidecar.any_running())

    # --- feature/tts-listen: панель «Прослухати» + автоглушіння (§8, §9.1) ----
    def _get_tts_controller(self):
        """Лінивий TtsController (оркеструє sidecar через координатор). on_playable
        маршалить combined-WAV у GUI-потік сигналом (QMediaPlayer лише там)."""
        ctrl = getattr(self, "_tts_controller", None)
        if ctrl is None:
            from .tts_controller import TtsController
            ctrl = TtsController(
                cfg=self.cfg, coordinator=self._heavy_coordinator,
                on_playable=self.tts_playable.emit,
                on_timings=self.tts_timings.emit,   # караоке Хв.2 → GUI-потік
                on_chunk_playable=lambda tok, p, t, first: self.tts_chunk.emit(p, t, first, tok),
                on_synth_dropped=self.tts_synth_dropped.emit,   # рецензія 5.3: disarm при обриві
                available_langs=self._tts_available_langs,
                on_export_done=lambda p: self.tray.notify(
                    tr("tts_saved", name=os.path.basename(p))),
                stop_playback=self._tts_stop_playback,  # звільнити файл перед cleanup temp
                lexicon_provider=self._tts_lexicon_snapshot,  # словник вимови (Хв.4)
                position_provider=self._tts_current_sentence,  # reverse-resume (Хв.5)
                toast=lambda key: self.tray.notify(tr(key)))
            self._tts_controller = ctrl
            self._tts_sidecar = ctrl.sidecar()   # для мік-гейту (оновиться на старті)
        return ctrl

    def listen_selection_from_hotkey(self) -> None:
        """GUI-слот хоткея/трею «Прослухати виділене»: читає системне виділення й
        запускає озвучення (latest-wins). Порожнє виділення → тост."""
        # рецензія 5.2 БЛОКЕР 2: не перехоплювати панель, поки відкритий діалог правки її
        # ж тексту — інакше гонка підміни тексту під модальним exec().
        if getattr(self, "_tts_panel_fix_active", False):
            self.tray.notify(tr("tts_busy_editing"))
            return
        try:
            text = capture_selection() or ""     # з .paste (уже імпортовано)
        except Exception:
            logging.exception("Не вдалося захопити виділений текст для озвучення")
            self.tray.notify(tr("tts_selection_capture_failed"))
            return
        if not text.strip():
            self.tray.notify(tr("tts_cancelled"))
            return
        self.open_listen_panel(text)

    def open_listen_panel(self, text: str) -> None:
        """Відкрити панель «Прослухати» й почати озвучення тексту (latest-wins)."""
        from .tts_panel import ListenPanel
        ctrl = self._get_tts_controller()
        ctrl.prewarm()                           # §2: прогрів рушія при відкритті панелі
        panel = getattr(self, "_tts_panel", None)
        if panel is None:
            panel = ListenPanel(getattr(self, "window", None), text=text,
                                has_voice=True, on_stop=ctrl.stop,
                                on_save=lambda: self._tts_save_dialog(text),
                                on_fix_word=self.open_pronunciation_dialog,  # §6.4 п.1
                                on_voice_fix=self.listen_panel_voice_fix,    # §9.2 Хв.5
                                on_close=lambda: self._heavy_coordinator.tts_grace_shutdown())  # §2 вивантаження при закритті
            self._tts_panel = panel
            self._tts_player = panel.player()
        else:
            panel.set_text(text)                 # караоке-ціль = поточний текст
        panel.show()
        panel.raise_()
        ctrl.play_text(text)

    def _on_tts_timings(self, words, starts) -> None:
        """Наскрізні word_timings готові (GUI-потік) → запустити караоке в панелі."""
        panel = getattr(self, "_tts_panel", None)
        if panel is not None:
            panel.set_timings(words, starts)

    def _on_tts_chunk(self, wav_path, chunk_timings, is_first, generation=None) -> None:
        """СТРІМІНГ (§3.2 TTFS, GUI-потік): готовий чанк → у чергу панелі; перший
        грає НЕГАЙНО (перший звук < 0.5 c, не чекаючи синтезу всього тексту).
        generation (рецензія 5.3) — токен synth-запиту; панель відсіює чанки чужих генерацій."""
        panel = getattr(self, "_tts_panel", None)
        if panel is not None:
            panel.enqueue_chunk(wav_path, chunk_timings, bool(is_first), generation)
        self._tts_grace_timer.start()            # активність → перезапустити grace (§3.3)

    def _on_tts_synth_dropped(self, generation) -> None:
        """Суд 5.3 (GUI-потік): playback-генерація завершилась, не віддавши жодного
        чанка (скасування/помилка/відхилення/порожньо) → зняти resume-arm цієї генерації,
        щоб наступний незалежний потік не успадкував чужу позицію."""
        panel = getattr(self, "_tts_panel", None)
        if panel is not None:
            panel.disarm(generation)

    def _tts_grace_fire(self) -> None:
        """grace минув: якщо озвучення не активне — завершити TTS-sidecar (звільнити
        RAM) навіть при STT idle=0."""
        ctrl = getattr(self, "_tts_controller", None)
        if ctrl is not None and not ctrl.is_busy():
            coord = getattr(self, "_heavy_coordinator", None)
            if coord is not None:
                coord.tts_grace_shutdown()

    def _tts_available_langs(self) -> set:
        """Мови із ЗАВАНТАЖЕНИМ голосом (для розвʼязання unknown/mixed §7.2)."""
        from whisper_core.tts import voices as _v
        langs = set()
        for lg, vid in (("uk", getattr(self.cfg, "tts_voice_uk", "styletts2_ua")),
                        ("en", getattr(self.cfg, "tts_voice_en", "kokoro_en"))):
            try:
                if _v.voice_available(vid):
                    langs.add(lg)
            except Exception:                    # noqa: BLE001
                pass
        return langs

    # --- менеджер голосів (Хвиля 3, §7.3) ------------------------------------
    def open_voice_manager(self) -> None:
        """Відкрити менеджер голосів (картки за мовою: завантажити/зразок/видалити/
        активний per-мова, «Інша модель»)."""
        from .tts_voices import VoiceManagerDialog
        from whisper_core.tts import voices as _v
        active = {"uk": getattr(self.cfg, "tts_voice_uk", "styletts2_ua"),
                  "en": getattr(self.cfg, "tts_voice_en", "kokoro_en")}
        dlg = VoiceManagerDialog(
            getattr(self, "window", None), root=str(_v.paths.tts_voices_dir()),
            active_by_lang=active,
            tts_enabled=bool(getattr(self.cfg, "tts_enabled", False)),
            on_toggle_enabled=self._tts_set_enabled,
            on_download=self._tts_download_voice,
            on_sample=self._tts_play_sample,
            on_delete=self._tts_delete_voice,
            on_activate=self._tts_activate_voice,
            on_add_custom=self._tts_add_custom_voice)
        self._voice_manager = dlg
        dlg.show()
        dlg.raise_()

    # --- словник вимови (Хвиля 4, §6) ----------------------------------------
    def _tts_lexicon_snapshot(self, voice_id: str) -> list:
        """IPC-знімок словника вимови активного профілю+голосу (§6.2)."""
        from whisper_core.tts import lexicon as _lex
        try:
            return _lex.active_pipeline(self.profile, voice_id).to_ipc()
        except Exception:                        # noqa: BLE001
            return []

    def open_pronunciation_dialog(self, word: str = "") -> None:
        """Відкрити діалог «Як вимовляти слово» (§6.4): прев'ю → зберегти у словник
        вимови активного профілю; відмінки через pymorphy3 з підтвердженням."""
        from .tts_pron import PronunciationDialog
        from whisper_core.tts import lexicon as _lex

        def on_preview(match, value, ctype):
            # прев'ю ДО збереження: озвучити варіант з правилом (для наголосу/заміни —
            # синтез самого value; StyleTTS2 сам обробить U+0301)
            self.open_listen_panel(value or match)

        def on_save(*, match, value, correction_type, match_mode, forms):
            # UPSERT + повертаємо статус (learned|updated) → діалог покаже коректний
            # текст (рецензія хвилі 4 БЛОКЕР 1: раніше завжди «збережено» попри відкинуте нове)
            try:
                res = _lex.learn(self.profile, match, value,
                                 match_mode=match_mode, correction_type=correction_type,
                                 forms=forms, source="manual")
                return res.status
            except _lex.RuleError:
                return "failed"

        dlg = PronunciationDialog(
            getattr(self, "window", None), word=word,
            on_preview=on_preview, on_save=on_save,
            on_forms=lambda w: _lex.generate_forms(w),
            on_list=lambda: _lex.list_rules(self.profile),      # undo-UI (§6)
            on_delete=lambda rid: _lex.revoke(self.profile, rid))
        self._pron_dialog = dlg
        dlg.show()
        dlg.raise_()

    # --- зв'язка зі зворотним диктуванням (Хвиля 5, §9.2) --------------------
    def _tts_current_sentence(self):
        """Індекс поточного речення озвучення (для reverse-resume). None — не грає."""
        panel = getattr(self, "_tts_panel", None)
        if panel is None:
            return None
        idx = panel.current_sentence_index()
        return idx if idx is not None and idx >= 0 else None

    def listen_panel_voice_fix(self):
        """ЄДИНИЙ вхід reverse-звʼязки (§9.2, рецензія хвилі 5): виправити голосом слово в
        тексті САМОЇ панелі «Прослухати». Читає виділення з ВЛАСНОГО редактора панелі,
        ставить TTS на паузу з позицією, відкриває Command Mode; правку МАПИТЬ у повний
        текст панелі (замінює виділення в редакторі) і продовжує з того ж речення. Cancel
        → стан чистий (не лишає застарілий індекс). Інші voice_edit_selection TTS не чіпають."""
        panel = getattr(self, "_tts_panel", None)
        ctrl = getattr(self, "_tts_controller", None)
        if panel is None or ctrl is None:
            return
        editor = getattr(panel, "_editor", None)
        if editor is None:
            return
        cursor = editor.textCursor()
        selected = cursor.selectedText() or editor.toPlainText()
        # рецензія 5.2 БЛОКЕР 2: зафіксувати ревізію тексту панелі на СТАРТІ правки. Модальний
        # ex() крутить вкладений event loop, у якому глобальний TTS-хоткей міг би
        # підмінити текст панелі (open_listen_panel іншого тексту). Якщо ревізія при
        # застосуванні змінилась — правка стосується вже ЧУЖОГО тексту → скасувати.
        rev0 = panel.text_revision()
        paused = bool(ctrl.mark_reverse_pause())
        applied = {"done": False}

        def apply_to_panel(new_text):
            applied["done"] = True
            if panel.text_revision() != rev0:
                # текст панелі підмінили під час діалогу (гонка) → НЕ застосовувати
                if paused:
                    ctrl.consume_reverse_index()   # скинути reverse-стан (не лишати)
                self.tray.notify(tr("tts_panel_text_changed"))
                return
            cur = editor.textCursor()
            ro = editor.isReadOnly()
            editor.setReadOnly(False)
            try:
                if cur.hasSelection():
                    cur.insertText(new_text)
                else:
                    editor.setPlainText(new_text)
            finally:
                editor.setReadOnly(ro)
            new_full = editor.toPlainText()
            idx = ctrl.consume_reverse_index() if paused else None
            self._tts_resume_panel(new_full, idx)

        # рецензія 5.2 БЛОКЕР 2 (простіший запобіжник): поки відкритий діалог правки САМОЇ
        # панелі, хоткей «Прослухати» не перехоплює її текст (listen_selection_from_hotkey).
        self._tts_panel_fix_active = True
        try:
            self.voice_edit_selection(selected, apply_to_panel)
        finally:
            self._tts_panel_fix_active = False
            if not applied["done"] and paused:
                ctrl.consume_reverse_index()
                self.tray.notify(tr("tts_reverse_cancelled"))

    def _tts_resume_panel(self, new_full, sentence_index):
        """Ресинтез повного (оновленого) тексту панелі + продовження з речення §9.2.
        БЛОКЕР 3: якщо збережене речення виходить за межі нового тексту (коротша правка)
        — чесний видимий стан (тост + читання з початку), НЕ тиха смерть панелі."""
        panel = getattr(self, "_tts_panel", None)
        ctrl = getattr(self, "_tts_controller", None)
        if panel is None or ctrl is None:
            return
        panel.set_text(new_full)
        idx = int(sentence_index) if sentence_index is not None else 0
        from whisper_core.tts.worker import split_sentences_spans
        n_sentences = len(split_sentences_spans(new_full)) or 1
        if idx >= n_sentences:
            idx = 0
            self.tray.notify(tr("tts_text_changed"))
        # рецензія 5.3: спершу СТАРТУВАТИ ресинтез, тоді прив'язати arm до ЙОГО generation-
        # токена. play_text синхронно виділяє токен (GUI-потік); перший чанк цієї
        # генерації обробиться пізніше (черга Qt), уже побачивши armed-resume. Якщо той
        # потік упаде/скасується ДО чанка — on_synth_dropped зніме саме цей arm.
        outcome = ctrl.play_text(new_full)
        if outcome == "playing":
            panel.set_resume_index(idx, generation=ctrl.last_generation())

    def _tts_set_enabled(self, on: bool) -> None:
        """§9-10: увімкнути/вимкнути пакет озвучення (тумблер у менеджері голосів)."""
        self.cfg.tts_enabled = bool(on)
        try:
            self.cfg.save()
        except Exception:                        # noqa: BLE001
            logging.exception("Не вдалося зберегти tts_enabled")

    def _tts_activate_voice(self, voice_id: str, lang: str) -> None:
        if str(lang) == "en":
            self.cfg.tts_voice_en = voice_id
        else:
            self.cfg.tts_voice_uk = voice_id
        try:
            self.cfg.save()
        except Exception:                        # noqa: BLE001
            logging.exception("Не вдалося зберегти вибір голосу TTS")

    def _tts_download_voice(self, voice_id: str, *, on_progress=None,
                            on_done=None, on_failed=None) -> None:
        """Завантажити голос (§7.3) через QThread-воркер (onboarding.
        VoiceDownloadWorker, вже вживаний майстром першого запуску) — прогрес і
        завершення йдуть Qt-сигналами, тож безпечно доходять у GUI-потік.

        Суд (тиха відмова, живий тест 30.07): попередня версія — сирий
        threading.Thread; на голосах без запінованих файлів (Хвиля 3, sherpa)
        мовчала повністю, а на провалі кликала self.tray.notify() ПРЯМО з
        фонового потоку — небезпечний крос-тредовий виклик Qt-віджета, який на
        практиці ніяк не проявлявся (ні тосту, ні винятку). Тепер: клік лишає
        запис у журналі ЗАВЖДИ (нижче), а прогрес/провал/скасування — воркер
        сам логує (onboarding.VoiceDownloadWorker.run) і чергує Qt-сигнали
        безпечно в GUI-потік."""
        from .onboarding import VoiceDownloadWorker
        from whisper_core.tts import voices as _v
        logging.info("TTS: натиснуто «Завантажити» для голосу %s", voice_id)
        workers = getattr(self, "_voice_download_workers", None)
        if workers is None:
            workers = {}
            self._voice_download_workers = workers
        old = workers.get(voice_id)
        if old is not None and old.isRunning():
            return                            # уже качається — не дублювати
        worker = VoiceDownloadWorker(
            voice_id, root=str(_v.paths.tts_voices_dir()), parent=self)
        workers[voice_id] = worker
        if on_progress:
            worker.progress.connect(on_progress)
        if on_done:
            worker.finished_ok.connect(on_done)
        if on_failed:
            worker.failed.connect(on_failed)
            worker.cancelled.connect(on_failed)
        worker.failed.connect(self._on_voice_download_failed)
        worker.finished.connect(lambda: workers.pop(voice_id, None))
        worker.start()

    def _on_voice_download_failed(self, _message: str) -> None:
        """Тост про провал завантаження голосу. Bound-метод DesktopApp (QObject)
        — Qt сам чергує виклик у GUI-потік, тож self.tray.notify() тут
        безпечний (на відміну від прямого виклику з фонового потоку раніше)."""
        self.tray.notify(tr("tts_voice_corrupt"))

    def _tts_delete_voice(self, voice_id: str) -> None:
        from whisper_core.tts import voices as _v
        _v.delete_voice(voice_id)

    def _tts_play_sample(self, voice_id: str) -> None:
        """«Почути зразок»: демо-WAV пресета БЕЗ завантаження моделі (§7.6). Немає
        WAV (ще не згенеровано на білді) → чесний тост."""
        from whisper_core.tts import voices as _v
        path = _v.sample_wav_path(voice_id)
        if path is None:
            self.tray.notify(tr("tts_voice_sample"))
            return
        player = getattr(self, "_tts_player", None)
        if player is None:
            from .player import InlinePlayer
            player = InlinePlayer(None, getattr(self, "window", None))
            self._tts_player = player
        player.set_source(str(path))
        player.play_from(0)

    def _tts_add_custom_voice(self) -> None:
        """«Інша модель…»: вибрати теку voice-pack → валідація безпеки (§4.4) →
        додати у cfg.tts_custom_voices."""
        from PySide6.QtWidgets import QFileDialog
        from whisper_core.tts import voices as _v
        from whisper_core.tts.security import VoicePackError
        d = QFileDialog.getExistingDirectory(
            getattr(self, "window", None), tr("tts_voice_custom"))
        if not d:
            return
        try:
            cv = _v.custom_voice_from_pack(d)
        except VoicePackError as exc:
            # чесна причина: невідомий формат АБО заборонений тип рушія (styletts2/radtts)
            key = getattr(exc, "reason_key", "tts_voice_custom_invalid")
            self.tray.notify(tr(key))
            return
        voices_list = list(getattr(self.cfg, "tts_custom_voices", []))
        voices_list.append(cv.to_json())
        self.cfg.tts_custom_voices = voices_list
        try:
            self.cfg.save()
        except Exception:                        # noqa: BLE001
            logging.exception("Не вдалося зберегти власний голос TTS")

    def _on_tts_playable(self, path: str) -> None:
        """Combined-WAV готовий (GUI-потік): віддати плеєру панелі й відтворити."""
        player = getattr(self, "_tts_player", None)
        if player is not None:
            try:
                player.set_source(path)
                player.play_from(0)
            except Exception:                    # noqa: BLE001
                pass

    def _tts_save_dialog(self, text: str) -> None:
        """«Зберегти озвучення…» — РЕАЛЬНИЙ QFileDialog + save.py (reject-busy)."""
        from PySide6.QtWidgets import QFileDialog
        from whisper_core.tts import save as tts_save
        stem = tts_save.default_stem_from()
        filters = tr("tts_save_wav") + " (*.wav)"
        path, _sel = QFileDialog.getSaveFileName(
            self._tts_panel if getattr(self, "_tts_panel", None) else None,
            tr("tts_save"), stem + ".wav", filters)
        if not path:
            return
        self._get_tts_controller().export_text(text, path)

    def _tts_cancel_active(self) -> None:
        """Скасувати активний synthesize (latest-wins/мікрофон перемагає)."""
        ctrl = getattr(self, "_tts_controller", None)
        if ctrl is not None:
            ctrl.stop()

    def _tts_shutdown(self) -> None:
        """Завершити TTS-sidecar (миттєво віддати ~2.5 ГБ RAM ОС)."""
        ctrl = getattr(self, "_tts_controller", None)
        if ctrl is not None:
            ctrl.shutdown()
        self._tts_sidecar = None

    def _tts_stop_playback(self) -> None:
        """Зупинити плеєр озвучення (QMediaPlayer — лише GUI-потік) + прибрати
        застигле караоке-підсвічування (мік-гейт: озвучка мовчить під час запису)."""
        player = getattr(self, "_tts_player", None)
        if player is not None:
            try:
                player.stop()
            except Exception:                    # noqa: BLE001
                pass
        panel = getattr(self, "_tts_panel", None)
        hl = panel.highlighter() if panel is not None else None
        if hl is not None:
            try:
                hl.stop()
            except Exception:                    # noqa: BLE001
                pass

    def _tts_playback_stopped(self) -> bool:
        """Підтвердити, що плеєр озвучення у Stopped/Paused (бар'єр §9.1). Без плеєра
        — True. Коротке очікування, бо stop() уже викликано синхронно."""
        player = getattr(self, "_tts_player", None)
        qmp = getattr(player, "_player", None) if player is not None else None
        if qmp is None:
            return True
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            import time as _t
            deadline = _t.monotonic() + 0.5
            while _t.monotonic() < deadline:
                st = qmp.playbackState()
                if st in (QMediaPlayer.PlaybackState.StoppedState,
                          QMediaPlayer.PlaybackState.PausedState):
                    return True
                self.app.processEvents()
                _t.sleep(0.02)
            return qmp.playbackState() != QMediaPlayer.PlaybackState.PlayingState
        except Exception:                        # noqa: BLE001
            return True

    def _before_microphone_start(self, reason: str) -> bool:
        """Синхронний БАР'ЄР ДО recorder/live-STT start у КОЖНІЙ точці мікрофона (§9.1).
        Повертає True лише коли playback ПІДТВЕРДЖЕНО зупинений; викликач мусить
        перервати старт при False. Без координатора — no-op True."""
        coord = getattr(self, "_heavy_coordinator", None)
        if coord is None:
            return True
        from .tts_mic_gate import before_microphone_start
        return before_microphone_start(
            reason, stop_playback=self._tts_stop_playback, coordinator=coord,
            confirm_stopped=self._tts_playback_stopped,
            hard_kill=self._tts_shutdown)         # timeout → примусово завершити TTS

    def _unload_idle_models(self) -> bool:
        """Фоновий callback lifecycle: відпустити STT і завершити Gemma sidecar."""
        from whisper_core.protocol import sidecar as protocol_sidecar
        with protocol_sidecar.idle_transition():
            if self._models_busy():
                return False
            with self._engine_lock:
                # Поки чекали старий live/STT, міг початися новий запис.
                if self._models_busy():
                    return False
                engine, self.engine = getattr(self, "engine", None), None
                if engine is not None:
                    close = getattr(engine, "close", None)
                    if callable(close):
                        close()
            # Зазвичай список порожній: генератори закривають sidecar у finally.
            # Виклик лишається явною гарантією для майбутнього persistent reuse.
            protocol_sidecar.shutdown_all()
        diagnostic_event("models_unloaded", reason="idle")
        return True

    def _load_stt_model(self) -> None:
        started = time.perf_counter()
        load_cfg = copy(getattr(self, "_engine_load_cfg", self.cfg))
        try:
            engine = Engine(load_cfg)
        except ModelRevisionUnavailable:
            self._stt_model_installed = False
            raise
        engine.cfg = self.cfg
        with self._engine_lock:
            self.engine = engine
            self._stt_model_installed = bool(
                getattr(engine, "is_available", True))
        diagnostic_event(
            "model_loaded", model=getattr(self.cfg, "model_name", None),
            device=getattr(self.cfg, "device", None),
            compute=getattr(self.cfg, "compute_type", None),
            reason="idle_reload",
            duration_s=round(time.perf_counter() - started, 3))

    def _check_model_idle(self):
        worker = getattr(self, "_model_idle_worker", None)
        if worker is not None and worker.is_alive():
            return
        worker = threading.Thread(
            target=self._model_lifecycle.check_idle,
            name="model-idle-unload", daemon=True)
        self._model_idle_worker = worker
        worker.start()

    def _request_model_reload(self):
        """Перший старт диктування: показати чесний стан і гріти модель у фоні."""
        self._model_lifecycle.touch()
        state = self._model_lifecycle.state
        if state == LOADED:
            return
        self.model_lifecycle_state.emit("loading")
        if state != UNLOADED:
            return
        threading.Thread(target=self._reload_model_worker,
                         name="model-idle-reload", daemon=True).start()

    def _reload_model_worker(self):
        try:
            self._model_lifecycle.ensure_loaded()
        except Exception:
            logging.exception("Не вдалося повторно завантажити STT-модель")
            self.model_lifecycle_state.emit("error")
        else:
            self.model_lifecycle_state.emit("loaded")

    def _on_model_lifecycle_state(self, state: str):
        if state == "loading":
            self.tray.set_state("loading")
            self.rec_state.emit("loading")
            return
        recorder = getattr(self, "recorder", None)
        ui_state = ("recording" if getattr(recorder, "recording", False)
                    else "busy" if getattr(self, "_busy", False) else "idle")
        self.tray.set_state(ui_state)
        self.rec_state.emit(ui_state)
        if state == "error":
            self.tray.notify(tr("app_transcribe_error"))

    # --- feature/qol-pack: автостоп по тиші + ліміт тривалості (GUI-таймер) ---
    def _on_dictation_watch(self):
        """Тік лише під час диктування: перевірити тишу (автостоп) і тривалість
        (ліміт + попередження). Працює незалежно від видимості вікна — тому в
        контролері, а не в LevelMeter вкладки."""
        if not self.recorder.recording:
            return
        now = time.time()
        elapsed = now - self._rec_started
        # ліміт тривалості
        limit = getattr(self.cfg, "dictation_max_duration_s", 0) or 0
        status = duration_status(elapsed, limit)
        if status == "stop":
            diagnostic_event("dictation_limit_reached", duration_s=round(elapsed, 3))
            self.tray.notify(tr("qol_duration_reached"))
            self._stop_and_transcribe()
            return
        if status == "warn" and not self._duration_warned:
            self._duration_warned = True
            self.tray.notify(tr("qol_duration_warn"))
        # автостоп по тиші (RMS без скидання піку — не гониться з LevelMeter)
        try:
            rms = self.recorder.current_rms()
        except Exception:
            rms = 1.0     # не змогли зчитати рівень — не зупиняємо помилково
        if self._autostop.update(rms, now):
            diagnostic_event("dictation_autostop", duration_s=round(elapsed, 3))
            self.tray.notify(tr("qol_autostopped"))
            self._stop_and_transcribe()

    def _start_dictation_watch(self):
        """Запустити нагляд, лише якщо є що наглядати (автостоп або ліміт)."""
        self._duration_warned = False
        self._autostop.silence_s = float(
            getattr(self.cfg, "dictation_autostop_silence_s", 0) or 0)
        self._autostop.reset()
        limit = getattr(self.cfg, "dictation_max_duration_s", 0) or 0
        if self._autostop.silence_s > 0 or limit > 0:
            if not self._dict_watch.isActive():
                self._dict_watch.start()

    def _stop_dictation_watch(self):
        if self._dict_watch.isActive():
            self._dict_watch.stop()

    # --- провайдер історії для трею (виклик на aboutToShow) ---
    def _recent_history(self):
        """5 найновіших записів АКТИВНОГО профілю на момент виклику.
        Профіль міг перемкнутися з трею — читаємо self.profile щоразу, не кеш.
        Читаємо НЕЗАЛЕЖНО від memory_enabled: пам'ять гейтить лише ЗАПИС нових
        розшифровок, а наявні записи так само видно у вікні «Історія» — трей
        має бути з ним узгоджений, інакше «порожня» при повній історії лякає."""
        return read_recent(self.profile, 5)

    # --- профілі (GUI-потік, з трей-меню) ---
    def _migrate_processing_mode(self, profile):
        """feature/processing-slider: одноразово вивести режим обробки зі старих
        глобальних прапорців (спека §5). Записуємо РАЗ, далі не перевиводимо —
        далі керує лише повзунок (профіль)."""
        try:
            if profile is None or profile.has_processing():
                return
            level = cleanup_level_for_cfg(getattr(self, "cfg", None))
            mode = processing.migrate_dictation_mode(
                level,
                preserve_speech=bool(getattr(self.cfg, "preserve_speech", False)),
                autocorrect_enabled=bool(getattr(self.cfg, "autocorrect_enabled", False)),
                punctuator_enabled=bool(getattr(self.cfg, "punctuator_enabled", False)))
            profile.set_processing_mode(DICTATION, mode)
            profile.set_processing_mode(processing.MEETING, processing.DEFAULT_MODE)
        except OSError:
            pass          # запис некритичний: повзунок покаже дефолт, спробуємо пізніше

    def switch_profile(self, name: str):
        profiles.set_active(ROOT, name)
        # feature/auto-profile: явний вибір профілю має пріоритет на сесію —
        # авто-вибір за вікном далі мовчить.
        self._profile_manual = True
        self.profile = profiles.get_active(ROOT)
        self._migrate_processing_mode(self.profile)  # feature/processing-slider
        self.terms = self._profile_terms(self.profile)
        self._reload_macros()   # feature/voice-macros: макроси нового профілю
        self.tray.set_memory_checked(self.profile.memory_enabled)
        self.tray.notify(tr("app_dict_switched", name=name))
        if hasattr(self, "window"):          # синхронізувати вкладку Словники
            self.window.vocab.refresh()
            # feature/processing-slider: оновити обидва повзунки БЕЗ запису (§5)
            for page in (getattr(self.window, "dictation", None),
                         getattr(self.window, "meeting", None)):
                refresh = getattr(page, "refresh_processing_mode", None)
                if callable(refresh):
                    refresh()

    def toggle_memory(self, on: bool):
        self.profile.set_memory(on)

    def reset_memory(self):
        bak = self.profile.reset_memory()
        self.tray.notify(tr("app_mem_cleared", name=bak.name) if bak
                         else tr("app_mem_empty"))

    def _profile_terms(self, profile):
        """feature/bilingual-memory + feature/selflearn-dict: словник термінів
        профілю. read_terms_dict уже підмішує terms.learned.toml (вивчені терміни).
        Вивчені ПАРИ-ФРАЗИ (phrases.learned.toml) підмішуємо ЗАВЖДИ — це вже
        підтверджені виправлення користувача, а не опційна пам'ять стилю. Ручну
        пам'ять фраз (phrases.toml) — лише за ввімкненим тумблером phrase_memory.
        Один Terms → рушій застосовує все одним детермінованим проходом
        (terms.apply_glossary) ПІСЛЯ STT і ДО вставки; канони живлять hotwords."""
        try:
            self_learning.ensure_projections(profile)   # перебудувати, якщо відстали
        except Exception:
            logging.debug("Проєкції самонавчання не перебудовано", exc_info=True)
        data = read_terms_dict(profile.terms_path)
        data = merge_terms_data(
            data, phrasebook.read_learned_phrases(profile.phrases_path))
        if getattr(self.cfg, "phrase_memory_enabled", False):
            data = merge_terms_data(
                data, phrasebook.read_phrases(profile.phrases_path))
        return build_terms(data)

    def reload_terms(self):
        self.terms = self._profile_terms(self.profile)

    # --- контекстні профілі (feature/context-profiles) ---
    def reload_context_profiles(self):
        """Перечитати context_profiles.toml і перебудувати матчер
        (порядок профілів у файлі = пріоритет). Кличеться на старті та після
        правок у Налаштуваннях."""
        profs, default = context.load_profiles(self._ctx_profiles_path)
        self._ctx_matcher = context.ProfileMatcher(profs, default)
        # feature/auto-profile: правила «вікно → профіль» з того самого файлу.
        # enabled не задано → увімкнено, лише якщо правила є (інакше нейтрально).
        rules, enabled = context.load_auto_rules(self._ctx_profiles_path)
        if enabled is None:
            enabled = bool(rules)
        self._auto_matcher = context.AutoProfileMatcher(rules, enabled)

    def _capture_context_dictionary(self):
        """Виклик (а): знімок активного вікна на старті запису → словник
        профілю для ЦІЄЇ фрази. Перемикаємо ЛИШЕ glossary (безпечно, локально):
        активний профіль і його пам'ять не чіпаємо, тож і «повертати назад» нема
        чого — терміни живуть лише в межах поточної транскрипції."""
        self._context_terms = None
        try:
            ctx = self._ctx_resolver.get_window_context()
            prof = self._ctx_matcher.match(ctx)
            name = prof.behavior.dictionary
            if name:
                wc = profiles.get(ROOT, name)
                if wc is not None:
                    self._context_terms = self._profile_terms(wc)
            # feature/auto-profile: контекст-профіль не задав словник — пробуємо
            # правила «вікно → профіль» (тимчасово, лише glossary цієї фрази).
            if self._context_terms is None:
                self._apply_auto_profile(ctx)
        except Exception:
            logging.debug("Контекстний словник не визначено", exc_info=True)
            self._context_terms = None

    def _apply_auto_profile(self, ctx):
        """feature/auto-profile: якщо ввімкнено «Авто за вікном», користувач не
        перемикав профіль вручну цієї сесії й правило збіглося — беремо словник
        того профілю ЛИШЕ для цієї фрази (активний профіль не чіпаємо, тож і
        «повертати» нічого не треба)."""
        if getattr(self, "_profile_manual", False):
            return                          # явний вибір користувача — пріоритет
        matcher = getattr(self, "_auto_matcher", None)
        if matcher is None:
            return
        name = matcher.match(ctx)           # None, якщо вимкнено чи без збігу
        if name and name != self.profile.name:
            wc = profiles.get(ROOT, name)
            if wc is not None:
                self._context_terms = self._profile_terms(wc)

    # --- голосові макроси (feature/voice-macros) ---
    def _reload_macros(self):
        """Перечитати macros.toml активного профілю в кеш і запам'ятати mtime.
        Кличеться на старті, після запису з вікна та при перемиканні профілю."""
        self.macros = load_macros(self.profile.macros_path)
        try:
            self._macros_mtime = self.profile.macros_path.stat().st_mtime
        except OSError:
            self._macros_mtime = None

    def _refresh_macros_if_changed(self):
        """Лінивий mtime-кеш: перечитати файл, лише якщо він змінився. Ручні
        правки й записи з вікна підхоплюються без перезапуску."""
        try:
            mtime = self.profile.macros_path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._macros_mtime:
            self._reload_macros()

    def show_window(self):
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        # Відкласти модалку до завершення show/activate: це також єдиний шлях
        # першого видимого відкриття після тихого --autostart.
        QTimer.singleShot(0, self.window.maybe_show_test_log_text_reminder)

    # === плаваюча нотатка (feature/scratchpad-note) ======================
    # Диктування у власне вікно як fallback до вставки в чужий застосунок.
    # Буфер (self._note_buffer) — джерело правди; вікно лише показує й редагує.
    # Взаємно виключне з PTT: _note_dictating гейтить on_press/record_start, а
    # note_busy() гейтить старт нотатки тими самими ознаками зайнятості.
    def show_note(self):
        """Пункт трею/хоткей: відкрити (чи підняти) вікно нотатки."""
        from .note import NoteWindow, center_on_screen
        if self._note_window is None:
            self._note_window = NoteWindow(self)
            center_on_screen(self._note_window)
        else:
            self._note_window.set_text(self._note_buffer)
        self._note_window.show()
        self._note_window.raise_()
        self._note_window.activateWindow()

    # --- буфер (контракт для NoteWindow) ---
    def note_text(self) -> str:
        return self._note_buffer

    def note_set_buffer(self, text: str):
        """Ручні правки з редактора → буфер (джерело правди)."""
        self._note_buffer = text or ""

    def note_clear(self):
        self._note_buffer = ""

    def note_state_value(self) -> str:
        return self._note_state

    def _on_note_appended(self, text: str):
        """GUI-слот: дописати розшифрований рядок у буфер і, якщо вікно живе,
        оновити редактор. Працює навіть коли вікно вже закрили під час
        розшифровки — буфер усе одно поповнюється."""
        from .note import append_note
        self._note_buffer = append_note(self._note_buffer, text)
        if self._note_window is not None:
            self._note_window.set_text(self._note_buffer)

    def note_on_window_closed(self):
        """Вікно закрили: відпустити посилання. Якщо саме йшов ЗАПИС у нотатку —
        зупинити мікрофон і зняти гейти (інакше recorder лишиться зайнятим і
        заблокує PTT назавжди). Розшифровку, що вже триває, не чіпаємо — вона
        сама допише буфер і зніме _note_dictating."""
        self._note_window = None
        if self._note_dictating and self.recorder.recording:
            self.recorder.stop()
            self._note_dictating = False
            self._set_note_state("idle")
            self.tray.set_state("idle")
            self.rec_state.emit("idle")

    def _set_note_state(self, state: str):
        self._note_state = state
        self.note_state.emit(state)

    # --- Command Mode: голосове редагування виділеного (feature/voice-edit-selection) ---
    def command_edit_from_clipboard(self):
        """Scenario A (зовнішній застосунок): зчитати виділене через Ctrl+C (зі
        збереженням/відновленням буфера), відкрити діалог голосового редагування;
        результат вставити НАЗАД у те саме вікно наявним безпечним paste-шляхом.

        Ціль-вікно запам'ятовуємо ЗАРАЗ (воно ще активне): модальний діалог стане
        активним вікном, тож перед вставкою повертаємо фокус саме цій цілі —
        той самий прийом, що feature/paste-preview."""
        from . import wininput
        target = wininput.get_foreground_window()
        selected = capture_selection()
        if not selected.strip():
            self.tray.notify(tr("cmdedit_no_selection_notify"))
            return
        holder = {}
        self.voice_edit_selection(
            selected, lambda new_text: holder.update(text=new_text), parent=None)
        text = holder.get("text")
        if not text:
            return                     # користувач лишив як було / скасував
        wininput.set_foreground_window(target)
        threading.Thread(target=_deliver_paste, args=(self, text, False),
                         daemon=True).start()

    def voice_edit_selection(self, selected_text, apply_fn, parent=None):
        """Спільна точка входу (сценарії A і B): відкрити модальний діалог Command
        Mode для вже зчитаного виділеного тексту. apply_fn(new_text) застосовує
        результат (вставка назад / заміна в редакторі). Голосову команду диктуємо
        через спільний recorder (гейт _command_dictating)."""
        from .pages.command_edit_ui import CommandEditDialog
        from whisper_core import paths, config as cfgmod
        preset_id = getattr(self.cfg, "protocol_model", "fast")
        # УВАГА (рецензія хвилі 5): voice_edit_selection — СПІЛЬНА точка 4 незалежних фіч
        # (глобальний Command Mode, AI-edit головного вікна, AI-edit наради, зворотне
        # диктування Історії). TTS-автопродовження тут НЕ чіпаємо — інакше правка
        # чужого тексту фантомно озвучувала б панель. Резюм-звʼязка — ЛИШЕ через
        # окремий вхід listen_panel_voice_fix() (правка тексту САМОЇ панелі «Прослухати»).
        dlg = CommandEditDialog(
            selected_text, preset_id, apply_fn,
            model_root=paths.protocol_models_dir(),
            custom_models=cfgmod.protocol_custom_models(self.cfg),
            mic_toggle_fn=self._command_record_toggle, parent=parent)
        self._command_dialog = dlg
        try:
            dlg.exec()
        finally:
            if self._command_dictating and self.recorder.recording:
                try:
                    self.recorder.stop()
                except Exception:      # noqa: BLE001
                    pass
            self._command_dictating = False
            self._command_dialog = None

    def _command_record_toggle(self):
        """Кнопка мікрофона в діалозі: почати/зупинити запис голосової команди."""
        if self._command_dictating and self.recorder.recording:
            self._command_record_stop()
        else:
            self._command_record_start()

    def _command_record_start(self):
        if self.note_busy():           # той самий набір гейтів, що нотатка/PTT
            self.tray.notify(tr("app_mic_unavailable"))
            return
        if not self.recorder.has_stream:
            self.tray.notify(tr("app_mic_unavailable"))
            return
        self._command_dictating = True
        self._rec_started = time.time()
        # feature/tts-listen (§9.1 БАР'ЄР): False → не стартуємо запис
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("command"):
            self._command_dictating = False
            return
        self.recorder.start()
        self.tray.set_state("recording")
        request_reload = getattr(self, "_request_model_reload", None)
        if callable(request_reload):
            request_reload()
        if self._command_dialog is not None:
            self._command_dialog.set_recording(True)

    def _command_record_stop(self):
        if not (self._command_dictating and self.recorder.recording):
            return
        chunks = self.recorder.stop()
        threading.Thread(target=self._command_work, args=(chunks, self.terms),
                         daemon=True).start()

    def _command_work(self, chunks, terms):
        """Робочий потік: розшифрувати голосову команду й віддати її в діалог.
        Ділить рушій із PTT/файлами через _engine_lock у _transcribe_with_fallback."""
        try:
            audio = self.recorder.to_audio(chunks)
            if audio is None:
                self.command_edit_cmd_error.emit(tr("app_dictation_silence"))
                return
            _raw, final, _dur, _words, _segs = self._transcribe_with_fallback(audio, terms)
            if final and final.strip():
                self.command_edit_cmd.emit(final)
            else:
                self.command_edit_cmd_error.emit(tr("app_dictation_silence"))
        except Exception as e:         # noqa: BLE001
            logging.exception("Помилка розшифровки голосової команди")
            if is_cuda_runtime_error(e):
                self.command_edit_cmd_error.emit(tr("app_cuda_error"))
            else:
                self.command_edit_cmd_error.emit(tr("app_transcribe_error"))
        finally:
            self._command_dictating = False
            self.tray.set_state("idle")

    def _on_command_cmd_ready(self, text: str):
        """GUI-потік: розшифрована команда → у поле діалогу (не виконуємо самі —
        користувач бачить, редагує за потреби й тисне «Виконати»)."""
        if self._command_dialog is not None:
            self._command_dialog.set_command_text(text)

    def _on_command_cmd_error(self, msg: str):
        if self._command_dialog is not None:
            self._command_dialog.set_recording(False)
        self.tray.notify(msg)

    # --- диктування в нотатку (той самий рушій; гейти взаємного виключення) ---
    def note_busy(self) -> bool:
        """Застосунок зайнятий чимось, що ділить мікрофон/модель — старт нотатки
        заборонено (той самий набір ознак, що й для запису з вікна/наради).
        feature/dictation-queue: _pipeline_busy = і стара блокувальна фраза, і
        активна/очікуюча фраза в черзі (спільні мікрофон і модель)."""
        return (_pipeline_busy(self) or self._capturing or self._mic_testing
                or self.recorder.recording or self._meeting_active)

    def note_record_toggle(self):
        """Кнопка мікрофона у вікні: почати або зупинити запис у нотатку."""
        if self._note_dictating and self.recorder.recording:
            self.note_record_stop()
        else:
            self.note_record_start()

    def note_record_start(self) -> bool:
        if self.note_busy():
            return False
        if not self.recorder.has_stream:
            self.tray.notify(tr("app_mic_unavailable"))
            return False
        self._note_dictating = True
        # feature/processing-slider: чернетка ділить знімок режиму диктування.
        self._dictation_mode = _snapshot_processing_mode(self)
        self._rec_started = time.time()
        diagnostic_event("dictation_started", mode="scratchpad")
        # feature/tts-listen (§9.1 БАР'ЄР): False → не стартуємо запис нотатки
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("note"):
            self._note_dictating = False
            return False
        self.recorder.start()
        self.tray.set_state("recording")
        self._set_note_state("recording")
        request_reload = getattr(self, "_request_model_reload", None)
        if callable(request_reload):
            request_reload()
        return True

    def note_record_stop(self):
        if not (self._note_dictating and self.recorder.recording):
            return
        chunks = self.recorder.stop()
        diagnostic_event("dictation_stopped", mode="scratchpad",
                         duration_s=_diagnostic_elapsed(
                             _diagnostic_attr(self, "_rec_started", None)))
        terms = self.terms
        self._busy = True
        self.tray.set_state("busy")
        self._set_note_state("busy")
        policy = policy_for_mode(getattr(self, "_dictation_mode", None))
        threading.Thread(target=self._note_work, args=(chunks, terms),
                         kwargs={"policy": policy}, daemon=True).start()

    # --- feature/punctuation-plus: постобробка тексту після STT ---------------
    def _apply_text_enhancements(self, final, terms, policy=None):
        """Два ЕКСПЕРИМЕНТАЛЬНІ кроки постобробки, ПІСЛЯ словників профілю
        (apply_glossary у engine.transcribe), ПЕРЕД вставкою й у фіксованому
        порядку: (1) автокорекція одруків, (2) пунктуатор/ITN — пунктуацію
        ставимо вже на виправлених словах. Кожен крок тихо пропускається, коли
        компонент недоступний (пакет/дані не завантажено). getattr — сумісність зі
        старими мок-конфігами тестів.

        feature/processing-slider (блокер №1): що ВМИКАЄ ці кроки — вирішує рівень
        обробки (policy), а не старі тумблери cfg.autocorrect_enabled/punctuator_enabled.
        «Під документ» ставить autocorrect=punctuator=True (спека §3: пунктуатор
        обов'язковий, автокорекція — коли встановлено), тож на типовому профілі, де
        старі тумблери вимкнені, обидва кроки все одно запускаються. За спекою §5 ті
        тумблери стали керуванням готовністю компонента, а не окремою активаційною
        політикою. policy=None → застарілий шлях (старі мок-контролери/тести): читаємо
        тумблери, як раніше. Фактична наявність пакета/даних усе одно перевіряється в
        _run_autocorrect/_run_punctuator, тож недоступний компонент — тихий прохід.

        feature/edit-pack «Не виправляй мою мову»: коли preserve_speech увімкнено —
        ОБИДВА кроки пропускаємо, щоб не чіпати суржик/діалект користувача без згоди.
        Словники профілю (apply_glossary в engine.transcribe) сюди не входять — вони
        лишаються завжди, бо це явні правила користувача."""
        if not final:
            return final
        if getattr(self.cfg, "preserve_speech", False):
            return final
        if policy is None:
            do_autocorrect = getattr(self.cfg, "autocorrect_enabled", False)
            do_punctuator = getattr(self.cfg, "punctuator_enabled", False)
        else:
            do_autocorrect = policy.autocorrect
            do_punctuator = policy.punctuator
        if do_autocorrect:
            _t = time.perf_counter()
            _before = final
            final = self._run_autocorrect(final, terms)
            test_log("pipe_autocorrect", ms=round((time.perf_counter() - _t) * 1000, 1),
                     replacements=_token_diff_count(_before, final), text_out=final)
        if do_punctuator:
            _t = time.perf_counter()
            _before = final
            final = self._run_punctuator(final)
            test_log("pipe_punctuator", ms=round((time.perf_counter() - _t) * 1000, 1),
                     changed=(final != _before), text_out=final)
        return final

    def clean_transcript_text(self, text, terms=None):
        """feature/player-pack: автоматичні зміни тексту, які показує «Огляд перед
        дією» для РУЧНОЇ розшифровки файлу — саме два кроки, названі у фічі: чистка
        філерів + автокорекція одруків. Кожен — за своїм opt-in прапорцем; вимкнені
        пропускаються. Пунктуацію/ITN сюди НЕ включаємо (фіча про філери й одруки).
        Повертає (можливо, незмінений) текст; НЕ мутує стан."""
        if not text:
            return text
        terms = self.terms if terms is None else terms
        out = text
        _level = cleanup_level_for_cfg(self.cfg)
        if _level != "off":
            out = apply_filler_cleanup(out, _level)
        if getattr(self.cfg, "autocorrect_enabled", False):
            out = self._run_autocorrect(out, terms)
        return out

    def _run_autocorrect(self, final, terms):
        """Автокорекція одруків із захистом слів профілю. Корректор (важкий
        частотний словник) будуємо один раз і кешуємо; захищений набір слів
        профілю рахуємо на кожен виклик (профіль може змінюватись)."""
        from whisper_core import paths
        from whisper_core.autocorrect_download import DICT_SHA256
        dict_path = paths.autocorrect_dict_path()
        if not autocorrect.available(dict_path, DICT_SHA256):
            return final
        corrector = getattr(self, "_autocorrector", None)
        if corrector is None:
            corrector = autocorrect.load_corrector(
                dict_path, DICT_SHA256)
            self._autocorrector = corrector
            if corrector is None:
                return final
        protected = _profile_protected_words(terms)
        return corrector.apply(final, protected)

    def _run_punctuator(self, final):
        """Пунктуатор/ITN. Модель (важка ONNX) будуємо один раз і кешуємо."""
        from whisper_core import paths
        model_dir = paths.punctuator_model_dir()
        if not punctuator.available(model_dir):
            return final
        model = getattr(self, "_punctuator_model", None)
        if model is None:
            model = punctuator.load_model(model_dir)
            self._punctuator_model = model
            if model is None:
                return final
        return punctuator.apply_punctuation(final, model, getattr(self.cfg, "language", "uk"))

    def _note_work(self, chunks, terms, policy=None):
        """Робочий потік: розшифрувати запис і дописати в нотатку (append).
        Ділить рушій із PTT/файлами через _engine_lock у _transcribe_with_fallback.
        Пунктуацію/чистку філерів застосовуємо (opt-in) — нотатки саме той сценарій.
        feature/processing-slider: чернетка ділить знімок режиму диктування (спека §3)."""
        try:
            audio = self.recorder.to_audio(chunks)
            if audio is None:
                self.transcription_error.emit(tr("app_dictation_silence"))
                return
            # Нотатка — швидка чернетка: свідомо не застосовуємо capture DSP,
            # щоб gate/AGC не додавали затримки й не змінювали початковий текст.
            _raw, final, _dur, _words, _segs = self._transcribe_with_fallback(
                audio, terms)
            # feature/processing-slider: та сама політика, що й у диктуванні (§3).
            if policy is None:
                _level = cleanup_level_for_cfg(getattr(self, "cfg", None)) if final else "off"
                _allow_voice = _allow_enhance = True
            else:
                if policy.source == "raw":
                    final = _raw
                _level = policy.cleanup_level if final else "off"
                _allow_voice = policy.voice_commands
                _allow_enhance = policy.autocorrect or policy.punctuator
            if final and _level != "off":
                final = apply_filler_cleanup(final, _level)
            if final and _allow_voice and getattr(self.cfg, "voice_punctuation", False):
                final = apply_voice_punctuation(final, self.cfg.language)
            # feature/punctuation-plus: автокорекція + пунктуатор (opt-in) —
            # ПІСЛЯ словників профілю й голосової пунктуації, ПЕРЕД доставкою.
            # getattr — старі мок-контролери тестів не мають методу.
            enhance = getattr(self, "_apply_text_enhancements", None)
            if enhance and _allow_enhance:
                final = enhance(final, terms, policy)
            if final:
                diagnostic_event("dictation_delivered", destination="scratchpad", chars=len(final))
                self.note_appended.emit(final)     # GUI-слот допише буфер
            else:
                self.transcription_error.emit(tr("app_dictation_silence"))
        except Exception as e:
            logging.exception("Помилка розшифровки нотатки")
            if is_cuda_runtime_error(e):
                self.transcription_error.emit(tr("app_cuda_error"))
            else:
                self.transcription_error.emit(tr("app_transcribe_error"))
        finally:
            self._busy = False
            self._note_dictating = False
            self._set_note_state("idle")
            self.finished.emit()                   # трей → idle (спільно з PTT)

    # --- глобальний хоткей нотатки (opt-in) ---
    def _apply_note_hotkey(self):
        """(Пере)повісити глобальну комбінацію відкриття нотатки за cfg.note_hotkey.
        Порожньо = вимкнено. Знімаємо ЛИШЕ свій хук (не unhook_all), тож PTT не
        зачіпаємо. КРИТИЧНО: Hotkey.rebind (зміна PTT-клавіші) робить unhook_all —
        тому _apply_key викликає цей метод ПІСЛЯ rebind, щоб повернути хук нотатки."""
        if self._note_hotkey is not None:
            self._note_hotkey.stop()
            self._note_hotkey = None
        combo = getattr(self.cfg, "note_hotkey", "") or ""
        if not combo:
            return
        hk = hotkeys_native.make_note_hotkey(self.cfg, combo)
        if hk.start():
            hk.triggered.connect(self.show_note)   # keyboard-потік → QueuedConnection
            self._note_hotkey = hk
        else:
            logging.warning("Хоткей нотатки «%s» не встановився — вимикаю", combo)
            self.cfg.note_hotkey = ""
            self.cfg.save()

    def set_note_hotkey(self, combo: str) -> bool:
        """Налаштування: задати/скинути комбінацію нотатки; діє одразу.
        Контракт як у command_edit/bookmark (звірка №6): попередня перевірка
        конфлікту з іншими комбінаціями + чесний bool, щоб UI не показував
        комбінацію, яка фізично не запрацювала."""
        from .hotkey import combos_equal
        combo = combo or ""
        if combo:
            taken = [getattr(self.cfg, key, "") for key in
                     ("ptt_key", "undo_paste_key", "insert_last_key",
                      "command_edit_hotkey", "meeting_bookmark_hotkey",
                      "panic_lock_hotkey")]
            if any(combos_equal(combo, other) for other in taken):
                self.tray.notify(tr("qol_key_conflict"))
                return False
        self.cfg.note_hotkey = combo
        self.cfg.save()
        self._apply_note_hotkey()
        # RegisterHotKey міг відхилити комбінацію (зайнята іншою програмою) —
        # _apply_note_hotkey тоді скидає cfg.note_hotkey; кажемо UI правду.
        if combo and not getattr(self.cfg, "note_hotkey", ""):
            self.tray.notify(tr("qol_key_conflict"))
            return False
        return True

    def start_note_key_capture(self):
        """Той самий діалог захоплення, що й для PTT (KeyCaptureDialog), але
        результат іде у cfg.note_hotkey. Гейтимо через _capturing, щоб PTT/нотатка
        не стартували, поки ловимо клавішу. Емітимо ЛИШЕ при успішній реєстрації."""
        if self._capturing:
            return
        from .pages.settings import KeyCaptureDialog
        self._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window)
            if dlg.exec() and dlg.result_key and self.set_note_hotkey(dlg.result_key):
                self.note_key_captured.emit(dlg.result_key)
        finally:
            self._capturing = False

    def clear_note_hotkey(self):
        """Прибрати комбінацію нотатки (лишається пункт трею)."""
        self.set_note_hotkey("")
        self.note_key_captured.emit("")

    def restart_app(self):
        """Перезапустити застосунок: запустити нову копію ВІДОКРЕМЛЕНИМ процесом,
        далі коректно вийти. Дитина переживає смерть батька (DETACHED_PROCESS +
        close_fds), а single-instance у новому процесі чекає, поки старий звільнить
        QLocalServer (гілка --relaunch у main()) — тож два клавіатурні хуки
        одночасно не існують. Аргументи крос-режимно: frozen — сам exe; dev —
        `python -m fronts.desktop` з cwd кореня репо (як autostart.bat)."""
        if paths.FROZEN:
            args = [sys.executable, "--relaunch"]
            cwd = None
        else:
            args = [sys.executable, "-m", "fronts.desktop", "--relaunch"]
            cwd = str(paths.APP_ROOT)
        flags = 0
        if sys.platform == "win32":
            flags = (subprocess.DETACHED_PROCESS
                     | subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            subprocess.Popen(args, cwd=cwd, creationflags=flags, close_fds=True)
        except Exception:
            logging.exception("Не вдалося запустити нову копію для перезапуску")
            return
        # aboutToQuit → _cleanup зніме keyboard-хуки; QLocalServer (дитина app)
        # звільниться на виході — новий процес саме цього й чекає
        self.app.quit()

    def _cleanup(self):
        """Вихід: запам'ятати геометрію, зняти клавіатурні хуки, звільнити мікрофон."""
        cancel_clipboard_restore()
        self._clear_meeting_plain_cache()
        # feature/tts-listen (§8.9): завершити озвучення й ПРИБРАТИ plaintext-аудіо на
        # виході (інакше конфіденційне WAV лишилось би у %TEMP% після exit).
        ctrl = getattr(self, "_tts_controller", None)
        if ctrl is not None:
            try:
                ctrl.shutdown()
            except Exception:                    # noqa: BLE001
                pass
        timer = getattr(self, "_model_idle_timer", None)
        if timer is not None:
            timer.stop()
        try:
            from whisper_core.protocol import sidecar as protocol_sidecar
            lifecycle = getattr(self, "_model_lifecycle", None)
            if lifecycle is not None:
                protocol_sidecar.remove_activity_listener(lifecycle.touch)
        except Exception:
            pass
        # Вихід не запускає розшифровку: лише контрольовано закриває активну сесію.
        self._shutdown_meeting_for_exit()
        for token in list(getattr(self, "_meeting_processing_jobs", {}).values()):
            token.cancel()
        # Live-consumers мають власні worker-потоки; зупиняємо їх до teardown
        # recorder і Qt-сигналів, щоб вони більше не могли емітити у GUI, що зникає.
        self._stop_live_dictation()
        self._stop_live_meeting()
        # ПЕРШИМ від'єднати cross-thread сигнали: daemon-воркери транскрипції
        # (_file_worker і кожен _work) та потік тесту мікрофона (_mic_test_work)
        # можуть ще працювати і згодом викликати .emit на об'єктах, чия
        # C++-частина руйнується на виході → use-after-free / access violation.
        # Без приймачів emit — безпечний no-op (той самий клас краху, що й для
        # QThread нижче).
        sigs = [self.transcribed, self.finished, self.file_status,
                self.file_done, self.rec_state, self.transcription_error,
                self.cpu_fallback, self.watch_ready,
                self.mic_test_result,                               # feature/audio-qol
                self.preview_ready,                                 # feature/paste-preview
                self.meeting_state, self.meeting_track_done,        # feature/meeting-ui
                self.meeting_session_done, self.meeting_error,
                self.meeting_audio_ready, self.meeting_storage_warning,
                self.meeting_screen_error, self.meeting_processing_progress,
                self.meeting_processing_done,                        # feature/screen-record
                self.screen_record_state, self.screen_record_error, self.screen_record_finished,
                self.live_dictation_segment, self.live_meeting_segment,
                self.live_error, self.live_disable_requested,
                self.dictation_audio_state, self.meeting_audio_state]  # feature/live-transcription
        # feature/scratchpad-note: getattr — мок-контролери тестів без цих сигналів
        for name in ("note_state", "note_appended", "model_lifecycle_state"):
            s = getattr(self, name, None)
            if s is not None:
                sigs.append(s)
        for sig in sigs:
            try:
                sig.disconnect()
            except (TypeError, RuntimeError):
                pass                    # не було з'єднань / об'єкт уже руйнується
        # дочекатись фонової перевірки оновлень, інакше Qt зруйнує живий QThread
        # на виході → «QThread: Destroyed while thread is still running» + abort
        t = getattr(self, "_update_thread", None)
        if t is not None and t.isRunning():
            # check_latest socket-timeout = 5 s; чекаємо довше за нього, інакше
            # Qt може зруйнувати ще живий _UpdateThread і аварійно завершитися.
            t.wait((updates.SOCKET_TIMEOUT_SECONDS + 1) * 1000)
        # feature/auto-update: фонове завантаження інсталятора може ще качати —
        # просимо зупинитись і чекаємо, щоб Qt не зруйнував живий QThread на виході
        dt = getattr(self, "_download_thread", None)
        if dt is not None and dt.isRunning():
            dt.requestInterruption()
            dt.wait(5000)
        # feature/audio-qol: потік тесту мікрофона (~3 с запису + відтворення) —
        # обмежено чекаємо, щоб він не смикав recorder після recorder.close()
        # нижче; mic_test_result уже від'єднано вище, тож його emit безпечний.
        mt = getattr(self, "_mic_test_thread", None)
        if mt is not None and mt.is_alive():
            mt.join(1.0)
        try:
            self.window.remember_geometry()
        except Exception:
            pass
        # feature/scratchpad-note: зняти хук нотатки до unhook_all (порядок не
        # критичний — обидва прибирають keyboard-хуки, але тримаємо симетрію)
        try:
            if getattr(self, "_note_hotkey", None) is not None:
                self._note_hotkey.stop()
        except Exception:
            pass
        # feature/native-hotkeys: native → зупинити менеджер (сам зніме
        # RegisterHotKey-реєстрації); legacy → keyboard.unhook_all, як раніше
        try:
            hotkeys_native.shutdown(self.cfg)
        except Exception:
            pass
        # feature/mouse-ptt: зняти хук миші (join у stop) — після цього хук-потік
        # більше не emit-ить сигнали в об'єкти, що руйнуються на виході
        try:
            if getattr(self, "mouse_hook", None) is not None:
                if not self.mouse_hook.stop():
                    logging.warning("mouse-ptt: зупинку хука під час виходу "
                                    "не підтверджено; посилання збережено")
        except Exception:
            pass
        try:
            self.recorder.close()
        except Exception:
            pass
        # feature/player-recordings: незакритий запис диктофона на виході —
        # фіналізувати (усе, що встигли записати, збережено; заголовок WAV
        # виправляє close, інакше файл лишився б битим)
        try:
            w = getattr(self, "_dictaphone_writer", None)
            if w is not None:
                self._dictaphone_writer = None
                w.close()
        except Exception:
            pass
        # feature/screen-record: спершу закрити MP4, потім аудіо-потоки наради.
        try:
            independent_screen = getattr(self, "_screen_recorder", None)
            if independent_screen is not None:
                independent_screen.stop()
            screen = getattr(self, "_meeting_screen_recorder", None)
            if screen is not None:
                screen.stop()
        except Exception:
            pass
        # feature/meeting-ui: зупинити активні аудіо-потоки наради на виході
        try:
            for s in self._meeting_streams.values():
                s.stop()
        except Exception:
            pass
        # від'єднані воркери докачки моделі (майстер/відновлення) можуть ще жити
        # у reaping-реєстрі до ~30с read-timeout — на виході обмежено дренуємо їх,
        # інакше живий QThread зруйнується на teardown → abort. No-op, коли
        # онбординг не імпортувався або реєстр порожній.
        try:
            from .onboarding import drain_workers
            drain_workers()
        except Exception:
            pass

    def request_quit(self):
        """Вихід із застосунку. Під час активного запису наради — попередити (С7),
        щоб користувач не обірвав нараду мовчки. aboutToQuit усе одно збереже
        сегменти як interrupted; тут даємо шанс скасувати вихід і повернутись."""
        if getattr(self, "_meeting_active", False):
            from PySide6.QtWidgets import QMessageBox
            answer = QMessageBox.question(
                getattr(self, "window", None),
                tr("meeting_quit_recording_title"),
                tr("meeting_quit_recording_body"))
            if answer != QMessageBox.Yes:
                return
        self.app.quit()

    def _shutdown_meeting_for_exit(self):
        """aboutToQuit: закрити носії й позначити сесію interrupted без postprocess."""
        detached = self._detach_meeting()
        if detached is None:
            return
        sess, screen = detached
        if screen is not None:
            # Це GUI-потік aboutToQuit: даємо MP4 лише короткий шанс на MOOV,
            # бо застосунок усе одно завершується, а довше очікування фризить вихід.
            if not screen.wait_finished(3.0):
                logging.error("MP4 не закрився за 3 с під час виходу; фіналізація interrupted")
                self._mark_screen_failed(sess, None)
            elif getattr(screen, "finished_error", False):
                self._mark_screen_failed(sess, screen.error)
        try:
            from whisper_core.meeting import session as msession
            sess.finalize(msession.STATUS_INTERRUPTED)
        except Exception:
            logging.exception("Не вдалося фіналізувати interrupted-нараду під час виходу")
    # --- налаштування (контракт для вкладки Налаштування) ---
    def save_config(self):
        self.cfg.save()

    def set_model(self, name: str):
        self.cfg.model_name = name
        self.cfg.save()          # набуде чинності після перезапуску (hot-swap — пізніше)

    def set_language(self, code: str):
        self.cfg.language = code # діє одразу: engine читає cfg.language на кожен виклик
        self.cfg.save()

    def set_ui_language(self, code: str):
        """Мова ІНТЕРФЕЙСУ (не диктування): зберегти; діє після перезапуску."""
        self.cfg.ui_language = code
        self.cfg.save()

    def set_device(self, dev: str):
        self.cfg.device = dev
        self.cfg.save()

    def set_compute_type(self, ctype: str):
        """Точність обчислень моделі (compute_type). Діє після перезапуску (модель
        уже завантажена). Startup-нормалізація відсіє несумісне з режимом."""
        self.cfg.compute_type = ctype
        self.cfg.save()

    def set_model_idle_unload(self, seconds: int):
        """Застосувати idle-таймер одразу; 0 = “Ніколи”."""
        seconds = max(0, int(seconds or 0))
        self.cfg.model_idle_unload_seconds = seconds
        self.cfg.save()
        lifecycle = getattr(self, "_model_lifecycle", None)
        if lifecycle is not None:
            lifecycle.set_timeout(seconds)
        timer = getattr(self, "_model_idle_timer", None)
        if timer is not None:
            if seconds:
                timer.start()
            else:
                timer.stop()

    def set_ptt_mode(self, mode: str):
        self.cfg.ptt_mode = mode
        self.cfg.save()
        self.window.dictation.set_ptt_mode(mode)

    def set_ptt_mouse_button(self, button: str):
        """feature/mouse-ptt: обрати бічну кнопку миші (none|x1|x2); перезапустити хук."""
        self.cfg.ptt_mouse_button = button
        self.cfg.save()
        self._apply_mouse_ptt()

    def _apply_mouse_ptt(self):
        """Підняти/зупинити хук бічної кнопки за поточним cfg. Зміна кнопки =
        перезапуск хука. Кнопка викликає ТІ САМІ сигнали hotkey (pressed/released),
        тож режим утримання/перемикача працює однаково з клавіатурою. Хук не
        встановився → попередження + тост + скид налаштування в none (щоб не лишати
        ввімкнене, що не діє)."""
        if self.mouse_hook is not None:
            if not self.mouse_hook.stop():
                logging.warning("mouse-ptt: зупинку чинного хука не підтверджено; "
                                "посилання збережено")
                return
            self.mouse_hook = None
        button = getattr(self.cfg, "ptt_mouse_button", "none")
        if button not in ("x1", "x2"):
            return
        from .mousehook import MouseHook
        hook = MouseHook(button,
                         on_press=self.hotkey.pressed.emit,
                         on_release=self.hotkey.released.emit)
        if hook.start():
            self.mouse_hook = hook
        else:
            logging.warning("mouse-ptt: хук бічної кнопки не встановився — "
                            "скидаю налаштування в none")
            self.cfg.ptt_mouse_button = "none"
            self.cfg.save()
            self.tray.notify(tr("app_mouse_ptt_failed"))

    def set_input_device(self, name):
        """Вибір мікрофона: зберегти ІМʼЯ у config і одразу перевідкрити потік.
        Помилка відкриття не має валити застосунок (recorder сам відкотиться)."""
        self.cfg.input_device = name or None
        self.cfg.save()
        try:
            self.recorder.set_input_device(self.cfg.input_device)
        except Exception:
            logging.exception("Не вдалося застосувати вибраний мікрофон")

    def set_model_dir(self, path: str):
        """Тека з моделями; порожній рядок = стандартний кеш HuggingFace.
        Набуде чинності після перезапуску (модель уже завантажена)."""
        self.cfg.model_dir = path or None
        self.cfg.save()

    # --- feature/ux-center: експорт/імпорт налаштувань одним .zip ---
    def export_settings_to(self, zip_path):
        """Зібрати переносний архів налаштувань (config/словники/профілі/сніпети,
        БЕЗ історії/аудіо/моделей). Кидає OSError при збою запису."""
        from whisper_core import settings_io
        settings_io.export_settings(
            zip_path,
            config_path=paths.config_path(),
            snippets_path=paths.snippets_path(),
            context_profiles_path=paths.context_profiles_path(),
            profiles_root=paths.profiles_root(),
            version=DISPLAY_VERSION)

    def import_settings_from(self, zip_path, backup_path):
        """Розпакувати архів у теку користувача (бекап поточного стану — поруч).
        Повертає шлях бекапа або None. Кидає settings_io.SettingsArchiveError
        на невалідному архіві (поточний стан при цьому не чіпається)."""
        from whisper_core import settings_io
        return settings_io.import_settings(
            zip_path,
            user_dir=paths.user_dir(),
            profiles_root=paths.profiles_root(),
            backup_path=backup_path,
            version=DISPLAY_VERSION)

    def inspect_profile_archive(self, zip_path):
        """Отримати метадані про архів перед підтвердженням імпорту."""
        from whisper_core import settings_io
        return settings_io.inspect_archive(
            zip_path, current_version=DISPLAY_VERSION)

    def import_profile_with_backup(self, zip_path):
        """Імпортувати архів з автоматичним створення теки бекапу у теці даних."""
        from whisper_core import settings_io
        return settings_io.import_settings_with_dir_backup(
            zip_path,
            user_dir=paths.user_dir(),
            profiles_root=paths.profiles_root(),
            version=DISPLAY_VERSION)

    # --- feature/ux-center: позиція плаваючого індикатора диктування ---
    def set_pill_position(self, x, y):
        self.cfg.pill_x = int(x)
        self.cfg.pill_y = int(y)
        self.cfg.save()

    def reset_pill_position(self):
        """Забути збережену позицію → пілюля повернеться до типової."""
        self.cfg.pill_x = None
        self.cfg.pill_y = None
        self.cfg.save()
        if getattr(self, "pill", None) is not None:
            self.pill.reset_to_default()

    def hotkey_bindings(self):
        """Активні гарячі клавіші для шпаргалки й сторінки Налаштувань. Лише те,
        що вже в master: диктування (клавіатура), режим hold/toggle, бічна кнопка
        миші PTT. Повертає список (ключ-назви, значення-для-показу)."""
        from .hotkey import pretty
        mode_key = ("hotkeys_mode_hold" if self.cfg.ptt_mode == "hold"
                    else "hotkeys_mode_toggle")
        rows = [("hotkeys_dictate", pretty(self.cfg.ptt_key)),
                ("hotkeys_mode", tr(mode_key))]
        btn = getattr(self.cfg, "ptt_mouse_button", "none")
        if btn in ("x1", "x2"):
            rows.append(("hotkeys_mouse", tr("set_mouse_" + btn)))
        bookmark = getattr(self.cfg, "meeting_bookmark_hotkey", "") or ""
        if bookmark:
            rows.append(("hotkeys_meeting_bookmark", pretty(bookmark)))
        return rows

    def show_cheat_sheet(self):
        """Пункт трею «Гарячі клавіші» → компактна картка-довідка активних
        комбінацій (feature/ux-center)."""
        from .cheatsheet import HotkeyCheatSheet
        sheet = getattr(self, "_cheat_sheet", None)
        if sheet is None:
            sheet = HotkeyCheatSheet(self.hotkey_bindings)
            self._cheat_sheet = sheet
        sheet.refresh()
        sheet.show()
        sheet.raise_()
        sheet.activateWindow()

    def open_help(self, _checked=False):
        """Пункт трею «Довідка» → інструкція (локальний README, інакше репо)."""
        from .help import open_user_guide
        open_user_guide(getattr(self, "window", None))

    # --- feature/audio-qol: чутливість VAD (діє з наступної транскрипції) ---
    def set_vad(self, threshold: float, min_silence_ms: int,
                min_speech_ms: "int | None" = None):
        """Параметри Silero VAD із Налаштувань. Наступний виклик engine.transcribe
        читає cfg — перезапуск не потрібен. min_speech_ms — feature/audio-center
        (None → не чіпати наявне значення, сумісність зі старими викликами)."""
        self.cfg.vad_threshold = float(threshold)
        self.cfg.vad_min_silence_ms = int(min_silence_ms)
        if min_speech_ms is not None:
            self.cfg.vad_min_speech_ms = int(min_speech_ms)
        self.cfg.save()

    # --- feature/audio-center: вивід для тесту мікрофона + обробка (gate/AGC) ---
    def set_output_device(self, name):
        """Пристрій виводу для відтворення тесту мікрофона. Зберігаємо ІМʼЯ;
        застосовується у recorder.play_audio (з відкатом, якщо пристрій зник)."""
        self.cfg.output_device = name or None
        self.cfg.save()

    def set_noise_gate(self, enabled: bool, threshold_db: float):
        """Шумовий гейт перед Whisper (opt-in). Діє з наступного диктування."""
        self.cfg.noise_gate_enabled = bool(enabled)
        self.cfg.noise_gate_threshold_db = float(threshold_db)
        self.cfg.save()

    def set_agc(self, enabled: bool, target_db: float):
        """Лінійний нормалізатор гучності перед Whisper (opt-in). Діє з наступного диктування."""
        self.cfg.agc_enabled = bool(enabled)
        self.cfg.agc_target_db = float(target_db)
        self.cfg.save()

    # --- feature/qol-pack: скасувати / повторити останню вставку ---
    def undo_last_paste(self):
        """Трей/хоткей: скасувати останню вставку (стерти вставлені символи
        Backspace-ами). Викликається в GUI-потоці; сама вставка/стирання — у
        daemon-потоці (є пауза на повернення фокуса після трей-меню)."""
        if not self._undo.has_undo():
            self.tray.notify(tr("qol_undo_nothing"))
            return
        count = self._undo.consume_undo()
        threading.Thread(target=self._undo_worker, args=(count,),
                         daemon=True).start()

    def _undo_worker(self, count):
        time.sleep(0.25)     # дати фокусу повернутись у ціль (після закриття меню)
        if not undo_paste(count):
            self.transcription_error.emit(tr("qol_undo_fail"))

    def deliver_text(self, text: str):
        """feature/voice-form-fill: доставити готовий текст існуючим paste-шляхом
        (той самий _insert_worker, що й «вставити ще раз»). Ціль — попереднє
        активне вікно ОС; модалку заповнення варто перед цим сховати."""
        if text:
            threading.Thread(target=self._insert_worker,
                             args=(text,), daemon=True).start()

    def insert_last_again(self):
        """Трей/хоткей: вставити останню розшифровку ще раз."""
        if not self._undo.has_text():
            self.tray.notify(tr("qol_insert_nothing"))
            return
        threading.Thread(target=self._insert_worker,
                         args=(self._undo.last_text,), daemon=True).start()

    # --- feature/paste-safety: буфер останніх вставок ---
    def _recent_pastes(self):
        """Провайдер трею: останні вставки цієї сесії (найновіша першою)."""
        return self._paste_history.recent()

    def paste_recent(self, text: str):
        """Трей «Останні вставки»: вставити обраний текст ще раз (той самий шлях,
        що й «Вставити ще раз»). Ціль — активне вікно ЗАРАЗ (свідома дія
        користувача з меню), тож закріплення не застосовуємо."""
        if text:
            threading.Thread(target=self._insert_worker,
                             args=(text,), daemon=True).start()

    def _insert_worker(self, text):
        restore = self.cfg.restore_clipboard
        if restore:
            previous = begin_clipboard_restore()
        else:
            cancel_clipboard_restore()
            previous = ""
        typing = getattr(self.cfg, "paste_typing_fallback", False)
        owner_hwnd = getattr(self, "_clipboard_owner_hwnd", None)
        try:
            if owner_hwnd:
                result = paste_text(text, typing_fallback=typing, owner_hwnd=owner_hwnd)
            else:
                result = paste_text(text, typing_fallback=typing)
        except BaseException:
            if restore:
                end_clipboard_restore()
            raise
        if result == PASTE_BLOCKED:
            if restore:
                end_clipboard_restore()
            self.transcription_error.emit(tr("app_paste_blocked"))
        elif result is None:
            if restore:
                end_clipboard_restore()
            self.transcription_error.emit(tr("app_paste_failed"))
        else:
            diagnostic_event("dictation_delivered", destination="paste", chars=len(text))
            if restore:
                restore_clipboard(previous, expected=text)
            self._undo.record(text)   # повторна вставка — теж скасовувана
            if getattr(self.cfg, "paste_history_enabled", True):
                self._paste_history.record(text)   # feature/paste-safety (тумблер)
            if getattr(self.cfg, "paste_confirm_sound", False):
                _play_chime(self.cfg, "paste")

    def set_log_level(self, value: str):
        self.cfg.log_level = apply_log_level(value)
        # у режимі тестування рівень тримаємо на DEBUG — не даємо його перебити
        apply_test_mode(self.cfg)
        self.cfg.save()

    def set_test_mode(self, enabled: bool, include_text: bool):
        """Режим тестування (детальний журнал). Вмикає DEBUG + дія-логер; DEBUG
        не персистить — вимкнення повертає рівень за log_level. Позначку в
        сайдбарі оновлює сигнал test_mode_changed."""
        self.cfg.test_mode = bool(enabled)
        self.cfg.test_mode_include_text = bool(include_text)
        apply_test_mode(self.cfg)
        self.cfg.save()
        self.test_mode_changed.emit(bool(enabled))

    # --- feature/qol-pack: setters пакета зручностей (діють одразу) ---
    def set_autostop_silence(self, seconds: int):
        self.cfg.dictation_autostop_silence_s = max(0, int(seconds))
        self.cfg.save()

    def set_max_duration(self, seconds: int):
        self.cfg.dictation_max_duration_s = max(0, int(seconds))
        self.cfg.save()

    def set_paste_confirm_sound(self, on: bool):
        self.cfg.paste_confirm_sound = bool(on)
        self.cfg.save()

    def set_paste_history_enabled(self, on: bool):
        """feature/paste-safety: тумблер буфера останніх вставок. Вимкнення чистить
        буфер сесії — щоб чутливі диктування (військовий кейс) не лишались у
        трей-підменю «Останні вставки» на розблокованій машині."""
        self.cfg.paste_history_enabled = bool(on)
        self.cfg.save()
        if not on:
            self._paste_history.clear()

    def set_player_backstep(self, seconds: float):
        self.cfg.player_resume_backstep_s = float(seconds)
        self.cfg.save()
        from .player import set_resume_backstep_ms
        set_resume_backstep_ms(int(float(seconds) * 1000))

    def set_quiet_hours(self, enabled: bool, start: str, end: str):
        self.cfg.quiet_hours_enabled = bool(enabled)
        self.cfg.quiet_hours_start = start
        self.cfg.quiet_hours_end = end
        self.cfg.save()

    def set_action_hotkey(self, which: str, key: str) -> bool:
        """Оновити хоткей дії ('undo' | 'insert'); "" = вимкнути. Перевішуємо
        одразу (окремо від PTT-хука). Конфлікт із PTT-комбо чи іншим хоткеєм
        дії → відхиляємо з тостом і повертаємо False (UI не оновлює напис)."""
        key = key or ""
        if which not in ("undo", "insert"):
            return False
        if key:
            taken = {
                "ptt": getattr(self.cfg, "ptt_key", ""),
                "undo": getattr(self.cfg, "undo_paste_key", ""),
                "insert": getattr(self.cfg, "insert_last_key", ""),
                "note": getattr(self.cfg, "note_hotkey", ""),
                "command_edit": getattr(self.cfg, "command_edit_hotkey", ""),
                "bookmark": getattr(self.cfg, "meeting_bookmark_hotkey", ""),
                "panic_lock": getattr(self.cfg, "panic_lock_hotkey", ""),
            }
            taken.pop(which, None)      # своє поточне значення — не конфлікт
            if any(combos_equal(key, other) for other in taken.values()):
                self.tray.notify(tr("qol_key_conflict"))
                return False
        if which == "undo":
            self.cfg.undo_paste_key = key
        else:
            self.cfg.insert_last_key = key
        self.cfg.save()
        self.action_hotkeys.apply(
            getattr(self.cfg, "undo_paste_key", "") or "",
            getattr(self.cfg, "insert_last_key", "") or "")
        return True

    def installed_model_names(self) -> list:
        """feature/qol-pack: моделі застосунку, доступні для повторної розшифровки —
        активна (завантажена в рушій) + ті, що є на диску. Для кнопки «Спробувати
        іншою моделлю».

        Активну модель додаємо ЗАВЖДИ й ПЕРШОЮ, незалежно від MODEL_REVISIONS: вона
        вже завантажена в рушій і працює, навіть якщо це small/medium чи власна
        модель (тека/HF-id), якої нема в пінах. Раніше перебирали лише ключі
        MODEL_REVISIONS → на small/medium/власній кнопка брехала «немає моделей»,
        хоча активна модель щойно розшифрувала (регрес по слабких ПК)."""
        from whisper_core.engine import MODEL_REVISIONS
        from whisper_core.models import repo_for, model_snapshot_size
        active = str(getattr(self.cfg, "model_name", "") or "").strip()
        out = [active] if active else []
        for name in MODEL_REVISIONS:
            if name == active:
                continue                         # активну вже додали першою
            try:
                if model_snapshot_size(self.cfg.model_dir, repo_for(name)) > 0:
                    out.append(name)
            except Exception:
                logging.debug("Не вдалося перевірити модель %s на диску", name,
                              exc_info=True)
        return out

    # --- feature/audio-qol: тест мікрофона (Discord/Teams: записав → програв) ---
    def mic_test_busy(self) -> bool:
        """Застосунок зайнятий (запис/розшифровка/захоплення клавіші/уже тестує/
        йде нарада) — той самий гейт зайнятості, що й для старту запису з вікна.
        Нарада ділить із тестом той самий мікрофон, тож блокує його теж [integration].
        feature/dictation-queue: _pipeline_busy враховує й активну/очікуючу чергу."""
        return (_pipeline_busy(self) or self._capturing or self._mic_testing
                or self.recorder.recording or self._meeting_active
                or getattr(self, "_note_dictating", False)   # feature/scratchpad-note
                or getattr(self, "_dictaphone_active", False))  # feature/player-recordings

    def start_mic_test(self) -> bool:
        """Записати MIC_TEST_SECONDS с з ОБРАНОГО мікрофона тим самим recorder, що
        й диктування, відтворити користувачу на пристрій виводу за замовчуванням і
        дати вердикт за піком. Робота — у daemon-потоці (блокує ~6 с); результат
        приходить сигналом mic_test_result у GUI-потік. Повертає False, якщо
        зайнято (кнопка й так неактивна — це страхувальний гейт)."""
        if self.mic_test_busy():
            return False
        if not self.recorder.has_stream:      # мікрофона немає — одразу «тиша»
            self.mic_test_result.emit("silence")
            return True
        self._mic_testing = True
        # feature/tts-listen (§9.1 БАР'ЄР): гейт у GUI-потоці ДО запуску фонового
        # _mic_test_thread (before_microphone_start чіпає QMediaPlayer — лише GUI).
        # False → тест мікрофона не стартує.
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("mic_test"):
            self._mic_testing = False
            return False
        self._mic_test_thread = threading.Thread(
            target=self._mic_test_work, daemon=True)
        self._mic_test_thread.start()
        return True

    def _mic_test_work(self):
        """Робочий потік тесту: запис → вердикт → відтворення. Будь-який виняток
        ловимо (тест не має нічого валити) і звітуємо 'error'."""
        verdict = "silence"
        try:
            self.recorder.start()
            time.sleep(MIC_TEST_SECONDS)
            chunks = self.recorder.stop()
            audio = self.recorder.to_audio(chunks)
            verdict = classify_mic_level(audio)
            if audio is not None:
                try:
                    play_audio(audio, self.cfg.sample_rate,
                               getattr(self.cfg, "output_device", None))
                except Exception:
                    logging.exception("Тест мікрофона: не вдалося відтворити запис")
        except Exception:
            logging.exception("Тест мікрофона впав")
            verdict = "error"
        finally:
            self._mic_testing = False
            self.mic_test_result.emit(verdict)

    # --- feature/auto-export: автозбереження розшифровок у теку (діє одразу) ---
    def set_auto_export_enabled(self, on: bool):
        self.cfg.auto_export_enabled = bool(on)
        self.cfg.save()

    def set_auto_export_dir(self, path: str):
        self.cfg.auto_export_dir = path or None
        self.cfg.save()

    def set_auto_export_format(self, fmt: str):
        self.cfg.auto_export_format = fmt if fmt in ("md", "txt") else "md"
        self.cfg.save()

    # --- feature/obsidian-channel: канал доставки нарад у Obsidian (діє одразу) ---
    def set_obsidian_enabled(self, on: bool):
        self.cfg.obsidian_enabled = bool(on)
        self.cfg.save()

    def set_obsidian_dir(self, path: str):
        self.cfg.obsidian_dir = path or None
        self.cfg.save()

    def set_obsidian_filename_template(self, template: str):
        from whisper_core import obsidian
        self.cfg.obsidian_filename_template = (
            (template or "").strip() or obsidian.DEFAULT_TEMPLATE)
        self.cfg.save()

    def _auto_export(self, text: str):
        """Дописати завершену розшифровку у файл-день вибраної теки (md/txt).
        Викликається лише коли автозбереження ввімкнене й теку задано (гейт —
        на місці виклику, як у voice_punctuation). Помилка запису (тека зникла /
        нема прав) не валить конвеєр: попередження в лог і ОДНОРАЗОВИЙ тост у
        трей (наступний успіх скидає прапорець), без спаму на кожен запис."""
        try:
            export.append_transcript(
                self.cfg.auto_export_dir, text, datetime.datetime.now(),
                getattr(self.cfg, "auto_export_format", "md"))
            self._export_warned = False
        except OSError:
            logging.warning("Автозбереження розшифровки не вдалося (тека %s)",
                            paths.anonymize_path(self.cfg.auto_export_dir), exc_info=True)
            if not self._export_warned:
                self.transcription_error.emit(tr("app_export_failed"))
                self._export_warned = True

    # --- feature/watch-folder: стеження за текою (діє одразу, без перезапуску) ---
    def set_watch_enabled(self, on: bool):
        self.cfg.watch_enabled = bool(on)
        self.cfg.save()
        self._apply_watch_config()

    def set_watch_dir(self, path: str):
        self.cfg.watch_dir = path or None
        self.cfg.save()
        self._apply_watch_config()

    def _apply_watch_config(self):
        """Увімкнути/вимкнути спостереження за поточним cfg. Наявні у теці файли
        заносимо у _watch_seen як «вже враховані» — обробляємо лише те, що
        з'явиться ПІСЛЯ цього моменту (не наявне на старті/на вмиканні)."""
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        if not (self.cfg.watch_enabled and self.cfg.watch_dir):
            return
        d = self.cfg.watch_dir
        if not os.path.isdir(d):
            logging.warning("Тека спостереження недоступна: %s", paths.anonymize_path(d))
            return
        try:
            for name in os.listdir(d):
                self._watch_seen.add(os.path.join(d, name))
        except OSError:
            logging.exception("Не вдалося прочитати теку спостереження: %s",
                               paths.anonymize_path(d))
            return
        self._watcher.addPath(d)

    def _on_watch_dir_changed(self, path):
        """GUI-потік: тека змінилась → знайти нові аудіофайли й у робочому потоці
        дочекатись, поки кожен допишеться (файл міг ще копіюватись)."""
        try:
            entries = [os.path.join(path, n) for n in os.listdir(path)]
        except OSError:
            return
        files = [f for f in entries if os.path.isfile(f)]
        for f in watch.new_audio_files(files, self._watch_seen):
            self._watch_seen.add(f)      # більше не розглядати (у роботі/оброблено)
            threading.Thread(target=self._watch_settle, args=(f,),
                             daemon=True).start()

    def _watch_settle(self, path):
        """Робочий потік: блокуюче чекання стабільності розміру (без гальмування
        GUI), готовий файл маршалимо в GUI сигналом (QueuedConnection)."""
        if watch.wait_until_stable(path):
            self.watch_ready.emit(path)

    def _watch_enqueue(self, path):
        """GUI-потік: у ту саму чергу, що й ручне додавання (add_files — і рядок
        у вкладці «Файли», і enqueue_file), тоді тост у трей."""
        try:
            self.window.files.add_files([path])
        except Exception:
            logging.exception("watch: не вдалося поставити файл у чергу: %s",
                              anonymize_path(path))
            return
        self.tray.notify(tr("app_watch_queued", name=os.path.basename(path)))

    def _apply_runtime_cpu_fallback(self):
        """GUI-потік: без гонки з Settings зберегти fallback і оновити радіокнопки."""
        _prepare_cpu_config(self.cfg)
        self.cfg.save()
        # Новий Engine створено зі snapshot-конфігом лише для конструктора;
        # далі повертаємо йому live-конфіг, щоб зміна мови діяла без перезапуску.
        self.engine.cfg = self.cfg
        if hasattr(self.window, "settings"):
            self.window.settings.sync_device("cpu")
        self.tray.notify(tr("app_cuda_fallback_retry"))

    # --- перевірка оновлень (GitHub Releases; жодного автозавантаження) ---
    def _maybe_check_updates(self):
        """Старт: перевірити, лише якщо минув тиждень від останньої вдалої спроби."""
        if not self.cfg.check_updates:
            return
        s = QSettings("Balachky", "Balachky")
        last = s.value("update_last_check", 0, type=int)
        if time.time() - last < _UPDATE_INTERVAL:
            return
        self._start_update_check()

    def check_updates_now(self):
        """Кнопка «Перевірити зараз»: ігнорує троттл і прапорець opt-out."""
        self._start_update_check()

    def _start_update_check(self):
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        s = QSettings("Balachky", "Balachky")
        etag = s.value("update_etag", "", type=str) or None
        self._update_thread = _UpdateThread(PEP440_VERSION, etag, self)
        self._update_thread.result.connect(self._on_update_result)
        self._update_thread.start()

    def _on_update_result(self, res):
        """GUI-потік: зберегти стан у QSettings і сповістити Налаштування.
        Час останньої перевірки оновлюємо ЛИШЕ при вдалому контакті — офлайн
        лишає старий стан, наступний запуск спробує знову (без ретраю зараз)."""
        s = QSettings("Balachky", "Balachky")
        if res.status != updates.OFFLINE:
            s.setValue("update_last_check", int(time.time()))
            if res.etag:
                s.setValue("update_etag", res.etag)
            if res.status == updates.UPDATE_AVAILABLE:
                s.setValue("update_latest", res.latest_version or "")
                s.setValue("update_url", res.url or "")
                # feature/auto-update: дані доставки (asset+SHA+«що нового»)
                s.setValue("update_installer_url", res.installer_url or "")
                s.setValue("update_sha256", res.sha256 or "")
                s.setValue("update_notes", res.notes or "")
            elif res.status == updates.UP_TO_DATE:
                # GitHub підтвердив latest release — прибрати застарілу «нову версію»
                for key in ("update_latest", "update_url", "update_installer_url",
                            "update_sha256", "update_notes"):
                    s.remove(key)
            # NOT_MODIFIED: нічого не змінилось — лишаємо збережену версію/URL
        self.update_result.emit(res)
        # opt-in автозавантаження: тихо тягнемо інсталятор у фоні (за замовч. ВИМК)
        if (res.status == updates.UPDATE_AVAILABLE
                and getattr(self.cfg, "auto_download_updates", False)):
            self.start_installer_download(res.installer_url, res.sha256)

    def update_state(self):
        """Поточний стан оновлень для вкладки Налаштування:
        (поточна_версія, нова_версія|None, url|None, вже_перевіряли). Нова
        версія — лише якщо збережений тег справді новіший за поточну."""
        s = QSettings("Balachky", "Balachky")
        latest = s.value("update_latest", "", type=str) or None
        url = s.value("update_url", "", type=str) or None
        if latest and (not updates.is_newer(latest, PEP440_VERSION)
                       or not updates.is_release_url(url)):
            # Неповний/тестовий кеш не показуємо як реальне оновлення.
            s.remove("update_latest")
            s.remove("update_url")
            latest, url = None, None
        checked = bool(s.value("update_last_check", 0, type=int))
        return DISPLAY_VERSION, latest, url, checked

    # --- feature/auto-update: доставка інсталятора (завантаження + запуск) ---
    def delivery_state(self):
        """Дані доставки нової версії: (installer_url|None, sha256|None,
        notes|None). Порожньо, коли реліз не має встановлюваного asset."""
        s = QSettings("Balachky", "Balachky")
        url = s.value("update_installer_url", "", type=str) or None
        sha = s.value("update_sha256", "", type=str) or None
        notes = s.value("update_notes", "", type=str) or None
        return url, sha, notes

    def start_installer_download(self, installer_url=None, sha256=None):
        """Запустити фонове завантаження інсталятора (ручне з Налаштувань або
        авто). Без URL+SHA — тиха відмова (немає що/як перевіряти)."""
        from whisper_core import updater
        if not installer_url or not sha256:
            installer_url, sha256, _ = self.delivery_state()
        if not installer_url or not sha256:
            return
        ready = updater.installer_ready(installer_url, sha256)
        if ready is not None:
            self.update_downloaded.emit(str(ready))
            return
        if self._download_thread is not None and self._download_thread.isRunning():
            return
        self._download_thread = _DownloadThread(installer_url, sha256, self)
        self._download_thread.progress.connect(self.download_progress)
        self._download_thread.done.connect(self.update_downloaded)
        self._download_thread.failed.connect(self.download_failed)
        self._download_thread.start()

    def launch_installer_and_quit(self, path):
        """Запустити завантажений інсталятор ЗВИЧАЙНИМ чином (не /SILENT —
        per-user Inno поверх, з майстром) і коректно завершити застосунок,
        щоб файли не були зайняті під час перевстановлення."""
        from PySide6.QtCore import QProcess
        from whisper_core import updater
        installer_url, sha256, _ = self.delivery_state()
        if not installer_url or not sha256:
            return
        ready = updater.installer_ready(
            installer_url, sha256, rehash=True)
        if ready is None:
            return
        try:
            if Path(path).resolve() != ready.resolve():
                return
        except OSError:
            return
        QProcess.startDetached(str(ready))
        self.app.quit()

    def start_key_capture(self):
        """Захоплення клавіші «як у Discord»: модальний діалог слухає клавіатуру,
        показує натиснуте live і валідовує (рівно 1 основна + >=1 модифікатор).
        Поки діалог відкритий — PTT гейтимо через _capturing, щоб запис не стартував."""
        if self._capturing:
            return
        from .pages.settings import KeyCaptureDialog
        self._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window)
            if dlg.exec() and dlg.result_key and self._apply_key(dlg.result_key):
                self.key_captured.emit(dlg.result_key)   # оновити напис у Налаштуваннях
        finally:
            self._capturing = False

    def _apply_key(self, name: str) -> bool:
        """GUI-потік: застосувати нову клавішу запису. Чесний контракт (звірка
        №8): конфлікт з іншими хоткеями відхиляємо ДО запису cfg; при відмові
        реєстрації (комбінацію тримає інша програма) cfg не чіпаємо і кажемо
        правду тостом — жодного «встановлено» при мовчазному відкаті."""
        from .hotkey import pretty, combos_equal
        taken = [getattr(self.cfg, key, "") for key in
                 ("undo_paste_key", "insert_last_key", "note_hotkey",
                  "command_edit_hotkey", "meeting_bookmark_hotkey",
                  "panic_lock_hotkey")]
        if any(combos_equal(name, other) for other in taken):
            self.tray.notify(tr("qol_key_conflict"))
            return False
        if not self.hotkey.rebind(name):
            self.tray.notify(tr("qol_key_conflict"))
            return False
        self.cfg.ptt_key = name
        self.cfg.save()
        # legacy-бекенд: rebind робить keyboard.unhook_all — повертаємо свої
        # хоткеї дій (нативний бекенд: reapply — безпечний no-op-перевіс)
        self.action_hotkeys.reapply()
        self.bookmark_hotkey.reapply()
        self.command_edit_hotkey.reapply()   # feature/voice-edit-selection
        self._apply_note_hotkey()
        self._apply_panic_hotkey()           # legacy rebind знімає всі хоткеї — вертаємо панік
        self.window.dictation.set_shortcut(name)
        self.tray.notify(tr("app_key_set", name=pretty(name)))
        return True

    def reset_ptt_key(self):
        """feature/ux-center: повернути типову комбінацію запису (сторінка
        «Гарячі клавіші», кнопка «Скинути»)."""
        from .hotkey import _DEFAULT_KEY
        if self._apply_key(_DEFAULT_KEY):
            self.key_captured.emit(_DEFAULT_KEY)   # оновити напис у Налаштуваннях

    # --- feature/live-transcription -----------------------------------------
    def set_live_transcription(self, on: bool):
        """GUI-потік: зберегти вибір і одразу змінити active live instance."""
        on = bool(on)
        self.cfg.live_transcription = on
        self.cfg.save()
        if on:
            if self.recorder.recording:
                self._start_live_dictation(self._context_terms or self.terms)
        else:
            self._stop_live_dictation()
            self._stop_live_meeting()

    def _disable_live_transcription_from_gui(self, failed_live):
        """Queued worker error → GUI: вимикаємо лише ще актуальний consumer."""
        if failed_live is not self._live_dictation and failed_live is not self._live_meeting:
            return
        self.set_live_transcription(False)
        # чекбокс живого режиму живе на вкладці Налаштування → Диктування
        # (перенесено з Наради, аудит 22.07)
        self.window.settings.sync_live_transcription(False)

    def _live_failed(self, live, exc):
        logging.warning("Live transcription disabled: %s", exc)
        self.live_disable_requested.emit(live)
        self.live_error.emit(tr("app_live_error"))

    def _start_live_dictation(self, terms):
        if not getattr(self.cfg, "live_transcription", False):
            return
        if getattr(self, "engine", None) is None:
            return                         # idle-reload: фінальний STT дочекається моделі
        self._stop_live_dictation()
        live = LiveTranscriber(
            self.engine, terms=terms, engine_lock=self._engine_lock,
            pause_ms=getattr(self.cfg, "vad_min_silence_ms", 500) + 200,
            on_segment=self.live_dictation_segment.emit)
        live.on_error = lambda exc, live=live: self._live_failed(live, exc)
        self._live_dictation = live
        self.recorder.set_live_sink(live.feed)

    def _stop_live_dictation(self):
        self.recorder.set_live_sink(None)
        live, self._live_dictation = self._live_dictation, None
        if live is not None:
            live.stop()  # async: не затримує stop запису

    def _start_live_meeting(self):
        """Залишено як compatibility no-op: нарада під час capture лише пише звук."""
        return

    def _stop_live_meeting(self):
        live, self._live_meeting = self._live_meeting, None
        if live is not None:
            live.stop()

    def set_meeting_live_processing(self, on: bool):
        """Тумблер довіри в Нараді: миттєво вимкнути (on=False) чи знову ввімкнути
        (on=True) ЖИВУ обробку — розшифровку й діаризацію — для ПОТОЧНОЇ сесії.
        Сам запис аудіо триває незалежно; пост-обробка після зупинки наради йде за
        налаштуваннями (цей прапорець її не чіпає). Скидається на новій нараді."""
        self._meeting_live_disabled = not on
        if not on:
            self._stop_live_meeting()      # прибрати вже активну живу розшифровку
        elif self._meeting_active:
            self._start_live_meeting()     # знову підняти (guard прочитає прапорець)

    # --- push-to-talk (GUI-потік, слоти сигналів hotkey) ---
    def on_press(self):
        was_down = self._key_down    # feature/double-tap: чи це авто-повтор (клавіша
        self._key_down = True        # вже була затиснута) — тоді не рахуємо як новий тап
        if self._mic_testing:        # feature/audio-qol: під час тесту мікрофона
            return                   # recorder зайнятий записом тесту — PTT ігноруємо
        if self._cancel_guard:       # запис скасували мишею, клавіша ще затиснута —
            return                   # авто-повтор не має рестартувати запис
        if self._capturing:          # під час вибору нової клавіші запис не стартує
            return
        if self._meeting_active:     # feature/meeting-ui: йде нарада — PTT тихо ігноруємо
            return                   # (один мікрофон, одна модель — розділ 3.2)
        if getattr(self, "_note_dictating", False):  # feature/scratchpad-note:
            return                   # диктування в нотатку тримає мікрофон — PTT виключне
        if getattr(self, "_command_dictating", False):  # feature/voice-edit-selection:
            return                   # диктування команди тримає мікрофон — PTT виключне
        if self._dictaphone_active:  # feature/player-recordings: диктофон зайняв recorder
            return                   # (той самий мікрофон) — PTT тихо ігноруємо
        if self.cfg.ptt_mode == "toggle":
            # натиснув — почав / натиснув — зупинив
            if self.recorder.recording:
                # 0.6с — захист: авто-повтор утримуваної клавіші не має зупиняти запис
                if time.time() - self._rec_started > 0.6:
                    self._stop_and_transcribe()
            # feature/dictation-queue: фонова розшифровка більше не блокує старт —
            # блокує лише повна черга (тоді чесний тост).
            elif _dictation_start_blocked(self):
                if _is_queue_full(self):
                    _queue_full_toast(self)
            else:
                self._start_recording()
            return
        if self.cfg.ptt_mode == "double_tap":
            self._on_double_tap(was_down)
            return
        # режим утримання; recorder.recording — захист від OS-автоповтору
        # (Windows шле повторні KEY_DOWN, без гейта кожен обнуляв би буфер)
        if _dictation_start_blocked(self):
            # feature/dictation-queue: повна черга → чесний тост (не на автоповтор
            # утримуваної клавіші — той відсіється, бо recorder.recording).
            if _is_queue_full(self) and not self.recorder.recording:
                _queue_full_toast(self)
            return
        self._start_recording()

    def on_release(self):
        self._key_down = False
        if self._mic_testing:        # feature/audio-qol: не чіпати recorder тесту
            return
        self._cancel_guard = False
        self._mic_warned = False        # нове утримання клавіші — попередити знову
        if self.cfg.ptt_mode in ("toggle", "double_tap"):
            return                       # тут відпускання нічого не робить —
                                         # старт/стоп керуються натисками (тапами)
        if not self.recorder.recording:
            return
        self._stop_and_transcribe()

    def _on_double_tap(self, was_down: bool):
        """feature/double-tap: hands-free тригер. Швидкий подвійний тап PTT-комбо
        (у межах double_tap_ms, клампимо 200..600) стартує запис; одинарний тап
        під час запису — зупиняє. Відпускання клавіші ролі не грає.
        was_down=True → це OS-автоповтор утримуваної клавіші, а не новий тап."""
        if was_down:
            return
        now = time.time()
        if self.recorder.recording:
            self._stop_and_transcribe()   # одинарний тап під час запису = стоп
            self._last_tap = 0.0
            return
        # feature/dictation-queue: фонова розшифровка більше не блокує старт —
        # блокує лише повна черга (тоді чесний тост).
        if _dictation_start_blocked(self):
            if _is_queue_full(self):
                _queue_full_toast(self)
            return
        window = min(600, max(200, int(self.cfg.double_tap_ms))) / 1000.0
        if self._last_tap and (now - self._last_tap) <= window:
            self._last_tap = 0.0          # другий тап у вікні → старт
            self._start_recording()
        else:
            self._last_tap = now          # перший тап (або надто пізній) → чекаємо на пару

    def _start_recording(self):
        # feature/no-model-state: мовний пакет розпізнавання ще не завантажено
        # (NullEngine) — запис не починаємо, чесно кажемо, чого бракує.
        # getattr — сумісність зі старими фейк-self тестами без has_model.
        if not getattr(self, "has_model", True):
            self.tray.notify(tr("app_model_absent_ptt"))
            self.transcription_error.emit(tr("app_model_absent_ptt"))
            return
        # мікрофон недоступний → потоку нема, запис дав би тишу без жодного
        # сигналу користувачу. Попереджаємо (раз на сесію-утримання) і не входимо
        # у стан «запис», щоб не було фальшивого «розпізнаю» без результату.
        if not self.recorder.has_stream:
            if not self._mic_warned:
                self.tray.notify(tr("app_mic_unavailable"))
                self._mic_warned = True
            return
        self._mic_warned = False
        self._capture_context_dictionary()   # feature/context-profiles: словник за вікном
        # feature/office-voice-nav: закріпити ціль навігації — активне вікно на
        # момент старту диктанту (документ Word/Excel; глобальний хоткей фокус не
        # краде). Перед надсиланням навігаційних клавіш звіримо, що фокус там само.
        from . import wininput
        self._nav_target_hwnd = wininput.get_foreground_window()
        # feature/paste-safety: закріпити ціль вставки ЗАРАЗ (активне вікно — те
        # поле, куди диктують). Перед фактичною вставкою звіримо, що фокус той самий.
        self._paste_target = wininput.capture_paste_target()
        # feature/processing-slider: знімок режиму обробки на СТАРТІ запису — рух
        # повзунка під час запису застосується вже до наступної фрази (спека §5).
        self._dictation_mode = _snapshot_processing_mode(self)
        self._rec_started = time.time()
        # feature/tts-listen (§9.1 БАР'ЄР): глушимо озвучення ДО live-STT І мікрофона;
        # False → не стартуємо (озвучка не підтверджено зупинена).
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("dictation"):
            return
        self._start_live_dictation(self._context_terms or self.terms)
        self.recorder.start()
        self.tray.set_state("recording")
        self.rec_state.emit("recording")
        request_reload = getattr(self, "_request_model_reload", None)
        if callable(request_reload):
            request_reload()
        self._start_dictation_watch()   # feature/qol-pack: автостоп/ліміт
        diagnostic_event("dictation_started", mode=_diagnostic_attr(
            _diagnostic_attr(self, "cfg", None), "ptt_mode"))

    def _stop_and_transcribe(self):
        diagnostic_event("dictation_stopped", mode=_diagnostic_attr(
            _diagnostic_attr(self, "cfg", None), "ptt_mode"),
                         duration_s=_diagnostic_elapsed(
                             _diagnostic_attr(self, "_rec_started", None)))
        self._stop_dictation_watch()    # feature/qol-pack
        chunks = self.recorder.stop()
        self._stop_live_dictation()     # feature/live-transcription: спинити прев'ю
        # знімок профілю/термінів на момент фрази: перемикання профілю в треї
        # під час розпізнавання не перекине запис у чужу пам'ять
        profile, terms = self.profile, self.terms
        # feature/context-profiles: якщо профіль вікна задав свій словник —
        # транскрибуємо з ним (пам'ять лишається профілю self.profile)
        if self._context_terms is not None:
            terms = self._context_terms
        self._refresh_macros_if_changed()     # feature/voice-macros: підхопити ручні правки
        # feature/processing-slider: політика зі знімка режиму на старті цієї фрази
        # (None → DEFAULT verbatim, безпечно для старих мок-контролерів тестів).
        policy = policy_for_mode(getattr(self, "_dictation_mode", None))
        # feature/dictation-queue: у режимі черги фразу ставимо в чергу — рушій
        # звільняється, а користувач може одразу диктувати наступну.
        if _should_queue(self):
            self._enqueue_dictation(chunks, profile, terms, policy)
            return
        self._busy = True
        self.tray.set_state("busy")
        self.rec_state.emit("busy")
        threading.Thread(target=self._work, args=(chunks, profile, terms),
                         kwargs={"policy": policy}, daemon=True).start()

    # --- feature/dictation-queue (запит Миколи №10) ------------------------------
    # Гейти _should_queue/_dictation_start_blocked/_is_queue_full/_pipeline_busy/
    # _queue_full_toast — модульні функції вгорі файлу (getattr-захищені для стабів).
    def _enqueue_dictation(self, chunks, profile, terms, policy=None):
        """Поставити фразу в чергу зі знімком СВОЄЇ цілі вставки (HWND+PID+заголовок)
        і навігаційної цілі — щоб результат пішов саме у своє вікно, навіть коли
        наступний запис уже перезаписав self._paste_target. feature/processing-slider:
        режим обробки теж закріплюється в джобі (знімок на старті фрази, спека §5)."""
        from . import wininput
        pinned = getattr(self, "_paste_target", None)   # (HWND, заголовок) старту фрази
        hwnd = pinned[0] if pinned else None
        title = pinned[1] if pinned else ""
        pid = wininput.get_window_pid(hwnd) if hwnd else None
        nav = getattr(self, "_nav_target_hwnd", None)
        dur = max(0.0, time.time() - getattr(self, "_rec_started", time.time()))
        job = self._queue.enqueue(chunks, profile, terms,
                                  paste_target=(hwnd, pid, title),
                                  nav_target=nav, duration_s=dur, policy=policy)
        if job is None:
            # Черга щойно переповнилась (рідкісна гонка) — фразу НЕ губимо:
            # обробляємо старим блокувальним шляхом.
            self._busy = True
            self.tray.set_state("busy")
            self.rec_state.emit("busy")
            threading.Thread(target=self._work, args=(chunks, profile, terms),
                             kwargs={"policy": policy}, daemon=True).start()

    def _process_queue_job(self, job):
        """Обробити одну фразу з черги (у потоці черги, строго по одній). Ціль
        вставки й PID — із джоба (закріплені на старті СВОЄЇ фрази)."""
        pt = job.paste_target or (None, None, "")
        self._work(job.chunks, job.profile, job.terms,
                   paste_target=(pt[0], pt[2]), paste_pid=pt[1], from_queue=True,
                   policy=job.processing_policy)

    def _apply_queue_state(self, pending: int, active: bool):
        """GUI-слот стану черги (queue_state — QueuedConnection). Пілюля показує
        «+N»; трей/стан ведемо лише коли ЗАРАЗ не йде запис нової фрази (щоб не
        перебити червоний «Запис»).

        pending — скільки фраз у черзі очікує. Бейдж = справжній ХВІСТ за поточною:
        коли активної фрази ще нема, найближча в черзі от-от стане активною, тож її
        з хвоста не рахуємо (щоб для однієї фрази не блимало «+1»)."""
        tail = pending if active else max(0, pending - 1)
        if self.pill is not None:
            try:
                self.pill.set_queue_count(tail)
            except Exception:
                pass
        if self.recorder.recording:
            return
        if active or pending:
            self.tray.set_state("busy")
            self.rec_state.emit("busy")
        else:
            self.tray.set_state("idle")
            self.rec_state.emit("idle")

    def _on_dictation_audio_state(self, state: str):
        diagnostic_event("dictation_audio_state", state=state)
        """GUI-слот стану recorder: тост не блокує retry, а крайня відмова
        завершує поточну фразу звичайним шляхом транскрипції."""
        if state == "reconnecting":
            self.tray.notify(tr("app_mic_reconnecting"))
        elif state == "reconnected":
            self.tray.notify(tr("app_mic_reconnected"))
        elif state == "fallback":
            self.tray.notify(tr("app_mic_fallback"))
        elif state == "failed":
            self.tray.notify(tr("app_mic_recovery_failed"))
            if self.recorder.recording:
                self._stop_and_transcribe()

    # --- запис кнопками з вікна (сторінка «Диктування») ---
    # Той самий конвеєр, що й PTT: _start_recording / _stop_and_transcribe;
    # стани синхронізує сигнал rec_state — з клавіші й з кнопок однаково.
    def record_start(self):
        """Почати запис із вікна. Гейти ті самі, що у PTT-натиску."""
        # feature/dictation-queue: _dictation_start_blocked уже враховує запис/‌busy/
        # повну чергу; фонова розшифровка більше не блокує старт.
        if (_dictation_start_blocked(self) or self._capturing
                or self._meeting_active       # feature/meeting-ui: нарада блокує диктування
                or getattr(self, "_note_dictating", False)   # note теж
                or self._dictaphone_active):  # feature/player-recordings: диктофон блокує
            if _is_queue_full(self) and not self.recorder.recording and not self._busy:
                _queue_full_toast(self)
            return
        self._mic_warned = False        # свідомий клік — попередити, якщо мікрофона нема
        self._start_recording()

    def record_stop(self):
        """Зупинити запис із вікна → стандартна транскрипція."""
        if self.recorder.recording:
            self._stop_and_transcribe()

    def record_cancel(self):
        """Скасувати запис: аудіо відкидається, транскрипція не запускається."""
        if not self.recorder.recording:
            return
        self._stop_dictation_watch()     # feature/qol-pack
        self.recorder.stop()             # знімок нікуди не передаємо — відкинуто
        self._stop_live_dictation()
        self._cancel_guard = self._key_down   # клавіша ще затиснута? глушимо повтори
        self.tray.set_state("idle")
        self.rec_state.emit("idle")

    def record_pause(self, on: bool):
        """Пауза: recorder перестає писати кадри (чесний прапорець у callback)."""
        self.recorder.set_paused(on)

    # --- feature/paste-preview: картка перегляду перед вставкою (GUI-потік) ---
    def _on_preview_ready(self, text, auto_enter):
        """Показати картку перегляду біля курсора.

        feature/paste-safety: ціль вставки беремо з ПОЧАТКУ диктування
        (self._paste_target), а НЕ перезахоплюємо тут. Інакше перемикання вікна
        під час розшифровки мовчки стало б новою ціллю — і вставка пішла б у чуже
        вікно (військовий кейс). Тут, поки картки ще нема й ціль ще активна,
        звіряємо поточний фокус зі стартовим піном: якщо користувач перемкнувся —
        фокус не той, і на «Вставити» спрацює той самий блок, що й у _deliver_paste
        (текст лишається в буфері + тост). Картку показуємо завжди — з неї можна
        «Копіювати» текст вручну."""
        from .preview import PreviewCard
        from . import wininput
        pinned = getattr(self, "_paste_target", None)   # (HWND, заголовок) старту
        gate = getattr(self.cfg, "paste_confirm_on_window_change", True)
        focus_ok, changed_window = True, ""
        if gate and pinned:
            cur_hwnd, cur_title = wininput.capture_paste_target()
            if target_changed(pinned[0], cur_hwnd):
                focus_ok, changed_window = False, cur_title
        self._close_preview()          # лишаємо одну картку: нова заміняє попередню
        card = PreviewCard(text)
        card.accepted.connect(
            lambda t, ae=auto_enter, tgt=pinned, ok=focus_ok, win=changed_window:
                self._preview_deliver(t, ae, tgt, ok, win))
        card.copied.connect(self._preview_copy)
        card.cancelled.connect(self._close_preview)
        self._preview_card = card
        card.show_near_cursor()

    def _close_preview(self):
        """Закрити й забути живу картку (безпечно, якщо вже зруйнована)."""
        card = self._preview_card
        self._preview_card = None
        if card is not None:
            try:
                card.close()
            except RuntimeError:
                pass                   # C++-обʼєкт картки вже зруйновано

    def _preview_deliver(self, text, auto_enter, target, focus_ok=True,
                         changed_window=""):
        """«Вставити»: повернути фокус цілі й вставити текст наявним paste-шляхом.
        paste_text блокує ~0.3с (sleep) — робимо у daemon-потоці, не морозячи GUI.
        target — (HWND, заголовок) цілі, ЗАКРІПЛЕНОЇ на СТАРТІ диктування.

        feature/paste-safety: focus_ok=False → під час розшифровки активним стало
        інше вікно (виявлено ще у _on_preview_ready). Тоді НЕ вставляємо наосліп —
        той самий військовий блок, що й у _deliver_paste: текст лишаємо в буфері
        обміну й повідомляємо. Інакше повертаємо фокус саме СТАРТОВІЙ цілі (картка
        могла його вкрасти під час правки) і вставляємо; _deliver_paste ще раз
        звірить активне вікно з піном — якщо повернути фокус не вдалось, теж блок."""
        from . import wininput
        self._preview_card = None      # картка сама закриється (WA_DeleteOnClose)
        gate = getattr(self.cfg, "paste_confirm_on_window_change", True)
        if gate and target and not focus_ok:
            import pyperclip
            pyperclip.copy(text)       # гарантуємо текст у буфері (paste_text не звали)
            logging.info("Перегляд: активне вікно змінилось від старту диктування — "
                         "вставку не роблю, текст лишається в буфері")
            self.transcription_error.emit(
                tr("app_paste_window_changed", window=(changed_window or "?")))
            return
        hwnd = target[0] if target else None
        wininput.set_foreground_window(hwnd)
        threading.Thread(target=_deliver_paste,
                         args=(self, text, auto_enter, target),
                         daemon=True).start()

    def _preview_copy(self, text):
        """«Копіювати»: лише в буфер, без вставки."""
        self._preview_card = None
        try:
            import pyperclip
            pyperclip.copy(text)
        except Exception:
            logging.exception("Перегляд: не вдалося скопіювати в буфер")

    # === режим «Нарада» (feature/meeting-ui) =============================
    # Ядро (whisper_core.meeting.capture/session/postprocess) підвантажується
    # ЛЕНИВО в методах: модуль існує лише після злиття Б1/Б3, а старт/смоук/
    # тести мусять працювати й без нього. Уся робота з залізом і диском — у
    # ядрі; тут лише оркестрація (черга, сигнали, гейти, трей).

    def _meetings_root(self) -> Path:
        """Активна тека сховища нарад (cfg або дефолт paths.meetings_dir())."""
        return Path(self.cfg.meeting_dir) if self.cfg.meeting_dir else paths.meetings_dir()

    def _meeting_session_dir(self, session_id) -> Path:
        """id сесії = імʼя теки (локальний час старту)."""
        return self._meetings_root() / session_id

    def meeting_session_dir(self, session_id) -> Path:
        """Публічний шлях теки сесії (feature/ai-protocol: збереження protocol.md)."""
        return self._meeting_session_dir(session_id)

    def _clear_meeting_plain_cache(self, session_id=None):
        cache = getattr(self, "_meeting_plain_cache", {})
        keys = list(cache) if session_id is None else [str(session_id)]
        success = True
        for key in keys:
            item = cache.get(key)
            if item is not None:
                try:
                    cleanup_result = item[0].cleanup()
                    if cleanup_result is False or Path(item[1]).exists():
                        success = False
                    else:
                        cache.pop(key, None)
                except Exception:
                    logging.warning("Could not remove decrypted meeting temp for %s", key)
                    success = False
        self._meeting_plain_cache = cache
        return success

    def _materialized_meeting_dir(self, session_id) -> Path:
        """Path-only media bridge; temporary plaintext is owned and cleaned."""
        original = self._meeting_session_dir(session_id)
        if not (original / "meeting.json.enc").exists():
            return original
        cache = getattr(self, "_meeting_plain_cache", None)
        if cache is None:
            self._meeting_plain_cache = cache = {}
        key = str(session_id)
        cached = cache.get(key)
        if cached is not None:
            return cached[1]
        from whisper_core.meeting import session as msession
        owner = tempfile.TemporaryDirectory(prefix="balachky-meeting-media-")
        destination = Path(owner.name) / original.name
        try:
            msession.decrypt_session_to(original, destination)
        except Exception:
            owner.cleanup()
            raise
        cache[key] = (owner, destination)
        return destination

    def protocol_model_ready(self) -> bool:
        """feature/ai-protocol: чи завантажено АКТИВНУ модель LLM (пресет або
        власну) для протоколу/Q&A/переформатування."""
        from whisper_core.protocol import service
        from whisper_core import paths as _paths, config as _cfgmod
        return service.model_available(
            getattr(self.cfg, "protocol_model", "fast"),
            _paths.protocol_models_dir(),
            _cfgmod.protocol_custom_models(self.cfg))

    def set_meeting_bookmark_hotkey(self, combo: str) -> bool:
        """Задати опційний глобальний хоткей «Мітка» без конфлікту з іншими."""
        from .hotkey import combos_equal
        combo = combo or ""
        if combo:
            taken = [getattr(self.cfg, key, "") for key in
                     ("ptt_key", "undo_paste_key", "insert_last_key", "note_hotkey",
                      "command_edit_hotkey", "panic_lock_hotkey")]   # симетрія: command_edit перевіряє нас
            if any(combos_equal(combo, other) for other in taken):
                self.tray.notify(tr("qol_key_conflict"))
                return False
        self.cfg.meeting_bookmark_hotkey = combo
        self.cfg.save()
        self.bookmark_hotkey.apply(combo, "")
        return True

    def start_meeting_bookmark_key_capture(self):
        if self._capturing:
            return
        from .pages.settings import KeyCaptureDialog
        self._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window)
            if dlg.exec() and dlg.result_key and self.set_meeting_bookmark_hotkey(dlg.result_key):
                self.meeting_bookmark_key_captured.emit(dlg.result_key)
        finally:
            self._capturing = False

    def clear_meeting_bookmark_hotkey(self):
        self.set_meeting_bookmark_hotkey("")
        self.meeting_bookmark_key_captured.emit("")

    # --- feature/voice-edit-selection: глобальний хоткей Command Mode ---
    def set_command_edit_hotkey(self, combo: str) -> bool:
        """Задати опційний глобальний хоткей «Редагувати виділене голосом» без
        конфлікту з іншими комбінаціями."""
        from .hotkey import combos_equal
        combo = combo or ""
        if combo:
            taken = [getattr(self.cfg, key, "") for key in
                     ("ptt_key", "undo_paste_key", "insert_last_key", "note_hotkey",
                      "meeting_bookmark_hotkey", "panic_lock_hotkey")]
            if any(combos_equal(combo, other) for other in taken):
                self.tray.notify(tr("qol_key_conflict"))
                return False
        self.cfg.command_edit_hotkey = combo
        self.cfg.save()
        self.command_edit_hotkey.apply(combo, "")
        return True

    def start_command_edit_key_capture(self):
        if self._capturing:
            return
        from .pages.settings import KeyCaptureDialog
        self._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window)
            if dlg.exec() and dlg.result_key and self.set_command_edit_hotkey(dlg.result_key):
                self.command_edit_key_captured.emit(dlg.result_key)
        finally:
            self._capturing = False

    def clear_command_edit_hotkey(self):
        self.set_command_edit_hotkey("")
        self.command_edit_key_captured.emit("")

    # --- feature/mil-hardening: Panic-Lock та захист екрана ---
    def _apply_panic_hotkey(self):
        if hasattr(self, "_panic_hotkey") and self._panic_hotkey is not None:
            self._panic_hotkey.stop()
            self._panic_hotkey = None
        combo = getattr(self.cfg, "panic_lock_hotkey", "") or ""
        if not combo:
            return
        hk = hotkeys_native.make_note_hotkey(self.cfg, combo)
        if hk.start():
            hk.triggered.connect(self.trigger_panic_lock)
            self._panic_hotkey = hk
        else:
            logging.warning("Хоткей Panic-Lock «%s» не встановився — вимикаю", combo)
            self.cfg.panic_lock_hotkey = ""
            self.cfg.save()

    def set_panic_lock_hotkey(self, combo: str) -> bool:
        combo = combo or ""
        if combo:
            taken = [getattr(self.cfg, key, "") for key in
                     ("ptt_key", "undo_paste_key", "insert_last_key", "note_hotkey",
                      "command_edit_hotkey", "meeting_bookmark_hotkey")]
            if any(combos_equal(combo, other) for other in taken):
                self.tray.notify(tr("qol_key_conflict"))
                return False
        self.cfg.panic_lock_hotkey = combo
        self.cfg.save()
        self._apply_panic_hotkey()
        if combo and not getattr(self.cfg, "panic_lock_hotkey", ""):
            self.tray.notify(tr("qol_key_conflict"))
            return False
        return True

    def start_panic_key_capture(self):
        if self._capturing:
            return
        from .pages.settings import KeyCaptureDialog
        self._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window)
            if dlg.exec() and dlg.result_key and self.set_panic_lock_hotkey(dlg.result_key):
                self.panic_lock_key_captured.emit(dlg.result_key)
        finally:
            self._capturing = False

    def clear_panic_lock_hotkey(self):
        self.set_panic_lock_hotkey("")
        self.panic_lock_key_captured.emit("")

    def trigger_panic_lock(self):
        """Panic-lock (хоткей чи кнопка) — СУВОРІШИЙ за meeting_vault_lock:
        1. Знищити відкриті й залишкові plaintext temp-файли.
        2. Вивантажити З ПАМ’ЯТІ всі DEK нарад — не лише активного сейфу (lock_vault
           чистить один корінь; .clear() чистить усі → суворіше).
        3. Очистити буфер обміну.
        4. Згорнути вікна застосунку.
        """
        def clear_keys():
            from whisper_core.meeting import storage_crypto
            storage_crypto._PASSWORD_CACHE.clear()
            return True

        def clear_system_clipboard():
            from whisper_core.win_hardening import clear_clipboard
            return panic_clear_clipboard(clear_clipboard)

        def minimize_window():
            if hasattr(self, "window") and self.window:
                self.window.showMinimized()
            return True

        steps = (
            ("panic_step_meeting_cache", self._clear_meeting_plain_cache),
            ("panic_step_temp_files", _cleanup_panic_plaintext_temps),
            ("panic_step_keys", clear_keys),
            ("panic_step_clipboard", clear_system_clipboard),
            ("panic_step_window", minimize_window),
        )
        failures = []
        for key, action in steps:
            try:
                if action() is False:
                    failures.append(key)
            except Exception:
                logging.exception("Panic-lock step failed: %s", key)
                failures.append(key)

        result = tuple(failures)
        if result:
            items = ", ".join(tr(key) for key in result)
            self.tray.notify(tr("panic_toast_partial", items=items))
        else:
            self.tray.notify(tr("panic_toast_locked"))
        return result

    def set_screen_protection(self, enable: bool):
        from whisper_core.win_hardening import (
            is_display_affinity_supported, set_capture_protection_enabled)
        self.cfg.screen_protection = bool(enable)
        self.cfg.save()
        # Реєстр містить головне вікно й живі вікна нарад. Не вгадуємо стан із
        # cfg: агрегуємо тільки підтверджені результати кожного Win32-виклику.
        results = set_capture_protection_enabled(bool(enable))
        if enable:
            state = _aggregate_screen_protection_state(
                results, supported=is_display_affinity_supported())
        elif results and any(
                not getattr(result, "succeeded", False)
                for result in results):
            state = _aggregate_screen_protection_state(
                results, supported=is_display_affinity_supported())
        else:
            state = ""
        return DesktopApp._publish_screen_protection_state(self, state)

    def screen_protection_state(self) -> str:
        """Фактичний стан; порожньо, коли вимкнення підтверджене або ще pending."""
        from whisper_core.win_hardening import (
            capture_protection_results, is_display_affinity_supported)
        supported = is_display_affinity_supported()
        if not supported:
            return "unsupported"
        results = capture_protection_results()
        if not bool(getattr(self.cfg, "screen_protection", False)):
            failed_removals = tuple(
                result for result in results
                if not getattr(result, "enabled", True)
                and not getattr(result, "succeeded", False)
            )
            if failed_removals:
                return _aggregate_screen_protection_state(
                    failed_removals, supported=supported)
            return ""
        if not results:
            return ""
        return _aggregate_screen_protection_state(
            results, supported=supported)

    def _publish_screen_protection_state(
            self, state: str | None = None, *, only_if_changed: bool = False
    ) -> str:
        if state is None:
            state = DesktopApp.screen_protection_state(self)
        previous = getattr(
            self, "_screen_protection_last_emitted_state", None)
        self._screen_protection_last_emitted_state = state
        if not only_if_changed or state != previous:
            self.screen_protection_state_changed.emit(state)
        return state

    def _on_screen_protection_result_removed(self) -> str:
        return DesktopApp._publish_screen_protection_state(
            self, only_if_changed=True)

    def apply_screen_protection_to_window(self, widget):
        """Зареєструвати живе вікно й оприлюднити фактичний результат."""
        from whisper_core.win_hardening import protect_window
        result = protect_window(
            widget,
            on_result_removed=lambda:
                DesktopApp._on_screen_protection_result_removed(self))
        DesktopApp._publish_screen_protection_state(self)
        return result

    def remove_screen_protection_from_window(self, widget) -> bool:
        """Явно прибрати закрите вікно; GC-finalizer лишається страхувальником."""
        from whisper_core.win_hardening import unprotect_window
        return unprotect_window(widget)

    def add_meeting_bookmark(self, title: str = "", source: str = "live_button") -> bool:
        """Зберегти мітку від початку активної наради; поза нею — тихий no-op.

        Викликається і з кнопки на сторінці, і з глобальної гарячої клавіші —
        ЖОДЕН зі шляхів не має блокувати нараду діалогом: підпис можна додати
        пізніше (Етап 2+ спеки). ``source`` лише позначає, звідки прийшла
        мітка (``live_button``/``live_hotkey``) — для доказової події нижче.

        Подія ``bookmark_added`` дописується у audit.jsonl тим самим шляхом
        (``_audit_event``), що і created/stopped/finalized: збій журналу
        (повний диск, тека лише для читання) не має зривати саму мітку."""
        sess = getattr(self, "_meeting_session", None)
        if not getattr(self, "_meeting_active", False) or sess is None:
            return False
        try:
            elapsed = max(0.0, time.monotonic() - getattr(self, "_meeting_started_at", time.monotonic()))
            add = getattr(sess, "add_bookmark", None)
            sess_dir = getattr(sess, "dir", None) or getattr(sess, "_dir", None)
            if callable(add):
                add(elapsed, title)
            else:  # сумісність із застарілим ядром/фейками UI
                from whisper_core.meeting.session import add_bookmark
                if sess_dir is not None:
                    add_bookmark(sess_dir, elapsed, title)
            if sess_dir is not None:
                _audit_event(sess_dir, "bookmark_added", note={
                    "timestamp": round(elapsed, 3),
                    "title": (title or "").strip()[:120],
                    "source": source,
                })
            self.tray.notify(tr("meeting_bookmark_added"))
            return True
        except Exception:
            logging.exception("Не вдалося додати мітку наради")
            self.tray.notify(tr("meeting_bookmark_failed"))
            return False
    def set_meeting_sources(self, preset: str):
        """Пресет із вкладки/Налаштувань → cfg.meeting_sources (пишемо назад,
        щоб наступного разу пропонувати той самий)."""
        from whisper_core.config import (MEETING_SYSTEM_SOURCE,
                                         meeting_microphone_token,
                                         meeting_mic_devices,
                                         meeting_sources_for_preset)
        self.cfg.meeting_sources = meeting_sources_for_preset(preset)
        if preset == "multimic":
            names = meeting_mic_devices(self.cfg)
        else:
            names = [getattr(self.cfg, "input_device", None)]
        self.cfg.meeting_record_sources = [meeting_microphone_token(name) for name in names]
        if preset == "both":
            self.cfg.meeting_record_sources.append(MEETING_SYSTEM_SOURCE)
        self.cfg.save()

    def set_meeting_dir(self, path: str):
        """Тека записів нарад; порожньо = дефолт (локальна, поза синхронізацією)."""
        self.cfg.meeting_dir = path or None
        self.cfg.save()

    def set_meeting_screen_enabled(self, enabled: bool):
        self.cfg.meeting_screen_enabled = bool(enabled)
        self.cfg.save()

    def set_meeting_screen_monitor(self, monitor_index: int):
        self.cfg.meeting_screen_monitor = max(1, int(monitor_index))
        self.cfg.save()

    def list_meeting_screen_monitors(self):
        """Ліниво перелічити екрани: mss може бути відсутнім у старій інсталяції."""
        try:
            from whisper_core.meeting.screen_record import list_monitors
            return list_monitors()
        except Exception:
            logging.exception("Не вдалося перелічити монітори наради")
            return []

    # --- незалежний «Запис екрана» ------------------------------------------
    def _screen_recordings_root(self) -> Path:
        return (Path(self.cfg.screen_recordings_dir) if getattr(self.cfg, "screen_recordings_dir", None)
                else paths.screen_recordings_dir())

    def set_screen_recordings_dir(self, path: str):
        self.cfg.screen_recordings_dir = path or None
        self.cfg.save()

    def list_screen_monitors(self):
        try:
            from whisper_core.meeting.screen_record import list_monitors
            return list_monitors()
        except Exception:
            logging.exception("Не вдалося перелічити монітори запису екрана")
            return []

    def list_screen_windows(self):
        try:
            from whisper_core.screen.win32 import list_windows
            return list_windows()
        except Exception:
            logging.exception("Не вдалося перелічити вікна запису екрана")
            return []

    def screen_record_start(self, source: dict, options: dict) -> bool:
        if self._screen_recorder and self._screen_recorder.is_running:
            logging.warning("Запис екрана вже триває — повторний старт відхилено")
            self.screen_record_error.emit(tr("screen_error_already_recording"))
            return False
        try:
            from whisper_core.screen.recorder import ScreenRecorder
            ext = options.get("format", "mp4")
            path = self._screen_recordings_root() / (time.strftime("screen-%Y%m%d-%H%M%S") + "." + ext)
            self._screen_recorder = ScreenRecorder(
                on_error=lambda exc: self.screen_record_error.emit(str(exc)),
                on_finished=lambda out, ok: self.screen_record_finished.emit(str(out), ok))
            if not self._screen_recorder.start(source, options, path):
                logging.warning("Запис екрана не стартував: рушій відхилив старт (%s)", source)
                self.screen_record_error.emit(tr("screen_error_start_failed"))
                return False
            logging.info("Запис екрана розпочато: джерело=%s опції=%s", source, options)
            self.screen_record_state.emit("recording")
            return True
        except Exception as exc:
            logging.exception("Не вдалося почати незалежний запис екрана")
            self.screen_record_error.emit(str(exc))
            return False

    def screen_record_stop(self):
        if self._screen_recorder:
            self._screen_recorder.stop()

    def list_screen_recordings(self):
        root = self._screen_recordings_root()
        try:
            return sorted((p for p in root.glob("*.*") if p.suffix.lower() in {".mp4", ".mkv", ".webm"}),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            return []

    def open_screen_recordings_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._screen_recordings_root())))

    # --- RecordActionBar: дії над одним
    # записом екрана. Плоскі файли без журналу/шифрування (на відміну від
    # Наради — meeting/audit_log.py), тож видалення файлу нічого осиротілого
    # не лишає: журналу цілісності для запису екрана просто немає.
    def rename_screen_recording(self, path, new_name: str):
        """Перейменувати відеозапис (нова БАЗОВА назва, без розширення) у межах
        теки записів екрана. Відмова (None) — небезпечне ім'я, файл поза
        текою/відсутній, або колізія з наявним файлом."""
        from .record_action_bar import is_safe_display_name
        path = Path(path)
        root = self._screen_recordings_root()
        if not is_safe_display_name(new_name):
            return None
        if not path.is_file() or not _within_root(path, root):
            return None
        new_path = path.with_name(new_name.strip() + path.suffix)
        if new_path.exists():
            return None
        try:
            path.rename(new_path)
        except OSError:
            logging.exception("Не вдалося перейменувати запис екрана %s",
                              anonymize_path(path))
            return None
        return new_path

    def delete_screen_recording(self, path) -> bool:
        """Видалити файл запису екрана. False — файл поза текою/уже відсутній
        або помилка диска; True — видалено (або вже не існував після traversal-
        перевірки — повторний виклик безпечний)."""
        path = Path(path)
        root = self._screen_recordings_root()
        if not _within_root(path, root):
            return False
        if not path.exists():
            return True
        try:
            path.unlink()
        except OSError:
            logging.exception("Не вдалося видалити запис екрана %s",
                              anonymize_path(path))
            return False
        return True

    def show_screen_recording_in_folder(self, path) -> None:
        """Відкрити Провідник із виділеним файлом (Windows); поза Windows або
        при збої — просто відкрити теку записів (як «Відкрити папку»)."""
        path = Path(path)
        if sys.platform.startswith("win") and path.is_file():
            try:
                subprocess.Popen(["explorer", "/select,", str(path)])
                return
            except OSError:
                logging.exception("Не вдалося відкрити провідник із виділенням %s",
                                  anonymize_path(path))
        self.open_screen_recordings_folder()

    # --- команди вкладки (контракт розділу 3.1) ---
    def meeting_start(self, preset: str) -> bool:
        """Створити сесію за пресетом, відкрити 1-2 потоки, старт. Гейт: диктування/
        розпізнавання/захоплення клавіші/тест мікрофона блокують нараду (один
        мікрофон, одна модель; тест мікрофона ділить той самий пристрій [integration])."""
        # feature/dictation-queue: _pipeline_busy — і стара блокувальна фраза, і
        # активна/очікуюча фраза в черзі (нарада не стартує, поки триває розшифровка).
        if (_pipeline_busy(self) or self.recorder.recording or self._capturing
                or self._mic_testing
                or bool(getattr(self, "_meeting_processing_jobs", {}))
                or getattr(self, "_note_dictating", False)   # note: спільний мікрофон
                or getattr(self, "_dictaphone_active", False)):  # player-recordings: диктофон
            self.tray.notify(tr("meeting_busy_wait"))
            return False
        if self._meeting_active:
            return False
        try:
            from whisper_core.meeting import capture as mcapture
            from whisper_core.meeting import session as msession
        except Exception:
            logging.exception("Модуль наради недоступний")
            self.tray.notify(tr("meeting_error_generic"))
            return False
        # Новий config зберігає довільну комбінацію мікрофонів + loopback.
        # SimpleNamespace/старі інсталяції без поля лишають legacy preset path.
        try:
            from whisper_core.config import (MeetingSourceSpec,
                                             meeting_record_source_specs)
            if hasattr(self.cfg, "meeting_record_sources"):
                source_specs = meeting_record_source_specs(self.cfg)
            else:
                source_specs = [MeetingSourceSpec("microphone", self.cfg.input_device)]
                if preset == "both":
                    source_specs.append(MeetingSourceSpec("system", None))
                elif preset == "multimic":
                    from whisper_core.config import meeting_mic_devices
                    source_specs = [MeetingSourceSpec("microphone", name)
                                    for name in meeting_mic_devices(self.cfg)]
        except Exception:
            logging.exception("Не вдалося прочитати вибір джерел наради")
            self.tray.notify(tr("meeting_error_generic"))
            return False
        mic_specs = [spec for spec in source_specs if spec.kind == "microphone"]
        want_sys = any(spec.kind == "system" for spec in source_specs)
        if not mic_specs and not want_sys:
            self.tray.notify(tr("meeting_sources_empty"))
            return False
        # Резолв пристроїв під guard: збій енумерації (PortAudio впав, хост-API
        # недоступний) не має кидати виняток у GUI-слот (F1 чекера, раунд 2).
        try:
            # Явно вибраний mic не підміняємо default-пристроєм, якщо він зник.
            mic_names = [spec.device_name for spec in mic_specs]
            resolve_selected = getattr(mcapture, "selected_input", mcapture.default_input)
            mic_devs = [resolve_selected(name) for name in mic_names]
            sys_dev = mcapture.default_loopback() if want_sys else None
        except Exception:
            logging.exception("Збій енумерації аудіо-пристроїв наради")
            self.tray.notify(tr("meeting_error_generic"))
            return False
        if any(dev is None for dev in mic_devs):
            self.tray.notify(tr("app_mic_unavailable"))
            return False
        if want_sys and sys_dev is None:
            self.tray.notify(tr("meeting_no_loopback"))
            return False
        mic_tracks = (["mic"] if len(mic_devs) == 1 else
                      [f"mic{i + 1}" for i in range(len(mic_devs))])
        sources = mic_tracks + (["sys"] if want_sys else [])
        track_devices = {track: dev.get("name")
                         for track, dev in zip(mic_tracks, mic_devs)}
        if want_sys:
            track_devices["sys"] = sys_dev.get("name")
        recording_sources = [
            {"track": track, "kind": "microphone", "device_name": dev.get("name")}
            for track, dev in zip(mic_tracks, mic_devs)
        ]
        if want_sys:
            recording_sources.append({
                "track": "sys", "kind": "system", "device_name": sys_dev.get("name")})
        speaker_names = {track: tr("meeting_microphone_number", number=track[3:])
                         for track in mic_tracks if track != "mic"}
        if getattr(self.cfg, "meeting_encrypt", False):
            try:
                from whisper_core.meeting.storage_crypto import (
                    VaultPasswordRequired, ensure_dek)
                ensure_dek(self._meetings_root())
            except VaultPasswordRequired:
                self.meeting_vault_needed.emit()
                self.tray.notify(tr("meeting_error_vault_locked"))
                return False
            except Exception:
                logging.exception("Meeting vault key unavailable before capture")
                self.tray.notify(tr("meeting_error_encryption_start"))
                return False
        try:
            sess = msession.create_session(
                self._meetings_root(), sources,
                mic_device=(mic_devs[0] or {}).get("name") if len(mic_devs) == 1 else None,
                sys_device=(sys_dev or {}).get("name"),
                track_devices=track_devices, recording_sources=recording_sources,
                export_segment_seconds=max(1, int(getattr(
                    self.cfg, "meeting_export_segment_minutes", 10))) * 60,
                speaker_names=speaker_names)
        except Exception:
            logging.exception("Не вдалося створити сесію наради")
            self.tray.notify(tr("meeting_error_generic"))
            return False
        # feature/evidence-plus: «хто зафіксував» з налаштувань (порожньо → не пишемо).
        from whisper_core.meeting import audit_log as _audit
        _audit_event(sess.dir, "created", note=_audit.created_note(
            preset, sources, recorded_by=getattr(self.cfg, "operator_name", "")))
        streams = {}
        sink_for = getattr(sess, "sink", None)
        def track_sink(track):
            if callable(sink_for):
                return sink_for(track)
            return sess.sys_sink if track == "sys" else sess.mic_sink
        def configure_recovery(stream, track, resolver):
            """Новий capture має hook recovery; guard лишає сумісними старі
            ядра/фейки, у яких CaptureStream ще має початкову сигнатуру."""
            configure = getattr(stream, "configure_recovery", None)
            if not callable(configure):
                return
            configure(
                device_resolver=resolver,
                on_audio_state=lambda state, t=track:
                    self.meeting_audio_state.emit(t, state),
                on_gap=lambda seconds, t=track:
                    getattr(sess, "record_audio_gap", lambda *_: None)(t, seconds))
        # Архітектурний інваріант slice: capture не запускає ASR/діаризацію.
        self._meeting_live_disabled = True
        # feature/tts-listen (§9.1 БАР'ЄР): глушимо озвучення ДО capture; False → нараду
        # не стартуємо (озвучка не підтверджено зупинена — не пишемо з витоком).
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("meeting"):
            self.tray.notify(tr("meeting_error_generic"))
            return False
        # Аудит чесності (31.07, знахідка 1): котрий потік упав — мік чи
        # системний звук — визначаємо за доріжкою, на якій стались збій, а
        # НЕ вигадуємо це з того, що впало останнім want_sys-гілка. Інакше
        # людина зі зіпсованим мікрофоном читає пораду про колонки.
        failing_track = None
        try:
            # Спершу створюємо всі потоки, тоді стартуємо щільним циклом. Кожен
            # CaptureStream веде власну wall-clock шкалу й заповнює reconnect-gap
            # тишею, тому N WAV не дрейфують у часі.
            for track, dev, configured_name in zip(mic_tracks, mic_devs, mic_names):
                failing_track = track
                streams[track] = mcapture.CaptureStream(
                    kind=track, device_index=dev["index"],
                    channels=mcapture.NATIVE_CHANNELS, rate=mcapture.NATIVE_RATE,
                    sink=track_sink(track),
                    on_stall=lambda t=track: sess.close_segment(t),
                    on_device_lost=lambda exc, t=track: self._on_meeting_device_lost(t, exc),
                    on_sink_error=lambda exc, elapsed, t=track: self._on_meeting_storage_error(
                        sess, t, exc, elapsed))
                configure_recovery(streams[track], track,
                                   lambda name=configured_name: resolve_selected(name))
            if want_sys:
                failing_track = "sys"
                streams["sys"] = mcapture.CaptureStream(
                    kind="sys", device_index=sys_dev["index"],
                    channels=mcapture.NATIVE_CHANNELS, rate=mcapture.NATIVE_RATE,
                    sink=track_sink("sys"),
                    on_stall=lambda: sess.close_segment("sys"),
                    on_device_lost=lambda exc: self._on_meeting_device_lost("sys", exc),
                    on_sink_error=lambda exc, elapsed: self._on_meeting_storage_error(
                        sess, "sys", exc, elapsed),
                    # Ризик 5 (аудит 30.07): вартовий тиші лише для системної
                    # доріжки — саме її Teams/Meet можуть грати повз нас.
                    on_silence=lambda: self.meeting_audio_state.emit("sys", "silence"),
                    # Суддівське зауваження 30.07: коли звук зрештою
                    # з'являється, попередження мусить зникнути — інакше банер
                    # бреше, що проблема триває, коли її вже нема.
                    on_silence_resolved=lambda: self.meeting_audio_state.emit(
                        "sys", "silence_resolved"))
                configure_recovery(streams["sys"], "sys", mcapture.default_loopback)
            # Послідовне PyAudio.open для N пристроїв може тривати помітно
            # довше за один блок. Даємо всім CaptureStream одну часову опору:
            # затримка старту конкретного пристрою стане тишею на його доріжці.
            clock_origin = time.monotonic()
            for track, stream in streams.items():
                failing_track = track
                set_clock_origin = getattr(stream, "set_clock_origin", None)
                if callable(set_clock_origin):
                    set_clock_origin(clock_origin)
                stream.start()
        except Exception as exc:
            self._stop_live_meeting()
            logging.exception("Не вдалося відкрити аудіо-потік наради")
            for s in streams.values():
                try:
                    s.stop()
                except Exception:
                    pass
            try:
                self._discard_meeting_session(sess, msession.STATUS_ERROR)
            except Exception:
                logging.exception("Не вдалося прибрати сесію після збою аудіо-потоку")
            # Ризик 6 (аудит 30.07) + аудит чесності (31.07, знахідка 1):
            # окреме зрозуміле повідомлення для кожної причини/доріжки замість
            # одного загального тексту про системний звук, який брехав людям
            # зі зіпсованим мікрофоном.
            is_sys = failing_track == "sys" or failing_track is None
            if mcapture.is_device_busy_error(exc):
                self.tray.notify(tr("meeting_device_busy") if is_sys
                                  else tr("meeting_mic_device_busy"))
            elif is_sys:
                self.tray.notify(tr("meeting_no_loopback"))
            else:
                self.tray.notify(tr("meeting_mic_start_failed"))
            return False
        self._meeting_session = sess
        self._meeting_streams = streams
        self._meeting_active = True
        request_reload = getattr(self, "_request_model_reload", None)
        if callable(request_reload):
            request_reload()
        # capture phase свідомо НЕ запускає ASR/діаризацію (record-phase):
        # нарада лише пише звук, обробка — після зупинки.
        cfg_for_log = _diagnostic_attr(self, "cfg", None)
        diagnostic_event("meeting_started", tracks="+".join(sources),
                         screen=bool(_diagnostic_attr(cfg_for_log, "meeting_screen_enabled", False)),
                         live=False,
                         diarization=bool(_diagnostic_attr(cfg_for_log, "diarization_enabled", False)))
        self._meeting_started_at = time.monotonic()
        self._meeting_screen_recorder = None
        if getattr(self.cfg, "meeting_screen_enabled", False):
            try:
                from whisper_core.meeting.screen_record import ScreenRecorder
                monitor = self._screen_monitor_for_start()
                screen = ScreenRecorder(
                    on_started=lambda started_at, actual_monitor:
                        self._mark_screen_started(sess, started_at, actual_monitor),
                    on_error=lambda exc: self._mark_screen_failed(sess, exc))
                if screen.start(monitor, sess.dir / "screen.webm",
                                getattr(self.cfg, "meeting_screen_fps", 12)):
                    self._meeting_screen_recorder = screen
            except Exception:
                logging.exception("Не вдалося запустити запис екрана наради")
                self._mark_screen_failed(sess, None)
        self.meeting_state.emit("recording")   # трей оновить _on_meeting_state_tray
        return True

    def meeting_stop(self):
        """Миттєво зупинити захоплення; MP4 закриває фоновий координатор."""
        detached = self._detach_meeting()
        if detached is None:
            return
        sess, screen = detached
        diagnostic_event("meeting_stopped", **_meeting_diagnostic_fields(sess))
        self._meeting_postprocessing.add(sess.id)
        self.meeting_state.emit("processing")   # трей оновить _on_meeting_state_tray
        threading.Thread(target=self._complete_meeting_stop,
                         args=(sess, screen), daemon=True).start()

    def meeting_cancel(self):
        """Скасувати після закриття MP4, щоб потік не писав у видалену теку."""
        detached = self._detach_meeting()
        if detached is None:
            return
        sess, screen = detached
        diagnostic_event("meeting_cancelled", **_meeting_diagnostic_fields(sess))
        threading.Thread(target=self._complete_meeting_cancel,
                         args=(sess, screen), daemon=True).start()

    def _await_screen_close(self, sess, screen, timeout=10.0):
        """Фоновий бар'єр: до нього meeting.json лишається recording."""
        if screen is None:
            return True
        if not screen.wait_finished(timeout):
            logging.error("MP4 не закрився за %.1f с після stop; сесію фіналізуємо примусово", timeout)
            self.meeting_screen_error.emit(tr("meeting_screen_error"))
            self._mark_screen_failed(sess, None)
            return False
        if getattr(screen, "finished_error", False):
            self._mark_screen_failed(sess, screen.error)
            return False
        return True

    def _complete_meeting_stop(self, sess, screen):
        # Спершу закрити capture, тоді виконати лише легкий WAV-export. Жодної
        # транскрибуції/діаризації у фазі запису: користувач запускає її окремо.
        self._await_screen_close(sess, screen)
        try:
            from whisper_core.meeting import session as msession
            sess.finalize(msession.STATUS_STOPPED)
            from whisper_core.meeting import postprocess as mpost
            builder = getattr(mpost, "build_segmented_wavs", None)
            if callable(builder):
                exports = builder(sess.dir)
            else:  # compatibility із раннім ядром/тестовими doubles
                exports = {track: [path]
                           for track, path in mpost.build_session_wavs(sess.dir).items()}
            recorder = getattr(msession, "record_audio_exports", None)
            if callable(recorder):
                recorder(sess.dir, exports)
            finalize_dir = getattr(msession, "finalize_dir", None)
            meta = (finalize_dir(sess.dir, msession.STATUS_DONE)
                    if callable(finalize_dir) else sess.finalize(msession.STATUS_DONE))
            _audit_event(sess.dir, "finalized", note={
                "phase": "record", "tracks": list(exports),
                "files": sum(len(paths) for paths in exports.values()),
            })
            meta = _secure_meeting_finish(self, None, sess.dir, msession.STATUS_DONE)
            self.meeting_audio_ready.emit(sess.id)
            self.meeting_session_done.emit(sess.id, meta)
        except Exception as exc:
            logging.exception("Не вдалося підготувати аудіофайли наради")
            try:
                from whisper_core.meeting import session as msession
                finalize_dir = getattr(msession, "finalize_dir", None)
                if callable(finalize_dir):
                    finalize_dir(sess.dir, msession.STATUS_ERROR)
                _secure_meeting_finish(self, None, sess.dir, msession.STATUS_ERROR)
            except Exception:
                pass
            self.meeting_error.emit(sess.id, str(exc))
        finally:
            self._meeting_postprocessing.discard(sess.id)
            self.meeting_state.emit("idle")

    def _complete_meeting_cancel(self, sess, screen):
        self._await_screen_close(sess, screen)
        try:
            from whisper_core.meeting import session as msession
            self._discard_meeting_session(sess, msession.STATUS_STOPPED)
        except Exception:
            logging.exception("Не вдалося скасувати нараду")
        self.meeting_state.emit("idle")

    def _discard_meeting_session(self, sess, status):
        """Close and remove a cancelled or never-started plaintext session."""
        from whisper_core.meeting import audit_log, session as msession
        try:
            sess.finalize(status)
        finally:
            with audit_log.deletion_barrier(sess.dir):
                msession.delete_session(sess.dir, self._meetings_root())

    def set_meeting_encryption(self, enabled: bool) -> bool:
        """Enable encryption and migrate completed legacy sessions before saving."""
        enabled = bool(enabled)
        if enabled:
            try:
                from whisper_core.meeting import session as msession
                from whisper_core.meeting.storage_crypto import ensure_dek
                dek = ensure_dek(self._meetings_root())
                msession.resume_encryption(self._meetings_root())
                msession.migrate_unencrypted_sessions(self._meetings_root(), dek)
            except Exception:
                logging.exception("Could not enable meeting encryption")
                self.tray.notify(tr("meeting_error_encryption_start"))
                return False
        self.cfg.meeting_encrypt = enabled
        self.cfg.save()
        return True

    def meeting_vault_state(self) -> str:
        from whisper_core.meeting.storage_crypto import VaultKeyLost, is_unlocked, vault_mode
        root = self._meetings_root()
        try:
            mode = vault_mode(root)
        except VaultKeyLost:
            return "lost"
        if mode is None:
            return "none"
        if mode == "dpapi":
            return "dpapi"
        opened = is_unlocked(root)
        if mode == "keyfile":
            return "keyfile" if opened else "keyfile_locked"
        if mode == "password+keyfile":
            return "twofactor" if opened else "twofactor_locked"
        return "password" if opened else "locked"

    def meeting_vault_lock(self):
        from whisper_core.meeting.storage_crypto import lock_vault
        self._clear_meeting_plain_cache()
        lock_vault(self._meetings_root())

    def meeting_vault_unlock(self, password) -> bool:
        from whisper_core.meeting.storage_crypto import ensure_dek
        try:
            ensure_dek(self._meetings_root(), password)
        except Exception:
            logging.warning("Meeting vault password rejected")
            return False
        _cleanup_stale_meeting_temps()
        _resume_meeting_encryption(self)
        return True

    def meeting_vault_set_password(self, new_password, current=None):
        from whisper_core.meeting.storage_crypto import (
            VaultKeyLost, VaultPasswordRequired, VaultWrongPassword,
            ensure_dek, set_password)
        root = self._meetings_root()
        try:
            if current is not None:
                ensure_dek(root, current)
            self._vault_recovery_code = set_password(root, new_password)
        except (VaultWrongPassword, VaultPasswordRequired):
            return "vault_pw_wrong"
        except VaultKeyLost:
            return "meeting_error_key_lost"
        except Exception:
            logging.exception("Could not set meeting vault password")
            return "meeting_error_generic"
        return None

    def meeting_vault_pop_recovery_code(self):
        code = getattr(self, "_vault_recovery_code", None)
        self._vault_recovery_code = None
        return code

    def meeting_vault_unlock_with_recovery(self, code) -> bool:
        from whisper_core.meeting.storage_crypto import unlock_with_recovery
        try:
            unlock_with_recovery(self._meetings_root(), code)
        except Exception:
            logging.warning("Meeting vault recovery code rejected")
            return False
        _resume_meeting_encryption(self)
        return True

    def meeting_vault_regenerate_recovery(self, current=None):
        from whisper_core.meeting.storage_crypto import (
            VaultKeyLost, VaultPasswordRequired, VaultWrongPassword,
            ensure_dek, regenerate_recovery)
        root = self._meetings_root()
        try:
            if current is not None:
                ensure_dek(root, current)
            return None, regenerate_recovery(root)
        except (VaultWrongPassword, VaultPasswordRequired):
            return "vault_pw_wrong", None
        except VaultKeyLost:
            return "meeting_error_key_lost", None
        except Exception:
            logging.exception("Could not regenerate meeting recovery code")
            return "meeting_error_generic", None

    def meeting_vault_remove_password(self, current=None):
        from whisper_core.meeting.storage_crypto import (
            VaultKeyLost, VaultPasswordRequired, VaultWrongPassword, remove_password)
        try:
            remove_password(self._meetings_root(), current)
        except (VaultWrongPassword, VaultPasswordRequired):
            return "vault_pw_wrong"
        except VaultKeyLost:
            return "meeting_error_key_lost"
        except Exception:
            logging.exception("Could not remove meeting vault password")
            return "meeting_error_generic"
        return None

    def meeting_vault_generate_keyfile(self, path):
        from whisper_core.meeting.storage_crypto import generate_keyfile
        try:
            generate_keyfile(path)
        except Exception:
            logging.exception("Could not create meeting key-file")
            return "vault_keyfile_write_error"
        return None

    def meeting_vault_set_keyfile(self, keyfile_path, password=None):
        from whisper_core.meeting.storage_crypto import (
            VaultKeyLost, VaultWrongKeyfile, set_keyfile)
        try:
            self._vault_recovery_code = set_keyfile(
                self._meetings_root(), keyfile_path, password)
        except VaultWrongKeyfile:
            return "vault_keyfile_bad"
        except VaultKeyLost:
            return "meeting_error_key_lost"
        except Exception:
            logging.exception("Could not set meeting key-file")
            return "meeting_error_generic"
        return None

    def meeting_vault_unlock_with_keyfile(self, keyfile_path, password=None) -> bool:
        from whisper_core.meeting.storage_crypto import unlock_with_keyfile
        try:
            unlock_with_keyfile(self._meetings_root(), keyfile_path, password)
        except Exception:
            logging.warning("Meeting key-file/password rejected")
            return False
        _resume_meeting_encryption(self)
        return True

    def meeting_vault_remove_keyfile(self):
        from whisper_core.meeting.storage_crypto import (
            VaultKeyLost, VaultPasswordRequired, remove_keyfile)
        try:
            remove_keyfile(self._meetings_root())
        except VaultPasswordRequired:
            return "vault_locked_open_first"
        except VaultKeyLost:
            return "meeting_error_key_lost"
        except Exception:
            logging.exception("Could not remove meeting key-file protection")
            return "meeting_error_generic"
        return None

    def _screen_monitor_for_start(self):
        configured = max(1, int(getattr(self.cfg, "meeting_screen_monitor", 1)))
        monitors = self.list_meeting_screen_monitors()
        available = [mon.index for mon in monitors]
        if available and configured not in available:
            configured = available[0]
            self.set_meeting_screen_monitor(configured)
        return configured

    def _mark_screen_started(self, sess, started_at, monitor):
        try:
            sess.set_screen_recording(started_at, monitor)
        except Exception:
            logging.exception("Не вдалося зберегти метадані запису екрана")

    def _mark_screen_failed(self, sess, exc):
        try:
            sess.set_screen_failed()
        except Exception:
            logging.exception("Не вдалося зберегти помилку запису екрана")
        if exc is not None:
            self.meeting_screen_error.emit(tr("meeting_screen_error"))
    def _on_meeting_state_tray(self, state: str):
        """GUI-слот meeting_state → трей. Єдина точка дотику наради до трею:
        emit бува з worker-потоків (_finish_meeting), а трей крутить QTimer —
        чіпати його можна лише з GUI-потоку (авто-конект дає QueuedConnection)."""
        if state == "recording":
            self.tray.set_state("recording")
        elif state in ("processing", "postprocessing", "diarizing"):
            self.tray.set_state("busy")
        else:
            self.tray.set_state("idle")

    def _detach_meeting(self):
        """Миттєво від'єднати нараду; чекати MP4 тут категорично не можна (GUI)."""
        if not self._meeting_active:
            return None
        self._meeting_active = False
        self._stop_live_meeting()
        screen = getattr(self, "_meeting_screen_recorder", None)
        self._meeting_screen_recorder = None
        if screen is not None:
            try:
                request_stop = getattr(screen, "request_stop", None)
                (request_stop or screen.stop)()
            except Exception:
                logging.exception("Не вдалося надіслати stop запису екрана наради")
        for s in self._meeting_streams.values():
            try:
                s.stop()
            except Exception:
                logging.exception("Не вдалося зупинити потік наради")
        self._meeting_streams = {}
        sess = self._meeting_session
        self._meeting_session = None
        return (sess, screen)

    def start_meeting_processing(self, session_id) -> bool:
        """Явно запустити важку ASR-фазу після завершення запису."""
        if not getattr(self, "has_model", True):
            self.tray.notify(tr("app_model_absent_meeting"))
            return False
        from whisper_core.meeting import meeting_pipeline as pipeline
        from whisper_core.meeting import session as msession
        jobs = getattr(self, "_meeting_processing_jobs", {})
        if getattr(self, "_meeting_active", False) or jobs:
            self.tray.notify(tr("meeting_processing_busy"))
            return False
        if not msession.is_safe_session_id(session_id):
            return False
        session_dir = self._meeting_session_dir(session_id)
        meta = msession.load_meta(session_dir)
        if meta is None or not (getattr(meta, "audio_files", {}) or {}):
            self.tray.notify(tr("meeting_processing_no_audio"))
            return False
        current = dict(getattr(meta, "processing", {}) or {})
        if current.get("status") in ("complete", "partial"):
            return False
        token = pipeline.CancelToken()
        jobs[session_id] = token
        self._meeting_processing_jobs = jobs
        self._meeting_postprocessing.add(session_id)
        self.meeting_state.emit("postprocessing")
        threading.Thread(
            target=self._process_meeting_worker,
            args=(session_id, session_dir, token),
            daemon=True,
        ).start()
        return True

    def cancel_meeting_processing(self, session_id) -> bool:
        """Попросити pipeline зупинитися перед наступним 10-хв WAV-блоком."""
        token = getattr(self, "_meeting_processing_jobs", {}).get(session_id)
        if token is None:
            return False
        token.cancel()
        try:
            from whisper_core.meeting import session as msession
            msession.update_processing(
                self._meeting_session_dir(session_id),
                cancel_requested=True,
            )
        except Exception:
            logging.exception("Не вдалося записати запит скасування обробки")
        return True

    def _process_meeting_worker(self, session_id, session_dir, token):
        from whisper_core.meeting import meeting_pipeline as pipeline
        from whisper_core.meeting import session as msession
        try:
            try:
                from importlib.metadata import version
                engine_version = version("faster-whisper")
            except Exception:
                engine_version = "unknown"
            try:
                from whisper_core.models import revision_for
                model_revision = revision_for(
                    getattr(self.cfg, "model_name", "")) or "local"
            except Exception:
                model_revision = "unknown"
            provenance = {
                "engine": "faster-whisper",
                "engine_version": engine_version,
                "model": str(getattr(self.cfg, "model_name", "unknown")),
                "model_revision": str(model_revision),
                "language": str(getattr(self.cfg, "language", "auto")),
                "device": str(getattr(self.cfg, "device", "unknown")),
                "compute_type": str(
                    getattr(self.cfg, "compute_type", "unknown")),
            }

            def transcribe(path, *, include_word_timestamps=False):
                return self._transcribe_with_fallback(
                    path, self.terms, include_word_timestamps=True)

            # Діаризацію вмикаємо лише коли є і пакет sherpa, і перевірені моделі.
            # Рантайм вантажимо ЛІНИВО (після ASR) — не тримаємо sherpa-рушій у
            # пам'яті під час розшифровки. Крива кількість (не 2..10) → auto.
            diar_settings = diar_loader = None
            try:
                from whisper_core.meeting import diarize as _diarmod
                if (getattr(self.cfg, "diarization_enabled", False)
                        and _diarmod.runtime_available()
                        and _diarmod.models_available(
                            getattr(self.cfg, "diarization_model_dir", None))):
                    from whisper_core.meeting.diarization_pipeline import (
                        DiarizationSettings)
                    count = _diarmod.validate_speaker_count(
                        getattr(self.cfg, "diarization_num_speakers", None))
                    diar_settings = DiarizationSettings(
                        enabled=True, num_speakers=count,
                        voice_memory_enabled=bool(getattr(self.cfg, "voice_memory_enabled", False)),
                        profile=getattr(self, "profile", None))
                    _model_dir = getattr(self.cfg, "diarization_model_dir", None)
                    diar_loader = lambda: _diarmod.load_runtime(_model_dir)
            except Exception:
                logging.exception("Підготовка діаризації впала — обробка без неї")
                diar_settings = diar_loader = None

            # Шифроване сховище: розшифровуємо у робочу теку, обробляємо там
            # (діаризація/ASR), синхронізуємо результат назад у шифрований вигляд.
            with msession.materialize_session(session_dir) as work_dir:
                result = pipeline.process_meeting(
                    work_dir,
                    transcribe=transcribe,
                    asr_provenance=provenance,
                    me_label=tr("meeting_speaker_me"),
                    others_label=tr("meeting_speaker_others"),
                    microphone_label=tr(
                        "meeting_microphone_number", number="{number}"),
                    diarization=diar_settings,
                    diarization_runtime_loader=diar_loader,
                    speaker_label=tr("meeting_speaker_number", number="{number}"),
                    cancel=token,
                    progress=lambda state: self.meeting_processing_progress.emit(
                        session_id, state),
                )
                if Path(work_dir) != Path(session_dir):
                    msession.sync_materialized_session(work_dir, session_dir)
        except Exception as exc:
            logging.exception("Явна обробка наради %s впала", session_id)
            try:
                msession.update_processing(
                    session_dir, status="failed", stage="failed",
                    error=str(exc)[:300], finished_at=int(time.time()))
            except Exception:
                pass
            result = pipeline.ProcessingResult("failed", 0, {})
        finally:
            getattr(self, "_meeting_processing_jobs", {}).pop(session_id, None)
            getattr(self, "_meeting_postprocessing", set()).discard(session_id)

        if result.status in ("complete", "partial"):
            _audit_event(session_dir, "meeting_processed", note={
                "status": result.status,
                "words": result.word_count,
                "pipeline": pipeline.PIPELINE_VERSION,
            })
        self.meeting_processing_done.emit(session_id, result)
        meta = msession.load_meta(session_dir)
        if meta is not None:
            self.meeting_session_done.emit(session_id, meta)
        if not getattr(self, "_meeting_processing_jobs", {}):
            self.meeting_state.emit("idle")

    def _meeting_raw_f32_paths(self, session_id):
        """Сирі crash-safe .f32-сегменти сесії (С10): <сесія>/<доріжка>/*.f32."""
        from whisper_core.meeting import session as msession
        if not msession.is_safe_session_id(session_id):
            return []
        d = self._meeting_session_dir(session_id)
        meta = msession.load_meta(d)
        if meta is None:
            return []
        paths = []
        for track in (getattr(meta, "sources", []) or []):
            track = str(track)
            if "/" in track or "\\" in track or track in (".", ".."):
                continue
            tdir = d / track
            if tdir.is_dir():
                paths.extend(sorted(tdir.glob("*.f32")))
                paths.extend(sorted(tdir.glob("*.f32.enc")))
        return paths

    def meeting_raw_audio_bytes(self, session_id) -> int:
        """Скільки місця займають сирі .f32 (0 = нічого прибирати)."""
        total = 0
        for p in self._meeting_raw_f32_paths(session_id):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def meeting_free_raw_audio(self, session_id) -> int:
        """Прибрати сирі .f32 після успішного WAV-export (С10): лишаємо тільки
        Whisper-ready WAV. Доступно ЛИШЕ коли WAV уже готові — інакше сирі дані
        єдина копія аудіо (осиротіла сесія), і чіпати їх не можна."""
        from whisper_core.meeting import session as msession
        d = self._meeting_session_dir(session_id)
        meta = msession.load_meta(d) if msession.is_safe_session_id(session_id) else None
        if meta is None or not (getattr(meta, "audio_files", {}) or {}):
            return 0
        freed = 0
        for p in self._meeting_raw_f32_paths(session_id):
            try:
                size = p.stat().st_size
                p.unlink()
                freed += size
            except OSError:
                pass
        return freed

    def delete_meeting(self, session_id):
        """Best-effort м'яке видалення наради: тека переїжджає в кошик
        (``sessions/.trash/``) замість негайного rmtree — «Повернути» у тості
        (``restore_meeting``) відновлює її на місці. Фізично зникає лише
        застарілий вміст кошика (``list_meetings`` прибирає його при
        відкритті сторінки)."""
        failures = []
        session_dir = None
        self._last_trashed_meeting = None
        try:
            from whisper_core.meeting import session as msession
            if not msession.is_safe_session_id(session_id):
                logging.warning("delete_meeting: небезпечний id %r — відмова", session_id)
                return ("meeting_delete_step_files",)
            root = self._meetings_root()
            session_dir = root / session_id
            from whisper_core import trash as mtrash
            from whisper_core.meeting import audit_log
            # Той самий барʼєр, що й раніше перед rmtree (_discard_meeting_session):
            # tombstone + lock серіалізує з паралельним append_event, інакше
            # straggler-writer, що допише подію ПІСЛЯ переносу в кошик, мовчки
            # відроджує фантомну теку через mkdir(parents=True) в append_event.
            with audit_log.deletion_barrier(session_dir):
                self._last_trashed_meeting = mtrash.soft_delete(session_dir, root)
        except Exception:
            logging.exception("Не вдалося видалити нараду %s", session_id)
        if session_dir is None:
            failures.append("meeting_delete_step_files")
        else:
            try:
                session_dir.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                failures.append("meeting_delete_step_files")
            else:
                failures.append("meeting_delete_step_files")

        # Прибираємо pending-центроїди незалежно від результату файлів, але не
        # приховуємо частковий збій: UI покаже кожну невдалу частину.
        try:
            from whisper_core.meeting import voice_memory
            if not voice_memory.delete_pending_centroids(
                    self.profile, str(session_id)):
                failures.append("meeting_delete_step_voice_memory")
        except Exception:
            logging.exception("Не вдалося прибрати voice_pending наради %s", session_id)
            failures.append("meeting_delete_step_voice_memory")
        return tuple(failures)

    def restore_meeting(self, trashed_dir=None):
        """Повернути нараду з кошика («Повернути» у тості після видалення).

        Без аргументу — відновлює останню видалену цим сеансом (стан тосту).
        Конфлікт імені на місці — не затираємо, ``trash.restore`` додає суфікс.
        Повертає новий шлях сесії або None, якщо повертати нічого/не вдалось.
        """
        trashed_dir = trashed_dir or self._last_trashed_meeting
        if trashed_dir is None:
            return None
        try:
            from whisper_core import trash as mtrash
            from whisper_core.meeting import audit_log
            # Оригінальна назва — до restore(), бо той прибирає trash_info.json.
            original = mtrash.original_name(trashed_dir)
            restored = mtrash.restore(trashed_dir, self._meetings_root())
            # Тека фізично на місці — знімаємо tombstone, інакше журнал
            # відновленої наради більше НІКОЛИ не прийме append_event.
            audit_log.clear_deletion_barrier(restored)
            if restored.name != original:
                # Конфлікт імені: відновлено як "X (2)", а .X.audit.deleted
                # лишився б осиротілим назавжди — новоживу "X" він не блокує
                # (identity-перевірка рятує), але то сміття. root/original тут
                # фізично існує — саме він і викликав конфлікт _unique_dir.
                audit_log.clear_deletion_barrier(
                    self._meetings_root() / original)
        except Exception:
            logging.exception("Не вдалося повернути нараду з кошика %s", trashed_dir)
            return None
        if trashed_dir == self._last_trashed_meeting:
            self._last_trashed_meeting = None
        return restored

    def recover_meeting(self, session_id):
        """Осиротілий capture → відновити лише WAV-файли, без автоматичної ASR."""
        try:
            from whisper_core.meeting import session as msession
        except Exception:
            return
        d = self._meeting_session_dir(session_id)
        if msession.load_meta(d) is None:
            return
        if session_id in self._live_meeting_ids():
            return
        self._meeting_postprocessing.add(session_id)
        self.meeting_state.emit("processing")   # трей оновить _on_meeting_state_tray
        threading.Thread(target=self._recover_meeting_recording,
                         args=(session_id, d), daemon=True).start()

    def _recover_meeting_recording(self, session_id, session_dir):
        try:
            from whisper_core.meeting import postprocess as mpost
            from whisper_core.meeting import session as msession
            exports = mpost.build_segmented_wavs(session_dir)
            msession.record_audio_exports(session_dir, exports)
            meta = msession.finalize_dir(session_dir, msession.STATUS_DONE)
            meta = _secure_meeting_finish(
                self, None, session_dir, msession.STATUS_DONE)
            self.meeting_audio_ready.emit(session_id)
            self.meeting_session_done.emit(session_id, meta)
        except Exception as exc:
            logging.exception("Не вдалося відновити аудіофайли наради")
            self.meeting_error.emit(session_id, str(exc))
        finally:
            self._meeting_postprocessing.discard(session_id)
            self.meeting_state.emit("idle")

    def _meeting_tracks(self, session_id) -> list[str]:
        """Треки зі схеми сесії; старі meeting.json мають legacy fallback."""
        try:
            from whisper_core.meeting import session as msession
            meta = msession.load_meta(self._meeting_session_dir(session_id))
            return list(meta.sources) if meta and meta.sources else ["mic", "sys"]
        except Exception:
            return ["mic", "sys"]

    def open_meeting_audio(self, session_id):
        """Відкрити першу доступну WAV-доріжку сесії системним плеєром."""
        for wav in self.meeting_audio_paths(session_id).values():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(wav)))
            return
        self.tray.notify(tr("meeting_error_silence"))

    def meeting_audio_paths(self, session_id) -> dict:
        """Перший WAV-блок кожної доріжки для preview-плеєра; legacy теж читаємо."""
        d = self._meeting_session_dir(session_id)
        try:
            from whisper_core.meeting import session as msession
            meta = msession.load_meta(d)
            media_dir = self._materialized_meeting_dir(session_id)
            segmented = {}
            for track, names in (getattr(meta, "audio_files", {}) or {}).items():
                if not names:
                    continue
                path = (media_dir / names[0]).resolve()
                if path.is_relative_to(media_dir.resolve()) and path.is_file():
                    segmented[track] = path
            if segmented:
                return segmented
        except Exception as exc:
            from whisper_core.meeting.storage_crypto import VaultPasswordRequired
            if isinstance(exc, VaultPasswordRequired):
                self.meeting_vault_needed.emit()
            logging.warning("Meeting media unavailable for %s", session_id)
            return {}
        d = self._materialized_meeting_dir(session_id)
        return {track: wav for track in self._meeting_tracks(session_id)
                if (wav := d / f"{track}.wav").exists()}

    def meeting_screen_video(self, session_id):
        """Шлях до відеозапису екрана наради, якщо він є і придатний. Нові наради
        пишуть screen.webm (VP9); старі наради зі screen.mp4 (H.264) далі
        відкриваються. None — коли відео не писалось, файл зник або запис збійний."""
        original = self._meeting_session_dir(session_id)
        try:
            d = self._materialized_meeting_dir(session_id)
        except Exception as exc:
            from whisper_core.meeting.storage_crypto import VaultPasswordRequired
            if isinstance(exc, VaultPasswordRequired):
                self.meeting_vault_needed.emit()
            return None
        video = next((d / name for name in ("screen.webm", "screen.mp4")
                      if (d / name).is_file()), None)
        if video is None:
            return None
        try:
            from whisper_core.meeting import session as msession
            if getattr(msession.load_meta(original), "screen_status", "ok") == "failed":
                return None
        except Exception:
            pass
        return video

    def export_meeting_audio(self, session_id, output, fmt: str, bitrate_kbps: int, *, mix=False, start=None, end=None):
        from whisper_core.meeting.media import export_audio
        tracks = self.meeting_audio_paths(session_id)
        result = export_audio(list(tracks.values()), output, fmt,
                              bitrate_kbps, mix=mix, start=start, end=end)
        session_dir = self._meeting_session_dir(session_id)
        _audit_event(session_dir, "exported",
                     note={"kind": "audio", "format": fmt, "name": Path(output).name})
        self._bundle_audit_log(session_dir, output)
        return result

    def save_meeting_mix(self, session_id, output, track_volumes: dict) -> Path:
        """feature/save-mix-balance: зберегти зведення наради з ПОТОЧНИМ балансом
        мікшера плеєра (гучність/mute/соло на доріжку) в окремий WAV поруч.

        Рішення власника про монтаж проти доказовості: оригінальні mic.wav/sys.wav
        НІКОЛИ не чіпаються (лише читаються), зведення — новий похідний файл за
        шляхом, який обрав користувач. Подія exported у журналі цілісності фіксує
        коефіцієнти балансу — журнал і далі посилається на оригінали, не на мікс."""
        from whisper_core.meeting.media import export_balanced_wav
        tracks = self.meeting_audio_paths(session_id)
        order = list(tracks)
        weights = [float(track_volumes.get(key, 0.0)) for key in order]
        result = export_balanced_wav(
            [tracks[key] for key in order], output, weights)
        session_dir = self._meeting_session_dir(session_id)
        _audit_event(session_dir, "exported",
                     note={"kind": "audio_mix_balanced",
                           "volumes": {key: weights[i] for i, key in enumerate(order)},
                           "name": Path(output).name})
        self._bundle_audit_log(session_dir, output)
        return result

    def log_meeting_export(self, session_id, kind: str, output) -> None:
        """feature/chain-of-custody: зафіксувати експорт транскрипту (.txt/.md) у
        журналі й покласти копію журналу поряд із файлом."""
        session_dir = self._meeting_session_dir(session_id)
        _audit_event(session_dir, "exported",
                     note={"kind": kind, "name": Path(output).name})
        self._bundle_audit_log(session_dir, output)

    def meeting_integrity_meta(self, session_id):
        """feature/chain-of-custody: ДЕШЕВИЙ статус журналу (є/немає + записаний SHA)
        БЕЗ перехешування артефактів — для рендеру картки на кожному showEvent.
        Повну перевірку (з хешуванням) робить meeting_integrity лише за запитом."""
        from whisper_core.meeting import audit_log
        try:
            return audit_log.read_chain_meta(self._meeting_session_dir(session_id))
        except Exception:
            logging.exception("Не вдалося прочитати журнал цілісності %s", session_id)
            return audit_log.ChainResult(status=audit_log.STATUS_ABSENT)

    def meeting_integrity(self, session_id):
        """feature/chain-of-custody: ПОВНА верифікація журналу цілісності сесії
        (перехешовує аудіо/транскрипт з диска). Дорога — кликати ЛИШЕ за явним
        запитом користувача (діалог «Журнал цілісності»), не на рендері картки.
        Збій → absent, картка не падає."""
        from whisper_core.meeting import audit_log
        try:
            return audit_log.verify_chain(self._meeting_session_dir(session_id))
        except Exception:
            logging.exception("Не вдалося перевірити журнал цілісності %s", session_id)
            return audit_log.ChainResult(status=audit_log.STATUS_ABSENT)

    def meeting_add_review(self, session_id, reviewer: str) -> bool:
        """feature/evidence-plus: другий офіцер підтверджує цілісність (принцип
        «чотирьох очей») — дописати подію reviewed у журнал. ``reviewer`` — вільний
        текст (ім'я/посада, не акаунт). Порожній → нічого не пишемо. Ланцюг журналу
        лишається цілим (звичайний append). Повертає True, якщо подію додано."""
        reviewer = (reviewer or "").strip()
        if not reviewer:
            return False
        session_dir = self._meeting_session_dir(session_id)
        _audit_event(session_dir, "reviewed", note={"reviewer": reviewer})
        return True

    def export_meeting_evidence(self, session_id, out_zip):
        """feature/evidence-plus: скласти доказовий пакет наради (zip: файли наради +
        журнал цілісності + незалежний verify.py + людино-читний REPORT.txt) для
        передачі комісії/слідчому. Пакет відображає стан НА МОМЕНТ експорту; сам факт
        експорту фіксуємо в журналі окремою подією ПІСЛЯ складання."""
        from whisper_core.meeting import evidence
        session_dir = self._meeting_session_dir(session_id)
        pkg = evidence.export_evidence(
            session_dir, out_zip, app_version=DISPLAY_VERSION)
        _audit_event(session_dir, "exported",
                     note={"kind": "evidence", "name": Path(out_zip).name})
        return pkg

    def _bundle_audit_log(self, session_dir, output) -> None:
        """feature/chain-of-custody: покласти копію журналу цілісності поряд із
        експортованим файлом (``<output>.audit.jsonl``), щоб доказ супроводжував
        винесений назовні запис. Некритично — збій копіювання не валить експорт."""
        try:
            from whisper_core.meeting import session as msession
            data = msession.read_artifact(Path(session_dir), "audit.jsonl")
            dst = Path(output).with_name(Path(output).name + ".audit.jsonl")
            dst.write_bytes(data)
        except FileNotFoundError:
            return
        except OSError:
            logging.exception("Не вдалося вкласти журнал цілісності до експорту")

    def meeting_export_formats(self) -> dict:
        from whisper_core.meeting.media import available_formats
        return available_formats()

    def open_meeting_folder(self, session_id):
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._meeting_session_dir(session_id))))

    # --- feature/obsidian-channel: надіслати нараду .md у сховище Obsidian ---
    def _meeting_display_title(self, session_id) -> str:
        """Назва наради для імені/frontmatter: збережена назва або id сесії."""
        from whisper_core.meeting import session as msession
        meta = msession.load_meta(self._meeting_session_dir(session_id))
        title = getattr(meta, "title", None) if meta is not None else None
        return title or str(session_id)

    def _obsidian_placeholders(self, session_id):
        """(date, time) для плейсхолдерів імені: з id сесії «РРРР-ММ-ДД_ГГ-ХХ-СС».
        Непарсибельний id → поточні дата й час (імʼя все одно вийде коректним)."""
        sid = str(session_id)
        if "_" in sid:
            d, _sep, t = sid.partition("_")
            parts = t.split("-")
            return d, ("-".join(parts[:2]) if len(parts) >= 2 else t)
        now = datetime.datetime.now()
        return now.strftime("%Y-%m-%d"), now.strftime("%H-%M")

    def _meeting_obsidian_markdown(self, session_id, title) -> str:
        """Markdown наради для Obsidian: той самий transcript-Markdown (frontmatter
        + секції за мовцями), з додатковим полем type: meeting у frontmatter."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        utterances = self.read_meeting_utterances(session_id)
        meta = msession.load_meta(self._meeting_session_dir(session_id))
        speaker_names = meta.speaker_names if meta is not None else None
        date, _time = self._obsidian_placeholders(session_id)
        return mpost.to_transcript_markdown(
            utterances,
            me_label=tr("meeting_speaker_me"),
            others_label=tr("meeting_speaker_others"),
            speaker_names=speaker_names,
            meta={"date": date, "type": "meeting", "source": title or str(session_id)})

    def export_meeting_to_obsidian(self, session_id, title=None):
        """Записати нараду .md-файлом у вибрану папку Obsidian; вернути шлях або None.

        None = канал не налаштований (вимкнено або папку не вибрано) — картка
        покаже підказку. Імʼя — за шаблоном (дата/назва/час), колізія → суфікс «-2»
        (оригінал недоторканий), запис ЛИШЕ в межах вибраної папки (safe_under).
        Помилку запису (папка зникла / нема прав / поза межами) піднімаємо
        викликачеві — він покаже підказку."""
        from whisper_core import obsidian
        if not getattr(self.cfg, "obsidian_enabled", False):
            return None
        vault = getattr(self.cfg, "obsidian_dir", None)
        if not vault:
            return None
        if title is None:
            title = self._meeting_display_title(session_id)
        date, clock = self._obsidian_placeholders(session_id)
        filename = obsidian.render_filename(
            getattr(self.cfg, "obsidian_filename_template", None)
            or obsidian.DEFAULT_TEMPLATE,
            date=date, name=title or "", time=clock)
        md = self._meeting_obsidian_markdown(session_id, title)
        path = obsidian.write_markdown(vault, filename, md)
        self.log_meeting_export(session_id, "obsidian", path)
        return path

    def obsidian_open(self, target=None):
        """Відкрити нараду в Obsidian (obsidian://open). target — шлях до вже
        надісланого .md або None. Graceful: якщо Obsidian не встановлений (ОС не
        обробила схему) або шляху немає — відкриваємо папку сховища у Провіднику.
        Нічого не налаштовано → no-op."""
        from whisper_core import obsidian
        opened = False
        if target:
            opened = QDesktopServices.openUrl(QUrl(obsidian.open_uri(target)))
        if not opened:
            vault = getattr(self.cfg, "obsidian_dir", None)
            if vault:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(vault)))

    def _auto_obsidian(self, session_id):
        """Після фіналізації наради — якщо канал увімкнено й папку задано, надіслати
        .md у сховище Obsidian. Некритично: збій (папка зникла / нема прав / поза
        межами) лише в лог, не валить фіналізацію."""
        if not getattr(self.cfg, "obsidian_enabled", False):
            return
        if not getattr(self.cfg, "obsidian_dir", None):
            return
        try:
            self.export_meeting_to_obsidian(session_id)
        except (OSError, ValueError):
            logging.warning("Не вдалося надіслати нараду %s до Obsidian", session_id)

    def meeting_mic_level(self) -> tuple:
        return self._track_level("mic")

    def meeting_sys_level(self) -> tuple:
        return self._track_level("sys")

    def meeting_track_level(self, track: str) -> tuple:
        return self._track_level(track)

    def _track_level(self, track) -> tuple:
        s = self._meeting_streams.get(track)
        if s is not None:
            try:
                return s.take_level()
            except Exception:
                pass
        return (0.0, 0.0)

    def list_meetings(self) -> list:
        """Список сесій (новіші першими). Осиротілі (краш) → «перервано» (розділ 2.6)."""
        try:
            from whisper_core.meeting import session as msession
        except Exception:
            return []
        root = self._meetings_root()
        try:
            from whisper_core import trash as mtrash
            mtrash.purge_expired(root)
        except Exception:
            logging.exception("Не вдалося прибрати застарілий кошик нарад")
        try:
            live_ids = self._live_meeting_ids()
            for orphan in msession.find_orphans(root):
                if orphan.id in live_ids:
                    continue
                msession.mark_interrupted(root / orphan.id)
        except Exception:
            logging.exception("Не вдалося позначити осиротілі наради")
        try:
            metas = msession.list_sessions(root)
            active_jobs = set(
                getattr(self, "_meeting_processing_jobs", {}).keys())
            for index, meta in enumerate(metas):
                processing = dict(getattr(meta, "processing", {}) or {})
                if (processing.get("status") == "running"
                        and getattr(meta, "id", "") not in active_jobs):
                    recovered = msession.update_processing(
                        root / meta.id,
                        status="cancelled",
                        stage="cancelled",
                        cancel_requested=True,
                        error="processing_interrupted",
                        finished_at=int(time.time()),
                    )
                    if recovered is not None:
                        recovered.id = meta.id
                        metas[index] = recovered
            return metas
        except Exception as exc:
            from whisper_core.meeting.storage_crypto import (
                VaultKeyLost, VaultPasswordRequired)
            if isinstance(exc, VaultPasswordRequired):
                self.meeting_vault_needed.emit()
                return []
            if isinstance(exc, VaultKeyLost):
                self.tray.notify(tr("meeting_error_key_lost"))
                return []
            logging.exception("Не вдалося перелічити наради")
            return []

    def build_search_index(self):
        """feature/global-search: зібрати індекс глобального пошуку з ЛОКАЛЬНИХ
        джерел — history.jsonl усіх словників (диктування + розшифровки файлів) і
        transcript.json усіх нарад. Нічого нікуди не відправляє (канон приватності)."""
        from whisper_core.search_index import SearchIndex
        from whisper_core import profiles
        try:
            profs = profiles.list_profiles()
        except Exception:
            logging.exception("Пошук: не вдалося перелічити словники")
            profs = [self.profile]
        try:
            return SearchIndex.build(history_paths=profs,
                                     meetings_root=self._meetings_root())
        except Exception:
            logging.exception("Пошук: не вдалося зібрати індекс")
            return SearchIndex.build()

    def _live_meeting_ids(self) -> set:
        """Сесії, якими досі володіє цей процес (capture або postprocess)."""
        ids = set(getattr(self, "_meeting_postprocessing", set()))
        active = getattr(self, "_meeting_session", None)
        if active is not None:
            ids.add(active.id)
        ids.update(getattr(self, "_meeting_pending", {}).keys())
        return ids

    def set_meeting_speaker_name(self, session_id, speaker_id, name):
        """Зберегти ім’я й одразу перерендерити всі експорти транскрипта."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        d = self._meeting_session_dir(session_id)
        meta = msession.load_meta(d)
        if meta is None:
            return
        names = dict(meta.speaker_names or {})
        default = tr("meeting_speaker_number", number=str(speaker_id).rsplit("_", 1)[-1])
        new_name = name.strip() or default
        names[str(speaker_id)] = new_name
        try:
            data = json.loads(msession.read_artifact(d, "transcript.json").decode("utf-8"))
            utterances = [mpost.Utterance(**item) for item in data]
            # Експорти мають бути готові до того, як комітяться нові метадані.
            mpost.write_transcript(d, utterances, me_label=tr("meeting_speaker_me"),
                                   others_label=tr("meeting_speaker_others"),
                                   speaker_names=names)
            msession.set_speaker_name(d, speaker_id, names[str(speaker_id)])
            self._clear_meeting_plain_cache(session_id)

            # feature/voice-memory (Т41): бутстрап голосу з ренейму за увімкненої згоди.
            # Центроїди-біометрія свідомо НЕ в diarization.final.json (артефакт тече у
            # доказовий пакет) — їх складено у per-профільне voice_pending/ поза текою
            # сесії. Забираємо звідти (працює і після рестарту), enroll-имо у voices.json,
            # використаний запис видаляється всередині take_pending_centroid.
            if getattr(self.cfg, "voice_memory_enabled", False) and new_name != default:
                try:
                    from whisper_core.meeting import voice_memory
                    centroid = voice_memory.take_pending_centroid(
                        self.profile, str(session_id), str(speaker_id))
                    if centroid:
                        voice_memory.add_or_update_voice(self.profile, new_name, centroid)
                except Exception:
                    logging.exception("Не вдалося зберегти відбиток голосу для %s", new_name)
        except (OSError, ValueError, TypeError):
            logging.exception("Не вдалося оновити транскрипт після перейменування мовця")

    def list_voice_memories(self) -> list:
        from whisper_core.meeting import voice_memory
        return voice_memory.list_voices(self.profile)

    def delete_voice_memory(self, name: str) -> bool:
        from whisper_core.meeting import voice_memory
        return voice_memory.delete_voice(self.profile, name)

    def clear_voice_memories(self) -> int:
        from whisper_core.meeting import voice_memory
        return voice_memory.clear_voices(self.profile)

    def set_meeting_title(self, session_id, title):
        """feature/diary-calendar: зберегти назву наради у meeting.json."""
        from whisper_core.meeting import session as msession
        result = msession.set_title(self._meeting_session_dir(session_id), title)
        self._clear_meeting_plain_cache(session_id)
        return result

    def suggest_meeting_title(self, session_id):
        """feature/diary-calendar: підказка назви з календаря (.ics), якщо файл
        задано в Налаштуваннях і подія покриває час старту наради. Немає файлу /
        події → None. Нічого не нав'язує — лише повертає рядок для передзаповнення."""
        path = getattr(self.cfg, "meeting_ics_path", None)
        if not path:
            return None
        from datetime import datetime
        from whisper_core import ics
        from whisper_core.meeting import session as msession
        meta = msession.load_meta(self._meeting_session_dir(session_id))
        if meta is None:
            return None
        created = getattr(meta, "created", 0) or 0
        try:
            return ics.suggest_meeting_name(path, datetime.fromtimestamp(created))
        except Exception:
            logging.exception("Не вдалося підказати назву наради з календаря")
            return None

    def read_meeting_transcript(self, session_id):
        """Готовий транскрипт, decrypted in memory when the session is sealed."""
        try:
            from whisper_core.meeting import session as msession
            return msession.read_artifact(
                self._meeting_session_dir(session_id), "transcript.txt").decode("utf-8")
        except Exception as exc:
            from whisper_core.meeting.storage_crypto import VaultPasswordRequired
            if isinstance(exc, VaultPasswordRequired):
                self.meeting_vault_needed.emit()
            return None

    def write_meeting_transcript(self, session_id, text) -> bool:
        """feature/transcript-editing: записати відредагований текст назад у
        transcript.txt сесії. transcript.json (структурне джерело) НЕ чіпаємо."""
        from whisper_core.meeting import postprocess as mpost
        session_dir = self._meeting_session_dir(session_id)
        ok = mpost.write_transcript_text(session_dir, text) is not None
        if ok:
            # feature/chain-of-custody: легітимна правка фіксується подією edited
            # з новим SHA транскрипту — інакше зміна файлу читалась би як підміна.
            from whisper_core.meeting import audit_log
            _audit_event(session_dir, "edited",
                         artifacts=audit_log.hash_artifacts(session_dir, ["transcript.txt"]))
            self._clear_meeting_plain_cache(session_id)
        return ok

    def update_file_transcript(self, old_text: str, new_text: str) -> None:
        """feature/transcript-editing: записати відредаговану розшифровку файлу
        назад у history.jsonl (лише коли пам'ять профілю ввімкнена). Оновлюється
        final; raw лишається оригіналом. Некритично: нема запису → тихий no-op
        (правка вже застосована в пам'яті картки)."""
        from whisper_core.history import update_final
        prof = self.profile
        if not getattr(prof, "memory_enabled", False):
            return
        update_final(prof.history_path, old_text, new_text, source="file")

    def read_meeting_utterances(self, session_id):
        """feature/markdown-export: репліки сесії з transcript.json → список
        Utterance (для експорту .md із секціями за мітками мовців). Немає файлу
        чи він битий → [] (експорт дасть лише frontmatter, без краху)."""
        import json
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        try:
            data = json.loads(msession.read_artifact(
                self._meeting_session_dir(session_id), "transcript.json").decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            return []
        out = []
        for d in data:
            try:
                out.append(mpost.Utterance(
                    d["start"], d["end"], d["speaker"], d["text"],
                    d.get("source", ""), tuple(d.get("word_ids", ()))))
            except (KeyError, TypeError):
                continue
        return out

    # --- конвеєр наради (worker-потоки + агрегатор у GUI-потоці) ---
    def _meeting_postprocess(self, session_id, session_dir, sess):
        """Worker: зібрати WAV з сирих сегментів → чергу розшифровки. sess може
        бути None (відновлення осиротілої сесії)."""
        from whisper_core.meeting import postprocess as mpost
        getattr(self, "_meeting_postprocessing", set()).add(session_id)
        try:
            wavs = mpost.build_session_wavs(session_dir)
        except Exception:
            logging.exception("Не вдалося зібрати WAV наради %s", session_id)
            wavs = {}
        if not wavs:
            try:
                from whisper_core.meeting import session as msession
                _secure_meeting_finish(self, sess, session_dir, msession.STATUS_ERROR)
            except Exception:
                pass
            self.meeting_error.emit(session_id, tr("meeting_error_silence"))
            getattr(self, "_meeting_postprocessing", set()).discard(session_id)
            return
        self._meeting_pending[session_id] = {
            "session": sess, "dir": session_dir, "post_started": time.perf_counter(),
            "expected": len(wavs), "tracks": {}}
        # диктофон-режим: аудіо доступне ще ДО завершення розшифровки
        self.meeting_audio_ready.emit(session_id)
        for track, wav in wavs.items():
            self.enqueue_meeting_track(session_id, track, wav)

    def _on_meeting_track_done(self, session_id, track, text, segments):
        """GUI-слот агрегатора: збирає доріжки; коли прийшли ВСІ очікувані —
        зшивка у ще одному worker-потоці."""
        pending = self._meeting_pending.get(session_id)
        if pending is None:
            return
        pending["tracks"][track] = (text, segments)
        if len(pending["tracks"]) >= pending["expected"]:
            threading.Thread(target=self._finish_meeting,
                             args=(session_id,), daemon=True).start()

    def _persist_speaker_names(self, session_dir, speaker_names):
        """Зберегти імена мовців у meeting.json ЧЕРЕЗ encryption-aware write_artifact
        (як session.set_title/_write_meta), а НЕ прямий atomic_write_json.

        На запечатаній сесії (meeting.json.enc) прямий plaintext-запис клав би
        НЕЗАШИФРОВАНИЙ meeting.json (хто-що-казав) поруч із .enc — витік, що
        переживає краш. write_artifact сам перевіряє маркер шифрування й шифрує,
        якщо сесія запечатана. None — коли meeting.json відсутній/битий."""
        from whisper_core.meeting import session as msession
        fresh = msession.load_meta(session_dir)
        if fresh is None:
            return None
        fresh.speaker_names = dict(speaker_names)
        return msession.write_artifact(
            session_dir, "meeting.json", fresh.to_json().encode("utf-8"))

    def _finish_meeting(self, session_id):
        """Worker: зшити доріжки за часом, записати транскрипт, фіналізувати DONE."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        pending = self._meeting_pending.get(session_id)
        if pending is None:
            return
        sess = pending.get("session")
        try:
            track_segments = {track: payload[1] for track, payload in pending["tracks"].items()}
            sys_t = pending["tracks"].get("sys")
            sys_segs = track_segments.get("sys")
            fresh = msession.load_meta(pending["dir"])
            speaker_names = dict((fresh.speaker_names if fresh else {}) or {})
            # Окремі мікрофони вже є фізичними мовцями: дефолтні мітки не
            # залежать від sherpa й одразу доступні для inline-перейменування.
            for track in track_segments:
                if track.startswith("mic") and track != "mic":
                    number = track[3:] or track
                    speaker_names.setdefault(track, tr("meeting_microphone_number", number=number))
            diarization_enabled = bool(getattr(self.cfg, "diarization_enabled", False))
            timed_words = []
            # Діаризація лишається тільки на системному треку; mic1..micN її
            # принципово не проходять, бо мовець уже визначений пристроєм.
            if (diarization_enabled and sys_t and isinstance(sys_segs, tuple) and
                    len(sys_segs) == 2 and isinstance(sys_segs[0], list) and
                    isinstance(sys_segs[1], list)):
                sys_segs, timed_words = sys_segs
                track_segments["sys"] = sys_segs
            if sys_t and diarization_enabled:
                diar_started = time.perf_counter()
                try:
                    from whisper_core.meeting import diarize as mdiag
                    if mdiag.models_available(getattr(self.cfg, "diarization_model_dir", None)):
                        self.meeting_state.emit("diarizing")
                        num_spk = mdiag.validate_speaker_count(
                            getattr(self.cfg, "diarization_num_speakers", None))
                        diar = mdiag.diarize(
                            mdiag.load_wav_f32_16k(pending["dir"] / "sys.wav"),
                            num_speakers=num_spk,
                            configured_dir=getattr(self.cfg, "diarization_model_dir", None))
                        enhanced_sys_segs, diar_labels = mpost.speaker_segments_from_words(timed_words, diar)
                        if enhanced_sys_segs:
                            track_segments["sys"] = enhanced_sys_segs
                        for label in diar_labels.values():
                            speaker_names.setdefault(
                                label, tr("meeting_speaker_number", number=label.rsplit("_", 1)[-1]))
                        diagnostic_event("meeting_diarization_done", speakers=len(speaker_names),
                                         duration_s=round(time.perf_counter() - diar_started, 3))
                except Exception:
                    logging.exception("Діаризація наради %s не вдалася", session_id)
                    self.transcription_error.emit(tr("meeting_diarization_failed"))
            stitch_tracks = getattr(mpost, "stitch_tracks", None)
            utterances = (stitch_tracks(track_segments) if callable(stitch_tracks) else
                          mpost.stitch(track_segments.get("mic"), track_segments.get("sys")))
            transcript_kwargs = {
                "me_label": tr("meeting_speaker_me"),
                "others_label": tr("meeting_speaker_others"),
            }
            if speaker_names:
                transcript_kwargs["speaker_names"] = speaker_names
            mpost.write_transcript(pending["dir"], utterances, **transcript_kwargs)
            if speaker_names:
                # encryption-aware: на запечатаній сесії пише в .enc, не плейнтекст
                self._persist_speaker_names(pending["dir"], speaker_names)
            # І для живої, і для відновленої сесії статус «done» на диск пише
            # finalize_dir: живий sess.finalize після стопу — no-op (ідемпотент-
            # ність Б1 заморожує 'stopped'). getattr-guard усередині хелпера: на
            # старому ядрі без finalize_dir транскрипт усе одно записаний.
            meta = _finalize_meeting_status(
                sess, pending["dir"], msession.STATUS_DONE)
            # feature/chain-of-custody: зафіксувати SHA фінального аудіо+транскрипту
            # у незмінному журналі (доказ, що запис наради потім не змінювали).
            artifacts = [wav.name for wav in sorted(pending["dir"].glob("*.wav"))]
            artifacts += [name for name in ("transcript.txt", "transcript.json")
                          if (pending["dir"] / name).is_file()]
            try:
                from whisper_core.meeting import audit_log
                audit_log.finalize(pending["dir"], artifacts)
            except AuditLogCorrupt:
                _warn_audit_corrupt(pending["dir"])          # блокер Т56: чесно, а не мовчки
            except Exception:
                logging.exception("Не вдалося зафіксувати фіналізацію в журналі цілісності")
            diagnostic_event("meeting_finalized", status="done", speakers=len(speaker_names),
                             duration_s=_diagnostic_elapsed(
                                 pending.get("post_started"), clock=time.perf_counter))
            self._meeting_pending.pop(session_id, None)
            getattr(self, "_meeting_postprocessing", set()).discard(session_id)
            self.meeting_session_done.emit(session_id, meta)
            self._auto_obsidian(session_id)   # feature/obsidian-channel (некритично)
            self.meeting_state.emit("idle")   # трей оновить _on_meeting_state_tray
        except Exception:
            logging.exception("Не вдалося зшити нараду %s", session_id)
            try:
                _finalize_meeting_status(
                    sess, pending["dir"], msession.STATUS_ERROR)
            except Exception:
                pass
            self._meeting_pending.pop(session_id, None)
            getattr(self, "_meeting_postprocessing", set()).discard(session_id)
            self.meeting_error.emit(session_id, tr("meeting_error_generic"))

    def _on_meeting_error(self, session_id, message):
        """GUI-слот: фінал сесії у стан ERROR (якщо ще в роботі) + тост у трей.
        Стан трею виставляє meeting_state → _on_meeting_state_tray (дубль
        прибрано); notify тут безпечний — слот виконується у GUI-потоці."""
        pending = self._meeting_pending.pop(session_id, None)
        getattr(self, "_meeting_postprocessing", set()).discard(session_id)
        if pending is not None and pending.get("session") is not None:
            try:
                from whisper_core.meeting import session as msession
                _finalize_meeting_status(
                    pending["session"], pending["dir"], msession.STATUS_ERROR)
            except Exception:
                logging.exception("Не вдалося фіналізувати помилкову нараду")
        self.tray.notify(message)
        self.meeting_state.emit("idle")

    def _on_meeting_storage_error(self, sess, track, exc, elapsed):
        """Стійкий збій sink: capture триває, а банер і тост чесно показують втрату."""
        disk_full = isinstance(exc, OSError) and exc.errno == errno.ENOSPC
        if disk_full:
            logging.error("Закінчилось місце під час наради (%s): %s", track, exc)
            try:
                sess.mark_storage_error(track, elapsed)
            except Exception:
                # Запис meta теж може не вдатись на повному диску; попередження в
                # UI однаково мусить дійти, а audio capture лишається керованим.
                logging.exception("Не вдалося позначити ENOSPC у meeting.json")
            minute = max(1, int(float(elapsed) // 60) + 1)
            message = tr("meeting_storage_full", minute=minute)
        else:
            logging.error("Запис доріжки наради не вдався (%s): %s", track, exc)
            message = tr("meeting_storage_write_failed")
        self.meeting_storage_warning.emit(sess.id, float(elapsed), message)
        self.transcription_error.emit(message)

    def _on_meeting_audio_state(self, track: str, state: str):
        diagnostic_event("meeting_audio_state", track=track, state=state)
        """GUI-слот runtime-відновлення наради. Картка слухає цей самий
        сигнал, а тут лишаються глобальний тост і чесний terminal path."""
        if state == "reconnecting":
            self.tray.notify(tr("meeting_mic_reconnecting", track=track))
            return
        if state == "reconnected":
            self.tray.notify(tr("meeting_mic_reconnected", track=track))
            return
        if state == "silence":
            # Ризик 5: capture триває, це лише попередження — нараду НЕ зупиняємо.
            self.tray.notify(tr("meeting_sys_silence_warning"))
            return
        if state == "silence_resolved":
            # Звук з'явився після попередження — картка сама ховає банер
            # (meeting.py._on_audio_state); тут лише слід у журналі через
            # diagnostic_event на початку методу, без окремого тосту.
            return
        if state != "failed" or not self._meeting_active:
            return
        # С1: відмова однієї доріжки НЕ валить усю багатодоріжкову нараду.
        # Зупиняємо лише провалену доріжку; решта пишуть далі. Уся нарада йде в
        # interrupted тільки коли впала ОСТАННЯ активна доріжка.
        stream = self._meeting_streams.pop(track, None)
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                logging.exception("Не вдалося зупинити провалену доріжку %s", track)
        if self._meeting_streams:
            self.tray.notify(tr("meeting_track_dropped_continue", track=track))
            return
        self.tray.notify(tr("meeting_mic_recovery_failed", track=track))
        detached = self._detach_meeting()
        if detached is None:
            return
        sess, screen = detached
        if screen is not None:
            # GUI-потік: короткий бар'єр на закриття MP4 (як _shutdown_meeting_for_exit),
            # щоб довге очікування не фризило інтерфейс під час аварійного переривання.
            if not screen.wait_finished(3.0):
                logging.error("MP4 не закрився за 3 с при перериванні наради; фіналізація interrupted")
                self._mark_screen_failed(sess, None)
            elif getattr(screen, "finished_error", False):
                self._mark_screen_failed(sess, screen.error)
        try:
            from whisper_core.meeting import session as msession
            meta = sess.finalize(msession.STATUS_INTERRUPTED)
        except Exception:
            logging.exception("Не вдалося зберегти перервану нараду")
            meta = None
        self.meeting_state.emit("idle")
        if meta is not None:
            self.meeting_session_done.emit(sess.id, meta)

    def _on_meeting_screen_error(self, message):
        """Відео — додаткове: показуємо помилку, але нараду не зупиняємо."""
        self.tray.notify(message)

    def _on_meeting_device_lost(self, track, exc):
        """Callback рідера: зафіксувати причину; UI отримує наступний
        on_audio_state("reconnecting") через GUI-safe сигнал."""
        logging.warning("Пристрій наради (%s) змінився: %s", track, exc)

    # === диктофон (feature/player-recordings) ============================
    # Простий запис голосу БЕЗ негайної розшифровки. Ділить recorder із
    # диктуванням (один мікрофон) — взаємно заблокований гейтами вище. Файл
    # зберігається у теку записів; звідти його можна відтворити вбудованим
    # плеєром і за бажанням поставити в чергу вкладки «Файли».

    def _recordings_root(self) -> Path:
        """Активна тека записів (cfg або дефолт paths.recordings_dir())."""
        return (Path(self.cfg.recordings_dir) if getattr(self.cfg, "recordings_dir", None)
                else paths.recordings_dir())

    def set_recordings_dir(self, path: str):
        """Тека записів диктофона; порожньо = дефолт (локальна, поза синхронізацією)."""
        self.cfg.recordings_dir = path or None
        self.cfg.save()

    def dictaphone_busy(self) -> bool:
        """Застосунок зайнятий чимось, що конфліктує із записом диктофона.
        feature/dictation-queue: _pipeline_busy враховує й активну/очікуючу чергу."""
        return (_pipeline_busy(self) or self._capturing or self._mic_testing
                or self.recorder.recording or self._meeting_active
                or getattr(self, "_dictaphone_active", False))

    def dictaphone_start(self) -> bool:
        """Почати запис диктофона. Гейти ті самі, що у диктуванні/наради (один
        мікрофон, одна модель). Немає потоку мікрофона → тост і відмова.

        РІШЕННЯ проти росту RAM: запис НЕ буферизується в пам'яті (година дала б
        ~500 МБ піку) — кожен блок з audio-callback стрімиться на диск через
        recordings.RecordingWriter (sink у recorder.start)."""
        if self.dictaphone_busy():
            self.tray.notify(tr("meeting_busy_wait"))
            return False
        if not self.recorder.has_stream:
            self.tray.notify(tr("app_mic_unavailable"))
            return False
        try:
            writer = recordings.RecordingWriter(
                self._recordings_root(), self.cfg.sample_rate)
        except OSError:
            logging.exception("Не вдалося відкрити файл запису диктофона")
            self.tray.notify(tr("rec_save_fail"))
            return False
        self._dictaphone_writer = writer
        self._dictaphone_active = True
        self._dictaphone_started = time.time()
        # feature/tts-listen (§9.1 БАР'ЄР): False → диктофон не стартує
        if not getattr(self, "_before_microphone_start", lambda *_a, **_k: True)("dictaphone"):
            self._dictaphone_active = False
            self._dictaphone_writer = None
            return False
        self.recorder.start(sink=writer.write)
        self.tray.set_state("recording")
        return True

    def dictaphone_stop(self):
        """Зупинити запис і фіналізувати WAV. Повертає шлях або None (закоротший
        за recordings.MIN_SECONDS запис видаляється — з чесним тостом)."""
        if not self._dictaphone_active:
            return None
        self._dictaphone_active = False
        self.recorder.stop()
        writer, self._dictaphone_writer = self._dictaphone_writer, None
        self.tray.set_state("idle")
        if writer is None:
            return None
        path = writer.close()
        if path is None:
            # закоротко/тиша — файл не збережено; без тосту користувач вважав
            # би, що запис зник безслідно
            self.tray.notify(tr("rec_too_short"))
        return path

    def dictaphone_cancel(self):
        """Скасувати запис: файл видаляється, нічого не зберігається."""
        if not self._dictaphone_active:
            return
        self._dictaphone_active = False
        self.recorder.stop()
        writer, self._dictaphone_writer = self._dictaphone_writer, None
        if writer is not None:
            writer.abort()
        self.tray.set_state("idle")

    def dictaphone_level(self) -> tuple:
        """Рівень мікрофона для смужки під час запису (як у диктуванні)."""
        try:
            return self.recorder.take_meter()
        except Exception:
            return (0.0, 0.0)

    def list_recordings(self) -> list:
        """Збережені записи диктофона (новіші першими)."""
        try:
            return recordings.list_recordings(self._recordings_root())
        except Exception:
            logging.exception("Не вдалося перелічити записи диктофона")
            return []

    def delete_recording(self, name) -> bool:
        """Видалити один запис за іменем (traversal-захист у ядрі)."""
        try:
            return recordings.delete_recording(self._recordings_root(), name)
        except Exception:
            logging.exception("Не вдалося видалити запис %s", name)
            return False

    def transcribe_recording(self, path):
        """Поставити збережений запис у ту саму чергу, що й ручне додавання файлів."""
        try:
            self.window.files.add_files([str(path)])
        except Exception:
            logging.exception("Не вдалося поставити запис у чергу: %s",
                              anonymize_path(path))

    def transcribe_audio_range(self, path, start_s: float, end_s: float):
        """Експортувати виділення окремим WAV і віддати спільній черзі «Файли».

        Черга лишається джерелом правди для статусу, історії та повторної
        розшифровки іншою моделлю; оригінальний запис не змінюється.
        """
        try:
            root = self._recordings_root() / "editor"
            stem = Path(path).stem
            out = root / f"{stem}-selection-{time.time_ns()}.wav"
            return audioedit.queue_range(
                path, out, start_s, end_s,
                lambda clip: self.window.files.add_files([clip]))
        except Exception:
            logging.exception("Не вдалося поставити виділення в чергу: %s",
                              anonymize_path(path))
            return None

    def redact_transcript(self, audio_path, start_s: float, end_s: float, *, marker: str, note: str,
                          source: "str | None" = None):
        """feature/redaction: заредагувати репліки транскрипту, що перетинають
        [start, end), у ОКРЕМИЙ файл ``transcript-redacted.txt/.json`` поряд із
        сесією — текст перекритих реплік → ``marker``, у кінець дописується
        ``note`` («фрагмент N:NN–M:MM вилучено»). ОРИГІНАЛЬНІ transcript.* НЕ
        чіпаємо (їх переписати = безповоротна втрата початкових слів).

        ``source`` (код обраної доріжки) звужує редакцію до ОДНОГО джерела: у
        нараді mic+sys затирання торкається лише реплік цієї доріжки, репліки
        інших голосів лишаються цілими. ``None`` — одна доріжка / legacy.

        Повертає шлях до відредагованого transcript при успіху, ``None`` — коли
        редагувати нічого (звичайний запис без transcript.json). Помилки читання/
        серіалізації/запису НЕ ковтаємо — вони піднімаються викликачу, щоб той
        показав користувачу збій, а не фальшиве «готово»."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        logging.info("Redaction requested for meeting artifact [%.2f–%.2f]", start_s, end_s)
        # Реальне аудіо доріжки лежить вкладено: <session>/audio/<track>/0000.wav
        # (parent = «sys»/«mic», НЕ id сесії) або пласко <session>/<track>.wav, у
        # самій теці сесії чи в materialized-теці розшифрованої сесії. Ідемо вгору
        # батьками до ПЕРШОГО сегмента, що є безпечним id сесії (формат
        # create_session) і мапиться на наявну теку — брати parent аудіо було хибою.
        audio_path = Path(audio_path)
        session_dir = None
        for parent in audio_path.parents:
            if not msession.is_safe_session_id(parent.name):
                continue
            candidate = self._meeting_session_dir(parent.name)
            if candidate.is_dir():
                session_dir = candidate
                break
        if session_dir is None:
            session_dir = audio_path.parent   # звичайний запис / legacy пласка тека
        try:
            transcript_data = msession.read_artifact(session_dir, "transcript.json")
        except FileNotFoundError:
            return None
        data = json.loads(transcript_data.decode("utf-8"))
        utterances = [mpost.Utterance(**item) for item in data]
        redacted = mpost.redact_utterances(utterances, start_s, end_s, marker=marker, source=source)
        meta = msession.load_meta(session_dir)
        txt_path, _ = mpost.write_transcript(
            session_dir, redacted,
            me_label=tr("meeting_speaker_me"),
            others_label=tr("meeting_speaker_others"),
            speaker_names=(meta.speaker_names if meta else None),
            stem=REDACTED_TRANSCRIPT_STEM)
        mpost.append_transcript_note(session_dir, note, stem=REDACTED_TRANSCRIPT_STEM)
        self._clear_meeting_plain_cache(session_dir.name)
        return txt_path

    def open_recordings_folder(self):
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._recordings_root())))

    # --- збирач корпусу точності (feature/accuracy-corpus) ---
    # feature/reverse-dictation: скільки останніх аудіо диктувань тримати на диску
    # (dictation_audio/ профілю). Старіші видаляються — «Переслухати» на давніх
    # записах стає неактивним, текст лишається. Обмежує ріст диска.
    _DICTATION_AUDIO_MAX = 400

    def _dictation_audio_dir(self, profile) -> "Path":
        """Тека аудіо диктувань профілю (поряд з history.jsonl)."""
        return Path(profile.dir) / "dictation_audio"

    def _persist_dictation_audio(self, profile, audio) -> "str | None":
        """Зберегти аудіо диктування як WAV у dictation_audio/ профілю; повернути
        ІМ'Я файлу (для запису історії) або None (порожнє аудіо / помилка).
        Після запису підрізаємо теку до _DICTATION_AUDIO_MAX найновіших."""
        try:
            sr = int(getattr(self.recorder, "sr", 16000))
            root = self._dictation_audio_dir(profile)
            path = recordings.save_recording(root, audio, sr)
            if path is None:
                return None
            self._prune_dictation_audio(root)
            return path.name
        except Exception:
            logging.exception("Не вдалося зберегти аудіо диктування")
            return None

    def _prune_dictation_audio(self, root) -> None:
        """Лишити лише _DICTATION_AUDIO_MAX найновіших WAV у теці (за mtime)."""
        try:
            wavs = sorted(Path(root).glob("*.wav"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for p in wavs[self._DICTATION_AUDIO_MAX:]:
                p.unlink(missing_ok=True)
        except OSError:
            logging.debug("Не вдалося підрізати теку аудіо диктувань", exc_info=True)

    def dictation_audio_path(self, rec):
        """Повний шлях до збереженого аудіо запису історії або None (немає поля
        audio / файл видалено). Для картки Історії: кнопка «Переслухати»."""
        name = rec.get("audio") if isinstance(rec, dict) else None
        if not name or not recordings.is_safe_recording_name(name):
            return None
        p = self._dictation_audio_dir(self.profile) / name
        return p if p.is_file() else None

    def delete_dictation_audio(self, rec) -> None:
        """Видалити аудіо-файл запису (при видаленні картки Історії). Безпечно:
        перевірка імені + realpath під текою (recordings.delete_recording)."""
        name = rec.get("audio") if isinstance(rec, dict) else None
        if name:
            recordings.delete_recording(self._dictation_audio_dir(self.profile), name)

    def apply_correction(self, rec, new_final: str, *, profile=None,
                         save_corpus: bool = False, recognized=None):
        """feature/selflearn-dict: ЄДИНИЙ шлях збереження виправлення (зворотне
        диктування ТА діалог «Виправити розпізнавання»). За ОДНУ дію користувача:
          1) оновити final саме того запису історії (за стабільним id, інакше ts);
             raw (verbatim) лишається цілим;
          2) (опц.) зберегти зразок корпусу точності — стара поведінка «погано»;
          3) вивести й зберегти БЕЗПЕЧНЕ правило в ЗАХОПЛЕНИЙ профіль
             («виправив раз — назавжди»), БЕЗ походу в налаштування;
          4) перечитати терміни, якщо цей профіль досі активний.

        profile — знімок на момент відкриття діалогу: перемикання словника поки
        діалог відкритий НЕ переадресує навчання на новоактивний профіль.
        Повертає self_learning.LearnResult (для тосту й undo)."""
        profile = profile or self.profile
        before, rid, ts = "", None, None
        if isinstance(rec, dict):
            before = (rec.get("final") or rec.get("raw") or "").strip()
            rid, ts = rec.get("id"), rec.get("ts")
        updated = False
        if rid:
            updated = update_final_by_id(
                profile.history_path, rid, new_final,
                fallback=(ts, rec.get("raw"), rec.get("final"), rec.get("source")))
        elif ts is not None:
            updated = update_record(profile.history_path, ts,
                                    final=new_final, mark_edited=True)
        if save_corpus:
            self.save_corpus_sample(
                recognized if recognized is not None else before, new_final,
                ts=ts, source=(isinstance(rec, dict) and rec.get("source")) or "desktop",
                profile=profile)
        base = recognized if recognized is not None else before
        result = self_learning.learn_from_correction(
            profile, base, new_final, history_id=rid or "",
            source=(isinstance(rec, dict) and rec.get("source")) or "history")
        result.history_updated = updated
        if result.status == "learned" and profile.name == self.profile.name:
            self.reload_terms()          # активний профіль побачить правило одразу
        return result

    def revoke_learned(self, profile, entry_id: str) -> bool:
        """Скасувати вивчене правило (undo-тост / «Прибрати» у Словниках).
        Перечитує терміни, якщо профіль ще активний."""
        ok = self_learning.revoke(profile, entry_id)
        if ok and profile.name == self.profile.name:
            self.reload_terms()
        return ok

    def learn_from_report(self, recognized: str, corrected: str, *,
                          source: str = "desktop", profile=None):
        """feature/selflearn-dict: навчання з діалогу «Розпізнано погано» — з diff
        (recognized→corrected) у ЗАХОПЛЕНИЙ словник, без оновлення історії (це шлях
        звіту точності, текст запису не переписуємо). Повертає LearnResult.

        profile — знімок на момент відкриття діалогу (як в apply_correction):
        перемикання словника з трею, поки модалка відкрита, НЕ переадресує навчання
        на новоактивний профіль. За замовчуванням — активний профіль."""
        profile = profile or self.profile
        result = self_learning.learn_from_correction(
            profile, recognized or "", corrected or "", source=source or "history")
        if result.status == "learned" and profile.name == self.profile.name:
            self.reload_terms()
        return result

    _CORPUS_AUDIO_MAX = 30   # скільки останніх кліпів тримати в пам'яті

    def _remember_corpus_audio(self, ts, audio) -> None:
        """Покласти аудіо-кліп диктування в буфер під ключем ts; витіснити
        найстаріший, коли перевалили за ліміт (обмежуємо RAM)."""
        try:
            sr = int(getattr(self.recorder, "sr", 16000))
            self._corpus_audio[ts] = (np.asarray(audio, dtype=np.float32), sr)
            while len(self._corpus_audio) > self._CORPUS_AUDIO_MAX:
                self._corpus_audio.popitem(last=False)
        except Exception:
            logging.exception("Не вдалося запам'ятати аудіо для корпусу")

    def save_corpus_sample(self, recognized, corrected, *, ts=None,
                           src_wav=None, source="desktop", profile=None):
        """Зберегти зразок у корпус. Аудіо-кліп береться: з буфера диктування за
        ts (диктування), або зі src_wav (аудіофайл/нарада). Немає ні того, ні
        іншого → текстовий зразок. Повертає dict або None.

        profile — знімок словника на момент виправлення (Profile або None→активний):
        прив'язує зразок до словника, щоб щоденник помилок і підказки фільтрувались
        за профілем (feature/selflearn-dict)."""
        audio = sr = None
        buf = self._corpus_audio.get(ts) if ts is not None else None
        if buf is not None:
            audio, sr = buf
        prof_name = getattr(profile or self.profile, "name", "") or ""
        try:
            return corpus.save_sample(
                recognized, corrected, audio=audio, sample_rate=sr,
                src_wav=src_wav, model=getattr(self.cfg, "model_name", ""),
                source=source, profile=prof_name)
        except Exception:
            logging.exception("Не вдалося зберегти зразок корпусу")
            return None

    def corpus_count(self) -> int:
        try:
            return corpus.count()
        except Exception:
            logging.exception("Не вдалося порахувати корпус")
            return 0

    def open_corpus_folder(self):
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(paths.corpus_dir())))

    # --- транскрипція файлів (вкладка «Файли») ---
    def enqueue_file(self, path, model=None) -> int:
        self._job_seq += 1
        # дескриптор черги: ("file", jid, path, model). model=None → активна
        # модель рушія; заданий — feature/qol-pack «повторно розшифрувати іншою
        # моделлю» (тимчасовий рушій на час задачі). Спільний воркер розрізняє
        # гілки ЛИШЕ на кроці emit — саме транскрибування спільне.
        self._file_jobs.put(("file", self._job_seq, str(path), model))
        return self._job_seq

    def cancel_file_job(self, jid) -> bool:
        """Скасувати задачу черги файлів (fix/cancel-transcription).

        Черга queue.Queue не дає видалити елемент із середини, тож задачу
        позначаємо: ще не розпочата — воркер її пропустить і не витратить ані
        секунди моделі; активна — рушій уривається на найближчому сегменті
        (TranscriptionCancelled). В обох гілках картка отримує стан «скасовано»
        одним і тим самим file_done, тобто рядок черги закривається завжди.
        Повертає False, коли задачі з таким номером ніколи не було."""
        if not isinstance(jid, int) or jid < 1 or jid > self._job_seq:
            return False
        with self._file_cancel_lock:
            self._file_cancelled.add(jid)
        return True

    def _file_job_cancelled(self, jid) -> bool:
        with self._file_cancel_lock:
            return jid in self._file_cancelled

    def _forget_file_job(self, jid) -> None:
        """Задача закрита (готово/помилка/скасовано) — прибрати позначку, щоб
        множина не росла за сесію і номер не «тягнув» скасування за собою."""
        with self._file_cancel_lock:
            self._file_cancelled.discard(jid)

    def enqueue_meeting_track(self, session_id, track, wav_path):
        """feature/meeting-ui: доріжка наради у ТУ САМУ чергу розшифровки."""
        self._file_jobs.put(("meeting", session_id, track, str(wav_path)))

    def _file_worker(self):
        """Один воркер на всі файли Й доріжки наради; ділить модель із PTT через
        _engine_lock. Гілки розрізняються лише на emit (feature/meeting-ui)."""
        while True:
            job = self._file_jobs.get()
            profile, terms = self.profile, self.terms
            if job[0] == "meeting":
                _, session_id, track, path = job
                try:
                    include_word_timestamps = (
                        track == "sys" and
                        bool(getattr(self.cfg, "diarization_enabled", False)))
                    if include_word_timestamps:
                        result = self._transcribe_with_fallback(
                            path, terms, include_word_timestamps=True)
                    else:
                        result = self._transcribe_with_fallback(path, terms)
                    _raw, final, _dur, _words, segs = result[:5]
                    timed_words = result[5] if len(result) > 5 else []
                    # нарада НЕ пише у history.jsonl — власне сховище (розділ 3.4)
                    self.meeting_track_done.emit(session_id, track,
                                                 final or "", ((segs or [], timed_words or [])
                                                                if include_word_timestamps else (segs or [])))
                except ModelAbsentError:
                    self.meeting_error.emit(session_id, tr("app_model_absent_meeting"))
                except Exception as e:
                    logging.exception("Помилка розшифровки доріжки наради %s/%s",
                                      session_id, track)
                    msg = (tr("app_cuda_error") if is_cuda_runtime_error(e)
                           else tr("meeting_error_generic"))
                    self.meeting_error.emit(session_id, msg)
                continue
            # ("file", jid, path, model): emit-имо КОДИ стану (FileStatus), не
            # перекладений текст — UI сам покаже бейдж через tr. meta успіху несе
            # тривалість. model — опційне перевизначення моделі (feature/qol-pack).
            _, jid, path, model = job
            # Скасовано, поки задача чекала у черзі (кнопка на картці доступна
            # від постановки в чергу) — не витрачаємо модель узагалі.
            if self._file_job_cancelled(jid):
                self._forget_file_job(jid)
                self.file_done.emit(jid, tr("files_cancelled_body"),
                                    FileStatus.CANCELLED, [], [])
                continue
            self.file_status.emit(jid, FileStatus.TRANSCRIBING)
            try:
                # feature/model-bottlenecks (під-хвиля 2): word_timestamps за тим
                # самим тумблером, що й диктування — картка файлу підсвічує непевні
                # слова тим самим кодом (main_window.render_uncertainty_html).
                _hl = bool(getattr(getattr(self, "cfg", None),
                                   "highlight_uncertain_words", True))
                raw, final, dur, words, segs = self._transcribe_file_job(
                    path, terms, model, include_word_timestamps=_hl,
                    should_cancel=lambda j=jid: self._file_job_cancelled(j))
                log_history(profile.history_path, raw, final,
                            source="file", enabled=profile.memory_enabled)
                self.file_done.emit(jid, final or tr("files_silence"),
                                    f"{FileStatus.DONE}:{dur:.0f}", segs, words)
                # feature/auto-export: завершений файл черги теж дописуємо у теку
                # (лише коли є текст і автозбереження ввімкнене; гейт getattr — як у _work)
                if (final and getattr(self.cfg, "auto_export_enabled", False)
                        and getattr(self.cfg, "auto_export_dir", None)):
                    self._auto_export(final)
            except TranscriptionCancelled:
                # Свідома дія користувача — не помилка: ні лога-винятку, ні
                # запису в історію, ні тексту. Картка йде у стан «скасовано».
                self.file_done.emit(jid, tr("files_cancelled_body"),
                                    FileStatus.CANCELLED, [], [])
            except ModelAbsentError:
                self.file_done.emit(jid, tr("files_no_model_banner"),
                                    FileStatus.ERROR, [], [])
            except Exception as e:
                logging.exception("Помилка транскрипції файлу %s", anonymize_path(path))
                if is_cuda_runtime_error(e):
                    body = tr("app_cuda_error")
                else:
                    body = tr("files_error_body")
                self.file_done.emit(jid, body, FileStatus.ERROR, [], [])
            finally:
                self._forget_file_job(jid)

    def _transcribe_file_job(self, path, terms, model, *,
                             include_word_timestamps=False, should_cancel=None):
        """Розшифрувати файл черги. model=None/активна → звичайний шлях (рушій +
        CUDA-fallback). Інша встановлена модель (feature/qol-pack «спробувати
        іншою моделлю») → тимчасовий рушій на час задачі під тим самим
        _engine_lock (щоб не конкурувати з PTT/іншими файлами за пам'ять/модель);
        активний рушій не чіпаємо. Тимчасовий рушій відпускається одразу після
        задачі — GC звільнить пам'ять моделі.

        should_cancel — опційний зворотний виклик «скасовано?»; рушій уривається
        між сегментами й кидає TranscriptionCancelled. None (типово) додаємо в
        kwargs лише за наявності: старі мок-рушії тестів його не знають."""
        # [:5] нормалізує 5-/6-кортеж (з timed_words при word_timestamps): картці
        # файлу потрібні лише (raw, final, dur, words, segs); timed відкидаємо.
        cancel_kw = {} if should_cancel is None else {"should_cancel": should_cancel}
        if not model or model == self.cfg.model_name:
            return self._transcribe_with_fallback(
                path, terms, include_word_timestamps=include_word_timestamps,
                **cancel_kw)[:5]
        cfg2 = copy(self.cfg)
        cfg2.model_name = model
        lifecycle = getattr(self, "_model_lifecycle", None)
        lease = lifecycle.activity(load=False) if lifecycle is not None else nullcontext()
        with lease:
            with self._engine_lock:
                engine = Engine(cfg2)  # ~кілька сек + пам'ять моделі (свідома дія)
                try:
                    return engine.transcribe(
                        path, terms,
                        include_word_timestamps=include_word_timestamps,
                        **cancel_kw)[:5]
                finally:
                    engine.close()

    # --- робочий потік PTT ---
    def _transcribe_with_fallback(self, audio, terms, *, include_word_timestamps=False,
                                  should_cancel=None):
        """Транскрибувати; при збої CUDA один раз перебудувати рушій на CPU.

        Це зберігає поточний запис: користувач отримує текст із тієї ж аудіопорції,
        а не лише тост із вимогою повторити диктування після ручної зміни.

        should_cancel — див. _transcribe_file_job; TranscriptionCancelled не є
        збоєм CUDA, тож гілка fallback його не перехоплює (проліта нагору).
        """
        cancel_kw = {} if should_cancel is None else {"should_cancel": should_cancel}
        lifecycle = getattr(self, "_model_lifecycle", None)
        lease = lifecycle.activity() if lifecycle is not None else nullcontext()
        with lease:
            with self._engine_lock:
                try:
                    if include_word_timestamps:
                        return self.engine.transcribe(
                            audio, terms, include_word_timestamps=True, **cancel_kw)
                    return self.engine.transcribe(audio, terms, **cancel_kw)
                except Exception as cuda_error:
                    if self.cfg.device != "cuda" or not is_cuda_runtime_error(cuda_error):
                        raise
                    logging.warning("CUDA-транскрипція впала; повторюю поточне аудіо на CPU")
                    diagnostic_event("compute_fallback", reason="cuda_to_cpu", level=logging.WARNING)
                    cpu_cfg = _prepare_cpu_config(copy(self.cfg))
                    try:
                        cpu_engine = Engine(cpu_cfg)
                    except Exception:
                        raise cuda_error
                    # До queued GUI-slot shared cfg не мутуємо й не пишемо з worker.
                    cpu_engine.cfg = self.cfg
                    self._engine_load_cfg = copy(cpu_cfg)
                    old_engine, self.engine = self.engine, cpu_engine
                    close = getattr(old_engine, "close", None)
                    if callable(close):
                        close()
                    self.cpu_fallback.emit()
                    if include_word_timestamps:
                        return self.engine.transcribe(
                            audio, terms, include_word_timestamps=True, **cancel_kw)
                    return self.engine.transcribe(audio, terms, **cancel_kw)

    def _work(self, chunks, profile, terms, *, paste_target=_PASTE_TARGET_UNSET,
              paste_pid=None, from_queue=False, policy=None):
        # feature/dictation-queue: у режимі черги ціль вставки й PID приходять із
        # ДЖОБА (закріплені на старті СВОЄЇ фрази), а не з self._paste_target, який
        # уже міг перезаписати наступний запис. from_queue=True → станом idle/трею
        # керує потік черги (_apply_queue_state), не finally цього методу.
        # Тихих провалів тут бути НЕ може: заглушений у Windows мікрофон дає
        # порожній запис, і без явного сигналу користувач вважає, що все ок
        # (вкладка «Файли» такий випадок уже показує — files_silence).
        try:
            audio = self.recorder.to_audio(chunks)
            if audio is None:
                logging.warning("Диктування: запис порожній або закороткий — "
                                "можливо, мікрофон заглушено")
                self.transcription_error.emit(tr("app_dictation_silence"))
                return
            # feature/audio-center: опційні gate/AGC ПЕРЕД Whisper (лише диктування)
            audio = _apply_capture_dsp(getattr(self, "cfg", None), audio)
            _t = time.perf_counter()
            # feature/model-bottlenecks (під-хвиля 2): у диктуванні вмикаємо
            # word_timestamps, коли ввімкнено підсвітку непевних слів (тумблер,
            # дефолт True) — інакше seg.words порожні й золота підсвітка у стрічці
            # (main_window._render_html) ніколи не спрацьовує. Прапорець додає
            # DTW-прохід вирівнювання, тож керований (Розширене диктування).
            # result[:5] стійке до 5- та 6-кортежу (з timed_words), який ігноруємо.
            _hl = bool(getattr(getattr(self, "cfg", None),
                               "highlight_uncertain_words", True))
            result = self._transcribe_with_fallback(
                audio, terms, include_word_timestamps=_hl)
            raw, final, _, words, _segs = result[:5]
            test_log("pipe_transcribe", ms=round((time.perf_counter() - _t) * 1000, 1),
                     words=len(words or []), text_raw=raw, text_final=final)
            # ── ПОРЯДОК КОНВЕЄРА ТЕКСТУ (диктування) ─────────────────────────
            # словники профілю (в engine.transcribe) → філери → [FORMFILL-
            # перехоплення: чистий текст у поле діалогу, далі не йде] → сніпети →
            # макроси (хіт вимикає голосову пунктуацію) → голосова пунктуація →
            # автокорекція → пунктуатор → вставка.
            # feature/processing-slider: рівень обробки — ВЕРХНЯ МЕЖА змін тексту
            # (спека §3). policy=None → застарілий шлях (точно як до повзунка: читаємо
            # cfg, всі етапи), щоб наявні тести й старі мок-контролери не змінили
            # поведінку. Реальний конвеєр передає знімок режиму профілю, захоплений на
            # старті запису. У «Дослівно»/«Без слів-паразитів» стартуємо від СИРОГО
            # тексту (обхід словників — «лише філери» має означати лише філери), але
            # історія все одно пише незайманий raw (гарантія відновлюваності, §7).
            if policy is None:
                _level = cleanup_level_for_cfg(getattr(self, "cfg", None)) if final else "off"
                _allow_voice = _allow_macros = _allow_enhance = _allow_ctxfmt = True
            else:
                if policy.source == "raw":
                    final = raw
                _level = policy.cleanup_level if final else "off"
                _allow_voice = policy.voice_commands
                _allow_macros = policy.macros
                _allow_enhance = policy.autocorrect or policy.punctuator
                _allow_ctxfmt = policy.context_formatting
            # feature/filler-cleanup: ПІСЛЯ термінів (у engine.transcribe), ПЕРЕД
            # шаблонами й пунктуацією — щоб слова-паразити зникли до того, як
            # спрацює точний-збіг шаблон чи команди стануть знаками. Лише диктування
            # (не файли черги).
            if final and _level != "off":
                _t = time.perf_counter()
                _before = final
                final = apply_filler_cleanup(final, _level)
                test_log("pipe_fillers", ms=round((time.perf_counter() - _t) * 1000, 1),
                         changed=(final != _before), text_out=final)
            # feature/voice-form-fill: перехоплення диктанту у ПОТОЧНЕ поле
            # діалогу шаблону — ДО всього конвеєра вставки (шаблони/макроси/
            # голосова пунктуація/автокорекція/пунктуатор), але ПІСЛЯ словників
            # (engine.transcribe) і чистки філерів: текст у поле має бути чистий,
            # проте БЕЗ розгортання шаблонів і БЕЗ перетворення слів-команд
            # («кома», «крапка») на знаки — у полі диктують значення. Далі — не
            # вставка й не історія; розбір команд «наступне поле» на боці діалогу.
            if final and getattr(self, "formfill_capturing", False):
                test_log("pipe_formfill", destination="formfield", text_out=final)
                self.formfill_text.emit(final)
                return
            # feature/office-voice-nav: голосова навігація полями зовнішніх
            # документів. ПЕРЕД макросами/пунктуацією/вставкою — щоб команда
            # («наступне поле», «комірка Б7») не стала текстом чи тригером макроса.
            # Лише коли режим увімкнено (opt-in) і НЕ в captured-режимі шаблону
            # (там своя навігація). Розпізнана команда споживається (клавіші у
            # закріплену ціль), далі конвеєром НЕ йде; невпізнане — звичайний текст.
            if final and _allow_voice and getattr(self.cfg, "voice_nav_enabled", False):
                nav_action = navcommands.match(
                    final, self.cfg.language, getattr(self, "_nav_aliases", None))
                if nav_action is not None:
                    nav_result = _deliver_nav(self, nav_action)
                    test_log("pipe_nav", kind=nav_action[0], arg=str(nav_action[1]),
                             result=str(nav_result), text_out=final)
                    return
            # feature/voice-macros: точний-збіг голосові макроси — ПІСЛЯ термінів і
            # чистки філерів, ПЕРЕД голосовою пунктуацією, щоб тригер матчився на
            # природно сказаній фразі. Розгортка може містити {дата}/{час} — вони
            # підставляються поточними в момент вставки. Якщо макрос спрацював —
            # голосову пунктуацію НЕ застосовуємо: розгортка заготовлена ДОСЛІВНО, і
            # слово-команда в ній («Кома», «Крапка», «новий рядок») не має тихо
            # перетворюватись на символ. getattr — старі мок-контролери тестів не
            # мають self.macros.
            macros = getattr(self, "macros", None)
            macro_hit = False
            if final and _allow_macros and macros:
                _t = time.perf_counter()
                expanded = apply_macro(final, macros)
                if expanded != final:
                    macro_hit = True
                    final = expanded
                test_log("pipe_macros", ms=round((time.perf_counter() - _t) * 1000, 1),
                         hit=macro_hit, text_out=final)
            # feature/voice-punctuation: лише для диктування (не для файлів черги)
            # і ПІСЛЯ заміни термінів у engine.transcribe; пропускаємо, коли вставили
            # макрос (його текст дослівний). getattr — сумісність зі старими
            # мок-конфігами тестів; порядок гейтів короткозамикає порожній final.
            if (final and _allow_voice and not macro_hit
                    and getattr(self.cfg, "voice_punctuation", False)):
                _t = time.perf_counter()
                _before = final
                final = apply_voice_punctuation(final, self.cfg.language)
                test_log("pipe_voice_punct", ms=round((time.perf_counter() - _t) * 1000, 1),
                         changed=(final != _before), text_out=final)
            # feature/punctuation-plus: автокорекція одруків + пунктуатор/ITN
            # (обидва opt-in, експериментальні) — ПІСЛЯ словників профілю,
            # чистки філерів, шаблонів і голосової пунктуації, ПЕРЕД вставкою.
            # Пропускаємо, коли вставили макрос (його текст дослівний).
            # getattr — старі мок-контролери тестів не мають методу.
            enhance = getattr(self, "_apply_text_enhancements", None)
            if enhance and _allow_enhance and not macro_hit:
                final = enhance(final, terms, policy)
            if final:
                # feature/context-profiles: рішення про вставку за активним
                # вікном. held=True → НЕ вставляємо (лише картка+трей-нота):
                # або блок-лист безпеки (менеджери паролів), або enabled=false
                # у профілі вікна. Картку (transcribed.emit) показуємо завжди.
                behavior, blocked = _paste_context(self)
                # feature/output-formats: детермінований профіль форматування
                # виводу (plain/markdown/code/letter) з поведінки контекстного
                # профілю. Застосовуємо до final ПЕРЕД вставкою/історією/карткою,
                # тож усі три бачать той самий переформатований текст. plain —
                # нейтральна нормалізація, тож поведінка без профілю не змінюється.
                fmt = getattr(behavior, "formatting", "plain") if behavior is not None else "plain"
                if _allow_ctxfmt and fmt and fmt != "plain":
                    _before_fmt = final
                    final = apply_output_format(final, fmt)
                    test_log("pipe_format", mode=fmt,
                             changed=(final != _before_fmt), text_out=final)
                held = blocked or (behavior is not None and not behavior.enabled)
                test_log("pipe_insert", mode=self.output_mode, held=held,
                         blocked=blocked,
                         preview=bool(getattr(self.cfg, "paste_preview", False)),
                         text_out=final)
                if self.output_mode in ("paste", "both") and not held:
                    auto_enter = behavior is not None and behavior.auto_enter
                    if getattr(self.cfg, "paste_preview", False):
                        # feature/paste-preview: не вставляти одразу — показати
                        # картку перегляду/правки в GUI-потоці. Вставка піде тим
                        # самим paste-шляхом (_deliver_paste) після підтвердження.
                        diagnostic_event("dictation_delivered", destination="preview", chars=len(final))
                        self.preview_ready.emit(final, auto_enter)
                    else:
                        # усі способи вставки могли впасти (цільове вікно з
                        # підвищеними правами блокує симуляцію Ctrl+V) — _deliver_paste
                        # сигналить у трей, бо текст лишається лише в буфері.
                        # feature/cascade-paste (набір посимвольно) і клас вікна
                        # (консоль/RDP/менеджер паролів) вирішуються всередині.
                        # feature/qol-pack: undo-буфер і звук підтвердження вставки
                        # тепер живуть у _deliver_paste (запис ПІСЛЯ фактичної
                        # вставки), тож і негайний шлях, і вставка після перегляду
                        # (feature/paste-preview) однаково запам'ятовують текст.
                        # feature/paste-safety: передаємо закріплену ціль (звірка
                        # вікна перед вставкою).
                        _pt = (paste_target if paste_target is not _PASTE_TARGET_UNSET
                               else getattr(self, "_paste_target", None))
                        _deliver_paste(self, final, auto_enter,
                                       pinned_target=_pt, pinned_pid=paste_pid)
                elif self.output_mode in ("paste", "both") and held:
                    self.transcription_error.emit(
                        tr("ctx_paste_blocked") if blocked else tr("ctx_paste_held"))
                # feature/reverse-dictation: зберегти аудіо цього диктування на
                # диск (поряд з історією), щоб пізніше в Історії можна було
                # ПЕРЕСЛУХАТИ своє вимовляння перед виправленням. Гейт — пам'ять
                # профілю + тумблер save_dictation_audio (усе локально).
                audio_name = None
                if profile.memory_enabled and getattr(
                        self.cfg, "save_dictation_audio", True):
                    audio_name = self._persist_dictation_audio(profile, audio)
                rec = log_history(profile.history_path, raw, final,
                                  source="desktop", enabled=profile.memory_enabled,
                                  audio=audio_name)
                ts = rec["ts"] if rec else None   # None → у файл не писали (пам'ять off)
                # feature/accuracy-corpus: запам'ятати аудіо-кліп цього диктування
                # (float32), щоб пізніша дія «Розпізнано погано…» на картці мала що
                # зберегти в корпус. Ключ — ts картки; ts=None (пам'ять off) не
                # адресується карткою, тож тоді не тримаємо.
                if ts is not None:
                    self._remember_corpus_audio(ts, audio)
                self.transcribed.emit(raw, final, words, ts)
                # feature/auto-export: гейт getattr на місці виклику (сумісність зі
                # старими мок-конфігами тестів, як voice_punctuation вище)
                if (getattr(self.cfg, "auto_export_enabled", False)
                        and getattr(self.cfg, "auto_export_dir", None)):
                    self._auto_export(final)
            else:
                logging.warning("Диктування: розпізнано порожній текст (VAD "
                                "зрізав тишу) — можливо, мікрофон заглушено")
                self.transcription_error.emit(tr("app_dictation_silence"))
        except ModelAbsentError:
            self.transcription_error.emit(tr("app_model_absent_ptt"))
        except Exception as e:
            traceback.print_exc()
            logging.exception("Помилка транскрипції диктування")
            test_log("pipe_error", error=type(e).__name__)  # повний трейс — у logging.exception
            if is_cuda_runtime_error(e):
                self.transcription_error.emit(tr("app_cuda_error"))
            else:
                self.transcription_error.emit(tr("app_transcribe_error"))
        finally:
            # feature/dictation-queue: джоби черги НЕ чіпають _busy і не шлють
            # finished — інакше idle-стан перебивав би наступні фрази; трей/пілюлю
            # веде _apply_queue_state, коли черга спорожніє.
            if not from_queue:
                self._busy = False
                self.finished.emit()


def _install_qt_translation(app):
    """Український (чи інший) переклад стандартних рядків самого Qt — кнопки
    діалогів на кшталт «Показати деталі…». Мова — з уже виставленої i18n; без
    файлу перекладу тихо пропускаємо (рядки лишаться дефолтні)."""
    lang = i18n.current_language()
    if lang == "en":
        return
    try:
        from PySide6.QtCore import QTranslator, QLibraryInfo
        path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
        qt_tr = QTranslator(app)
        if qt_tr.load("qtbase_" + lang, path):
            app.installTranslator(qt_tr)
            app._qt_translator = qt_tr   # тримаємо посилання (щоб не зібрав GC)
    except Exception:
        logging.debug("Переклад Qt не підключено", exc_info=True)


def _instance_channel_name() -> str:
    """Ім'я каналу single-instance. Продуктовий процес — завжди "balachky-single"
    (літерал, не змінювати: оновлення поверх старої версії покладається на
    незмінність цього імені). Тестові/offscreen процеси додають суфікс з
    BALACHKY_INSTANCE_SUFFIX (виставляє tests/_isolation.py), щоб не займати
    робочий канал — інакше живий unittest-гейт відбиває реальний запуск
    власника (кейс 31.07)."""
    return "balachky-single" + os.environ.get("BALACHKY_INSTANCE_SUFFIX", "")


def main():
    from .crash import setup_logging, install_excepthooks
    setup_logging()          # найпершим: жоден стартовий збій — без сліду в лозі
    from .theme import QSS, load_fonts
    # DPI: без PassThrough масштаби 125/150% округлюються → розмиття
    if sys.platform == "win32":
        import ctypes as _ct
        from ctypes import wintypes as _wt
        try:
            shcore = _ct.windll.shcore
            shcore.SetProcessDpiAwareness.argtypes = (_ct.c_int,)
            shcore.SetProcessDpiAwareness.restype = _ct.c_long
            shcore.SetProcessDpiAwareness(2)  # per-monitor DPI
        except Exception:
            pass
        try:
            # власна ідентичність у таскбарі (інакше вікно групується як python.exe);
            # обов'язково ДО створення QApplication
            shell32 = _ct.windll.shell32
            shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = (
                _wt.LPCWSTR,)
            shell32.SetCurrentProcessExplicitAppUserModelID.restype = _ct.c_long
            shell32.SetCurrentProcessExplicitAppUserModelID("Zhukovets.Balachky")
        except Exception:
            pass
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)

    install_excepthooks(app)   # неперехоплені винятки → лог + діалог
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon(paths.assets_dir() / "balachky.ico"))
    load_fonts()             # Fixel (OFL); без файлів — фолбек Segoe UI у QSS
    app.setStyleSheet(QSS)   # єдина гама «Balachky»

    # Один екземпляр: другий клавіатурний хук небезпечний. Якщо канал живий —
    # кажемо першому екземпляру показати вікно і тихо виходимо.
    # Виняток: --relaunch (перезапуск із Налаштувань) — старий екземпляр саме
    # завершується, тож НЕ виходимо, а чекаємо (до 5с), поки він звільнить канал,
    # і лише тоді слухаємо самі. Так DesktopApp (а з ним і PTT-хук) створюється
    # ЛИШЕ після смерті старого — двох клавіатурних хуків одночасно не існує.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    channel = _instance_channel_name()
    relaunch = "--relaunch" in sys.argv
    probe = QLocalSocket()
    probe.connectToServer(channel)
    if probe.waitForConnected(300):
        if not relaunch:
            probe.write(b"show")
            probe.flush()
            probe.waitForBytesWritten(300)
            print("«Балачки у Коростені» вже запущені — відкриваю наявне вікно.",
                  flush=True)
            sys.exit(0)
        probe.abort()
        # пауза без блокування (sleep у GUI-петлі — зависання): QEventLoop +
        # QTimer.singleShot дає керування назад Qt, тоді повторно пробуємо канал
        from PySide6.QtCore import QEventLoop, QTimer

        deadline = time.time() + 5.0
        while time.time() < deadline:
            loop = QEventLoop()
            QTimer.singleShot(100, loop.quit)
            loop.exec()
            again = QLocalSocket()
            again.connectToServer(channel)
            alive = again.waitForConnected(100)
            again.abort()
            if not alive:
                break        # старий вивільнив сервер — можна слухати
        # вичерпаний дедлайн (старий завис) → форсуємо нижче через removeServer
    QLocalServer.removeServer(channel)  # мертвий канал після краху / форс
    server = QLocalServer(app)
    server.listen(channel)

    # Перший запуск → майстер налаштування (модель/мова/докачка).
    # Три сигнали (баг «чистої деінсталяції» + скарга власника 25.07):
    #   • реєстровий прапорець onboarded МІГ пережити видалення, тож майстер
    #     показуємо і коли нема config.toml;
    #   • версія, на якій майстер востаннє пройдено, відрізняється від
    #     поточної DISPLAY_VERSION — оновлення додало новий крок
    #     («Додаткові можливості»), інакше людина його ніколи не побачить.
    # Дев-кейс: env WHISPER_TYPER_MODELS означає «моделі вже є у дев-кеші» —
    # вважаємо onboarded і майстер не показуємо. Логіка — _should_show_onboarding.
    settings = QSettings("Balachky", "Balachky")
    # мова UI — ДО майстра першого запуску (він теж локалізований через tr)
    i18n.set_language(getattr(Config.load(), "ui_language", "uk"))
    _install_qt_translation(app)   # українізувати стандартні кнопки Qt (діалоги)
    config_exists_at_start = paths.config_path().exists()
    if _should_show_onboarding(settings, config_exists_at_start,
                               os.environ.get("WHISPER_TYPER_MODELS"),
                               current_version=DISPLAY_VERSION):
        is_repeat = _is_onboarding_repeat(settings, config_exists_at_start)
        wizard = _build_onboarding_wizard(
            is_repeat, Config.load() if is_repeat else None)
        if wizard.exec():
            cfg = Config.load()
            model_skipped = _apply_onboarding_result(
                cfg, wizard, settings, current_version=DISPLAY_VERSION)
            logging.info("Перше налаштування завершено: модель=%s, тека=%s, мова=%s",
                         cfg.model_name, anonymize_path(cfg.model_dir), cfg.language)
            if model_skipped:
                # Слабкий інтернет: людина натиснула «Пропустити». Налаштування вже
                # збережені вище, тож наступний запуск не починає майстер з нуля.
                # Кажемо стан і стартуємо БЕЗ мовного пакета (feature/no-model-state).
                # Раніше тут стояв вихід із програми — власник це відхилив: пропуск
                # завантаження не має виганяти людину із застосунку.
                try:
                    from .onboarding import drain_workers
                    drain_workers()      # не лишити живий QThread на teardown Qt
                except Exception:
                    pass
                from PySide6.QtWidgets import QMessageBox
                box = QMessageBox()
                box.setIcon(QMessageBox.Information)
                box.setWindowTitle(tr("onb_skipped_title"))
                box.setText(tr("onb_skipped_title"))
                box.setInformativeText(tr("onb_skipped_body"))
                box.exec()
                logging.info("Завантаження моделі пропущено — старт без мовного пакета")
        else:
            # скасував/пропустив (X, Esc, «Скасувати», кнопка «Закрити» на
            # повторному показі) — feature/no-model-state: НЕ виходимо. cfg
            # лишається БЕЗ ЗМІН (у повторному режимі — з наявними
            # налаштуваннями; у першому — без обраної моделі, спроба
            # завантажити її нижче впаде у _recover_engine_on_gui, де відмова
            # від докачки дає NullEngine — застосунок стартує чесно без
            # мовного пакета). «Більше не показувати» — окремо нижче.
            _handle_onboarding_dismissed(
                wizard, settings, DISPLAY_VERSION)
            logging.info("Перше налаштування скасовано — старт без мовного пакета")

    # Splash «Прокидання» + винос завантаження рушія у потік: показуємо заставку,
    # рушій (~3 ГБ) вантажиться у _EngineLoadThread ПОЗА GUI-потоком (тож splash
    # реально рухається, а не морожений), і лише на ready будуємо DesktopApp.
    # Гілка needs_recovery — RecoveryDialog+само-лікування СТРОГО на GUI-потоці
    # (_recover_engine_on_gui), бо RecoveryDialog — модальний GUI-обʼєкт.
    from PySide6.QtCore import QElapsedTimer, QEventLoop
    from . import motion
    from .splash import SplashScreen

    autostart = "--autostart" in sys.argv
    smoke = "--smoke" in sys.argv

    cfg = Config.load()
    cfg.log_level = apply_log_level(getattr(cfg, "log_level", "INFO"))
    apply_test_mode(cfg)   # Режим тестування: DEBUG + дія-логер (якщо ввімкнено в конфігу)
    motion.init_config(cfg)                   # splash шанує прапорець animations
    # feature/night-mode: нічний/червоний режим вмикаємо ДО splash і побудови вікна,
    # щоб уся заставка й інтерфейс одразу були в червоній гамі (без блимання денної).
    # Т50: поки фіча схована (theme.NIGHT_MODE_AVAILABLE=False), night_enabled_for
    # завжди False → старт форситься в день навіть при збереженому night=true.
    from . import theme as _theme
    active_color = _theme.resolve_ui_color(cfg)
    if active_color != "classic":
        _theme.set_ui_color(active_color)
        app.setStyleSheet(_theme.QSS)
        app.setWindowIcon(app_icon(paths.assets_dir() / "balachky.ico"))
    # Роль Link = акцент активної теми (інакше плейн-`<a>` малюються синім — не-
    # червоне світло вночі). ПІСЛЯ можливого нічного свопу, щоб узяти правильне GOLD.
    from . import theme as _theme_links
    _theme_links.apply_link_colors(app)
    # CUDA-рантайм активуємо ДО Engine (порядок критичний) — тепер у main, бо
    # Engine будується у потоці, а не в DesktopApp.__init__.
    cuda_fallback = _normalize_startup_config(cfg)

    # Splash показуємо ЛИШЕ коли буде видиме вікно. Тихий автозапуск
    # (--autostart) лишається тихим — жодного спалаху заставки (як на master):
    # модель однаково вантажиться у потоці, просто без заставки.
    splash = None
    _shown = None
    if not autostart:
        # Привітальне «прокидання» жука на splash — ЛИШЕ на першому запуску в житті
        # продукту (одноразова церемонія, не податок на кожен старт). Далі — тиха
        # статична емблема. Прапорець живе в реєстрі поряд з «onboarded».
        first_greet = settings.value("splash_greeted") is None
        if first_greet:
            settings.setValue("splash_greeted", 1)
        splash = SplashScreen(greet=first_greet)
        splash.show()
        app.processEvents()
        _shown = QElapsedTimer()
        _shown.start()

    _box = {}
    _load_loop = QEventLoop()
    engine_thread = _EngineLoadThread(cfg)

    def _on_engine_ready(eng):
        _box["engine"] = eng
        _load_loop.quit()

    def _on_engine_recovery(err):
        _box["error"] = err
        _load_loop.quit()

    def _on_engine_failed(err):
        _box["fatal"] = err
        _load_loop.quit()

    engine_thread.ready.connect(_on_engine_ready)
    engine_thread.needs_recovery.connect(_on_engine_recovery)
    engine_thread.failed.connect(_on_engine_failed)
    # Страхувальник від вічного hang: навіть якщо потік завершився, НЕ емітнувши
    # жодного сигналу (теоретичний edge), finished усе одно квітне петлю.
    engine_thread.finished.connect(_load_loop.quit)
    engine_thread.start()
    _load_loop.exec()                         # splash анімується, поки крутиться петля
    engine_thread.wait(5000)                  # потік уже завершився — join із таймаутом

    # Фатальний виняток рушія АБО потік без результату → чесний вихід, НЕ hang.
    if "fatal" in _box or not ("engine" in _box or "error" in _box):
        if splash is not None:
            splash._stop_motion()
            splash.close()
        _fatal_engine_error(_box.get("fatal"))    # лог + діалог + sys.exit(1)

    if "error" in _box:
        engine = _recover_engine_on_gui(cfg, _box["error"], splash)
    else:
        engine = _box["engine"]

    # Мінімальний час показу ~800мс (лише коли splash видимий): на теплому кеші
    # заставка не має «блимнути». НІКОЛИ не затримуємо довше реального
    # завантаження — лише доповнюємо до 800.
    if splash is not None and _shown is not None:
        _remain = _splash_min_remaining(_shown.elapsed())
        if _remain > 0:
            _min_loop = QEventLoop()
            QTimer.singleShot(_remain, _min_loop.quit)
            _min_loop.exec()

    dapp = DesktopApp(app, engine, cfg, cuda_fallback)
    diagnostic_event("application_started", version=DISPLAY_VERSION,
                     model=_diagnostic_attr(cfg, "model_name"),
                     device=_diagnostic_attr(cfg, "device"),
                     compute=_diagnostic_attr(cfg, "compute_type"))
    app.aboutToQuit.connect(lambda: diagnostic_event("application_stopped"))

    def _second_instance():
        conn = server.nextPendingConnection()
        while conn is not None:
            conn.readAll()
            conn = server.nextPendingConnection()
        dapp.show_window()

    server.newConnection.connect(_second_instance)
    if smoke:
        from PySide6.QtCore import QTimer
        if splash is not None:
            splash.finish_to(dapp.window)    # crossfade заставки у головне вікно
        dapp.show_window()
        # картка у стрічку через сигнал; «проба» непевна (<0.5) → перевірка підсвітки,
        # «звуку» замінено на «слова» → у final збігу нема, підсвітки не буде
        dapp.transcribed.emit("проба звуку", "проба слова",
                              [("проба", 0.42), ("звуку", 0.31)], None)
        QTimer.singleShot(3000, app.quit)
        mem = "on" if dapp.profile.memory_enabled else "off"
        cards = dapp.window.dictation._feedbox.count() - 1  # мінус stretch
        print(f"SMOKE: збірка ок; вікно+стрічка ок (карток: {cards}); "
              f"профіль={dapp.profile.name}, пам'ять={mem}, вихід ~3с", flush=True)
    else:
        if not autostart:
            splash.finish_to(dapp.window)    # crossfade заставки у головне вікно
        # автозапуск: splash не створювався (тихий вхід) — вікно не показуємо
        print("«Балачки у Коростені» запущено (значок біля годинника). "
              "Затисни клавішу запису -> говори -> відпусти.", flush=True)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
