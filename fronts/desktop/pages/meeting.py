"""Вкладка «Нарада»: запис одним тумблером, окрема явна post-meeting обробка.

feature/meeting-ui (Б2). Джерела вибираються в Налаштуваннях; вкладка читає
канонічний вибір без дублювання чи перезапису. Під час запису — жива панель:
золота «жива кромка» StatusTag,
таймер, одна або дві смужки рівня. Червоний REC-пульс несе велика кругла кнопка
(RecButton) — санкціонований виняток канону. Картки минулих сесій — зі статусами,
доступом до аудіо (диктофон-режим) і помітним повним видаленням.

Уся робота з аудіо-залізом і диском — у контролері (app.py) та ядрі наради
(whisper_core.meeting); тут — лише Qt.
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QRect, QSize, QThread, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea, QStackedWidget,
    QFrame, QApplication, QMessageBox, QFileDialog, QMenu, QLineEdit,
    QCheckBox, QComboBox, QGridLayout, QInputDialog, QDialog, QProgressBar,
    QToolButton, QPushButton,
)

from ..glass import GlassButton, RecButton, StatusTag
from ..empty_state import EmptyState
from ..i18n import tr
from .. import theme
from ..level import LevelMeter
from ..player import InlinePlayer
from ..player_tracks import MultiTrackPlayer, should_show_track_panel
from ..audio_editor import AudioEditorPanel
from .. import motion
from . import page_header


def _audit_journal_lines(events, labels):
    """Рядки подій для діалогу «Журнал цілісності».

    Пошкоджений маркер ({"_corrupt": True}, ts відсутній) НЕ рендеримо як
    подію 1970 року — показуємо один чесний рядок «Пошкоджений запис».
    Решта подій — «[час] Назва». Без Qt → тестується юнітом."""
    lines = []
    for ev in events:
        if isinstance(ev, dict) and ev.get("_corrupt"):
            lines.append(tr("meeting_audit_corrupt_row"))
            continue
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ev.get("ts", 0)))
        label = labels.get(ev.get("type"), ev.get("type", ""))
        lines.append(f"[{stamp}] {label}")
    return lines


# Статуси сесії на диску (дзеркало whisper_core.meeting.session.STATUS_*).
_ST_RECORDING = "recording"
_ST_STOPPED = "stopped"
_ST_PROCESSING = "processing"
_ST_DONE = "done"
_ST_ERROR = "error"
_ST_INTERRUPTED = "interrupted"
_ST_CORRUPTED = "corrupted"

# статус на диску → (kind StatusTag, ключ-i18n бейджа)
_BADGE = {
    _ST_RECORDING: ("busy", "meeting_badge_recording"),
    _ST_STOPPED: ("busy", "meeting_badge_processing"),
    _ST_PROCESSING: ("busy", "meeting_badge_processing"),
    _ST_DONE: ("done", "meeting_badge_done"),
    _ST_ERROR: ("error", "meeting_badge_error"),
    _ST_INTERRUPTED: ("error", "meeting_badge_interrupted"),
    _ST_CORRUPTED: ("error", "meeting_badge_corrupted"),
}


def _show_meeting_box(parent, title: str, text: str, *, kind: str = "question",
                      buttons=None, default_button=None, rich: bool = False) -> int:
    """Хелпер повідомлень для сторінки «Нарада»: замінює системні іконки Qt на
    іконки в нашій стилістиці (qtawesome / theme-токени).
    kind: 'question' | 'warning' | 'info' | 'critical'
    rich=True — текст як RichText із клікабельними посиланнями (consent на
    ліцензію моделі веде на її сторінку в браузері).
    """
    import qtawesome as qta
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    if rich:
        box.setTextFormat(Qt.RichText)
        for lbl in box.findChildren(QLabel):
            lbl.setOpenExternalLinks(True)   # клік по ліцензії → браузер
    else:
        box.setTextFormat(Qt.PlainText)

    icon_map = {
        "question": ("fa6s.circle-question", theme.GOLD),
        "warning": ("fa6s.triangle-exclamation", theme.ALERT),
        "info": ("fa6s.circle-info", theme.GOLD),
        "critical": ("fa6s.circle-xmark", theme.ALERT),
    }
    icon_name, color = icon_map.get(kind, ("fa6s.circle-info", theme.GOLD))
    try:
        pm = qta.icon(icon_name, color=color).pixmap(40, 40)
        box.setIconPixmap(pm)
    except Exception:
        pass

    if buttons is not None:
        box.setStandardButtons(buttons)
        # Системні підписи кнопок англійські навіть в укр UI («Yes»/«No»/«OK») —
        # ставимо локалізовані явно (i18n парність стереже test_i18n_parity).
        _btn_keys = {
            QMessageBox.StandardButton.Yes: "dialog_yes",
            QMessageBox.StandardButton.No: "dialog_no",
            QMessageBox.StandardButton.Ok: "dialog_ok",
        }
        for std, key in _btn_keys.items():
            b = box.button(std)
            if b is not None:
                b.setText(tr(key))
    if default_button is not None:
        box.setDefaultButton(default_button)
    return box.exec()


def _meeting_confirm(parent, title: str, text: str, *, rich: bool = False) -> bool:
    Yes = QMessageBox.StandardButton.Yes
    No = QMessageBox.StandardButton.No
    res = _show_meeting_box(parent, title, text, kind="question",
                            buttons=Yes | No, default_button=No, rich=rich)
    return res == Yes


def _meeting_warn(parent, title: str, text: str) -> None:
    _show_meeting_box(parent, title, text, kind="warning",
                      buttons=QMessageBox.StandardButton.Ok)


def _meeting_info(parent, title: str, text: str) -> None:
    _show_meeting_box(parent, title, text, kind="info",
                      buttons=QMessageBox.StandardButton.Ok)


class WrapLabel(QLabel):
    """QLabel(wordWrap), який НЕ дає стиснути себе нижче за реальну висоту тексту.

    Стоковий QLabel із переносом віддає мінімумом ОДИН рядок незалежно від того,
    скільки їх насправді вийде на поточній ширині, — і в тісній колонці рядок
    layout'а не росте, а підпис ріжеться знизу (клас text_clipped_v візуального
    гейта). Мінімум рахуємо ТИМИ САМИМИ метриками, що ними QLabel малює текст:
    QFontMetrics.boundingRect із шириною contentsRect і прапорцями вирівнювання
    (не heightForWidth — у Qt він для деяких шрифтів завищує на рядок; та сама
    формула у scripts/visual_gate.py)."""

    def minimumSizeHint(self) -> QSize:
        base = super().minimumSizeHint()
        if not self.wordWrap():
            return base
        text = self.text()
        avail = self.contentsRect().width()
        if not text or avail <= 0:
            return base
        flags = int(Qt.TextWordWrap) | int(self.alignment())
        need = QFontMetrics(self.font()).boundingRect(
            QRect(0, 0, avail, 1 << 20), flags, text).height()
        margins = self.contentsMargins()
        need += margins.top() + margins.bottom()
        return QSize(base.width(), max(base.height(), need))

    def resizeEvent(self, event):     # ширина змінилась → інша кількість рядків
        super().resizeEvent(event)
        if self.wordWrap() and event.oldSize().width() != event.size().width():
            self.updateGeometry()


class FitScrollArea(QScrollArea):
    """Прокрутка, що просить рівно висоту вмісту, а стискається — до нуля.

    Стоковий QScrollArea обрізає власний sizeHint до 24 рядків тексту, тож у
    колонці сторінки вміст просив би менше, ніж йому треба. Тут sizeHint —
    повна висота вмісту (є місце → жодної смуги, панель як була), а мінімум
    лишається малим: коли місця немає, зайве ПРОКРУЧУЄТЬСЯ, а не тисне підписи
    нижче за рядок (той самий висновок, що й на сторінці Словників, vocab.py:
    сторінка вища за клієнтську область 1080p)."""

    def sizeHint(self) -> QSize:
        inner = self.widget()
        if inner is None:
            return super().sizeHint()
        frame = 2 * self.frameWidth()
        hint = inner.sizeHint()
        return QSize(hint.width() + frame, hint.height() + frame)


def classify_meeting_security(session_dir, status, *, materialized=False) -> str:
    """Return open | encrypted | open_view from durable state, never config."""
    session_dir = Path(session_dir)
    if materialized:
        return "open_view"
    if status in (_ST_RECORDING, _ST_STOPPED, _ST_PROCESSING):
        return "open"
    sealed = (session_dir / "meeting.json.enc").exists()
    pending = (session_dir / "encrypting.marker").exists()
    return "encrypted" if sealed and not pending else "open"


def next_utterance_ms(pos_ms: int, utterances: list) -> "int | None":
    """Старт першої репліки строго ПІСЛЯ поточної позиції (запас 50 мс), у мс.
    Немає такої — None."""
    for u in utterances:
        start_ms = float(u.start) * 1000
        if start_ms > pos_ms + 50:
            return int(start_ms)
    return None


def prev_utterance_ms(pos_ms: int, utterances: list) -> "int | None":
    """Старт першої репліки строго ПЕРЕД поточною позицією (запас 50 мс), у мс.
    Немає такої — None."""
    for u in reversed(utterances):
        start_ms = float(u.start) * 1000
        if start_ms < pos_ms - 50:
            return int(start_ms)
    return None


class DiarizationDownloadWorker(QThread):
    """Тягне й ставить моделі діаризації у фоні (діаризація живе на вкладці
    «Нарада»). Перенесено сюди з settings.py: реорг Налаштувань у вкладки
    (618f567) прибрав клас звідти, а лінивий імпорт наради лишався старим."""
    progress = Signal(object, object)
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, target, parent=None):
        super().__init__(parent)
        self._target = target

    def run(self):
        try:
            from whisper_core.meeting.diarization_models import download_and_install
            download_and_install(self._target, self.progress.emit)
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class IntegrityVerifyWorker(QThread):
    """Повна перевірка журналу цілісності (перехешування аудіо/транскрипту) у
    фоні — щоб навіть відкриття діалогу «Журнал цілісності» не морозило UI.
    Рендер картки її НЕ запускає: там лише дешевий read_chain_meta."""
    done = Signal(object)

    def __init__(self, controller, session_id, parent=None):
        super().__init__(parent)
        self._controller = controller
        self._session_id = session_id

    def run(self):
        from whisper_core.meeting import audit_log
        try:
            res = self._controller.meeting_integrity(self._session_id)
        except Exception:
            res = audit_log.ChainResult(status=audit_log.STATUS_ABSENT)
        self.done.emit(res)


class MeetingPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._ui_state = "idle"          # idle | recording | processing
        self._rec_started = 0.0
        self._show_sys = False           # чи писати другу смужку (онлайн-дзвінок)
        self._multi_levels = {}           # track -> (LevelMeter, row)

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        # --- шапка: заголовок + ГОЛОВНА дія (кнопка-тумблер запису) праворуч ---
        # Аудит Миколи 22.07: запис — головна дія екрана, тож кнопка помітна вгорі
        # з підписом («Почати запис» / «Зупинити»), а не губиться круглою іконкою.
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addLayout(page_header(tr("nav_meeting"), tr("meeting_subtitle")),
                       stretch=1)
        self._rec_btn = RecButton()
        self._rec_btn.setToolTip(tr("meeting_start"))
        self._rec_btn.setAccessibleName(tr("meeting_start"))
        self._rec_btn.clicked.connect(self._on_toggle)
        rec_box = QVBoxLayout()
        rec_box.setSpacing(4)
        rec_box.setContentsMargins(0, 0, 0, 0)
        rec_box.addWidget(self._rec_btn, alignment=Qt.AlignHCenter)
        self._rec_caption = QLabel(tr("meeting_start"))
        self._rec_caption.setProperty("level", "hint")
        self._rec_caption.setAlignment(Qt.AlignHCenter)
        rec_box.addWidget(self._rec_caption)
        head.addLayout(rec_box)
        root.addLayout(head)
        root.addSpacing(16)

        security_box = QVBoxLayout()
        security_box.setSpacing(8)
        self._config_corrupt_warning = QLabel()
        self._config_corrupt_warning.setProperty("badge", "error")
        self._config_corrupt_warning.setWordWrap(True)
        self._config_corrupt_warning.hide()
        security_box.addWidget(self._config_corrupt_warning)

        self._pending_plaintext_warning = QLabel()
        self._pending_plaintext_warning.setProperty("badge", "error")
        self._pending_plaintext_warning.setWordWrap(True)
        self._pending_plaintext_warning.hide()
        security_box.addWidget(self._pending_plaintext_warning)
        root.addLayout(security_box)
        self._refresh_config_warning()
        self._on_pending_plaintext(
            int(getattr(controller, "_meeting_plaintext_count", 0)))

        # Налаштування наради — ДРУГОРЯДНІ: сховані під розкривачкою, щоб не
        # перекривати головну кнопку запису (аудит Миколи 22.07). Увесь наявний
        # функціонал (діаризація, модель ШІ) лишається досяжним у розкритому стані.
        # Тумблер «Записувати й екран» прибрано раніше (зауваж. 3): запис екрана має
        # окремий пункт nav_screen. Ядро meeting_screen (cfg + контролер) лишається.
        root.addWidget(self._build_settings_disclosure())

        consent = QLabel(tr("meeting_consent"))
        consent.setProperty("muted", True)
        consent.setWordWrap(True)
        root.addSpacing(8)
        root.addWidget(consent)
        root.addSpacing(14)

        # --- жива панель запису (таймер, бейдж, смужки рівня); прихована в idle ---
        root.addWidget(self._build_live_panel())
        root.addSpacing(14)

        # --- стрічка карток минулих сесій ⇄ порожній стан ---
        self._feedbox = QVBoxLayout()
        self._feedbox.setSpacing(12)
        self._feedbox.addStretch()
        feedhost = QWidget()
        feedhost.setLayout(self._feedbox)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(feedhost)
        self._scroll = scroll            # feature/global-search: прокрутка до картки

        empty = EmptyState("fa6s.users", tr("meeting_empty_title"),
                           tr("meeting_empty_hint"))
        self._stack = QStackedWidget()
        self._stack.addWidget(empty)     # 0 — порожній стан
        self._stack.addWidget(scroll)    # 1 — стрічка
        root.addWidget(self._stack, stretch=1)

        self._cards = {}                 # session_id -> (frame, StatusTag)
        self._processing_widgets = {}    # session_id -> progress/actions
        self._players = []               # вбудовані InlinePlayer — стоп при ховані/оновленні

        # сигнали контролера (наради)
        controller.meeting_state.connect(self._on_state)
        controller.meeting_audio_ready.connect(self._on_audio_ready)
        controller.meeting_session_done.connect(self._on_session_done)
        controller.meeting_error.connect(self._on_error)
        controller.meeting_storage_warning.connect(self._on_storage_warning)
        pending_signal = getattr(controller, "meeting_plaintext_pending", None)
        if pending_signal is not None:
            pending_signal.connect(self._on_pending_plaintext)
        self._unlock_open = False
        vault_needed = getattr(controller, "meeting_vault_needed", None)
        if vault_needed is not None:
            vault_needed.connect(self._on_vault_needed)
        processing_progress = getattr(controller, "meeting_processing_progress", None)
        if processing_progress is not None:
            processing_progress.connect(self._on_processing_progress)
        processing_done = getattr(controller, "meeting_processing_done", None)
        if processing_done is not None:
            processing_done.connect(self._on_processing_done)
        audio_state = getattr(controller, "meeting_audio_state", None)
        if audio_state is not None:      # сумісність з ранніми controller-фейками
            audio_state.connect(self._on_audio_state)

    def _build_settings_disclosure(self) -> QWidget:
        """Розкривна секція «Налаштування наради» (аудит Миколи 22.07): у спокої
        згорнута — головна дія (кнопка запису) не тоне під панеллю. Клік по
        заголовку зі стрілкою розкриває/згортає весь наявний функціонал."""
        host = QWidget()
        v = QVBoxLayout(host)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        toggle = QToolButton()
        toggle.setText(tr("meeting_settings"))
        toggle.setCheckable(True)
        toggle.setChecked(False)
        toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        toggle.setArrowType(Qt.RightArrow)
        toggle.setCursor(Qt.PointingHandCursor)
        toggle.setAccessibleName(tr("meeting_settings"))
        panel = self._build_meeting_settings_panel()
        # Розкрита панель вища за вільне місце сторінки на 1080p (мінімум панелі
        # ~825px проти ~700 вільних): без прокрутки колонка тисне вміст НИЖЧЕ за
        # його мінімум і ріже підписи карток моделі ШІ по вертикалі. Тому панель
        # живе у FitScrollArea: є місце — видно цілком і смуги немає, немає —
        # зайве прокручується (той самий прийом, що на сторінці Словників).
        scroll = FitScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(panel)
        scroll.setVisible(False)
        toggle.toggled.connect(self._on_settings_toggled)
        v.addWidget(toggle, alignment=Qt.AlignLeft)
        v.addWidget(scroll)
        self._settings_toggle = toggle
        self._settings_panel = panel
        self._settings_scroll = scroll
        return host

    def _on_settings_toggled(self, opened: bool):
        self._settings_scroll.setVisible(opened)
        self._settings_toggle.setArrowType(Qt.DownArrow if opened else Qt.RightArrow)

    def _build_meeting_settings_panel(self):
        """Налаштування обробки; джерела запису мають одну точку в Settings.
        Канон DESIGN-SYSTEM §2.1: описові лейбли — у колонці 0 (ЄДИНА ліва вісь),
        контроли (поле, картки) — у колонці 1. Раніше зі злиття
        meeting-record-phase «Кількість співрозмовників» жила у сабрядку колонки
        1 і зсувалась праворуч, а вісь лейблів «з'їжджала». Чекбокс живої
        розшифровки перенесено на вкладку Диктування (аудит 22.07) — на Нараді
        він був чужим (стосується диктування, не наради)."""
        panel = QFrame(); panel.setProperty("glasspanel", True)
        grid = QGridLayout(panel); grid.setContentsMargins(16, 12, 16, 12)
        grid.setHorizontalSpacing(12); grid.setVerticalSpacing(12); grid.setColumnStretch(1, 1)
        sources_note = QLabel(tr("meeting_sources_in_settings")); sources_note.setProperty("muted", True); sources_note.setWordWrap(True); grid.addWidget(sources_note, 0, 0, 1, 2)
        # рядок 1 — запис екрана під час наради (виправлення 1: без цього чекбокса
        # meeting_screen_enabled не вмикав ЖОДЕН продакшн-UI, тож кнопка «Дивитися
        # відео» на картці наради ніколи не з'являлась для нового профілю). Лейбл на
        # осі (кол.0), чекбокс у колонці контролів (кол.1), пояснення — під ним.
        screen_label = QLabel(tr("meeting_screen_record_label")); screen_label.setProperty("formlabel", True); grid.addWidget(screen_label, 1, 0, Qt.AlignTop)
        self._screen_record = QCheckBox(tr("meeting_screen_record_enable"))
        self._screen_record.setChecked(bool(getattr(self.controller.cfg, "meeting_screen_enabled", False)))
        self._screen_record.setToolTip(tr("meeting_screen_record_hint"))
        self._screen_record.toggled.connect(self._on_screen_record_toggle)
        grid.addWidget(self._screen_record, 1, 1)
        screen_hint = QLabel(tr("meeting_screen_record_hint")); screen_hint.setProperty("muted", True); screen_hint.setWordWrap(True); grid.addWidget(screen_hint, 2, 1)
        # рядок 3 — розпізнавання мовців (лейбл на осі, чекбокс у колонці 1)
        diar_label = QLabel(tr("set_diarization_label")); diar_label.setProperty("formlabel", True); grid.addWidget(diar_label, 3, 0, Qt.AlignTop)
        self._diar_enabled = QCheckBox(tr("set_diarization_enable")); self._diar_enabled.setChecked(bool(getattr(self.controller.cfg, "diarization_enabled", False))); self._diar_enabled.toggled.connect(self._on_diarization_toggle)
        grid.addWidget(self._diar_enabled, 3, 1)
        # рядок 4 — кількість співрозмовників (лейбл ТЕЖ у колонці 0, на осі).
        # Показуємо лише коли розрізнення готове й увімкнене (Sol §6: «count only
        # if checked») — інакше рядок марно займає висоту у станах недоступно/качати.
        self._diar_count_label = QLabel(tr("set_diarization_count")); self._diar_count_label.setProperty("formlabel", True); grid.addWidget(self._diar_count_label, 4, 0)
        self._diar_count = QComboBox()
        self._diar_count.setAccessibleName(tr("set_diarization_count"))
        self._diar_count.addItem(tr("set_diarization_auto"), None)
        for k in range(2, 11):
            self._diar_count.addItem(str(k), k)
        val = getattr(self.controller.cfg, "diarization_num_speakers", None)
        idx = self._diar_count.findData(val)
        if idx < 0:
            idx = 0
        self._diar_count.setCurrentIndex(idx)
        self._diar_count.currentIndexChanged.connect(self._on_diarization_count)
        self._diar_count.ensurePolished()
        fm = self._diar_count.fontMetrics()
        auto_w = fm.horizontalAdvance(tr("set_diarization_auto"))
        ten_w = fm.horizontalAdvance("10")
        need_w = max(auto_w, ten_w)
        self._diar_count.setFixedWidth(need_w + 24 + 28 + 2 + 12)
        count_row = QHBoxLayout(); count_row.setSpacing(8); count_row.addWidget(self._diar_count); count_row.addStretch()
        grid.addLayout(count_row, 4, 1)
        # рядок 5 — завантаження моделі діаризації (дія у колонці контролів)
        self._diar_download = GlassButton(tr("set_diarization_download")); self._diar_download.clicked.connect(self._download_diarization)
        grid.addWidget(self._diar_download, 5, 1, Qt.AlignLeft)
        # рядок 6 — статус / важливе пояснення «у розробці» (роль body: аудит 22.07
        # — раніше hint/muted читалось надто блідо для важливого попередження)
        self._diar_status = QLabel(); self._diar_status.setProperty("level", "body"); self._diar_status.setWordWrap(True)
        grid.addWidget(self._diar_status, 6, 1)
        self._refresh_diarization_controls()

        # рядок 7 — пам'ять голосів (Т41): тумблер згоди та пояснення
        vmem_label = QLabel(tr("voice_memory_title")); vmem_label.setProperty("formlabel", True); grid.addWidget(vmem_label, 7, 0, Qt.AlignTop)
        self._voice_mem_enabled = QCheckBox(tr("voice_memory_enabled"))
        self._voice_mem_enabled.setChecked(bool(getattr(self.controller.cfg, "voice_memory_enabled", False)))
        self._voice_mem_enabled.setToolTip(tr("voice_memory_hint"))
        self._voice_mem_enabled.setAccessibleName(tr("voice_memory_enabled"))
        self._voice_mem_enabled.toggled.connect(self._on_voice_memory_toggle)
        grid.addWidget(self._voice_mem_enabled, 7, 1)
        vmem_hint = QLabel(tr("voice_memory_hint")); vmem_hint.setProperty("muted", True); vmem_hint.setWordWrap(True); grid.addWidget(vmem_hint, 8, 1)

        # рядок 9 — ШІ-протокол наради (Т73): майстер-тумблер. Вимкнено → Нарада
        # працює повноцінно, лише без кнопок протоколу/Q&A (жодних заглушок).
        proto_label = QLabel(tr("meeting_protocol_ai_title")); proto_label.setProperty("formlabel", True); grid.addWidget(proto_label, 9, 0, Qt.AlignTop)
        self._protocol_ai_enabled = QCheckBox(tr("meeting_protocol_ai_enable"))
        self._protocol_ai_enabled.setChecked(bool(getattr(self.controller.cfg, "protocol_ai_enabled", True)))
        self._protocol_ai_enabled.setToolTip(tr("meeting_protocol_ai_hint"))
        self._protocol_ai_enabled.setAccessibleName(tr("meeting_protocol_ai_enable"))
        self._protocol_ai_enabled.toggled.connect(self._on_protocol_ai_toggle)
        grid.addWidget(self._protocol_ai_enabled, 9, 1)
        proto_hint = QLabel(tr("meeting_protocol_ai_hint")); proto_hint.setProperty("muted", True); proto_hint.setWordWrap(True); grid.addWidget(proto_hint, 10, 1)

        self._build_protocol_model_row(grid, 11)
        return panel

    def _on_voice_memory_toggle(self, checked: bool):
        self.controller.cfg.voice_memory_enabled = bool(checked)
        self.controller.cfg.save()

    def _on_protocol_ai_toggle(self, checked: bool):
        self.controller.cfg.protocol_ai_enabled = bool(checked)
        self.controller.cfg.save()

    # --- feature/llm-model-picker: секція «Модель ШІ» ---
    def _build_protocol_model_row(self, grid, row):
        """Секція «Модель ШІ»: список моделей (пресети + власні) картками
        (назва, розмір, статус, кнопки завантажити/видалити/зробити активною),
        плюс додавання власної моделі — локальним файлом .gguf або за
        ідентифікатором репозиторію в інтернеті. Активна модель помічена."""
        box = QVBoxLayout(); box.setSpacing(8)
        hint = QLabel(tr("protocol_model_hint")); hint.setProperty("muted", True); hint.setWordWrap(True)
        box.addWidget(hint)
        self._model_list_box = QVBoxLayout(); self._model_list_box.setSpacing(8)
        box.addLayout(self._model_list_box)
        # Підпис над кнопками додавання (аудит 22.07): раніше «Додати свій файл» /
        # «Додати з інтернету» висіли без пояснення, що це — інша, власна модель.
        other_caption = QLabel(tr("protocol_model_other_label")); other_caption.setProperty("formlabel", True)
        box.addSpacing(4); box.addWidget(other_caption)
        addrow = QHBoxLayout(); addrow.setSpacing(12)
        add_local = GlassButton(tr("protocol_model_add_local"))
        add_local.setAccessibleName(tr("protocol_model_add_local"))
        add_local.setToolTip(tr("protocol_model_add_local_tip"))
        add_local.clicked.connect(self._add_local_model)
        add_hf = GlassButton(tr("protocol_model_add_hf"))
        add_hf.setAccessibleName(tr("protocol_model_add_hf"))
        add_hf.setToolTip(tr("protocol_model_add_hf_tip"))
        add_hf.clicked.connect(self._add_hf_model)
        addrow.addWidget(add_local); addrow.addWidget(add_hf); addrow.addStretch()
        box.addLayout(addrow)
        protocol_label = QLabel(tr("protocol_model_label")); protocol_label.setProperty("formlabel", True)
        grid.addWidget(protocol_label, row, 0, Qt.AlignTop)
        grid.addLayout(box, row, 1)
        self._refresh_model_list()

    def _active_model_id(self) -> str:
        from whisper_core.protocol import model_manager as mm
        return str(getattr(self.controller.cfg, "protocol_model", mm.DEFAULT_PRESET)
                   or mm.DEFAULT_PRESET)

    def _iter_models(self):
        """(ResolvedModel, CustomModel|None) для кожної моделі: спершу пресети,
        потім власні. CustomModel=None → це пресет."""
        from whisper_core.protocol import model_manager as mm
        from whisper_core import paths, config as cfgmod
        root = paths.protocol_models_dir()
        custom = cfgmod.protocol_custom_models(self.controller.cfg)
        by_id = {c.id: c for c in custom}
        for mid in list(mm.PRESETS.keys()) + [c.id for c in custom]:
            resolved = mm.resolve(mid, root, custom)
            if resolved is not None:
                yield resolved, by_id.get(mid)

    def _refresh_model_list(self):
        while self._model_list_box.count():
            item = self._model_list_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        active = self._active_model_id()
        for resolved, custom in self._iter_models():
            self._model_list_box.addWidget(self._build_model_card(resolved, custom, active))

    def _build_model_card(self, resolved, custom, active_id):
        from .protocol_ui import _human_size
        label = tr(resolved.label_key) if resolved.label_key else resolved.label
        frame = QFrame(); frame.setProperty("glasspanel", True)
        # низ 16 (не 12): рамкована danger-кнопка в нижньому рядку інакше
        # візуально торкалась межі картки (знахідка суду 22.07)
        lay = QVBoxLayout(frame); lay.setContentsMargins(16, 12, 16, 16); lay.setSpacing(8)
        top = QHBoxLayout(); top.setSpacing(12)
        # Підписи картки — WrapLabel: назва моделі й пояснення переносяться, і
        # мінімальна висота рахується від fontMetrics під РЕАЛЬНУ ширину картки,
        # тож тісна колонка не ріже їх знизу.
        name = WrapLabel(label); name.setProperty("strong", True); name.setWordWrap(True)
        top.addWidget(name, stretch=1)
        is_active = (resolved.id == active_id)
        if is_active:
            top.addWidget(StatusTag("done", tr("protocol_model_active")))
        lay.addLayout(top)
        ready = resolved.available()
        parts = []
        if resolved.approx_size_bytes:
            parts.append(_human_size(resolved.approx_size_bytes))
        parts.append(tr("protocol_model_ready") if ready else tr("protocol_model_missing"))
        info = WrapLabel(" · ".join(parts)); info.setProperty("muted", True); info.setWordWrap(True)
        lay.addWidget(info)
        if resolved.hint_key:
            phint = WrapLabel(tr(resolved.hint_key)); phint.setProperty("muted", True); phint.setWordWrap(True)
            lay.addWidget(phint)
        # клікабельний «Про модель ↗» — сторінка моделі в інтернеті (пресет → HF-
        # сторінка з ліцензією; власна HF-модель → її репозиторій; локальний файл —
        # без лінка). Позначено як вихід у браузер (офлайн-аудиторія).
        from whisper_core.protocol import model_manager as mm
        page_url = ""
        if custom is None:
            preset = mm.PRESETS.get(resolved.id)
            if preset is not None:
                page_url = preset.page_url
        elif custom.kind == mm.CUSTOM_KIND_HF and custom.repo_id:
            page_url = "https://huggingface.co/{}".format(custom.repo_id)
        if page_url:
            def _about_text(u=page_url):
                # inline-колір (не QPalette.Link — жива зміна теми його не перечитує);
                # restyle-хук перебудовує текст під активний акцент теми
                return '<a href="{url}" style="color:{col};">{text}</a>'.format(
                    url=u, col=theme.GOLD, text=tr("protocol_model_about"))
            about = QLabel(_about_text())
            about.setOpenExternalLinks(True)   # лише https, системний браузер
            theme.register_restyle_call(about, lambda w: w.setText(_about_text()))
            about.setToolTip(tr("net_link_hint"))
            about.setAccessibleName(tr("protocol_model_about_name", label=label))
            about.setWordWrap(True)
            lay.addWidget(about)
        act = QHBoxLayout(); act.setSpacing(12)
        # Порядок і видимість дій (аудит Миколи 22.07):
        #  • поки модель НЕ завантажена — видно ЛИШЕ «Завантажити модель» (акцентна);
        #  • «Зробити активною» з'являється ПІСЛЯ завантаження (раніше обидві були
        #    рівноцінні й у нелогічному порядку — дубль стану на активній прибрано
        #    ще в 1.2.1: бейдж «Активна» вгорі картки);
        #  • «Завантажити заново» — ручне оновлення вже скачаної моделі (без API
        #    версій: чесний ручний шлях, коли автор перезалив ваги);
        #  • «Видалити модель» — вторинна danger-кнопка з підтвердженням (не голий
        #    ghost-текст без рамки).
        if resolved.downloadable and not ready:
            dl = QPushButton(tr("protocol_model_download")); dl.setProperty("accent", True)
            dl.setAccessibleName(f"{tr('protocol_model_download')}: {label}")
            dl.clicked.connect(lambda _=False, r=resolved, c=custom: self._download_model(r, c))
            act.addWidget(dl)
        if ready and not is_active:
            make = QPushButton(tr("protocol_model_make_active"))
            make.setAccessibleName(f"{tr('protocol_model_make_active')}: {label}")
            make.setToolTip(tr("protocol_model_make_active_tip"))
            make.clicked.connect(lambda _=False, mid=resolved.id: self._set_active_model(mid))
            act.addWidget(make)
        if ready and resolved.downloadable:      # пресет/HF: оновити файл вручну
            redl = QPushButton(tr("protocol_model_redownload"))
            redl.setAccessibleName(f"{tr('protocol_model_redownload')}: {label}")
            redl.setToolTip(tr("protocol_model_redownload_tip"))
            redl.clicked.connect(lambda _=False, r=resolved, c=custom, lb=label: self._redownload_model(r, c, lb))
            act.addWidget(redl)
        if custom is None:                       # пресет: видалення звільняє ГБ
            if ready:
                delete = QPushButton(tr("protocol_model_delete")); delete.setProperty("danger", True)
                delete.setAccessibleName(f"{tr('protocol_model_delete')}: {label}")
                delete.clicked.connect(lambda _=False, r=resolved, lb=label: self._delete_downloaded(r, lb))
                act.addWidget(delete)
        else:                                    # власна: прибрати зі списку (+опц. файл)
            remove = QPushButton(tr("protocol_model_remove")); remove.setProperty("ghost", True)
            remove.setAccessibleName(f"{tr('protocol_model_remove')}: {label}")
            remove.clicked.connect(lambda _=False, c=custom, r=resolved: self._remove_custom(c, r))
            act.addWidget(remove)
        act.addStretch()
        lay.addLayout(act)
        return frame

    def _set_active_model(self, mid):
        self.controller.cfg.protocol_model = mid
        self.controller.save_config()
        self._refresh_model_list()

    def _download_model(self, resolved, custom):
        from whisper_core.protocol import model_manager as mm
        from .protocol_ui import _human_size
        if custom is not None:                   # власна інтернет-модель
            url = mm.custom_hf_url(custom)
            if not _meeting_confirm(
                    self, tr("protocol_model_consent_title"),
                    tr("protocol_model_hf_consent", repo=custom.repo_id, url=url)):
                return
        else:                                    # пресет
            preset = mm.PRESETS[resolved.id]
            size = _human_size(resolved.approx_size_bytes)
            # згода з назвою ліцензії моделі й клікабельним посиланням на її сторінку
            if not _meeting_confirm(
                    self, tr("protocol_model_consent_title"),
                    tr("protocol_model_consent_body", size=size) + "<br><br>"
                    + tr("dl_consent_license", license=preset.license_name,
                         url=preset.page_url),
                    rich=True):
                return
        self._open_download_dialog(resolved, custom, force=False)

    def _open_download_dialog(self, resolved, custom, *, force=False):
        """Модальна докачка (stage → атомарна підміна), спільна для першого
        завантаження й «Завантажити заново» (force=True). Consent/підтвердження
        лишаються у викликачів — тут лише сам процес завантаження й оновлення
        списку моделей після нього."""
        from .protocol_ui import ProtocolModelDownloadDialog
        target = resolved.model_path.parent
        if custom is not None:
            dlg = ProtocolModelDownloadDialog(target, custom=custom, force=force,
                                              parent=self)
        else:
            dlg = ProtocolModelDownloadDialog(target, preset_id=resolved.id,
                                              force=force, parent=self)
        dlg.exec()
        self._refresh_model_list()

    def _delete_downloaded(self, resolved, label=""):
        # Деструктив (аудит 22.07 + канон DESIGN-SYSTEM §1.5): видалення завантаженого
        # файлу вимагає підтвердження — раніше стирало ГБ без запиту.
        from whisper_core.protocol import model_manager as mm
        if not _meeting_confirm(
                self, tr("protocol_model_delete"),
                tr("protocol_model_delete_confirm", label=label or resolved.id)):
            return
        mm.delete_model(resolved.model_path.parent)
        self._refresh_model_list()

    def _redownload_model(self, resolved, custom, label=""):
        """Виправлення 2: ручне оновлення вже завантаженої моделі — коли автор
        перезалив ваги (напр. Gemma 4 GGUF із оновленим chat template). Без API
        версій. STAGED-заміна: качаємо свіжий файл у stage й атомарно підмінюємо
        лише ПІСЛЯ успішної звірки — стара модель (5–7 ГБ) ЖИВА увесь час докачки;
        скасування чи збій лишають її на місці (раніше файл стирався ДО докачки,
        і скасування другого діалогу лишало користувача взагалі без моделі). Одне
        підтвердження на початку; другий consent зайвий — користувач уже погодився
        при першому завантаженні цієї ж моделі."""
        if not _meeting_confirm(
                self, tr("protocol_model_redownload"),
                tr("protocol_model_redownload_confirm", label=label or resolved.id)):
            return
        self._open_download_dialog(resolved, custom, force=True)

    def _add_local_model(self):
        import os
        from whisper_core.protocol import model_manager as mm
        path, _ = QFileDialog.getOpenFileName(
            self, tr("protocol_model_add_local"), "", tr("protocol_model_gguf_filter"))
        if not path:
            return
        if not mm.is_gguf_name(path) or not os.path.isfile(path):
            _meeting_warn(self, tr("protocol_model_add_local"),
                          tr("protocol_model_local_invalid"))
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        default_name = os.path.basename(path)
        name, ok = QInputDialog.getText(
            self, tr("protocol_model_add_local"), tr("protocol_model_name_label"),
            text=default_name)
        if not ok:
            return
        cm = mm.CustomModel(id=mm.new_custom_id(), label=(name.strip() or default_name),
                            kind=mm.CUSTOM_KIND_LOCAL, path=path, approx_size_bytes=size)
        self._append_custom_model(cm)

    def _add_hf_model(self):
        from whisper_core.protocol import model_manager as mm
        from .protocol_ui import HFModelDialog
        dlg = HFModelDialog(self)
        if dlg.exec() != QDialog.Accepted or not dlg.result_data:
            return
        repo, fname = dlg.result_data
        cm = mm.CustomModel(id=mm.new_custom_id(), label=f"{repo} · {fname}",
                            kind=mm.CUSTOM_KIND_HF, repo_id=repo, filename=fname)
        self._append_custom_model(cm)

    def _append_custom_model(self, cm):
        models = list(getattr(self.controller.cfg, "custom_models", []) or [])
        models.append(cm.to_json())
        self.controller.cfg.custom_models = models
        self.controller.save_config()
        self._refresh_model_list()

    def _remove_custom(self, cm, resolved):
        import os
        import shutil
        from whisper_core.protocol import model_manager as mm
        if not _meeting_confirm(
                self, tr("protocol_model_remove"),
                tr("protocol_model_remove_confirm", name=cm.label)):
            return
        target_dir = resolved.model_path.parent if cm.kind == mm.CUSTOM_KIND_HF else None
        also_file = False
        if cm.kind == mm.CUSTOM_KIND_HF and target_dir is not None and target_dir.exists():
            also_file = _meeting_confirm(
                self, tr("protocol_model_remove"),
                tr("protocol_model_remove_file"))
        elif cm.kind == mm.CUSTOM_KIND_LOCAL and os.path.isfile(cm.path):
            also_file = _meeting_confirm(
                self, tr("protocol_model_remove"),
                tr("protocol_model_remove_local_file"))
        # прибрати запис зі списку (за id), зберегти інші як є
        kept = []
        for raw in getattr(self.controller.cfg, "custom_models", []) or []:
            parsed = mm.CustomModel.from_json(raw)
            if parsed is not None and parsed.id == cm.id:
                continue
            kept.append(raw)
        self.controller.cfg.custom_models = kept
        # активна вказувала на цю модель → чесно повертаємо дефолтний пресет
        if self._active_model_id() == cm.id:
            self.controller.cfg.protocol_model = mm.DEFAULT_PRESET
        self.controller.save_config()
        if also_file:
            try:
                if cm.kind == mm.CUSTOM_KIND_HF and target_dir is not None:
                    shutil.rmtree(target_dir, ignore_errors=True)
                elif cm.kind == mm.CUSTOM_KIND_LOCAL:
                    os.remove(cm.path)
            except OSError:
                pass
        self._refresh_model_list()

    def _create_protocol(self, session_id):
        """Відкрити діалог генерації протоколу для завершеної наради."""
        from .protocol_ui import ProtocolDialog
        from whisper_core.protocol import service
        # Проактивна перевірка: без бекенда LLM генерація дала б заглушку —
        # кажемо це одразу, а не через кілька хвилин «генерації».
        if not service.backend_available():
            _meeting_warn(self, tr("protocol_dialog_title"),
                          tr("protocol_backend_missing"))
            return
        utterances = self.controller.read_meeting_utterances(session_id)
        if not utterances:
            _meeting_info(self, tr("protocol_dialog_title"), tr("protocol_empty"))
            return
        meta = None
        try:
            from whisper_core.meeting import session as msession
            meta = msession.load_meta(self.controller.meeting_session_dir(session_id))
        except Exception:
            meta = None
        labels = {
            "me_label": tr("meeting_speaker_me"),
            "others_label": tr("meeting_speaker_others"),
            "speaker_names": (meta.speaker_names if meta is not None else None) or None,
        }
        from whisper_core import config as cfgmod
        preset_id = getattr(self.controller.cfg, "protocol_model", "fast")
        ProtocolDialog(self.controller.meeting_session_dir(session_id),
                       utterances, preset_id, labels, self,
                       custom_models=cfgmod.protocol_custom_models(self.controller.cfg)).exec()

    def _ask_meeting(self, session_id, player=None):
        """Відкрити Q&A-чат по завершеній нараді. Цитати у відповіді клікабельні —
        стрибають плеєр картки на таймкод (як Smart Chapters)."""
        from .protocol_ui import QADialog
        from whisper_core.protocol import service
        # Проактивна перевірка (та сама, що в протоколі): без бекенда LLM
        # відповідь була б заглушкою — кажемо одразу, а не через хвилини «пошуку».
        if not service.backend_available():
            _meeting_warn(self, tr("qa_dialog_title"),
                          tr("protocol_backend_missing"))
            return
        utterances = self.controller.read_meeting_utterances(session_id)
        if not utterances:
            _meeting_info(self, tr("qa_dialog_title"), tr("qa_empty"))
            return
        meta = None
        try:
            from whisper_core.meeting import session as msession
            meta = msession.load_meta(self.controller.meeting_session_dir(session_id))
        except Exception:
            meta = None
        labels = {
            "me_label": tr("meeting_speaker_me"),
            "others_label": tr("meeting_speaker_others"),
            "speaker_names": (meta.speaker_names if meta is not None else None) or None,
        }
        from whisper_core import config as cfgmod
        preset_id = getattr(self.controller.cfg, "protocol_model", "fast")
        seek = player.play_from if player is not None else None
        QADialog(utterances, preset_id, labels, seek=seek, parent=self,
                 custom_models=cfgmod.protocol_custom_models(self.controller.cfg)).exec()

    def _diar_models_ready(self):
        # Дешева проба (розмір без SHA) — на кожен клік чекбокса, не морозить UI.
        from whisper_core.meeting.diarize import models_present_fast
        return models_present_fast(getattr(self.controller.cfg, "diarization_model_dir", None))
    def _refresh_diarization_controls(self):
        # Slice 3: три чесні стани контролів (дизайн Sol §6).
        #  • пакет sherpa відсутній → фіча недоступна в цій збірці;
        #  • пакет є, моделей нема → пропозиція завантажити ~34,3 МБ;
        #  • моделі перевірені → чекбокс активний, поле кількості лише якщо ввімкнено.
        from whisper_core.meeting.diarize import runtime_available

        def show_count(visible):
            self._diar_count_label.setVisible(visible)
            self._diar_count.setVisible(visible)

        if not runtime_available():
            self._diar_enabled.setEnabled(False)
            self._diar_enabled.setChecked(False)
            show_count(False)
            self._diar_download.setVisible(False)
            self._diar_status.setText(tr("set_diarization_unavailable"))
            return
        if not self._diar_models_ready():
            self._diar_enabled.setEnabled(False)
            show_count(False)
            self._diar_download.setVisible(True)
            self._diar_download.setEnabled(True)
            self._diar_status.setText(tr("set_diarization_missing"))
            return
        self._diar_enabled.setEnabled(True)
        self._diar_download.setVisible(False)
        # Поле кількості лише коли розрізнення ввімкнене (2..10 / авто).
        checked = self._diar_enabled.isChecked()
        show_count(checked)
        self._diar_count.setEnabled(checked)
        val = getattr(self.controller.cfg, "diarization_num_speakers", None)
        idx = self._diar_count.findData(val)
        if idx < 0:
            idx = 0
        if self._diar_count.currentIndex() != idx:
            self._diar_count.blockSignals(True)
            self._diar_count.setCurrentIndex(idx)
            self._diar_count.blockSignals(False)
        self._diar_status.setText(tr("set_diarization_ready"))
    def _on_screen_record_toggle(self, on):
        # Виправлення 1: вмикає запис екрана під час наради (screen.mp4 поряд із
        # аудіо). Контролер зберігає прапорець; meeting_start його читає.
        self.controller.set_meeting_screen_enabled(bool(on))
    def _on_diarization_toggle(self, on):
        self.controller.cfg.diarization_enabled=bool(on); self.controller.save_config(); self._refresh_diarization_controls()
    def _on_diarization_count(self, index=None):
        val = self._diar_count.currentData()
        if val is not None:
            try:
                val = int(val)
                if not (2 <= val <= 10):
                    val = None
            except (ValueError, TypeError):
                val = None
        self.controller.cfg.diarization_num_speakers = val
        self.controller.save_config()
    def _download_diarization(self):
        from ..dl_consent import confirm_download
        if not confirm_download(
                self.window(), name=tr("dl_name_diarization"), size_mb=34,
                license_links=[
                    ("pyannote segmentation — MIT",
                     "https://huggingface.co/csukuangfj/sherpa-onnx-pyannote-segmentation-3-0"),
                    ("3D-Speaker CampPlus — Apache-2.0",
                     "https://huggingface.co/csukuangfj/speaker-embedding-models")]):
            return
        from whisper_core import paths
        self._diar_download.setEnabled(False); self._diar_status.setText(tr("set_diarization_downloading")); self._diar_worker=DiarizationDownloadWorker(getattr(self.controller.cfg,"diarization_model_dir",None) or paths.diarization_models_dir(),self); self._diar_worker.finished_ok.connect(lambda: (self._diar_download.setEnabled(True),self._refresh_diarization_controls())); self._diar_worker.failed.connect(lambda error: (self._diar_download.setEnabled(True),self._diar_status.setText(self._humanize_diar_error(error)))); self._diar_worker.start()

    @staticmethod
    def _humanize_diar_error(error) -> str:
        """Технічний виняток завантаження → людське речення без дублювання.
        Мережеві/HTTP-помилки (404, timeout, DNS, SSL) читаються як «сервер
        недоступний», а не «Не вдалося… HTTP Error 404: Not Found» (аудит 1.2.1)."""
        low = str(error).lower()
        net = ("http error", "404", "403", "500", "502", "503", "timed out",
               "timeout", "connection", "urlopen", "getaddrinfo", "ssl",
               "network", "temporarily", "name or service", "resolve")
        if any(m in low for m in net):
            return tr("set_diarization_failed_network")
        return tr("set_diarization_failed", error=error)
    # ------------------------------------------------------------------ live
    def _build_live_panel(self) -> QWidget:
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 14, 18, 16)
        lay.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(10)
        self._live_badge = StatusTag("busy", tr("meeting_badge_recording"))
        top.addWidget(self._live_badge)
        self._live_security_badge = StatusTag("warn", tr("meeting_security_open"))
        self._live_security_badge.setToolTip(tr("meeting_security_open_tip"))
        top.addWidget(self._live_security_badge)
        top.addStretch()
        self._timer_lbl = QLabel("00:00")
        self._timer_lbl.setProperty("kbd", True)
        top.addWidget(self._timer_lbl)
        lay.addLayout(top)

        self._storage_warning = QLabel()
        self._storage_warning.setProperty("badge", "error")
        self._storage_warning.setWordWrap(True)
        self._storage_warning.hide()
        lay.addWidget(self._storage_warning)

        # смужка мікрофона (завжди) + смужка системного звуку (лише «Онлайн-дзвінок»)
        self._mic_level = LevelMeter(
            provider=lambda: self.controller.meeting_mic_level())
        self._mic_row = self._level_row(tr("meeting_level_mic"), self._mic_level)
        lay.addWidget(self._mic_row)
        self._sys_level = LevelMeter(
            provider=lambda: self.controller.meeting_sys_level())
        self._sys_row = self._level_row(tr("meeting_level_sys"), self._sys_level)
        lay.addWidget(self._sys_row)
        self._multimic_level_box = QVBoxLayout()
        self._multimic_level_box.setSpacing(8)
        lay.addLayout(self._multimic_level_box)

        self._audio_note = QLabel()
        self._audio_note.setProperty("muted", True)
        self._audio_note.setWordWrap(True)
        self._audio_note.hide()
        lay.addWidget(self._audio_note)

        # feature/live-transcription: append-only live feed; свідомо лише
        # мікрофонна доріжка «Я».
        self._live_segments = QVBoxLayout()
        self._live_segments.setSpacing(4)
        self._live_partial = QLabel()
        self._live_partial.setProperty("muted", True)
        self._live_partial.setWordWrap(True)
        lay.addLayout(self._live_segments)
        lay.addWidget(self._live_partial)

        # диктофон-режим: «Відкрити аудіо» вмикається щойно зібрано WAV (до розшифровки)
        actions = QHBoxLayout()
        actions.setSpacing(10)
        self._live_open_audio = GlassButton(tr("meeting_open_audio"))
        self._live_open_audio.hide()
        self._open_audio_slot = None   # підключений хендлер (щоб disconnect був точковий)
        self._bookmark_btn = GlassButton(tr("meeting_bookmark"))
        self._bookmark_btn.clicked.connect(self._add_bookmark)
        self._cancel_btn = GlassButton(tr("meeting_cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel)
        actions.addWidget(self._live_open_audio)
        actions.addWidget(self._bookmark_btn)
        actions.addWidget(self._cancel_btn)
        actions.addStretch()
        self._proc_note = QLabel(tr("meeting_processing_note"))
        self._proc_note.setProperty("muted", True)
        self._proc_note.setWordWrap(True)
        self._proc_note.hide()
        lay.addLayout(actions)
        lay.addWidget(self._proc_note)

        from PySide6.QtCore import QTimer
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_timer)

        hl.addWidget(card)
        host.hide()
        self._live_host = host
        return host

    @staticmethod
    def _level_row(caption: str, meter: LevelMeter) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        lbl = QLabel(caption)
        lbl.setProperty("formlabel", True)
        # Ширина колонки підписів — під найдовший підпис рівнів (не фікс 120px,
        # що різав «Системний звук»/«Microphone 88»). Рахуємо однаково для всіх
        # рядків → метри вирівняні; шрифт 14px як у QSS[formlabel].
        f = QFont(lbl.font())
        f.setPixelSize(14)
        fm = QFontMetrics(f)
        col = max(fm.horizontalAdvance(tr("meeting_level_mic")),
                  fm.horizontalAdvance(tr("meeting_level_sys")),
                  fm.horizontalAdvance(tr("meeting_microphone_number", number="88"))) + 16
        lbl.setFixedWidth(col)
        row.addWidget(lbl)
        row.addWidget(meter, stretch=1)
        return w

    def add_live_segment(self, segment):
        """GUI-slot: live-нарада append-only, partial лишається окремим хвостом."""
        if not getattr(self.controller.cfg, "live_transcription", False):
            return
        stamp = f"{int(segment.start) // 60:02d}:{int(segment.start) % 60:02d}"
        if segment.is_final:
            row = QLabel(f"{stamp} · {tr('meeting_speaker_me')}: {segment.text}")
            row.setWordWrap(True)
            self._live_segments.addWidget(row)
            self._live_partial.clear()
        else:
            self._live_partial.setText(f"{stamp} · {tr('meeting_speaker_me')}: {segment.text}")
        self._live_host.show()
    def _update_timer(self):
        elapsed = int(time.time() - self._rec_started)
        self._timer_lbl.setText(f"{elapsed // 60:02d}:{elapsed % 60:02d}")

    def _sync_multimic_levels(self, recording: bool):
        """Створити/показати N LevelMeter лише для активного мультимік-пресету."""
        try:
            tracks = list(getattr(self.controller, "_meeting_streams", {})) if recording else []
            wanted = [track for track in tracks
                      if track.startswith("mic") and track != "mic"]
        except Exception:
            wanted = []
        for track in list(self._multi_levels):
            meter, row = self._multi_levels[track]
            show = track in wanted
            row.setVisible(show)
            meter.set_active(recording and show)
        for track in wanted:
            if track in self._multi_levels:
                continue
            meter = LevelMeter(provider=lambda t=track: self.controller.meeting_track_level(t))
            row = self._level_row(tr("meeting_microphone_number", number=track[3:]), meter)
            self._multimic_level_box.addWidget(row)
            self._multi_levels[track] = (meter, row)
            meter.set_active(recording)

    # ---------------------------------------------------------------- sources
    def _start_preset(self) -> str:
        """Legacy-мітка сесії, виведена з актуального канонічного вибору."""
        try:
            from whisper_core.config import meeting_preset_for_cfg
            return meeting_preset_for_cfg(self.controller.cfg)
        except Exception:
            return "onlymic"

    # ------------------------------------------------------------------ toggle
    def _on_toggle(self):
        if self._ui_state == "recording":
            self.controller.meeting_stop()
        elif self._ui_state == "idle":
            self.controller.meeting_start(self._start_preset())
            # реальний перехід у «recording» зробить сигнал meeting_state

    def _add_bookmark(self):
        title, accepted = QInputDialog.getText(self, tr("meeting_bookmark"), tr("meeting_bookmark_title"))
        if accepted:
            self.controller.add_meeting_bookmark(title)

    def _on_cancel(self):
        # Симетрично з видаленням завершеної наради (_confirm_delete): живий запис
        # теж не стираємо без явного «Так». Кнопка стоїть поряд із «Мітка», тож
        # один хибний клік інакше знищив би всю нараду безповоротно (Б3).
        if not _meeting_confirm(
                self, tr("meeting_cancel"), tr("meeting_cancel_confirm")):
            return
        self.controller.meeting_cancel()

    # ------------------------------------------------------------- state slots
    def _on_state(self, state: str):
        """recording | processing | idle → жива панель, тумблер, смужки, таймер."""
        self._ui_state = (
            state if state in ("recording", "processing", "postprocessing")
            else "idle")
        recording = state == "recording"
        processing = state in ("processing", "diarizing")
        postprocessing = state == "postprocessing"
        self._show_sys = "sys" in getattr(self.controller, "_meeting_streams", {})
        self._sync_multimic_levels(recording)

        self._live_host.setVisible(recording or processing)
        self._mic_row.setVisible("mic" in getattr(self.controller, "_meeting_streams", {}))
        self._sys_row.setVisible(self._show_sys)
        self._mic_level.set_active(recording)
        self._sys_level.set_active(recording and self._show_sys)
        self._rec_btn.set_recording(recording)
        # підпис під кнопкою відображає дію (аудит 22.07: кнопка запису — помітна)
        self._rec_caption.setText(tr("meeting_stop") if recording else tr("meeting_start"))
        # у processing тумблер вимкнено (не можна стартувати новий, поки йде обробка)
        self._rec_btn.setEnabled(not (processing or postprocessing))
        self._rec_caption.setEnabled(not (processing or postprocessing))
        # Sol-ревізія №2: чекбокс запису екрана діє лише з НАСТУПНОЇ наради, тож
        # поки сесія активна (запис/обробка) — вимикаємо його з поясненням, а
        # після завершення вертаємо (доступний + звичайна підказка).
        busy = recording or processing or postprocessing
        self._screen_record.setEnabled(not busy)
        self._screen_record.setToolTip(
            tr("meeting_screen_record_locked") if busy
            else tr("meeting_screen_record_hint"))
        # кнопки дій — лише поки пишемо
        self._cancel_btn.setVisible(recording)
        self._bookmark_btn.setVisible(recording)
        self._proc_note.setVisible(processing)

        if recording:
            self._storage_warning.hide()
            self._rec_started = time.time()
            self._update_timer()
            self._tick.start()
            self._live_badge.set_state("busy", tr("meeting_badge_recording"))
            self._live_security_badge.set_state(
                "warn", tr("meeting_security_open"))
            self._live_open_audio.hide()
            tip = tr("meeting_stop")
        else:
            self._tick.stop()
            tip = tr("meeting_start")
        if processing:
            self._live_badge.set_state("busy", tr("meeting_badge_preparing_audio"))
        self._rec_btn.setToolTip(tip)
        self._rec_btn.setAccessibleName(tip)

    def _on_audio_ready(self, session_id: str):
        """WAV зібрано (до розшифровки) → диктофон-режим: «Відкрити аудіо».
        disconnect — точковий (лише свій попередній хендлер), а не сліпий
        disconnect() усього сигналу: той на непідключеній кнопці сипав
        RuntimeWarning «failed to disconnect»."""
        if self._open_audio_slot is not None:
            self._live_open_audio.clicked.disconnect(self._open_audio_slot)
        self._open_audio_slot = (
            lambda _=False, sid=session_id: self.controller.open_meeting_audio(sid))
        self._live_open_audio.clicked.connect(self._open_audio_slot)
        self._live_open_audio.show()

    def _on_audio_state(self, _track: str, state: str):
        """Нефатальний статус просто на live-картці; глобальний toast робить
        controller. Після успішного reconnect прибираємо підтвердження самі."""
        if state == "failed":
            # С1: одна доріжка відпала — решта пишуть далі; якщо це остання,
            # картку сховає _on_session_done. Нотатку тримаємо видимою.
            self._audio_note.setText(tr("meeting_track_dropped_continue", track=_track))
            self._audio_note.show()
            return
        key = {"reconnecting": "meeting_mic_reconnecting",
               "reconnected": "meeting_mic_reconnected"}.get(state)
        if key is None:
            return
        self._audio_note.setText(tr(key))
        self._audio_note.show()
        if state == "reconnected":
            from PySide6.QtCore import QTimer
            QTimer.singleShot(3500, self._audio_note.hide)

    def _on_session_done(self, session_id: str, meta):
        """Сесія готова: сховати живу панель, перебудувати стрічку з диска."""
        self._live_host.hide()
        self._tick.stop()
        self._ui_state = "idle"
        self.refresh()

    def _on_storage_warning(self, session_id: str, elapsed: float):
        """ENOSPC: помітний банер лишається на live-картці, запис не зупиняємо."""
        minute = max(1, int(float(elapsed) // 60) + 1)
        self._storage_warning.setText(tr("meeting_storage_full", minute=minute))
        self._storage_warning.show()

    def _on_pending_plaintext(self, count: int):
        count = max(0, int(count))
        if not count:
            self._pending_plaintext_warning.hide()
            return
        text = tr("meeting_pending_plaintext", count=count)
        self._pending_plaintext_warning.setText(text)
        self._pending_plaintext_warning.setAccessibleName(text)
        self._pending_plaintext_warning.show()

    def _refresh_config_warning(self):
        cfg = self.controller.cfg
        if not bool(getattr(cfg, "_config_corrupt", False)):
            self._config_corrupt_warning.hide()
            return
        key = ("config_corrupt_recovered"
               if bool(getattr(cfg, "_config_recovered_from_backup", False))
               else "config_corrupt_safe")
        text = tr(key)
        self._config_corrupt_warning.setText(text)
        self._config_corrupt_warning.setAccessibleName(text)
        self._config_corrupt_warning.show()

    def _on_error(self, session_id: str, message: str):
        self._live_host.hide()
        self._tick.stop()
        self._ui_state = "idle"
        self.refresh()

    def _on_vault_needed(self):
        """Зашифроване сховище закрите паролем: попросити пароль модально (3
        спроби, далі відмова без підказок), після успіху — перебудувати стрічку.
        Прапорець не дає накластися кільком діалогам (list_meetings/картки можуть
        емітити сигнал кілька разів поспіль)."""
        if self._unlock_open:
            return
        from .vault_dialogs import (VaultKeyfileUnlockDialog, VaultUnlockDialog)
        try:
            state = self.controller.meeting_vault_state()
        except Exception:
            state = "locked"
        if state == "keyfile_locked":
            dialog = VaultKeyfileUnlockDialog(self.controller, False, self)
        elif state == "twofactor_locked":
            dialog = VaultKeyfileUnlockDialog(self.controller, True, self)
        else:
            dialog = VaultUnlockDialog(self.controller, self)
        self._unlock_open = True
        try:
            accepted = dialog.exec() == QDialog.Accepted
        finally:
            self._unlock_open = False
        if accepted:
            self.refresh()

    # -------------------------------------------------------------- feed cards
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_config_warning()
        self.refresh()

    def hideEvent(self, event):
        """Сторінку сховали (перемкнули вкладку/закрили вікно) — спинити плеєри."""
        self._stop_players()
        super().hideEvent(event)

    def _stop_players(self):
        for pl in self._players:
            try:
                pl.stop()
            except RuntimeError:
                pass                     # C++-обʼєкт уже знищено
        self._players = []
        clear_plain = getattr(self.controller, "_clear_meeting_plain_cache", None)
        if callable(clear_plain):
            clear_plain()

    def refresh(self):
        """Перебудувати стрічку карток із диска (list_meetings уже позначає
        осиротілі сесії як «перервано»)."""
        self._stop_players()
        while self._feedbox.count() > 1:
            item = self._feedbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards = {}
        self._processing_widgets = {}

        try:
            metas = self.controller.list_meetings()
        except Exception:
            metas = []
        if not metas:
            self._stack.setCurrentIndex(0)
            return
        self._stack.setCurrentIndex(1)
        for meta in metas:
            self._add_card(meta)

    def focus_session(self, session_id: str) -> bool:
        """feature/global-search: показати стрічку й прокрутити до картки сесії.
        Картки будуються у showEvent → refresh, тож на момент виклику вони вже є
        (перехід на вкладку спершу показує сторінку). Немає такої картки → False."""
        entry = self._cards.get(session_id)
        if entry is None:
            return False
        card = entry[0]
        self._stack.setCurrentIndex(1)
        try:
            self._scroll.ensureWidgetVisible(card)
        except RuntimeError:
            return False
        return True

    def _add_card(self, meta):
        status = getattr(meta, "status", _ST_DONE)
        session_id = getattr(meta, "id", "")
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 15)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        title = QLabel(self._title_for(meta))
        title.setProperty("strong", True)
        title.setWordWrap(True)   # довга назва наради переноситься, не вилазить за бейдж
        top.addWidget(title, stretch=1)
        storage_error = getattr(meta, "storage_error", None) or {}
        kind, badge_key = _BADGE.get(status, ("queued", "meeting_badge_done"))
        processing = dict(getattr(meta, "processing", {}) or {})
        if status == _ST_DONE:
            process_badges = {
                "running": ("busy", "meeting_badge_transcribing"),
                "complete": ("done", "meeting_badge_processed"),
                "partial": ("queued", "meeting_badge_partial"),
                "cancelled": ("queued", "meeting_badge_cancelled"),
                "failed": ("error", "meeting_badge_processing_error"),
            }
            kind, badge_key = process_badges.get(
                processing.get("status"),
                ("done", "meeting_badge_audio_ready"),
            )
        # Навіть якщо доступний фрагмент уже розшифровано, ENOSPC — це стан
        # втрати даних, а не «готово»: бейдж має бути таким само помітним, як банер.
        if storage_error.get("kind") == "disk_full":
            kind, badge_key = "error", "meeting_badge_error"
        tag = StatusTag(kind, tr(badge_key))
        top.addWidget(tag)
        session_path = None
        resolver = getattr(self.controller, "meeting_session_dir", None)
        if not callable(resolver):
            resolver = getattr(self.controller, "_meeting_session_dir", None)
        if callable(resolver):
            session_path = resolver(session_id)
        if session_path is not None:
            materialized = session_id in getattr(
                self.controller, "_meeting_plain_cache", {})
            security = classify_meeting_security(
                session_path, status, materialized=materialized)
            security_badges = {
                "encrypted": ("done", "meeting_security_encrypted"),
                "open_view": ("queued", "meeting_security_open_view"),
                "open": ("warn", "meeting_security_open"),
            }
            sec_kind, sec_key = security_badges[security]
            sec_badge = StatusTag(sec_kind, tr(sec_key))
            if security == "open":
                sec_badge.setToolTip(tr("meeting_security_open_tip"))
            top.addWidget(sec_badge)
        lay.addLayout(top)
        if storage_error.get("kind") == "disk_full":
            minute = max(1, int(storage_error.get("elapsed_seconds", 0)) // 60 + 1)
            warning = QLabel(tr("meeting_storage_full", minute=minute))
            warning.setProperty("badge", "error")
            warning.setWordWrap(True)
            lay.addWidget(warning)

        if status == _ST_DONE:
            self._fill_done_card(lay, session_id, meta)
        else:
            self._fill_pending_card(lay, session_id, status)

        self._feedbox.insertWidget(self._feedbox.count() - 1, card)
        self._cards[session_id] = (card, tag)

    def _title_for(self, meta) -> str:
        title = getattr(meta, "title", None)
        if title:
            return title
        sid = getattr(meta, "id", "")
        # id = "2026-07-15_14-30-05" → "15.07.2026 14:30"
        try:
            d, t = sid.split("_")
            y, mo, day = d.split("-")
            hh, mm, _ss = t.split("-")
            return f"{day}.{mo}.{y} {hh}:{mm}"
        except (ValueError, AttributeError):
            return sid or "—"

    def _add_title_editor(self, lay, session_id, meta):
        """Поле вводу назви + кнопка «Зберегти назву». Якщо назви ще немає —
        передзаповнюємо підказкою з календаря (feature/diary-calendar). Поле і
        кнопка живуть у спільному горизонтальному ряді разом з іншими кнопками
        картки — Qt репарентить віджети під батьківський QHBoxLayout рядка."""
        current = getattr(meta, "title", None) or ""
        suggested = ""
        if not current:
            try:
                suggested = self.controller.suggest_meeting_title(session_id) or ""
            except Exception:
                suggested = ""
        edit = QLineEdit(current or suggested)
        edit.setPlaceholderText(self._title_for(meta))
        edit.setObjectName(f"meetingTitleEdit-{session_id}")
        edit.setAccessibleName(tr("meeting_title_label"))
        save = GlassButton(tr("meeting_title_save"))
        save.setAccessibleName(tr("meeting_title_save"))
        save.setObjectName(f"meetingSaveTitleButton-{session_id}")
        save.clicked.connect(
            lambda _=False, sid=session_id, box=edit:
            self._save_title(sid, box))
        lay.addWidget(edit, stretch=1)
        lay.addWidget(save)

    def _save_title(self, session_id, edit):
        self.controller.set_meeting_title(session_id, edit.text())
        try:
            motion.toast(self, tr("meeting_title_saved"))
        except Exception:
            pass
        self.refresh()

    def _add_raw_cleanup(self, lay, session_id, has_audio):
        """С10: коли WAV готові, а сирі .f32 ще лежать — дати кнопку звільнити
        місце. Без готових WAV сирі дані — єдина копія, тож нічого не пропонуємо
        (сесія лишається придатною до відновлення)."""
        if not has_audio:
            return
        try:
            raw = int(self.controller.meeting_raw_audio_bytes(session_id))
        except Exception:
            raw = 0
        if raw <= 0:
            return
        btn = GlassButton(tr("meeting_free_raw_audio"))
        btn.setProperty("ghost", True)
        btn.setAccessibleName(tr("meeting_free_raw_audio"))
        btn.setObjectName(f"meetingFreeRawButton-{session_id}")
        btn.setToolTip(tr("meeting_raw_audio_size",
                          size=f"{raw / (1024 * 1024):.0f}"))
        btn.clicked.connect(
            lambda _=False, sid=session_id: self._free_raw_audio(sid))
        lay.addWidget(btn)

    def _free_raw_audio(self, session_id):
        if not _meeting_confirm(
                self, tr("meeting_free_raw_audio"),
                tr("meeting_free_raw_confirm")):
            return
        try:
            self.controller.meeting_free_raw_audio(session_id)
            motion.toast(self, tr("meeting_free_raw_done"))
        except Exception:
            pass
        self.refresh()

    def _add_processing_controls(self, lay, session_id, meta, has_audio):
        """Окрема явна ASR-команда; стан запису тут уже завершений. Кнопка
        «Обробити нараду»; у процесі — сусідній прогрес-бар і «Скасувати»."""
        status = QLabel()
        status.setWordWrap(True)
        status.setProperty("muted", True)
        lay.addWidget(status, stretch=1)

        bar = QProgressBar()
        bar.setRange(0, 1000)
        bar.setTextVisible(True)
        bar.setAccessibleName(tr("meeting_processing_progress_a11y"))
        bar.setObjectName(f"meetingProcessingProgress-{session_id}")
        bar.hide()
        lay.addWidget(bar, stretch=1)

        cancel = GlassButton(tr("meeting_processing_cancel"))
        cancel.setAccessibleName(tr("meeting_processing_cancel"))
        cancel.setObjectName(f"meetingCancelProcessing-{session_id}")
        cancel.clicked.connect(
            lambda _=False, sid=session_id:
            self._cancel_processing(sid))
        cancel.hide()
        lay.addWidget(cancel)

        process = GlassButton(tr("meeting_process"))
        process.setProperty("accent", True)
        process.setAccessibleName(tr("meeting_process"))
        process.setObjectName(f"meetingProcessButton-{session_id}")
        process.clicked.connect(
            lambda _=False, sid=session_id:
            self._start_processing(sid))
        lay.addWidget(process)

        total = sum(
            len(paths)
            for paths in (getattr(meta, "audio_files", {}) or {}).values())
        self._processing_widgets[session_id] = {
            "status": status,
            "bar": bar,
            "cancel": cancel,
            "process": process,
            "has_audio": has_audio,
            "total_chunks": total,
        }
        self._apply_processing_state(
            session_id, dict(getattr(meta, "processing", {}) or {}))

    def _start_processing(self, session_id):
        if not self.controller.start_meeting_processing(session_id):
            return
        entry = self._processing_widgets.get(session_id, {})
        total = max(1, int(entry.get("total_chunks", 1)))
        self._apply_processing_state(session_id, {
            "status": "running",
            "completed_chunks": 0,
            "total_chunks": total,
        })

    def _cancel_processing(self, session_id):
        if not self.controller.cancel_meeting_processing(session_id):
            return
        entry = self._processing_widgets.get(session_id)
        if entry is None:
            return
        entry["cancel"].setEnabled(False)
        entry["status"].setText(tr("meeting_processing_cancelling"))

    def _on_processing_progress(self, session_id, state):
        self._apply_processing_state(session_id, dict(state or {}))

    def _on_processing_done(self, _session_id, _result):
        self.refresh()

    def _apply_processing_state(self, session_id, state):
        entry = self._processing_widgets.get(session_id)
        if entry is None:
            return
        status = state.get("status") or "ready"
        done = max(0, int(state.get("completed_chunks", 0) or 0))
        total = max(1, int(
            state.get("total_chunks", entry.get("total_chunks", 1)) or 1))
        entry["bar"].setValue(min(1000, int(round(1000 * done / total))))
        entry["bar"].setFormat(tr(
            "meeting_processing_progress", done=done, total=total))
        entry["bar"].setVisible(status == "running")
        entry["cancel"].setVisible(status == "running")
        entry["cancel"].setEnabled(not bool(state.get("cancel_requested")))
        entry["process"].setVisible(
            status in ("ready", "failed", "cancelled")
            and bool(entry["has_audio"]))
        entry["process"].setText(
            tr("meeting_processing_retry")
            if status == "failed" else tr("meeting_process"))
        messages = {
            "ready": "meeting_processing_ready",
            "running": "meeting_processing_running",
            "complete": "meeting_processing_complete",
            "partial": "meeting_processing_partial",
            "cancelled": "meeting_processing_cancelled",
            "failed": "meeting_processing_failed",
        }
        key = messages.get(status, "meeting_processing_ready")
        stage = state.get("stage")
        # Фаза діаризації: статус лишається "running", але окремий етап показуємо
        # чесно — «розрізняю голоси», а не «розшифровую».
        diarizing = status == "running" and stage == "diarizing"
        if diarizing:
            key = "meeting_processing_diarizing"
        entry["status"].setText(
            tr("meeting_processing_cancelling")
            if state.get("cancel_requested") and status == "running"
            else tr(key))
        tag_entry = self._cards.get(session_id)
        if tag_entry is not None:
            badge = {
                "running": ("busy", "meeting_badge_transcribing"),
                "complete": ("done", "meeting_badge_processed"),
                "partial": ("queued", "meeting_badge_partial"),
                "cancelled": ("queued", "meeting_badge_cancelled"),
                "failed": ("error", "meeting_badge_processing_error"),
            }.get(status)
            if diarizing:
                tag_entry[1].set_state("busy", tr("meeting_badge_diarizing"))
            elif badge:
                tag_entry[1].set_state(badge[0], tr(badge[1]))

    def _fill_done_card(self, lay, session_id, meta):
        # feature/diary-calendar: назва наради — редаговане поле. Якщо назви ще
        # немає, пробуємо підказати з календаря (.ics) за часом наради; підказку
        # НЕ нав'язуємо — просто передзаповнюємо, користувач може змінити.
        # Усі чотири кнопки рядка — прямі item'и одного QHBoxLayout: Qt репарентить
        # віджети, додані у під-layout, на батька того layout'у, тож хост QWidget
        # тримає їх під одним власним QHBoxLayout (а не під QVBoxLayout картки).
        row = QWidget()
        actions = QHBoxLayout(row)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        self._add_title_editor(actions, session_id, meta)
        has_audio = bool(getattr(meta, "audio_files", {}) or {})
        self._add_processing_controls(actions, session_id, meta, has_audio)
        self._add_raw_cleanup(actions, session_id, has_audio)
        # «Видалити нараду» — у тому самому ряді, але праворуч і відділена
        # розтяжкою, щоб її не натиснули випадково.
        actions.addStretch()
        actions.addWidget(self._delete_button(session_id))
        lay.addWidget(row)
        title = self._title_for(meta)
        text = ""
        try:
            text = self.controller.read_meeting_transcript(session_id) or ""
        except Exception:
            text = ""
        utterances = self.controller.read_meeting_utterances(session_id) or []
        player = self._add_audio_player(lay, session_id, meta)
        self._add_video_button(lay, session_id, meta)

        class TranscriptViewer(QLabel):
            def __init__(self, txt, utts, plr, parent=None):
                super().__init__(parent)
                self._text = txt
                self._utterances = utts
                self._player = plr
                self._last_idx = -1
                
                self.setWordWrap(True)
                self.setTextInteractionFlags(Qt.TextBrowserInteraction)
                self.setOpenExternalLinks(False)
                self.linkActivated.connect(self._on_anchor)
                self.setText(txt or tr("meeting_error_silence"))
                
                if self._player:
                    self._player.position_changed.connect(self._on_pos)
                    
            def setText(self, txt):
                self._text = txt
                import re
                html = txt.replace('\n', '<br>')
                # Linkify timestamps [MM:SS] та [H:MM:SS] (довгі наради ≥1 год).
                # Колір — з активної теми (золото вдень / червоне вночі), не вшитий hex.
                html = re.sub(
                    r'\[(\d+:\d{2}(?::\d{2})?)\]',
                    r'<a href="\1" style="text-decoration:none; color:'
                    + theme.GOLD + r';">[\1]</a>',
                    html)
                self._base_html = html
                super().setText(html)
                self._last_idx = -1
                
            def _on_anchor(self, link):
                parts = link.split(':')
                try:
                    nums = [int(p) for p in parts]
                except ValueError:
                    return
                if len(nums) == 2:            # MM:SS
                    ms = (nums[0] * 60 + nums[1]) * 1000
                elif len(nums) == 3:          # H:MM:SS (довга нарада)
                    ms = (nums[0] * 3600 + nums[1] * 60 + nums[2]) * 1000
                else:
                    return
                if self._player:
                    self._player.seek_ms(ms)

            def _on_pos(self, pos_ms):
                if not self._utterances: return
                idx = -1
                for i, u in enumerate(self._utterances):
                    start = float(u.start) * 1000
                    end = float(u.end) * 1000
                    if start <= pos_ms <= end:
                        idx = i
                        break
                        
                if idx == self._last_idx: return
                self._last_idx = idx
                
                if idx >= 0:
                    u = self._utterances[idx]
                    stamp = MeetingPage._stamp(float(u.start))
                    html = self._base_html
                    target_a = f'<a href="{stamp}"'
                    
                    next_stamp = None
                    if idx + 1 < len(self._utterances):
                        next_stamp = MeetingPage._stamp(float(self._utterances[idx+1].start))
                        
                    if target_a in html:
                        parts = html.split(target_a, 1)
                        before = parts[0]
                        after = target_a + parts[1]
                        
                        if next_stamp:
                            next_a = f'<a href="{next_stamp}"'
                            if next_a in after:
                                sub_parts = after.split(next_a, 1)
                                highlighted = sub_parts[0]
                                rest = next_a + sub_parts[1]
                                html = f'{before}<span style="background-color: {theme._LINE_HILITE};">{highlighted}</span>{rest}'
                        else:
                            html = f'{before}<span style="background-color: {theme._LINE_HILITE};">{after}</span>'
                    super().setText(html)
                else:
                    super().setText(self._base_html)

        body = TranscriptViewer(text, utterances, player)
        body._text = text
        lay.addWidget(body)


        # feature/protocol-enrich: Smart Chapters — розділи наради з protocol.md
        # як клікабельні чіпи; клік стрибає вбудований плеєр на таймкод розділу.
        chapters = self._protocol_chapters(session_id)
        if chapters and player is not None:
            chaprow = QHBoxLayout(); chaprow.setSpacing(8)
            caption = QLabel(tr("meeting_chapters")); caption.setProperty("formlabel", True)
            chaprow.addWidget(caption)
            for start, _end, ctitle in chapters:
                label = f"{self._stamp(start)} · {ctitle}" if ctitle else self._stamp(start)
                chip = GlassButton(label)
                chip.setToolTip(tr("meeting_chapter_seek"))
                chip.setAccessibleName(f"{tr('meeting_chapters')}: {label}")
                chip.clicked.connect(
                    lambda _=False, p=player, t=float(start): p.play_from(t))
                chaprow.addWidget(chip)
            chaprow.addStretch(); lay.addLayout(chaprow)

        # Мітки — короткі клікабельні переходи до «домовились Х».
        bookmarks = getattr(meta, "bookmarks", getattr(meta, "marks", [])) or []
        if bookmarks and player is not None:
            markrow = QHBoxLayout(); markrow.setSpacing(8)
            markrow.addWidget(QLabel(tr("meeting_bookmarks")))
            for item in bookmarks:
                stamp = float(item.get("timestamp", 0))
                label = item.get("title") or self._stamp(stamp)
                btn = GlassButton(f"{self._stamp(stamp)} · {label}")
                btn.clicked.connect(lambda _=False, p=player, t=stamp: p.play_from(t))
                markrow.addWidget(btn)
            markrow.addStretch(); lay.addLayout(markrow)

        # Структурні сегменти мають start/end: це і є маркери в транскрипті.
        utterances = self.controller.read_meeting_utterances(session_id)
        if utterances and player is not None:
            segrow = QHBoxLayout(); segrow.setSpacing(8)
            segrow.addWidget(QLabel(tr("meeting_timestamps")))
            for utterance in utterances[:12]:
                btn = GlassButton(self._stamp(float(utterance.start)))
                btn.setToolTip(utterance.text)
                btn.clicked.connect(lambda _=False, b=btn, sid=session_id, u=utterance, p=player: self._segment_menu(b, sid, u, p))
                segrow.addWidget(btn)
            segrow.addStretch(); lay.addLayout(segrow)
        names = getattr(meta, "speaker_names", {}) or {}
        if names:
            rename_row = QHBoxLayout()
            rename_row.setSpacing(8)
            rename_row.addWidget(QLabel(tr("meeting_rename_speaker")))
            for speaker_id, default in names.items():
                edit = QLineEdit(default)
                edit.setPlaceholderText(default)
                edit.setAccessibleName(tr("meeting_rename_speaker"))
                edit.editingFinished.connect(
                    lambda sid=session_id, spk=speaker_id, box=edit:
                    (self.controller.set_meeting_speaker_name(sid, spk, box.text()), self.refresh()))
                rename_row.addWidget(edit)
            rename_row.addStretch()
            lay.addLayout(rename_row)
        saved = QLabel()
        saved.setProperty("muted", True)
        saved.setWordWrap(True)   # шлях збереження — довгий рядок, не обрізати
        saved.hide()

        # Чекбокс мітки джерела «Я / Співрозмовники» в експорті (hint-рівень).
        # За замовч. увімкнено; діаризаційні імена мовців від нього не залежать
        # (вони мають пріоритет і завжди в тексті).
        src_chk = QCheckBox(tr("meeting_exp_source_labels"))
        src_chk.setChecked(True)
        src_chk.setProperty("muted", True)
        src_chk.setAccessibleName(tr("meeting_exp_source_labels"))

        btns = QHBoxLayout()
        btns.setSpacing(10)

        if getattr(self.controller.cfg, "protocol_ai_enabled", True):
            # 1. «Створити протокол»
            import qtawesome as qta
            protocol = GlassButton(tr("meeting_protocol_create"),
                                   icon=qta.icon("fa6s.file-lines", color=theme.GOLD))
            protocol.setAccessibleName(tr("meeting_protocol_create"))
            model_ready = self.controller.protocol_model_ready()
            protocol.setEnabled(bool(model_ready and text))
            fm_p = protocol.fontMetrics()
            protocol.setMinimumWidth(fm_p.horizontalAdvance(tr("meeting_protocol_create")) + 70)
            if not model_ready:
                protocol.setToolTip(tr("meeting_protocol_need_model"))
            else:
                protocol.setToolTip(tr("meeting_protocol_create_desc"))
            protocol.clicked.connect(lambda _=False, sid=session_id: self._create_protocol(sid))
            btns.addWidget(protocol)

            # 2. «Спитати про нараду»
            ask = GlassButton(tr("meeting_qa_ask"),
                              icon=qta.icon("fa6s.circle-question", color=theme.GOLD))
            ask.setAccessibleName(tr("meeting_qa_ask"))
            ask.setEnabled(bool(model_ready and text))
            fm_a = ask.fontMetrics()
            ask.setMinimumWidth(fm_a.horizontalAdvance(tr("meeting_qa_ask")) + 70)
            if not model_ready:
                ask.setToolTip(tr("meeting_protocol_need_model"))
            else:
                ask.setToolTip(tr("meeting_qa_ask_desc"))
            ask.clicked.connect(
                lambda _=False, sid=session_id, p=player: self._ask_meeting(sid, p))
            btns.addWidget(ask)

        # 3. Випадаюче меню «Експортувати в…»
        exp_btn = GlassButton(tr("meeting_export_menu"))
        exp_btn.setAccessibleName(tr("meeting_export_menu"))
        fm_e = exp_btn.fontMetrics()
        exp_btn.setMinimumWidth(fm_e.horizontalAdvance(tr("meeting_export_menu")) + 44)

        export_menu = QMenu(exp_btn)

        act_copy = export_menu.addAction(tr("meeting_copy"))
        act_copy.triggered.connect(lambda _=False, b=body: self._copy(b._text))

        act_txt = export_menu.addAction(tr("meeting_exp_txt"))
        act_txt.triggered.connect(
            lambda _=False, b=body, s=saved, sid=session_id, c=src_chk:
            self._save_txt(b._text if c.isChecked()
                           else self._render_txt(sid, show_source=False), sid, s))

        act_md = export_menu.addAction(tr("meeting_exp_md"))
        act_md.triggered.connect(
            lambda _=False, s=saved, ttl=title, sid=session_id, c=src_chk:
            self._save_md(sid, ttl, s, show_source=c.isChecked()))

        act_json = export_menu.addAction(tr("meeting_exp_json"))
        act_json.triggered.connect(
            lambda _=False, s=saved, sid=session_id:
            self._save_json(sid, s))

        export_menu.addSeparator()

        act_audio = export_menu.addAction(tr("meeting_export_audio"))
        act_audio.triggered.connect(
            lambda _=False, sid=session_id: self._export_audio(sid))

        act_folder = export_menu.addAction(tr("meeting_open_folder"))
        act_folder.triggered.connect(
            lambda _=False, sid=session_id: self.controller.open_meeting_folder(sid))

        export_menu.addSeparator()

        act_obs_send = export_menu.addAction(tr("meeting_obsidian_send"))
        act_obs_open = export_menu.addAction(tr("meeting_obsidian_open"))
        act_obs_open.setEnabled(False)

        def _send_obsidian(_=False, sid=session_id, ttl=title, s=saved, ao=act_obs_open):
            try:
                path = self.controller.export_meeting_to_obsidian(sid, ttl)
            except (OSError, ValueError):
                s.setText(tr("meeting_obsidian_fail")); s.show(); return
            if path is None:
                s.setText(tr("meeting_obsidian_off")); s.show(); return
            import os
            ao._obs_path = str(path)
            ao.setEnabled(True)
            s.setText(tr("meeting_obsidian_saved", name=os.path.basename(str(path))))
            s.show()

        act_obs_send.triggered.connect(_send_obsidian)
        act_obs_open.triggered.connect(
            lambda _=False, ao=act_obs_open:
            self.controller.obsidian_open(getattr(ao, "_obs_path", None)))

        exp_btn.clicked.connect(
            lambda _=False: export_menu.exec(exp_btn.mapToGlobal(exp_btn.rect().bottomLeft())))

        btns.addWidget(exp_btn)

        # feature/transcript-editing: правка транскрипту сесії (opt-in). Пише назад
        # у transcript.txt; transcript.json (структурне джерело) лишається. Німу
        # сесію (порожній текст) не редагуємо — нема чого правити.
        panel = None
        if text and getattr(self.controller.cfg, "transcript_editing_enabled", False):
            from .edit_search import TranscriptEditPanel

            def _apply_edit(new, b=body, sid=session_id):
                b._text = new
                b.setText(new or tr("meeting_error_silence"))
                self.controller.write_meeting_transcript(sid, new)

            panel = TranscriptEditPanel(
                body, lambda b=body: b._text, _apply_edit,
                # feature/voice-edit-selection: AI-редагувати виділене голосом
                ai_edit_fn=lambda sel, rep: self.controller.voice_edit_selection(
                    sel, rep, self))
            btns.addWidget(panel.edit_button)
        btns.addStretch()
        lay.addLayout(btns)
        # Чекбокс «Хто говорить» — ОКРЕМИМ рядком під кнопками: у ряду з кнопками
        # дій його довгий підпис не вміщався на 1000px і стискав кнопки, ріжучи
        # їхній текст (головна вимога хвилі: жодного обрізання на 1000-1920px).
        src_row = QHBoxLayout()
        src_row.addWidget(src_chk)
        src_row.addStretch()
        lay.addLayout(src_row)
        if panel is not None:
            lay.addWidget(panel)
        # feature/chain-of-custody: рядок цілісності — hint-статус + журнал подій.
        self._add_integrity_row(lay, session_id)
        lay.addWidget(saved)

    def _track_label(self, track, meta):
        """Людський підпис доріжки: mic → «Мій голос», sys → «Інші голоси»,
        решта (мультимік) → ім'я спікера, якщо задане."""
        if track == "mic":
            return tr("meeting_track_mic")
        if track == "sys":
            return tr("meeting_track_sys")
        return (getattr(meta, "speaker_names", {}) or {}).get(track, track)

    def _add_audio_player(self, lay, session_id, meta=None):
        """Вставити вбудований плеєр у картку. Немає WAV — нічого не додаємо.
        Дві+ доріжки (онлайн-дзвінок/мультимік) — синхронний плеєр із панеллю
        мікшера (увімк/гучність/соло на доріжку). Одна доріжка чи файл-мікс —
        тонкий ряд InlinePlayer, як раніше."""
        try:
            tracks = self.controller.meeting_audio_paths(session_id)
        except Exception:
            tracks = {}
        if not tracks:
            return
        order = list(tracks)
        master_track = order[0]
        if should_show_track_panel(len(order)):
            specs = [(t, self._track_label(t, meta), tracks[t]) for t in order]
            player = MultiTrackPlayer(specs, self)
        else:
            player = InlinePlayer(tracks[master_track])
        self._players.append(player)
        lay.addWidget(player)
        edit = GlassButton(tr("audioedit_open"))
        panel = {"widget": None, "track": None}

        # Кілька доріжок — селектор, ЯКУ доріжку відкриває редактор. Без нього
        # редактор завжди чіпав би майстер-доріжку («Мій голос»), а обрізати чи
        # заглушити голос співрозмовника (sys / мультимік) стало б неможливо через
        # UI — саме те, що робить audioedit_redact для чутливого запису.
        track_pick = None
        if should_show_track_panel(len(order)):
            track_pick = QComboBox()
            track_pick.setAccessibleName(tr("audioedit_track_pick"))
            for t in order:
                track_pick.addItem(self._track_label(t, meta), t)

        def _edit(_=False):
            track = track_pick.currentData() if track_pick is not None else master_track
            old = panel["widget"]
            if old is not None:
                if panel["track"] == track:      # та сама доріжка — показати/сховати
                    old.setVisible(not old.isVisible())
                    return
                lay.removeWidget(old)            # інша доріжка — перебудувати редактор
                old.deleteLater()
                panel["widget"] = None
            try:
                # Кілька доріжок → редакція мусить чіпати ЛИШЕ репліки цієї
                # доріжки. Джерело виводимо чесно з ключа доріжки (mic→«Я»,
                # sys→«Співрозмовники», мультимік→свій ключ). Одна доріжка →
                # None (затирати все перекрите виділенням).
                if track_pick is not None:
                    from whisper_core.meeting.postprocess import _track_source
                    source = _track_source(track)
                else:
                    source = None
                panel["widget"] = AudioEditorPanel(
                    tracks[track], player, self.controller, self,
                    marks=meta.bookmarks if meta else None, source=source)
                panel["track"] = track
                lay.addWidget(panel["widget"])
            except Exception as exc:
                motion.toast(self, str(exc))

        edit.clicked.connect(_edit)
        editrow = QHBoxLayout(); editrow.addWidget(edit)
        if track_pick is not None:
            editrow.addWidget(track_pick)
        
        try:
            utterances = self.controller.read_meeting_utterances(session_id)
        except Exception:
            utterances = []
        
        if utterances:
            from ..player import _IconButton

            def _nav_utterance(forward: bool):
                pos_ms = player._player.position()
                target_ms = (next_utterance_ms(pos_ms, utterances) if forward
                             else prev_utterance_ms(pos_ms, utterances))
                if target_ms is not None:
                    player.seek_ms(target_ms)
            
            btn_prev = _IconButton("fa6s.backward-step", tr("meeting_utterance_prev"))
            btn_prev.clicked.connect(lambda _=False: _nav_utterance(False))
            btn_next = _IconButton("fa6s.forward-step", tr("meeting_utterance_next"))
            btn_next.clicked.connect(lambda _=False: _nav_utterance(True))
            
            editrow.addWidget(btn_prev)
            editrow.addWidget(btn_next)

        editrow.addStretch()
        lay.addLayout(editrow)
        return player

    def _add_video_button(self, lay, session_id, meta=None):
        """Кнопка «Дивитися відео» — лише коли нарада має відеозапис екрана
        (screen.webm, старі наради — screen.mp4). Відкриває вбудований відеоплеєр
        тим самим діалогом, що й сторінка «Запис екрана». Аудіодоріжки наради
        передаємо у плеєр, щоб екран (німе відео) грав синхронно з mic/sys, а
        панель мікшера керувала звуком."""
        try:
            video = self.controller.meeting_screen_video(session_id)
        except Exception:
            video = None
        if not video:
            return
        try:
            tracks = self.controller.meeting_audio_paths(session_id)
        except Exception:
            tracks = {}
        specs = [(t, self._track_label(t, meta), tracks[t]) for t in tracks]
        from ..video_player import VideoPlayerDialog
        row = QHBoxLayout()
        watch = GlassButton(tr("screen_play"))
        watch.setAccessibleName(tr("screen_play"))
        watch.clicked.connect(
            lambda _=False, p=video, a=specs: VideoPlayerDialog.open_for(self, p, a))
        row.addWidget(watch)
        row.addStretch()
        lay.addLayout(row)

    @staticmethod
    def _stamp(seconds: float) -> str:
        """Секунди → «MM:SS» (або «H:MM:SS» для нарад ≥1 год). Дзеркалить
        whisper_core.meeting.postprocess._fmt_ts, щоб анкор транскрипту й
        ціль підсвітки збігалися символ-у-символ."""
        total = max(0, int(round(seconds)))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    def _protocol_chapters(self, session_id):
        """Розділи з protocol.md сесії → (start, end, title) для клікабельних
        чіпів. Немає файлу/секції → порожньо (чіпи не показуємо)."""
        from whisper_core.protocol import generate as pgen, service
        try:
            from whisper_core.meeting.session import read_artifact
            data = read_artifact(
                self.controller.meeting_session_dir(session_id),
                service.PROTOCOL_FILENAME)
            return pgen.parse_chapters(data.decode("utf-8"))
        except Exception:
            return []

    def _segment_menu(self, button, session_id, utterance, player):
        menu = QMenu(button)
        play = menu.addAction(tr("meeting_play_fragment"))
        export = menu.addAction(tr("meeting_export_fragment"))
        chosen = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        if chosen == play:
            player.play_from(float(utterance.start), float(utterance.end))
        elif chosen == export:
            self._export_audio(session_id, float(utterance.start), float(utterance.end))

    def _export_audio(self, session_id, start=None, end=None):
        formats = self.controller.meeting_export_formats()
        if not formats:
            motion.toast(self, tr("meeting_export_unavailable")); return
        choice, ok = QInputDialog.getItem(self, tr("meeting_export_audio"), tr("meeting_export_format"), [ext.upper() for ext in formats], 0, False)
        if not ok: return
        bitrate, ok = QInputDialog.getItem(self, tr("meeting_export_audio"), tr("meeting_export_bitrate"), ["96", "128", "192", "256"], 1, False)
        if not ok: return
        mix = _meeting_confirm(self, tr("meeting_export_audio"), tr("meeting_export_mix"))
        ext = choice.lower(); out, _ = QFileDialog.getSaveFileName(self, tr("meeting_export_audio"), f"{session_id}.{ext}", f"*.{ext}")
        if not out: return
        try:
            self.controller.export_meeting_audio(session_id, out, ext, int(bitrate), mix=mix, start=start, end=end)
            motion.toast(self, tr("meeting_export_done"))
        except Exception:
            motion.toast(self, tr("meeting_export_fail"))

    def _fill_pending_card(self, lay, session_id, status):
        btns = QHBoxLayout()
        btns.setSpacing(10)
        if status in (_ST_INTERRUPTED, _ST_ERROR):
            recover = GlassButton(tr("meeting_recover"))
            recover.clicked.connect(
                lambda _=False, sid=session_id: self.controller.recover_meeting(sid))
            btns.addWidget(recover)
            open_audio = GlassButton(tr("meeting_open_audio"))
            open_audio.clicked.connect(
                lambda _=False, sid=session_id: self.controller.open_meeting_audio(sid))
            btns.addWidget(open_audio)
        btns.addStretch()
        btns.addWidget(self._delete_button(session_id))
        lay.addLayout(btns)

    def _delete_button(self, session_id) -> GlassButton:
        # помітна дія повного видалення (посилена OPSEC): не захована в меню
        btn = GlassButton(tr("meeting_card_delete"))
        btn.setProperty("ghost", True)
        btn.clicked.connect(lambda _=False, sid=session_id: self._confirm_delete(sid))
        return btn

    # ---------------------------------------------------------------- actions
    def _copy(self, text: str):
        QApplication.clipboard().setText(text)
        motion.toast(self, tr("toast_copied"))

    def _save_txt(self, text: str, session_id, saved_lbl: QLabel):
        suggested = f"{session_id}.txt"
        out, _ = QFileDialog.getSaveFileName(
            self, tr("meeting_save_txt"), suggested, "Text (*.txt)")
        if not out:
            return
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            saved_lbl.setText(tr("meeting_save_fail"))
            saved_lbl.show()
            return
        import os
        self.controller.log_meeting_export(session_id, "txt", out)
        saved_lbl.setText(tr("meeting_saved", name=os.path.basename(out)))
        saved_lbl.show()

    def _save_json(self, session_id, saved_lbl: QLabel):
        suggested = f"{session_id}.json"
        out, _ = QFileDialog.getSaveFileName(
            self, tr("meeting_save_json"), suggested, "JSON (*.json)")
        if not out:
            return
        try:
            import json
            from whisper_core.meeting import postprocess as mpost
            utterances = self.controller.read_meeting_utterances(session_id)
            with open(out, "w", encoding="utf-8") as stream:
                json.dump(
                    mpost.to_transcript_json(utterances),
                    stream,
                    ensure_ascii=False,
                    indent=2,
                )
        except (OSError, ValueError):
            saved_lbl.setText(tr("meeting_save_fail"))
            saved_lbl.show()
            return
        import os
        self.controller.log_meeting_export(session_id, "json", out)
        saved_lbl.setText(tr(
            "meeting_saved", name=os.path.basename(out)))
        saved_lbl.show()

    def _render_txt(self, session_id, *, show_source: bool) -> str:
        """Перебудувати текст транскрипту з transcript.json із/без міток джерела.
        Використовується, коли чекбокс «мітки джерела» знято — інакше експорт бере
        готовий (можливо, відредагований) transcript.txt із картки."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        utterances = self.controller.read_meeting_utterances(session_id)
        meta = msession.load_meta(self.controller._meeting_session_dir(session_id))
        speaker_names = meta.speaker_names if meta is not None else None
        return mpost.to_transcript_text(
            utterances, me_label=tr("meeting_speaker_me"),
            others_label=tr("meeting_speaker_others"),
            speaker_names=speaker_names, show_source=show_source)

    def _save_md(self, session_id, title, saved_lbl: QLabel, *, show_source: bool = True):
        """Зберегти нараду в Markdown (frontmatter + секції за мітками мовців).
        Репліки беремо з transcript.json (структура з мітками); без нього
        (стара сесія) — порожній transcript, лише frontmatter."""
        from whisper_core.meeting import postprocess as mpost
        from whisper_core.meeting import session as msession
        utterances = self.controller.read_meeting_utterances(session_id)
        # Діаризація зберігає назви мовців у meeting.json; .txt уже містить
        # відрендерені назви, а Markdown будується наново з transcript.json.
        meta = msession.load_meta(self.controller._meeting_session_dir(session_id))
        speaker_names = meta.speaker_names if meta is not None else None
        # date у frontmatter — ISO з id сесії «2026-07-15_14-30-05»
        sid = str(session_id)
        date = sid.split("_")[0] if "_" in sid else ""
        md = mpost.to_transcript_markdown(
            utterances,
            me_label=tr("meeting_speaker_me"),
            others_label=tr("meeting_speaker_others"),
            speaker_names=speaker_names,
            meta={"source": title or sid, "date": date},
            show_source=show_source)
        suggested = f"{session_id}.md"
        out, _ = QFileDialog.getSaveFileName(
            self, tr("meeting_save_as"), suggested, tr("files_filt_md"))
        if not out:
            return
        try:
            # newline="" — рядки вже з чистим LF (Windows інакше подвоїв би CR)
            with open(out, "w", encoding="utf-8", newline="") as f:
                f.write(md)
        except OSError:
            saved_lbl.setText(tr("meeting_save_fail"))
            saved_lbl.show()
            return
        import os
        self.controller.log_meeting_export(session_id, "md", out)
        saved_lbl.setText(tr("meeting_saved", name=os.path.basename(out)))
        saved_lbl.show()

    # ---------------------------------------------- журнал цілісності (custody)
    def _add_integrity_row(self, lay, session_id):
        """Рядок цілісності й доказовості запису у підгрупі «ДОКАЗОВІСТЬ ТА ЦІЛІСНІСТЬ»."""
        from whisper_core.meeting import audit_log
        res = self.controller.meeting_integrity_meta(session_id)

        evidence_box = QVBoxLayout()
        evidence_box.setSpacing(6)

        hdr = QLabel(tr("meeting_evidence_group_title"))
        hdr.setProperty("level", "eyebrow")
        hdr.setProperty("muted", True)
        evidence_box.addWidget(hdr)

        row = QHBoxLayout()
        row.setSpacing(8)
        if res.status == audit_log.STATUS_ABSENT:
            text = tr("meeting_integrity_absent")
        else:
            text = tr("meeting_integrity_unverified")
        status = QLabel(f"🛡 {text}")
        status.setProperty("muted", True)
        status.setWordWrap(True)
        status.setAccessibleName(tr("meeting_integrity_title"))
        row.addWidget(status)
        if res.audio_sha:
            sha = QLabel(tr("meeting_integrity_audio_sha", sha=res.audio_sha[:16]))
            sha.setProperty("muted", True)
            sha.setToolTip(res.audio_sha)
            sha.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row.addWidget(sha)
        row.addStretch()
        evidence_box.addLayout(row)

        if res.status != audit_log.STATUS_ABSENT:
            # Кнопки доказовості — ОКРЕМИМ рядком під статусом: три довгі підписи в
            # один ряд зі статусом+SHA не вміщались і виїжджали за правий край вікна
            # (дефект живого тесту 5г). Кожна — мінімальна ширина по fontMetrics
            # (як кнопки дій вище), рядок ліворуч + stretch справа → нічого не ріжеться.
            btn_row = QHBoxLayout()
            btn_row.setSpacing(8)
            for key, acc, slot in (
                ("meeting_integrity_title", "meeting_integrity_title",
                 self._show_integrity_journal),
                ("meeting_review_button", "meeting_review_title",
                 self._confirm_review),
                ("meeting_evidence_export", "meeting_evidence_export",
                 self._export_evidence),
            ):
                b = GlassButton(tr(key))
                b.setAccessibleName(tr(acc))
                b.setMinimumWidth(b.fontMetrics().horizontalAdvance(tr(key)) + 44)
                b.clicked.connect(lambda _=False, sid=session_id, fn=slot: fn(sid))
                btn_row.addWidget(b)
            btn_row.addStretch()
            evidence_box.addLayout(btn_row)
        lay.addLayout(evidence_box)

    def _show_integrity_journal(self, session_id):
        """Діалог із ланцюгом подій журналу цілісності. Події показуємо ОДРАЗУ
        (дешеве читання журналу, без хешування), а ПОВНУ перевірку цілісності
        (перехешування аудіо/транскрипту) запускаємо у фоновому QThread — навіть
        відкриття діалогу не морозить UI. Заголовок оновлюється з «Перевіряю…»
        на підтверджено/порушено, коли воркер завершить. Тільки читання."""
        from whisper_core.meeting import audit_log
        meta = self.controller.meeting_integrity_meta(session_id)
        _labels = {
            audit_log.EVENT_CREATED: tr("meeting_audit_created"),
            audit_log.EVENT_STOPPED: tr("meeting_audit_stopped"),
            audit_log.EVENT_FINALIZED: tr("meeting_audit_finalized"),
            audit_log.EVENT_DECRYPTED: tr("meeting_audit_decrypted"),
            audit_log.EVENT_EDITED: tr("meeting_audit_edited"),
            audit_log.EVENT_EXPORTED: tr("meeting_audit_exported"),
            audit_log.EVENT_REVIEWED: tr("meeting_audit_reviewed"),
        }

        def _body(head, audio_sha):
            # Обидві сторони: чесна підказка про природу перевірки (В-06, безпекова
            # хвиля) + фільтр пошкоджених записів журналу (блокер Т56).
            lines = [f"🛡 {head}", "", tr("meeting_integrity_hint"), ""]
            lines += _audit_journal_lines(meta.events, _labels)
            if audio_sha:
                lines += ["", tr("meeting_integrity_audio_sha", sha=audio_sha)]
            return "\n".join(lines)

        box = QMessageBox(self)
        box.setWindowTitle(tr("meeting_integrity_title"))
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.setText(_body(tr("meeting_integrity_checking"), meta.audio_sha))
        try:
            import qtawesome as qta
            box.setIconPixmap(qta.icon("fa6s.shield-halved", color=theme.GOLD).pixmap(40, 40))
        except Exception:
            pass

        def _on_verified(res):
            if res.status == audit_log.STATUS_VERIFIED:
                head = tr("meeting_integrity_ok")
            elif res.status == audit_log.STATUS_BROKEN:
                head = tr("meeting_integrity_broken")
            else:
                head = tr("meeting_integrity_absent")
            try:
                box.setText(_body(head, res.audio_sha or meta.audio_sha))
            except RuntimeError:
                pass                       # діалог уже закрито — оновлювати нічого

        # Воркер живе на self, щоб не був зібраний GC до завершення. Модальний
        # exec() крутить вкладений цикл подій, тож сигнал done з фонового потоку
        # доставляється й оновлює текст «на льоту».
        self._integrity_worker = IntegrityVerifyWorker(self.controller, session_id, self)
        self._integrity_worker.done.connect(_on_verified)
        self._integrity_worker.start()
        box.exec()

    def _confirm_review(self, session_id):
        """feature/evidence-plus: другий офіцер підтверджує цілісність (принцип
        «чотирьох очей»). Вводить ім'я/посаду → подія reviewed у журналі. Порожнє
        ім'я нічого не додає. Оновлюємо картку, щоб перегляд показався в журналі."""
        name, ok = QInputDialog.getText(
            self, tr("meeting_review_title"), tr("meeting_review_prompt"))
        if not ok:
            return
        if self.controller.meeting_add_review(session_id, name):
            self.refresh()

    def _export_evidence(self, session_id):
        """feature/evidence-plus: скласти доказовий пакет (zip) для комісії/слідчого.
        Пакет містить РЕАЛЬНІ дані наради (аудіо + текст) — попереджаємо перед
        експортом (OpSec: користувач свідомо вирішує, куди й що виходить)."""
        if not _meeting_confirm(
                self, tr("meeting_evidence_export"), tr("meeting_evidence_warn")):
            return
        default = f"{session_id}-evidence.zip"
        out, _ = QFileDialog.getSaveFileName(
            self, tr("meeting_evidence_export"), default, tr("files_filt_zip"))
        if not out:
            return
        try:
            self.controller.export_meeting_evidence(session_id, out)
        except (OSError, ValueError):
            _meeting_warn(
                self, tr("meeting_evidence_export"), tr("meeting_evidence_fail"))
            return
        import os
        _meeting_info(
            self, tr("meeting_evidence_export"),
            tr("meeting_evidence_done", name=os.path.basename(out)))

    def _confirm_delete(self, session_id):
        if _meeting_confirm(
                self, tr("meeting_card_delete"), tr("meeting_delete_confirm")):
            # СПЕРШУ спинити плеєри (stop звільняє mic.wav/sys.wav — інакше
            # rmtree сесії падає з WinError 32), ПОТІМ видаляти
            self._stop_players()
            self.controller.delete_meeting(session_id)
            self.refresh()

    def sync_animations(self):
        """Застосувати живий animation-toggle до REC-кнопки (як у Диктуванні)."""
        self._rec_btn.sync_animations()
