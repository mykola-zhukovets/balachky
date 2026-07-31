"""Головне вікно: сайдбар + вкладки (Диктування / Аудіофайли / Історія /
Словники / Налаштування).

Сайдбар — колона скляних кнопок (GlassButton, glass.py); анімації — motion.py.
Закриття вікна = сховати у трей (застосунок живе, поки трей активний).
"""
import ctypes
from ctypes import wintypes
import html
import logging
import re
import time
from pathlib import Path

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QStackedWidget,
    QHBoxLayout, QVBoxLayout, QLabel, QScrollArea, QFrame,
    QButtonGroup, QFileDialog, QApplication, QMenu, QInputDialog,
    QSizePolicy, QMessageBox, QProgressBar, QPushButton,
)
from PySide6.QtCore import Qt, QSettings, QTimer, QSize, Signal, QRectF
from PySide6.QtGui import QShortcut, QKeySequence, QPixmap, QImage, QColor, QIcon, QPainter

import qtawesome as qta

from whisper_core.paths import asset_root, punctuator_model_dir, anonymize_path
from whisper_core.terms import add_term

from whisper_core import processing, punctuator  # feature/processing-slider
from . import motion
from .processing_slider import ProcessingChip  # feature/processing-slider (Т47: чіп+попап)
from .watch import AUDIO_EXT   # feature/watch-folder: спільний список аудіо-розширень
from .glass import GlassButton, RecButton, StatusTag, TipToolButton
from .empty_state import EmptyState
from .hotkey import pretty
from .i18n import tr
from .level import LevelMeter
from .player import InlinePlayer, fmt_time
from .audio_editor import AudioEditorPanel
from .pages import page_header
from . import theme   # нічний режим: іконки/HTML читають палітру наживо
from .theme import spaced
from whisper_core import history


class TextLogReminderDialog(QMessageBox):
    """Одноразове privacy-нагадування для старого include_text=True."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setIcon(QMessageBox.Information)
        self.setWindowTitle(tr("test_log_text_notice_title"))
        self.setText(tr("test_log_text_notice_body"))
        self.keep_button = self.addButton(
            tr("test_log_text_notice_keep"), QMessageBox.RejectRole)
        self.disable_button = self.addButton(
            tr("test_log_text_notice_disable"), QMessageBox.AcceptRole)
        self.keep_button.setAccessibleName(tr("test_log_text_notice_keep"))
        self.disable_button.setAccessibleName(tr("test_log_text_notice_disable"))
        self.setDefaultButton(self.keep_button)
        self.setEscapeButton(self.keep_button)


def maybe_show_test_log_text_reminder(parent, controller) -> bool:
    """Показати re-consent лише у вже видимому, не згорнутому головному вікні."""
    cfg = controller.cfg
    if (not bool(getattr(cfg, "test_mode_include_text", False))
            or bool(getattr(cfg, "test_mode_text_notice_shown", False))
            or parent is None
            or not parent.isVisible()
            or parent.isMinimized()):
        return False

    dialog = TextLogReminderDialog(controller, parent)
    dialog.exec()
    cfg.test_mode_text_notice_shown = True
    if dialog.clickedButton() is dialog.disable_button:
        controller.set_test_mode(bool(getattr(cfg, "test_mode", False)), False)
    else:
        controller.save_config()
    return True


def _norm_word(word: str) -> str:
    """Нормалізація для зіставлення: без пунктуації, у нижньому регістрі."""
    return re.sub(r"[\W_]+", "", word).lower()


def render_uncertainty_html(final: str, words) -> str:
    """final → HTML: непевні слова (probability < 0.5) підсвічено золотим.

    Спільний рендер підсвітки для стрічки диктування (DictationPage) і карток
    файлів (FilesPage) — обидві показують ту саму «золоту» позначку непевних
    слів (Т40, feature/model-bottlenecks під-хвиля 2). Раніше жив лише в
    DictationPage; винесено сюди, щоб і файли рендерились одним кодом.

    КОМПРОМІС: words — слова сирого тексту від моделі, а final уже після заміни
    термінів, тож дослівного вирівнювання нема. Мапимо прагматично: final
    токенізуємо по пробілах, для кожного токена шукаємо ПЕРШЕ ще не використане
    слово з words із тим самим нормалізованим текстом. Замінені терміни збігу не
    мають — лишаються без підсвітки (їхню правильність гарантує словник).
    Повторювані слова мапляться по порядку — можлива неточність, якщо модель
    непевна лише в одному з повторів."""
    used = [False] * len(words or [])
    parts = []
    for tok in final.split(" "):
        esc = html.escape(tok)  # екрануємо ДО вставки span-ів
        uncertain = False
        norm = _norm_word(tok)
        if norm:
            for i, (w, prob) in enumerate(words or []):
                if not used[i] and _norm_word(w) == norm:
                    used[i] = True
                    uncertain = prob < 0.5
                    break
        if uncertain:
            parts.append(f'<span style="color:{theme.GOLD_EYEBROW}; '
                         f'border-bottom:1px dashed {theme.GOLD_EYEBROW};">{esc}</span>')
        else:
            parts.append(esc)
    # line-height 155% — повітря в багаторядкових розшифровках
    return ('<p style="line-height:155%; margin:0;">'
            + " ".join(parts) + "</p>")


class FileStatus:
    """Коди стану файлу в черзі. ПОРІВНЮЄМО їх у логіці, а бейдж показуємо через
    tr() — переклад назв станів не має ламати гілки коду (напр. success/error)."""
    QUEUED = "queued"
    TRANSCRIBING = "transcribing"
    DONE = "done"           # у meta приходить як "done:<секунди>" (тривалість аудіо)
    ERROR = "error"
    CANCELLED = "cancelled"  # скасовано користувачем — не помилка, тексту немає


class ElidedLabel(QLabel):
    """Однорядковий підпис, що зберігає початок і розширення довгого імені."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def resizeEvent(self, event):
        shown = self.fontMetrics().elidedText(
            self._full_text, Qt.ElideMiddle, max(0, self.width()))
        if shown != self.text():
            super().setText(shown)
        super().resizeEvent(event)

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())


# векторні іконки (не емодзі: ті рендеряться по-різному, не фарбуються під тему)
# (іконка, ключ-i18n): текст беремо через tr у момент побудови nav (мова вже задана)
_PAGES = [("fa6s.microphone", "nav_dictation"), ("fa6s.folder-open", "nav_audio"),
          ("fa6s.users", "nav_meeting"),        # feature/meeting-ui
          ("fa6s.desktop", "nav_screen"),
          ("fa6s.clock-rotate-left", "nav_history"),
          ("fa6s.book", "nav_dictionaries"), ("fa6s.gear", "nav_settings"),
          ("fa6s.magnifying-glass", "nav_search")]   # feature/global-search (останній — не зсуває індекси)


def _nav_icon(name: str):
    return qta.icon(name, color=theme.TEXT_MUTED,
                    color_selected=theme.GOLD_EYEBROW, color_active=theme.GOLD_EYEBROW)


class _TiledStack(QStackedWidget):
    """Стек сторінок із гнучким тлом робочої зони ("mascot" / "solid" / "custom").

    Підтримує дефолтне тайлове тло з маскотом, однотонну заливку або власний
    растр із масштабуванням cover. При пошкодженні/зникненні користувацької
    картинки виконується чесний фолбек на "mascot" без краху."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tile = None
        self._custom_pm = None
        self._bg_mode = "mascot"
        self._last_cfg = None
        self.reload_background()
        theme.register_restyle(self._reload_tile)   # нічний режим / зміна теми

    def reload_background(self, cfg=None) -> None:
        """Перечитати налаштування тла з cfg. Зберегти посилання на cfg для restyle."""
        if cfg is not None:
            self._last_cfg = cfg
        mode = getattr(self._last_cfg, "workspace_bg", "mascot")
        custom_path = getattr(self._last_cfg, "workspace_custom_bg_path", None)

        self._custom_pm = None
        self._tile = None
        self._bg_mode = mode

        if mode == "solid":
            pass
        elif mode == "custom":
            loaded = False
            if custom_path:
                from whisper_core.paths import user_dir
                full_path = user_dir() / custom_path
                if full_path.is_file():
                    pm = QPixmap(str(full_path))
                    if not pm.isNull():
                        self._custom_pm = pm
                        loaded = True
            if not loaded:
                self._bg_mode = "mascot"

        if self._bg_mode == "mascot":
            # Тайл — денний жук-мікрофон (бренд, не перефарбовується — той самий
            # канон, що й медальйон сплеша). У класиці показуємо як є; для БУДЬ-
            # ЯКОЇ персоналізації кольору (red і довільний hue) ховаємо: денна
            # золото-хакі текстура на весь робочий простір під іншим кольором
            # інтерфейсу виглядає чужою, а не персоналізованою.
            hue = theme._hue_for(theme.current_ui_color())
            self._tile = None if hue is not None else QPixmap(theme._BG_TILE)

        self.update()

    def _reload_tile(self) -> None:
        self.reload_background()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(theme._TILE_BASE))
        if self._bg_mode == "custom" and self._custom_pm is not None and not self._custom_pm.isNull():
            rect = self.rect()
            pw, ph = self._custom_pm.width(), self._custom_pm.height()
            if pw > 0 and ph > 0 and rect.width() > 0 and rect.height() > 0:
                scale = max(rect.width() / pw, rect.height() / ph)
                sw = rect.width() / scale
                sh = rect.height() / scale
                sx = (pw - sw) / 2.0
                sy = (ph - sh) / 2.0
                p.drawPixmap(QRectF(rect), self._custom_pm, QRectF(sx, sy, sw, sh))
        elif self._bg_mode == "mascot" and self._tile is not None and not self._tile.isNull():
            p.drawTiledPixmap(self.rect(), self._tile)
        # НЕ викликаємо super().paintEvent: QStackedWidget сам тла не малює,
        # а QSS-фон (тепер transparent) не має перекривати наш тайл.


def _red_tint(pm: QPixmap) -> QPixmap:
    """Нічний режим: перевести кольоровий растр (маскот-жук) у моно-червону гаму,
    щоб на екрані не лишалось не-червоного світла. Кожен непрозорий піксель →
    червоний за яскравістю (G/B тримаємо низько), альфа зберігається."""
    img = pm.toImage().convertToFormat(QImage.Format_ARGB32)
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            a = c.alpha()
            if a == 0:
                continue
            lum = (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) / 255.0
            img.setPixelColor(x, y, QColor(int(70 + 175 * lum),
                                           int(28 * lum), int(28 * lum), a))
    out = QPixmap.fromImage(img)
    out.setDevicePixelRatio(pm.devicePixelRatio())
    return out


def app_icon(ico_path) -> QIcon:
    """Іконка застосунку (заголовок вікна + панель завдань) за активною темою.
    Удень — оригінальний повнокольоровий жук-мікрофон (бренд). Уночі — той самий
    силует у моно-червоній гамі (_red_tint), щоб у титулбарі/таскбарі не лишалось
    не-червоного світла (мілітарі-вимога «жодного не-червоного світла»). Зворотно
    вдень: денний режим повертає оригінальний .ico без змін.

    Викликати на старті (app.py / scripts) І при живій зміні теми (reapply_theme):
    QIcon самої .ico не читає тему, тож без цього золотий жук світився б у титулі
    навіть у нічному режимі.

    Для довільної персоналізації кольору (teal/blue/...) іконка НЕ перефарбовується
    (той самий канон бренд-маскота, що й splash._hue_tint): панель завдань стоїть
    поруч із чужими застосунками, де впізнаваність важливіша за узгодження з
    кольором інтерфейсу; крутиться лише 'red' (вимога нічного бачення)."""
    base = QIcon(str(ico_path))
    if not theme.is_night():
        return base
    tinted = QIcon()
    for size in (64, 48, 32, 24, 16):
        pm = base.pixmap(size, size)
        if pm.isNull():
            continue
        tinted.addPixmap(_red_tint(pm))
    # порожня .ico (теоретично) → віддаємо базову, ніж невидиму іконку
    return tinted if not tinted.isNull() else base


class ClickableFrame(QFrame):
    """Рамка-кнопка: клік мишею або Enter/Пробіл → сигнал clicked. Курсор-палець
    і accessibleName роблять її розпізнаваною як інтерактив (шапка сайдбара →
    «Про програму»)."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)   # доступна з клавіатури

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class SidebarDownloadIndicator(QFrame):
    """Індикатор фонового завантаження моделі AI-протоколу, видимий З БУДЬ-ЯКОЇ
    сторінки (E-бекграунд-докачка, аудит 31.07.2026).

    Обґрунтування місця (сайдбар, між прокруткою навігації й кнопкою
    «Налаштування»): сайдбар присутній на всіх сторінках — Диктування, Аудіо,
    Нарада, Запис екрана, Історія, Словник, Налаштування, Пошук — тож людина
    бачить стан якісного качання, гортаючи будь-яку з них, без окремої
    QStatusBar знизу (та забирала б 24-32px висоти й ламала скляний дизайн
    сайдбара на кожній сторінці, а не лише поки щось качається).

    Приховано, доки немає активного завантаження; клік по картці відкриває
    повний ProtocolModelDownloadDialog (приєднання, ALREADY_THIS)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("card", True)
        self.hide()
        self._speed_bps = None
        self._speed_ref_time = None
        self._speed_ref_bytes = 0
        self._last_ui_update = 0.0
        self._active_key = None
        self._active_label = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        top = QHBoxLayout(); top.setSpacing(6)
        self._title = QLabel()
        self._title.setProperty("strong", True)
        self._title.setWordWrap(True)
        top.addWidget(self._title, stretch=1)
        self._cancel_btn = QPushButton("✕")
        self._cancel_btn.setFixedWidth(22)
        self._cancel_btn.setToolTip(tr("protocol_model_wait_cancel_download"))
        self._cancel_btn.setAccessibleName(tr("protocol_model_wait_cancel_download"))
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        top.addWidget(self._cancel_btn)
        lay.addLayout(top)
        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(6)
        lay.addWidget(self._bar)
        self._status = QLabel()
        self._status.setProperty("muted", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(tr("protocol_model_wait_title"))
        self.setToolTip(tr("protocol_model_wait_title"))

        from .download_manager import DownloadManager
        self._manager = DownloadManager.instance()
        self._manager.started.connect(self._on_started)
        self._manager.progress.connect(self._on_progress)
        self._manager.finished_ok.connect(self._on_finished_ok)
        self._manager.failed.connect(self._on_failed)
        self._manager.cancelled.connect(self._on_cancelled)
        # Програма могла запуститись, а завантаження вже (частково) триває —
        # неможливо насправді (менеджер живе лише в межах процесу), проте
        # захист не завадить, якщо порядок ініціалізації колись зміниться.
        if self._manager.is_busy():
            self._on_started(self._manager.active_key(), self._manager.active_label())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._active_key is not None:
            self._open_dialog()
            event.accept()
            return
        super().mousePressEvent(event)

    def _open_dialog(self):
        from .pages.protocol_ui import ProtocolModelDownloadDialog
        dlg = ProtocolModelDownloadDialog(
            self._active_key, label=self._active_label, parent=self.window())
        dlg.show(); dlg.raise_(); dlg.activateWindow()

    def _on_cancel_clicked(self):
        self._manager.cancel_download()

    def _on_started(self, key, label):
        self._active_key = key
        self._active_label = label or tr("protocol_model_wait_title")
        self._title.setText(self._active_label)
        self._bar.setRange(0, 0)
        self._status.setText(tr("protocol_model_downloading_status"))
        self._speed_bps = None
        self._speed_ref_time = None
        self._speed_ref_bytes = 0
        self._last_ui_update = 0.0
        self.show()

    def _on_progress(self, key, done, total):
        if key != self._active_key:
            return
        now = time.monotonic()
        is_final = bool(total) and done >= total
        if not is_final and (now - self._last_ui_update) < 0.2:
            return
        self._last_ui_update = now
        if self._speed_ref_time is None:
            self._speed_ref_time = now
            self._speed_ref_bytes = done
        else:
            elapsed = now - self._speed_ref_time
            if elapsed >= 0.2:
                self._speed_bps = (done - self._speed_ref_bytes) / elapsed
                self._speed_ref_time = now
                self._speed_ref_bytes = done
        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
        from .pages.protocol_ui import _progress_text
        self._status.setText(_progress_text(done, total, self._speed_bps))

    def _on_finished_ok(self, key):
        if key != self._active_key:
            return
        self._status.setText(tr("protocol_model_ready"))
        self._bar.setRange(0, 1); self._bar.setValue(1)
        self._active_key = None
        QTimer.singleShot(3000, self._hide_if_idle)

    def _on_failed(self, key, _msg):
        if key == self._active_key:
            self._active_key = None
            self.hide()

    def _on_cancelled(self, key):
        if key == self._active_key:
            self._active_key = None
            self.hide()

    def _hide_if_idle(self):
        if self._active_key is None:
            self.hide()


def _verbatim_differs(raw: str, final: str) -> bool:
    """Чи обробка (чистка/пунктуація) справді змінила текст — ОДНА умова і для
    підпису «виправлено написання», і для кнопки/пункту меню «Копіювати
    дослівно». Крайні пробіли не рахуємо (інакше підпис з'являвся б там, де
    кнопки немає), порожній raw копіювати нема сенсу."""
    raw = (raw or "").strip()
    return bool(raw) and raw != (final or "").strip()


class TermFixMenuMixin:
    """Спільне ПКМ-меню виправлення слів у словник активного профілю.

    Використовують і Диктування, і Аудіофайли (DRY). Картка-QLabel має нести
    атрибут ._final_text — чистий текст (без HTML) для копіювання й заміни.
    Підклас за потреби перевизначає _render_fix_html (підсвітка непевних слів)
    і виставляє _fix_menu_allow_ban = True (пункт «більше не пропонувати»)."""

    _fix_menu_allow_ban = False

    @staticmethod
    def verbatim_available(label: QLabel) -> bool:
        """Чи має сенс «Копіювати дослівно» для ЦІЄЇ картки ЗАРАЗ. Єдина точка
        істини для кнопки в картці й пункту ПКМ-меню: після «Переформатувати…»
        чи виправлення слова обидва мусять вирішувати однаково."""
        return _verbatim_differs(getattr(label, "_raw_text", None),
                                 getattr(label, "_final_text", ""))

    def _set_card_text(self, label: QLabel, new_text: str, words=None):
        """Єдина точка зміни тексту картки: перерендер + перерахунок усього, що
        від тексту залежить (видимість «Копіювати дослівно»). Через неї йдуть і
        «Переформатувати…», і виправлення слова у словник."""
        label._final_text = new_text
        if words is not None:
            label._words = words
        label.setText(self._render_fix_html(label, new_text))
        sync = getattr(label, "_sync_verbatim", None)
        if sync is not None:
            sync()

    def install_fix_menu(self, label: QLabel):
        """Підключити ПКМ-меню виправлення до QLabel-картки з розшифровкою."""
        label.setContextMenuPolicy(Qt.CustomContextMenu)
        label.customContextMenuRequested.connect(
            lambda pos, lbl=label: self._term_fix_menu(lbl, pos))

    def _term_fix_menu(self, label: QLabel, pos):
        # виділення читаємо ОДРАЗУ: ПКМ може скинути його до показу меню
        word = label.selectedText().strip()
        menu = QMenu(label)
        if word:
            fix = menu.addAction(tr("fixmenu_fix", word=word))
            fix.triggered.connect(lambda _=False: self._fix_word(label, word))
            if self._fix_menu_allow_ban:
                ban = menu.addAction(tr("fixmenu_ban", word=word))
                ban.triggered.connect(
                    lambda _=False: self.controller.profile.add_ignored([word]))
        else:
            hint = menu.addAction(tr("fixmenu_hint"))
            hint.setEnabled(False)
        menu.addSeparator()
        # feature/accuracy-corpus: позначити всю картку як погано розпізнану →
        # діалог із виправленням → зразок у локальний корпус. ts (диктування) або
        # _src_wav (аудіофайл) на мітці кажуть, звідки взяти аудіо-кліп.
        bad = menu.addAction(tr("corpus_menu_bad"))
        bad.triggered.connect(lambda _=False: self._report_corpus(label))
        copy = menu.addAction(tr("fixmenu_copy"))
        copy.triggered.connect(
            lambda _=False: self._copy_with_toast(label._final_text))
        # feature/processing-slider (спека §7): відновлення дослівного видиме
        # користувачу — показуємо «Копіювати дослівно» лише коли обробка справді
        # змінила текст (raw ≠ final), щоб не дублювати «Копіювати» без потреби.
        raw_text = getattr(label, "_raw_text", None)
        if self.verbatim_available(label):
            copy_raw = menu.addAction(tr("common_copy_verbatim"))
            copy_raw.triggered.connect(
                lambda _=False, r=raw_text: self._copy_with_toast(r))
        menu.exec(label.mapToGlobal(pos))

    def _report_corpus(self, label: QLabel):
        """«Розпізнано погано…» над карткою: відкрити діалог збирача корпусу."""
        from .corpus_dialog import report_bad
        report_bad(self, self.controller, label._final_text,
                   ts=getattr(label, "_ts", None),
                   src_wav=getattr(label, "_src_wav", None),
                   source=getattr(label, "_corpus_source", "desktop"))

    def _copy_with_toast(self, text: str):
        """Скопіювати в буфер + короткий in-window Toast (ТЗ п.5)."""
        QApplication.clipboard().setText(text)
        motion.toast(self, tr("toast_copied"))

    def _fix_word(self, label: QLabel, word: str):
        """Спитати правильну форму → у словник профілю → оновити картку."""
        canon, ok = QInputDialog.getText(
            self, tr("fixdlg_title"), tr("fixdlg_prompt", word=word), text=word)
        canon = canon.strip()
        if not ok or not canon or canon == word:
            return
        add_term(self.controller.profile.terms_path, canon, word)
        self.controller.reload_terms()
        # замінити слово в тексті картки (усі входження цілим словом)
        new_final = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)",
                           canon, label._final_text)
        self._set_card_text(label, new_final)
        win = getattr(self.controller, "window", None)
        if win is not None and getattr(win, "vocab", None) is not None:
            win.vocab.refresh()
        motion.toast(self, tr("toast_terms_fixed"))   # ТЗ п.5

    def _render_fix_html(self, label: QLabel, text: str) -> str:
        """Перерендер картки після виправлення слова / правки тексту з тією ж
        підсвіткою непевних слів, що й на побудові (за label._words). Спільний
        для Диктування й Файлів — getattr дає [] там, де картка слів не несе."""
        return render_uncertainty_html(text, getattr(label, "_words", []))


class DictationPage(TermFixMenuMixin, QWidget):
    """Стрічка транскрипцій + перемикач виводу."""

    _fix_menu_allow_ban = True   # у диктуванні є ще «більше не пропонувати»

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        raw_key = controller.cfg.ptt_key or "ctrl+shift+space"
        # F23 — це кнопка Copilot на нових клавіатурах; "бабусі" кажемо саме так.
        # Інакше — нейтрально за фактичною клавішею/комбінацією («Ctrl + Shift + Space»)
        self._key_label = self._format_key_label(raw_key)
        self._ptt_mode = controller.cfg.ptt_mode
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        # head-рядок: шапка сторінки + кнопки запису мишею (праворуч).
        # Стан кнопок синхронізує controller.rec_state — однаково для
        # запису з клавіші та з вікна.
        head = QHBoxLayout()
        head.setSpacing(16)
        header = page_header(tr("nav_dictation"), self._subtitle_text())
        self._subtitle = header.itemAt(1).widget()
        head.addLayout(header, stretch=1)
        # feature/voice-form-fill: заповнення шаблону голосом — окрема модалка,
        # відкривається з шапки (не новий пункт навігації, не в рядку «Куди текст»)
        self._formfill_btn = GlassButton(tr("formfill_open"))
        self._formfill_btn.setIcon(qta.icon("fa6s.file-lines", color=theme.TEXT_MUTED))
        theme.register_restyle_call(self._formfill_btn, lambda w: w.setIcon(  # нічний режим
            qta.icon("fa6s.file-lines", color=theme.TEXT_MUTED)))
        self._formfill_btn.setToolTip(tr("formfill_tip"))
        self._formfill_btn.setAccessibleName(tr("formfill_tip"))
        self._formfill_btn.clicked.connect(self._open_formfill)
        head.addWidget(self._formfill_btn, alignment=Qt.AlignVCenter)
        self._pause_btn = GlassButton(tr("common_pause"))
        self._pause_btn.clicked.connect(self._toggle_pause)
        self._pause_btn.hide()
        self._cancel_btn = GlassButton(tr("common_cancel"))
        self._cancel_btn.clicked.connect(controller.record_cancel)
        self._cancel_btn.hide()
        self._rec_btn = RecButton()
        start_tip = tr("dict_tip_start")
        self._rec_btn.setToolTip(start_tip)
        self._rec_btn.setAccessibleName(start_tip)
        self._rec_btn.clicked.connect(self._on_rec_clicked)
        for b in (self._pause_btn, self._cancel_btn, self._rec_btn):
            head.addWidget(b, alignment=Qt.AlignVCenter)
        self._rec_ui_state = "idle"   # локальне дзеркало rec_state
        self._paused = False
        controller.rec_state.connect(self._on_rec_state)
        root.addLayout(head)

        # Т47: рівень обробки тексту — компактний ЧІП біля кнопки запису
        # (замість смуги-слайдера на всю ширину). Клік → скляний попап зі
        # слайдером; вибір діє миттєво. Дефолт нових профілів — «Дослівно»
        # (пряма відповідь на скаргу «ШІ сам перефразовує»).
        self._processing = ProcessingChip(processing.DICTATION)
        self._processing.setToolTip(tr("proc_slider_dict_hint"))
        # feature/processing-slider (блокер №2): «З пунктуацією» капабіліті-гейтимо
        # РЕАЛЬНОЮ наявністю пунктуатора (спека §3 — обов'язковий компонент). Гейт
        # ставимо ДО відновлення збереженого режиму (committed=0 → без відкату) і ДО
        # під'єднання сигналів, щоб відновлення недоступної позиції не дало
        # ні тосту на старті, ні перезапису профілю на FILLERS. Оновлюється в
        # showEvent — коли компонент доставили пізніше через Налаштування.
        self._doc_ready = self._document_ready()
        self._processing.setDocumentAvailable(
            self._doc_ready, tr("proc_punct_unavailable"))
        self._processing.setMode(processing.profile_mode(
            getattr(controller, "profile", None), processing.DICTATION))
        self._processing.modeChanged.connect(self._on_processing_mode)
        self._processing.documentUnavailable.connect(self._on_document_unavailable)
        # Чіп стоїть у шапці, ліворуч від кластера пауза/скасувати/запис
        # (після кнопки заповнення шаблону): «біля кнопки запису», не смугою.
        head.insertWidget(2, self._processing, 0, Qt.AlignVCenter)

        # жива смужка рівня: провайдер lazy — recorder у контролері зʼявляється
        # пізніше за вікно. Хост-обгортка з верхнім відступом ховається цілком
        # (layout пропускає прихований віджет), тож у idle зайвого проміжку нема;
        # нижній addSpacing(20) лишає звичний відступ head→стрічка. Показ — лише
        # під час запису (_on_rec_state).
        self._level = LevelMeter(
            provider=lambda: self.controller.recorder.take_meter())
        self._level_host = QWidget()
        _lh = QVBoxLayout(self._level_host)
        _lh.setContentsMargins(0, 12, 0, 0)
        _lh.setSpacing(0)
        _lh.addWidget(self._level)
        self._level_host.hide()
        root.addWidget(self._level_host)
        # Компактне live-превʼю біля смужки рівня; FINAL звичайний, PARTIAL приглушений курсивом.
        self._live_preview = QFrame()
        self._live_preview.setProperty("glasspanel", True)
        live_lay = QVBoxLayout(self._live_preview)
        live_lay.setContentsMargins(14, 9, 14, 9)
        live_lay.setSpacing(3)
        self._live_final = QLabel()
        self._live_final.setWordWrap(True)
        self._live_partial = QLabel()
        self._live_partial.setProperty("muted", True)
        self._live_partial.setWordWrap(True)
        live_lay.addWidget(self._live_final)
        live_lay.addWidget(self._live_partial)
        self._live_lines = []
        self._live_preview.hide()
        root.addWidget(self._live_preview)
        root.addSpacing(20)

        self._feedbox = QVBoxLayout()
        self._feedbox.setSpacing(12)
        self._feedbox.addStretch()
        feedhost = QWidget()
        feedhost.setLayout(self._feedbox)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(feedhost)
        self._scroll = scroll

        # порожній стан ⇄ стрічка: перемикається у add_entry. Спільний
        # компонент EmptyState (аудит 31.07: один компонент, не шість копій)
        # — раніше Диктування будувало власну копію тим самим набором рядків.
        empty = EmptyState("fa6s.microphone", tr("dict_empty_title"),
                           tr("dict_empty_hint"))
        spaced(empty.hint_label, center=True)   # підказка диктування — просторіші рядки

        self._feed_stack = QStackedWidget()
        self._feed_stack.addWidget(empty)      # 0 — порожній стан
        self._feed_stack.addWidget(scroll)     # 1 — стрічка
        root.addWidget(self._feed_stack, stretch=1)
        root.addSpacing(14)

        # перемикач виводу: скляна панель. Підпис «Куди текст» + ⓘ — окремим
        # рядком над сегментованими кнопками, щоб три широкі режими вміщалися
        # повністю навіть на вузькому вікні (канон DESIGN-TYPOGRAPHY §3: без
        # обрізання підписів; в один ряд з підписом вони не влазять при ~1000px).
        panel = QFrame()
        panel.setProperty("glasspanel", True)
        outcol = QVBoxLayout(panel)
        outcol.setContentsMargins(16, 10, 16, 10)
        outcol.setSpacing(8)
        labrow = QHBoxLayout()
        labrow.setSpacing(6)
        outlbl = QLabel(tr("dict_output_label"))
        outlbl.setProperty("formlabel", True)
        labrow.addWidget(outlbl)
        # feature/text-rewrite: ⓘ саме тут, де контрол бачать уперше
        from .pages.settings import info_hint
        labrow.addWidget(info_hint("hint_output_live"))
        labrow.addStretch()
        outcol.addLayout(labrow)
        outrow = QHBoxLayout()
        outrow.setSpacing(10)
        self._modes = QButtonGroup(self)
        for mode, label in (("paste", tr("out_paste_here")),
                            ("show", tr("out_show_window")),
                            ("both", tr("out_both"))):
            seg = GlassButton(label)
            seg.setCheckable(True)
            seg.setProperty("mode", mode)
            seg.setChecked(mode == controller.output_mode)
            self._modes.addButton(seg)
            outrow.addWidget(seg)
            # Т49: ⓘ саме біля опції «Вставляти під курсор» — коротке пояснення,
            # куди потрапить текст (той самий ⓘ-патерн, що перефарбовується вночі)
            if mode == "paste":
                outrow.addWidget(info_hint("hint_paste_here"),
                                 alignment=Qt.AlignVCenter)
        self._modes.buttonClicked.connect(
            lambda b: setattr(self.controller, "output_mode", b.property("mode")))
        outrow.addStretch()
        outcol.addLayout(outrow)
        root.addWidget(panel)
        self._formfill_dialog = None

    def _open_formfill(self):
        """Відкрити модалку заповнення шаблоном (створюємо лениво, перевикор.)."""
        from .formfill_dialog import FormFillDialog
        if self._formfill_dialog is None:
            self._formfill_dialog = FormFillDialog(self.controller, self)
        else:
            self._formfill_dialog._reload_templates()   # підхопити нові файли
        self._formfill_dialog.show()
        self._formfill_dialog.raise_()
        self._formfill_dialog.activateWindow()

    @staticmethod
    def _format_key_label(raw_key: str) -> str:
        return (tr("dict_key_copilot") if raw_key.lower() == "f23"
                else tr("dict_key_named", key=pretty(raw_key)))

    def _subtitle_text(self) -> str:
        key = {"toggle": "dict_subtitle_toggle",
               "double_tap": "dict_subtitle_double"}.get(
                   self._ptt_mode, "dict_subtitle_hold")
        return tr(key, key_label=self._key_label)

    def _on_processing_mode(self, mode: str):
        # feature/processing-slider: діє з наступного запису (знімок на старті).
        self.controller.profile.set_processing_mode(processing.DICTATION, mode)

    def _on_document_unavailable(self, reason: str):
        motion.toast(self.window() or self, reason or tr("proc_punct_unavailable"))

    def _document_ready(self) -> bool:
        """feature/processing-slider (блокер №2): чи готовий компонент для
        «З пунктуацією». Спека §3 вимагає пунктуатор (автокорекція — «коли встановлено»,
        не обов'язкова), тож гейтимо саме за ним. Перевірка легка (find_spec +
        маркер файлу), без важких імпортів; сама available() ковтає OSError/ImportError."""
        return punctuator.available(punctuator_model_dir())

    def _refresh_document_availability(self):
        """Перечитати наявність пунктуатора — компонент могли доставити в
        Налаштуваннях. Коли він щойно став доступний, відновлюємо збережений режим
        профілю (міг бути «З пунктуацією», якого гейт раніше не давав закомітити)."""
        ready = self._document_ready()
        became_ready = ready and not self._doc_ready
        self._doc_ready = ready
        self._processing.setDocumentAvailable(ready, tr("proc_punct_unavailable"))
        if became_ready:
            self._processing.setMode(processing.profile_mode(
                getattr(self.controller, "profile", None), processing.DICTATION))

    def refresh_processing_mode(self):
        """Синхронізувати повзунок із активним профілем БЕЗ запису (спека §5:
        switch_profile оновлює контрол без емісії)."""
        self._processing.setMode(processing.profile_mode(
            getattr(self.controller, "profile", None), processing.DICTATION))

    def set_ptt_mode(self, mode: str) -> None:
        self._ptt_mode = mode
        self._subtitle.setText(self._subtitle_text())

    def set_shortcut(self, raw_key: str) -> None:
        self._key_label = self._format_key_label(raw_key)
        self._subtitle.setText(self._subtitle_text())

    def showEvent(self, event):
        """Синхронізувати перемикач виводу — режим могли змінити в Налаштуваннях."""
        super().showEvent(event)
        for rb in self._modes.buttons():
            rb.blockSignals(True)
            rb.setChecked(rb.property("mode") == self.controller.output_mode)
            rb.blockSignals(False)
        self._refresh_document_availability()

    # --- запис мишею: кругла кнопка + пауза/скасувати ---
    def _on_rec_clicked(self):
        if self._rec_ui_state == "recording":
            self.controller.record_stop()
        elif self._rec_ui_state == "idle":
            self.controller.record_start()

    def _toggle_pause(self):
        self._paused = not self._paused
        self.controller.record_pause(self._paused)
        self._pause_btn.setText(tr("common_resume") if self._paused
                                else tr("common_pause"))

    def _on_rec_state(self, state: str):
        """Слот rec_state: кнопки віддзеркалюють стан запису (клавіша чи миша)."""
        self._rec_ui_state = state
        recording = state == "recording"
        if not recording:
            self._paused = False
            self._pause_btn.setText(tr("common_pause"))
        # смужка рівня: видима + таймер лише під час запису (idle/busy — сховати)
        self._level_host.setVisible(recording)
        self._live_preview.setVisible(recording and bool(getattr(self.controller.cfg, "live_transcription", False)))
        if recording:
            self._live_lines = []
            self._live_final.clear()
            self._live_partial.clear()
        self._level.set_active(recording)
        self._pause_btn.setVisible(recording)
        self._cancel_btn.setVisible(recording)
        self._rec_btn.set_recording(recording)
        self._rec_btn.setEnabled(state != "busy")
        tip = {"recording": tr("dict_tip_stop"),
               "busy": tr("dict_tip_busy")}.get(state, tr("dict_tip_start"))
        self._rec_btn.setToolTip(tip)
        self._rec_btn.setAccessibleName(tip)

    def add_live_segment(self, segment):
        """GUI-slot для LiveSegment; FINAL лишається, PARTIAL замінює хвіст."""
        if not getattr(self.controller.cfg, "live_transcription", False):
            return
        if segment.is_final:
            self._live_lines.append(segment.text)
            self._live_partial.clear()
            self._live_final.setText(" ".join(self._live_lines[-3:]))
        else:
            self._live_partial.setText(f"<i>{html.escape(segment.text)}</i>")
        self._live_preview.show()
    def sync_animations(self):
        """Застосувати живий animation toggle до REC-кнопки."""
        self._rec_btn.sync_animations()

    @classmethod
    def _render_html(cls, final: str, words) -> str:
        """Тонкий делегат до модульного render_uncertainty_html (спільний із
        картками файлів). Лишаємо як метод — сумісність із наявними викликами
        self._render_html / DictationPage._render_html у коді й тестах."""
        return render_uncertainty_html(final, words)

    def add_entry(self, raw: str, final: str, words=None, ts=None):
        """Додати картку транскрипції (викликати ЛИШЕ з GUI-потоку — через сигнал).
        words: [(слово, ймовірність), ...] від рушія — для підсвітки непевних.
        ts: секунда запису в history.jsonl (round(time.time())) або None, якщо
        пам'ять вимкнена і рядка у файлі нема — тоді видалення лише прибирає картку."""
        self._feed_stack.setCurrentIndex(1)   # перша картка ховає порожній стан
        card = QFrame()
        card.setProperty("card", True)
        motion.lift_on_hover(card)   # наведення «підйом + тінь» на картку-плитку
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 15)
        lay.setSpacing(5)
        meta = time.strftime("%H:%M")
        if _verbatim_differs(raw, final):
            meta += tr("dict_meta_fixed")
        m = QLabel(meta)
        m.setProperty("muted", True)
        # meta-рядок: час ліворуч, кнопка видалення праворуч (muted → червона на hover)
        metarow = QHBoxLayout()
        metarow.setContentsMargins(0, 0, 0, 0)
        metarow.setSpacing(6)
        metarow.addWidget(m)
        metarow.addStretch()
        # ─── ОКРЕМИЙ ряд дій під текстом картки ───
        # Дії НЕ в мета-рядку свідомо. Мета-підпис «виправлено написання деяких
        # слів» сам займає ~247 точок; разом із трьома підписаними кнопками ряд
        # просив 776 точок, а картка на мінімумі вікна (1000) дає 683 — Qt
        # стискав кнопки нижче minimumSizeHint і різав НЕ ЛИШЕ нову «Копіювати
        # дослівно», а й здорову «Переформатувати…» (знахідка рецензії 25.07).
        # Окремий ряд просить 495 точок і вміщається з запасом на найвужчому
        # вікні в обох мовах — без скорочення підписів і без ховання дій у меню
        # «…» (обидві дії часті: копіювання — головний сценарій картки).
        # Той самий порядок «текст → ряд дій», що на сторінці Історії.
        actionrow = QHBoxLayout()
        actionrow.setContentsMargins(0, 0, 0, 0)
        actionrow.setSpacing(6)
        actionrow.addStretch()
        # feature/copy-on-card: «Копіювати» просто в картці — раніше текст
        # доводилось виділяти мишею. Ліворуч від «Переформатувати…».
        copy_btn = GlassButton(tr("common_copy"))
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setToolTip(tr("dict_card_copy_tip"))
        copy_btn.setAccessibleName(tr("common_copy"))
        actionrow.addWidget(copy_btn, alignment=Qt.AlignVCenter)
        # той самий поділ, що на сторінці Історії: окрема кнопка для незайманого
        # raw — лише коли обробка (чистка/пунктуація) змінила вивід. Видимість
        # НЕ вирішується раз на побудові: перераховуємо через _sync_verbatim тією
        # ж умовою, що й пункт ПКМ-меню (інакше після «Переформатувати…» кнопка й
        # меню на ОДНІЙ картці розходились би).
        copy_raw_btn = GlassButton(tr("common_copy_verbatim"))
        copy_raw_btn.setCursor(Qt.PointingHandCursor)
        copy_raw_btn.setToolTip(tr("dict_card_copy_verbatim_tip"))
        copy_raw_btn.setAccessibleName(tr("common_copy_verbatim"))
        actionrow.addWidget(copy_raw_btn, alignment=Qt.AlignVCenter)
        # feature/output-formats: «Переформатувати…» — переписати текст картки
        # локальним AI (лист/задачі/рапорт/стисло). Плоска кнопка-посилання праворуч.
        rewrite_btn = GlassButton(tr("rewrite_card_btn"))
        rewrite_btn.setCursor(Qt.PointingHandCursor)
        rewrite_btn.setToolTip(tr("rewrite_card_btn_tip"))
        rewrite_btn.setAccessibleName(tr("rewrite_card_btn"))
        actionrow.addWidget(rewrite_btn, alignment=Qt.AlignVCenter)
        # ✕: TipToolButton — гарантований тултип на hover (той самий надійний
        # механізм, що ⓘ у Налаштуваннях), а не ненадійний штатний setToolTip
        del_btn = TipToolButton(tr("dict_card_delete"))

        def _style_del(w):           # нічний режим: перечитати іконку/фокус-рамку
            w.setIcon(qta.icon("fa6s.xmark", color=theme.TEXT_MUTED,
                               color_active=theme.ALERT))
            w.setStyleSheet(
                "QToolButton { border: 2px solid transparent; background: transparent; }"
                f"QToolButton:focus {{ border-color: {theme.FOCUS}; border-radius: 4px; }}")

        _style_del(del_btn)
        theme.register_restyle_call(del_btn, _style_del)
        del_btn.setIconSize(QSize(13, 13))
        del_btn.setFixedSize(24, 24)   # мін. 24×24 для клікабельних цілей (WCAG 2.5.8)
        del_btn.setAutoRaise(True)
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setFocusPolicy(Qt.StrongFocus)
        metarow.addWidget(del_btn, alignment=Qt.AlignVCenter)
        lay.addLayout(metarow)
        text = QLabel()
        text.setTextFormat(Qt.RichText)
        text.setText(self._render_html(final, words))
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text._final_text = final          # чистий текст (без HTML) — для копіювання
        text._raw_text = raw              # незайманий розпізнаний текст — «Копіювати дослівно» (§7)
        text._words = words or []         # для перерендеру після виправлення
        text._ts = ts                     # feature/accuracy-corpus: ключ аудіо-буфера

        def _sync_verbatim(lbl=text, btn=copy_raw_btn):
            btn.setVisible(self.verbatim_available(lbl))

        text._sync_verbatim = _sync_verbatim
        _sync_verbatim()
        self.install_fix_menu(text)       # ПКМ → «виправити»/«скопіювати» (міксин)

        # нічний режим: HTML картки несе вшитий колір підсвітки (GOLD_EYEBROW) з
        # моменту побудови — жива зміна теми рядок не перебудовує. Тож картка,
        # зроблена вдень, лишила б золоту підсвітку непевних слів після свопу в ніч.
        # Перемальовуємо HTML із поточної палітри (вимога «жодного не-червоного світла»).
        def _restyle_text(lbl):
            lbl.setText(self._render_html(lbl._final_text, lbl._words))

        theme.register_restyle_call(text, _restyle_text)
        lay.addWidget(text)
        lay.addLayout(actionrow)
        # вставляємо перед stretch-елементом (нове — внизу стрічки);
        # wrap_appear — анімація появи (fade + відступ), ТЗ п.1. При увімкнених
        # анімаціях повертає ХОСТ-обгортку навколо card — саме її треба знімати
        # з layout при видаленні (не card), тож захоплюємо в змінну.
        wrap = motion.wrap_appear(card)
        self._feedbox.insertWidget(self._feedbox.count() - 1, wrap)
        del_btn.clicked.connect(
            lambda _=False: self._delete_entry(wrap, ts, final))
        rewrite_btn.clicked.connect(
            lambda _=False, lbl=text: self._open_rewrite(lbl))
        # читаємо _final_text у момент кліку — після «Переформатувати…» картка
        # несе вже новий текст, копіювати треба його.
        copy_btn.clicked.connect(
            lambda _=False, lbl=text: self._copy_with_toast(lbl._final_text))
        copy_raw_btn.clicked.connect(
            lambda _=False, lbl=text: self._copy_with_toast(lbl._raw_text))
        motion.smooth_scroll_to_end(self._scroll)   # ТЗ п.11: плавний докрут

    def _open_rewrite(self, label: QLabel):
        """feature/output-formats: «Переформатувати…» текст картки локальним AI.
        Проактивно перевіряємо бекенд (як Q&A/протокол): без llama-cpp-python —
        чесне «встановіть компонент», а не заглушка. На «Застосувати» оновлюємо
        текст картки й кладемо результат у буфер обміну."""
        from whisper_core.protocol import service
        if not service.backend_available():
            QMessageBox.warning(self, tr("rewrite_dialog_title"),
                                tr("protocol_backend_missing"))
            return
        text = getattr(label, "_final_text", "") or ""
        if not text.strip():
            return
        preset_id = getattr(self.controller.cfg, "protocol_model", "fast")

        def _apply(new_text: str):
            self._set_card_text(label, new_text, words=[])
            QApplication.clipboard().setText(new_text)

        from .pages.protocol_ui import RewriteDialog
        from whisper_core import config as cfgmod
        RewriteDialog(text, preset_id, on_apply=_apply, parent=self,
                      custom_models=cfgmod.protocol_custom_models(self.controller.cfg)).exec()

    def _delete_entry(self, wrap: QWidget, ts, final: str):
        """Прибрати картку зі стрічки і — якщо запис збережено — рядок з history.jsonl.
        ts=None → у файлі рядка нема (пам'ять була вимкнена), лише прибрати картку."""
        self._feedbox.removeWidget(wrap)
        wrap.deleteLater()
        if ts is not None:
            profile = self.controller.profile
            for line, rec in history.read_recent(profile):
                if (rec.get("ts") == ts and rec.get("final") == final
                        and rec.get("source") == "desktop"):
                    history.delete_line(profile.history_path, line)
                    break
        # лишився тільки stretch → повернути порожній стан
        if self._feedbox.count() == 1:
            self._feed_stack.setCurrentIndex(0)


class FilesPage(TermFixMenuMixin, QWidget):
    """Транскрипція файлів: drag&drop / вибір → черга → текст із діями."""

    AUDIO_EXT = AUDIO_EXT   # feature/watch-folder: спільна константа з watch.py

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setAcceptDrops(getattr(controller, "has_model", True))
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        root.addLayout(page_header(tr("nav_audio"), tr("files_subtitle")))
        root.addSpacing(20)

        # feature/no-model-state: мовний пакет розпізнавання ще не завантажено —
        # чесний банер зверху; сам вибір/перетягування файлів вимикаємо (нема
        # ким розшифровувати). Стан незмінний до перезапуску (feature/no-model-
        # state точка 4), тож перевірка одноразова, при побудові сторінки.
        self._no_model = not getattr(controller, "has_model", True)
        if self._no_model:
            banner = QLabel(tr("files_no_model_banner"))
            banner.setProperty("muted", True)
            banner.setWordWrap(True)
            root.addWidget(banner)
            root.addSpacing(14)

        dz = QFrame()
        dz.setProperty("dropzone", True)
        self._dz = dz            # адресат drag-реакції (motion.drag_glow)
        dzl = QVBoxLayout(dz)
        dzl.setContentsMargins(24, 22, 24, 22)
        dzl.setSpacing(12)
        ico = QLabel()
        ico.setPixmap(qta.icon("fa6s.file-audio", color=theme.IDLE).pixmap(30, 30))
        ico.setAlignment(Qt.AlignCenter)
        theme.register_restyle_call(ico, lambda w: w.setPixmap(   # нічний режим
            qta.icon("fa6s.file-audio", color=theme.IDLE).pixmap(30, 30)))
        t = QLabel(tr("files_drop"))
        t.setAlignment(Qt.AlignCenter)
        t.setProperty("muted", True)
        spaced(t, center=True)           # підказка файлів — просторіші рядки
        pick = GlassButton(tr("files_choose"))
        pick.clicked.connect(self._pick)
        pick.setEnabled(not self._no_model)
        dzl.addWidget(ico)
        dzl.addWidget(t)
        dzl.addWidget(pick, alignment=Qt.AlignCenter)
        root.addWidget(dz)
        root.addSpacing(18)

        # диктофон: простий запис БЕЗ негайної розшифровки (feature/player-recordings).
        # Місце обрано тут, бо збережений запис лягає у ТУ САМУ чергу файлів нижче:
        # «Записати → відтворити → Транскрибувати» — природний потік вкладки.
        root.addWidget(self._build_dictaphone_panel())
        root.addSpacing(18)

        self._qbox = QVBoxLayout()
        self._queue_empty = EmptyState("fa6s.file-audio", tr("files_queue_empty_title"),
                                       tr("files_queue_empty_hint"),
                                       button_text=tr("files_choose"), on_click=self._pick)
        self._queue_empty.button.setEnabled(not self._no_model)
        root.addWidget(self._queue_empty)

        self._qbox.setSpacing(12)
        self._qbox.addStretch()
        host = QWidget()
        host.setLayout(self._qbox)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        root.addWidget(scroll, stretch=1)

        self._rows = {}  # job_id -> (status_label, path, row_frame)
        controller.file_status.connect(self._on_status)
        controller.file_done.connect(self._on_done)

        # диктофон: збережені записи — окремі картки на початку стрічки; плеєри
        # тримаємо, щоб спиняти при ховані/оновленні (feature/player-recordings)
        self._rec_cards = []             # QFrame записів
        self._rec_players = []           # InlinePlayer записів
        self._dicta_recording = False

    # === диктофон (feature/player-recordings) ==============================
    def _build_dictaphone_panel(self) -> QFrame:
        panel = QFrame()
        panel.setProperty("glasspanel", True)
        pl = QVBoxLayout(panel)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(12)
        lbl = QLabel(tr("rec_label"))
        lbl.setProperty("formlabel", True)
        top.addWidget(lbl)
        self._dicta_timer_lbl = QLabel("0:00")
        self._dicta_timer_lbl.setProperty("kbd", True)
        self._dicta_timer_lbl.hide()
        top.addWidget(self._dicta_timer_lbl)
        top.addStretch()
        self._dicta_cancel = GlassButton(tr("common_cancel"))
        self._dicta_cancel.clicked.connect(self._dicta_cancel_clicked)
        self._dicta_cancel.hide()
        top.addWidget(self._dicta_cancel)
        open_folder = GlassButton(tr("rec_open_folder"))
        open_folder.clicked.connect(self.controller.open_recordings_folder)
        top.addWidget(open_folder)
        self._dicta_btn = RecButton()
        self._dicta_btn.setToolTip(tr("rec_start"))
        self._dicta_btn.setAccessibleName(tr("rec_start"))
        self._dicta_btn.clicked.connect(self._dicta_toggle)
        top.addWidget(self._dicta_btn, alignment=Qt.AlignVCenter)
        pl.addLayout(top)

        self._dicta_level = LevelMeter(
            provider=lambda: self.controller.dictaphone_level())
        self._dicta_level_host = QWidget()
        _dlh = QVBoxLayout(self._dicta_level_host)
        _dlh.setContentsMargins(0, 2, 0, 2)
        _dlh.setSpacing(0)
        _dlh.addWidget(self._dicta_level)
        self._dicta_level_host.hide()
        pl.addWidget(self._dicta_level_host)

        self._dicta_tick = QTimer(self)
        self._dicta_tick.setInterval(1000)
        self._dicta_tick.timeout.connect(self._dicta_update_timer)
        return panel

    def _dicta_toggle(self):
        if self._dicta_recording:
            self.controller.dictaphone_stop()
            self._dicta_set_recording(False)
            self.refresh_recordings()    # нова картка (якщо збережено) — на початку
        else:
            if self.controller.dictaphone_start():
                self._dicta_set_recording(True)

    def _dicta_cancel_clicked(self):
        self.controller.dictaphone_cancel()
        self._dicta_set_recording(False)

    def _dicta_set_recording(self, on: bool):
        self._dicta_recording = on
        self._dicta_btn.set_recording(on)
        self._dicta_level_host.setVisible(on)
        self._dicta_level.set_active(on)
        self._dicta_timer_lbl.setVisible(on)
        self._dicta_cancel.setVisible(on)
        tip = tr("rec_stop") if on else tr("rec_start")
        self._dicta_btn.setToolTip(tip)
        self._dicta_btn.setAccessibleName(tip)
        if on:
            self._dicta_update_timer()
            self._dicta_tick.start()
        else:
            self._dicta_tick.stop()

    def _dicta_update_timer(self):
        elapsed = int(time.time() - self.controller._dictaphone_started)
        self._dicta_timer_lbl.setText(f"{elapsed // 60}:{elapsed % 60:02d}")

    def _sync_dictaphone_ui(self):
        """Стан запису живе у контролері (переживає перемикання вкладок). При
        показі сторінки підхоплюємо його, щоб UI не розійшовся з реальністю."""
        active = getattr(self.controller, "_dictaphone_active", False)
        if active != self._dicta_recording:
            self._dicta_set_recording(active)

    # --- стрічка збережених записів ---
    def _stop_rec_players(self):
        for pl in self._rec_players:
            try:
                pl.stop()
            except RuntimeError:
                pass
        self._rec_players = []

    def refresh_recordings(self):
        """Перебудувати картки збережених записів (початок стрічки), не чіпаючи
        рядки черги транскрипції."""
        self._stop_rec_players()
        for card in self._rec_cards:
            self._qbox.removeWidget(card)
            card.deleteLater()
        self._rec_cards = []
        try:
            recs = self.controller.list_recordings()
        except Exception:
            recs = []
        for rec in reversed(recs):       # insert(0) → найновіші зверху
            self._add_recording_card(rec)

    def _add_recording_card(self, rec):
        self._queue_empty.setVisible(False)   # додали картку → не порожньо
        card = QFrame()
        card.setProperty("card", True)
        motion.lift_on_hover(card)   # наведення «підйом + тінь» на картку-плитку
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 15)
        lay.setSpacing(8)

        top = QHBoxLayout()
        title = QLabel(self._recording_title(rec))
        title.setProperty("strong", True)
        top.addWidget(title, stretch=1)
        dur = QLabel(fmt_time(int(rec.duration * 1000)))
        dur.setProperty("muted", True)
        top.addWidget(dur)
        lay.addLayout(top)

        player = InlinePlayer(rec.path)
        self._rec_players.append(player)
        lay.addWidget(player)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        edit = GlassButton(tr("audioedit_open"))
        panel = {"widget": None}

        def _edit(_=False, p=rec.path, pl=player):
            if panel["widget"] is None:
                try:
                    panel["widget"] = AudioEditorPanel(p, pl, self.controller, card)
                    lay.addWidget(panel["widget"])
                except Exception as exc:
                    logging.exception("Не вдалося відкрити редактор аудіо: %s", p)
                    motion.toast(self, str(exc))
                    return
            panel["widget"].setVisible(not panel["widget"].isVisible())

        edit.clicked.connect(_edit)
        btns.addWidget(edit)
        transcribe = GlassButton(tr("rec_transcribe"))

        def _transcribe(_=False, b=transcribe, p=rec.path):
            b.setEnabled(False)          # подвійний клік ≠ дублікат у черзі
            self.controller.transcribe_recording(p)

        transcribe.clicked.connect(_transcribe)
        btns.addWidget(transcribe)
        btns.addStretch()
        delete = GlassButton(tr("rec_delete"))
        delete.setProperty("ghost", True)
        delete.clicked.connect(
            lambda _=False, n=rec.name: self._confirm_delete_recording(n))
        btns.addWidget(delete)
        lay.addLayout(btns)

        self._qbox.insertWidget(0, card)
        self._rec_cards.insert(0, card)

    @staticmethod
    def _recording_title(rec) -> str:
        # ім'я "2026-07-16_14-30-05.wav" → "16.07.2026 14:30"
        stem = rec.name[:-4] if rec.name.endswith(".wav") else rec.name
        try:
            d, t = stem.split("_")
            y, mo, day = d.split("-")
            hh, mm = t.split("-")[0], t.split("-")[1]
            return f"{day}.{mo}.{y} {hh}:{mm}"
        except (ValueError, IndexError):
            return stem

    def _confirm_delete_recording(self, name):
        resp = QMessageBox.question(
            self, tr("rec_delete"), tr("rec_delete_confirm"))
        if resp == QMessageBox.Yes:
            # СПЕРШУ спинити плеєри (stop звільняє файловий хендл ffmpeg-бекенда,
            # інакше unlink на Windows падає з WinError 32), ПОТІМ видаляти
            self._stop_rec_players()
            if not self.controller.delete_recording(name):
                motion.toast(self, tr("rec_delete_fail"))
            self.refresh_recordings()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_dictaphone_ui()
        self.refresh_recordings()

    def hideEvent(self, event):
        self._stop_rec_players()
        super().hideEvent(event)

    # --- drag&drop ---
    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if any(Path(u.toLocalFile()).suffix.lower() in self.AUDIO_EXT for u in urls):
            event.acceptProposedAction()
            motion.drag_glow(self._dz, True)     # золота реакція dropzone (ТЗ п.10)

    def dragLeaveEvent(self, event):
        motion.drag_glow(self._dz, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        motion.drag_glow(self._dz, False)
        paths = [u.toLocalFile() for u in event.mimeData().urls()
                 if Path(u.toLocalFile()).suffix.lower() in self.AUDIO_EXT]
        self.add_files(paths)

    def _pick(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("files_dialog_title"), "", tr("files_filter"))
        self.add_files(paths)

    # --- статус-пілюля справа у рядку черги (StatusTag «Жива кромка») ---
    @staticmethod
    def _set_badge(tag: StatusTag, kind: str, text: str):
        """kind: queued | busy | done | error. Спінер/німб/морф — усередині StatusTag."""
        tag.set_state(kind, text)

    # --- черга ---
    def add_files(self, paths, model=None):
        """Поставити файли в чергу. model — feature/qol-pack: повторна
        розшифровка ІНШОЮ моделлю (новий рядок/новий запис в історії; старий
        результат не перезаписується)."""
        if paths and getattr(self.controller, "has_model", True) is False:
            self.controller.tray.notify(tr("files_no_model_banner"))
            return
        if paths:
            self._queue_empty.hide()   # у черзі з'явились файли → прибрати порожній стан
        for p in paths:
            p = Path(p)
            jid = self.controller.enqueue_file(p, model=model)
            row = QFrame()
            row.setProperty("card", True)
            motion.lift_on_hover(row)   # наведення «підйом + тінь» на картку-плитку
            rl = QVBoxLayout(row)
            rl.setContentsMargins(18, 13, 18, 15)
            rl.setSpacing(8)
            top = QHBoxLayout()
            name = ElidedLabel(p.name)
            name.setProperty("strong", True)
            status = StatusTag("queued", tr("badge_queued"))
            top.addWidget(name, stretch=1)
            top.addWidget(status)
            rl.addLayout(top)
            # fix/cancel-transcription: «Скасувати» живе від постановки в чергу
            # (передумала на десятому файлі — знімає його ще до старту) і до
            # завершення задачі. Смужка ходу — лише в стані розпізнавання:
            # невизначена (0,0), бо рушій не дає відсотків, тож вона показує
            # «працюю», а не вигаданий прогрес.
            prog_box = QWidget()
            pb = QHBoxLayout(prog_box)
            pb.setContentsMargins(0, 0, 0, 0)
            pb.setSpacing(12)
            bar = QProgressBar()
            bar.setRange(0, 0)
            bar.setTextVisible(False)
            bar.setAccessibleName(tr("badge_transcribing"))
            bar.hide()               # у черзі роботи ще немає — смужки теж
            cancel = GlassButton(tr("files_cancel"))
            cancel.setAccessibleName(tr("files_cancel"))
            cancel.setToolTip(tr("files_cancel_hint"))
            cancel.clicked.connect(
                lambda _=False, j=jid: self._cancel_job(j))
            pb.addWidget(bar, stretch=1)
            pb.addWidget(cancel)
            rl.addWidget(prog_box)
            row._progress_box = prog_box
            row._progress_bar = bar
            row._cancel_btn = cancel
            self._qbox.insertWidget(self._qbox.count() - 1,
                                    motion.wrap_appear(row))
            self._rows[jid] = (status, p, row)

    def _cancel_job(self, jid):
        """Кнопка «Скасувати»: просимо контролер зняти задачу — і ту, що вже
        розпізнається, і ту, що лише чекає в черзі. Кнопку гасимо одразу
        (повторний клік не має сенсу), а підпис стану ставить _on_done — рівно
        тоді, коли робота справді спинилась."""
        row = self._rows[jid][2] if jid in self._rows else None
        if row is not None:
            row._cancel_btn.setEnabled(False)
        self.controller.cancel_file_job(jid)

    @staticmethod
    def _show_progress(row, visible: bool):
        """Смужка ходу показується ЛИШЕ під час розпізнавання (у черзі роботи
        ще немає). «Скасувати» від неї не залежить — воно доступне й у черзі."""
        bar = getattr(row, "_progress_bar", None)
        if bar is not None:
            bar.setVisible(visible)

    @staticmethod
    def _hide_job_controls(row):
        """Задача закрита (готово/помилка/скасовано) — керування задачею зникає
        разом зі смужкою: скасовувати вже нічого."""
        box = getattr(row, "_progress_box", None)
        if box is not None:
            box.hide()

    def _on_status(self, jid, code):
        if jid not in self._rows:
            return
        status, _path, row = self._rows[jid]
        if code == FileStatus.TRANSCRIBING:
            self._set_badge(status, "busy", tr("badge_transcribing"))
            self._show_progress(row, True)
        else:
            self._set_badge(status, "queued", tr("badge_queued"))
            self._show_progress(row, False)

    def _on_done(self, jid, text, meta, segments=None, words=None):
        # feature/model-bottlenecks (під-хвиля 2): words — пари (слово, ймовірність)
        # від рушія для підсвітки непевних слів у картці файлу (тією самою золотою
        # позначкою, що й у стрічці диктування). Порожньо → без підсвітки.
        if jid not in self._rows:
            return
        status, path, row = self._rows[jid]
        # feature/edit-pack: тримаємо сегменти як список — імпорт субтитрів оновлює
        # їх на місці (export-лямбди й меню бачать той самий обʼєкт).
        segments = list(segments) if segments else []
        # Робота задачі скінчилась у будь-якому разі → смужка ходу й «Скасувати»
        # зникають. Картка НЕ лишається в «розпізнаю».
        self._hide_job_controls(row)
        # fix/cancel-transcription: скасовано — окрема гілка ПЕРЕД усім іншим.
        # Тексту немає, тож ні огляду змін, ні експортів: лише зрозумілий стан і
        # «Повторити» тією самою інфраструктурою, що й після помилки.
        if meta == FileStatus.CANCELLED:
            self._set_badge(status, "warn", tr("badge_cancelled"))
            note = QLabel(text or tr("files_cancelled_body"))
            note.setProperty("muted", True)
            note.setWordWrap(True)
            row.layout().addWidget(note)
            cbtns = QHBoxLayout()
            cbtns.setSpacing(10)
            again = GlassButton(tr("files_retry"))
            again.setAccessibleName(tr("files_retry"))
            again.clicked.connect(lambda _=False, p=path: self.add_files([p]))
            cbtns.addWidget(again)
            cbtns.addStretch()
            row.layout().addLayout(cbtns)
            motion.expand_height(row)
            return
        # meta — КОД стану (FileStatus), не перекладений текст: порівнюємо код,
        # а бейдж показуємо через tr. Успіх приходить як "done:<секунди аудіо>".
        is_error = meta == FileStatus.ERROR
        # feature/player-pack: «Огляд перед дією» — ЛИШЕ ручна розшифровка файлу
        # (не диктування). Коли ввімкнено й філери/автокорекція щось міняють —
        # показати diff і дати обрати «Застосувати/Лишити як було» ДО побудови
        # картки, тож картка вже несе підсумковий текст. Прийняте пишемо в історію
        # тим самим шляхом, що й ручна правка (update_file_transcript).
        if not is_error and getattr(self.controller.cfg, "review_text_changes", False):
            cleaned = self.controller.clean_transcript_text(text)
            if cleaned and cleaned != text:
                from .diff_review import DiffReviewDialog
                chosen = DiffReviewDialog.review(self, text, cleaned)
                if chosen != text:
                    self.controller.update_file_transcript(text, chosen)
                    text = chosen
        if is_error:
            self._set_badge(status, "error", tr("badge_error"))
        elif meta.startswith(FileStatus.DONE + ":"):
            sec = meta.split(":", 1)[1]
            self._set_badge(status, "done", tr("files_done_dur", sec=sec))
        else:
            self._set_badge(status, "done", tr("badge_done"))
        body = QLabel()
        body.setTextFormat(Qt.RichText)
        body._words = words or []         # для підсвітки й перерендеру після _fix_word
        body.setText(render_uncertainty_html(text, body._words))
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body._final_text = text           # для копіювання й заміни у _fix_word
        body._src_wav = path              # feature/accuracy-corpus: джерело аудіо-кліпу
        body._corpus_source = "file"
        # нічний режим: HTML несе вшитий колір підсвітки (GOLD_EYEBROW) з моменту
        # побудови — перемальовуємо з поточної палітри при зміні теми (як у стрічці).
        theme.register_restyle_call(
            body, lambda lbl: lbl.setText(
                render_uncertainty_html(lbl._final_text, getattr(lbl, "_words", []))))
        row.layout().addWidget(body)
        # Помилкова картка несе лише текст помилки: дії «Копіювати/Зберегти…» та
        # ПКМ-словник над ним безглузді (нема що зберігати чи виправляти). Даємо
        # тільки «Повторити» — ставить файл у чергу заново тією ж інфраструктурою,
        # що й перше додавання (стара картка лишається слідом невдалої спроби).
        if is_error:
            ebtns = QHBoxLayout()
            ebtns.setSpacing(10)
            retry = GlassButton(tr("files_retry"))
            retry.clicked.connect(lambda _=False, p=path: self.add_files([p]))
            ebtns.addWidget(retry)
            ebtns.addStretch()
            row.layout().addLayout(ebtns)
            motion.expand_height(row)     # ТЗ п.7: плавне розгортання результату
            return
        self.install_fix_menu(body)       # ПКМ → «виправити в словник» (міксин)
        btns = QHBoxLayout()
        btns.setSpacing(10)
        copy = GlassButton(tr("common_copy"))
        # читаємо body._final_text у момент КЛІКУ (а не text на побудові): після
        # виправлення слова в словник _fix_word оновлює саме body._final_text
        copy.clicked.connect(
            lambda _=False, b=body: self._copy_with_toast(b._final_text))
        save = GlassButton(tr("files_save_txt"))

        # підпис-стан збереження (як «готово» — з'являється після дії)
        saved_lbl = QLabel()
        saved_lbl.setProperty("muted", True)
        saved_lbl.hide()

        def _save(_=False, b=body, p=path):
            out = p.with_suffix(".txt")
            try:
                out.write_text(b._final_text, encoding="utf-8")
            except OSError:
                # тека лише для читання тощо — акуратна помилка на картці,
                # а не діалог краху через excepthook (як у «Зберегти як…»)
                logging.exception("Не вдалося зберегти розшифровку у %s", out)
                self._saved_note(saved_lbl, tr("files_save_fail"), error=True)
                return
            save.setText(tr("files_saved_name", name=out.name))

        save.clicked.connect(_save)

        # «Зберегти як…» — вибір формату через меню (txt/srt/vtt/docx)
        save_as = GlassButton(tr("files_save_as"))
        menu = QMenu(save_as)
        menu.setToolTipsVisible(True)      # тултипи вимкнених пунктів субтитрів
        act_txt = menu.addAction(tr("files_exp_txt"))
        act_md = menu.addAction(tr("files_exp_md"))
        act_srt = menu.addAction(tr("files_exp_srt"))
        act_vtt = menu.addAction(tr("files_exp_vtt"))
        act_docx = menu.addAction(tr("files_exp_docx"))
        has_seg = bool(segments)
        if not has_seg:
            tip = tr("files_srt_disabled")
            for a in (act_srt, act_vtt):
                a.setEnabled(False)
                a.setToolTip(tip)
        # ті самі актуальні дані: body._final_text у момент експорту, не стара форма
        act_txt.triggered.connect(
            lambda _=False: self._export(path, body._final_text, segments, "txt", saved_lbl))
        act_md.triggered.connect(
            lambda _=False: self._export(path, body._final_text, segments, "md", saved_lbl))
        act_srt.triggered.connect(
            lambda _=False: self._export(path, body._final_text, segments, "srt", saved_lbl))
        act_vtt.triggered.connect(
            lambda _=False: self._export(path, body._final_text, segments, "vtt", saved_lbl))
        act_docx.triggered.connect(
            lambda _=False: self._export(path, body._final_text, segments, "docx", saved_lbl))
        save_as.clicked.connect(
            lambda _=False: menu.exec(save_as.mapToGlobal(
                save_as.rect().bottomLeft())))

        # feature/qol-pack: повторно розшифрувати ІНШОЮ моделлю. Меню будуємо в
        # момент кліку (список встановлених моделей міг змінитись), лише коли
        # джерело-аудіо ще існує. Результат — НОВИЙ рядок черги і новий запис в
        # історії; цю картку не перезаписуємо.
        retry_model = GlassButton(tr("files_retry_model"))
        retry_model.clicked.connect(
            lambda _=False, b=retry_model, p=path: self._retry_model_menu(b, p))

        # feature/edit-pack: імпорт відредагованих субтитрів назад у розшифровку —
        # замикає цикл «розшифрував → правив у .srt/.vtt → повернув». Оновлює текст
        # картки й сегменти (звірка кількості, попередження при розбіжності).
        import_subs = GlassButton(tr("files_import_subs"))
        import_subs.setAccessibleName(tr("files_import_subs"))
        import_subs.clicked.connect(
            lambda _=False, b=body, seg=segments, p=path, lbl=saved_lbl:
            self._import_subtitles(b, seg, p, lbl))

        btns.addWidget(copy)
        btns.addWidget(save)
        btns.addWidget(save_as)
        btns.addWidget(retry_model)
        btns.addWidget(import_subs)

        # feature/transcript-editing: правка тексту прямо в застосунку (opt-in).
        # get/apply читають і пишуть саме body._final_text — той самий текст, що
        # беруть «Копіювати» й повторний експорт. Правка також іде у history.jsonl
        # (final; raw лишається оригіналом). Пошук — усередині панелі (Ctrl+F).
        if getattr(self.controller.cfg, "transcript_editing_enabled", False):
            from .pages.edit_search import TranscriptEditPanel
            body._stored_final = text     # що зараз у сховищі (для точкового апдейту)

            def _apply_edit(new, b=body):
                old = getattr(b, "_stored_final", b._final_text)
                b._final_text = new
                b.setText(self._render_fix_html(b, new))
                self.controller.update_file_transcript(old, new)
                b._stored_final = new

            panel = TranscriptEditPanel(
                body, lambda b=body: b._final_text, _apply_edit,
                # feature/voice-edit-selection: AI-редагувати виділене голосом
                ai_edit_fn=lambda sel, rep: self.controller.voice_edit_selection(
                    sel, rep, self))
            btns.addWidget(panel.edit_button)
            btns.addStretch()
            row.layout().addLayout(btns)
            row.layout().addWidget(panel)
            row.layout().addWidget(saved_lbl)
            motion.expand_height(row)
            return

        btns.addStretch()
        row.layout().addLayout(btns)
        row.layout().addWidget(saved_lbl)
        motion.expand_height(row)   # ТЗ п.7: плавне розгортання результату

    # feature/qol-pack: людяні назви моделей (ті самі, що в Налаштуваннях)
    _MODEL_LABEL_KEYS = {
        "large-v3-turbo": "set_model_fast",
        "large-v3": "set_model_precise",
    }

    def _retry_model_menu(self, anchor: QWidget, path):
        """Меню вибору встановленої моделі для повторної розшифровки файлу.
        Будується в момент кліку: склад моделей на диску міг змінитись. Джерело
        зникло (переміщене/видалене) → чесний тост, нічого не ставимо в чергу."""
        if not Path(path).exists():
            self.controller.tray.notify(tr("files_retry_model_missing"))
            return
        menu = QMenu(anchor)
        try:
            names = self.controller.installed_model_names()
        except Exception:
            names = []
        if not names:
            a = menu.addAction(tr("files_retry_model_none"))
            a.setEnabled(False)
        for name in names:
            label = (tr(self._MODEL_LABEL_KEYS[name])
                     if name in self._MODEL_LABEL_KEYS else name)
            a = menu.addAction(label)
            a.triggered.connect(
                lambda _=False, p=path, m=name: self.add_files([p], model=m))
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    # формати «Зберегти як…»: розширення → ключ-i18n фільтра діалогу
    _EXPORT_FILTER_KEYS = {
        "txt": "files_filt_txt",
        "md": "files_filt_md",
        "srt": "files_filt_srt",
        "vtt": "files_filt_vtt",
        "docx": "files_filt_docx",
    }

    def _export(self, path, text, segments, kind, saved_lbl):
        """Зберегти розшифровку у вибраному форматі через діалог «Зберегти як…».
        Ім'я підставляємо від джерела; після запису — підпис на картці."""
        suggested = str(Path(path).with_suffix("." + kind))
        out, _ = QFileDialog.getSaveFileName(
            self, tr("files_save_as"), suggested,
            tr(self._EXPORT_FILTER_KEYS[kind]))
        if not out:
            return
        out = Path(out)
        try:
            from whisper_core import export
            if kind == "txt":
                # SRT/VTT — з явними \n (newline=""), щоб Windows не подвоїв CR
                out.write_text(text, encoding="utf-8")
            elif kind == "md":
                meta = {"source": Path(path).name,
                        "date": time.strftime("%Y-%m-%d")}
                out.write_text(export.to_markdown(text, meta, segments),
                               encoding="utf-8", newline="")
            elif kind == "srt":
                out.write_text(export.to_srt(segments), encoding="utf-8", newline="")
            elif kind == "vtt":
                out.write_text(export.to_vtt(segments), encoding="utf-8", newline="")
            elif kind == "docx":
                meta = {"filename": Path(path).name,
                        "date": time.strftime("%d.%m.%Y")}
                # без сегментів текст іде одним абзацом — щоб .docx ніколи
                # не виявився порожнім із хибним «Збережено»
                export.to_docx(segments, meta, out, fallback_text=text)
        except Exception:
            logging.exception("Не вдалося експортувати розшифровку у %s", out)
            self._saved_note(saved_lbl, tr("files_save_fail"), error=True)
            return
        self._saved_note(saved_lbl, tr("files_saved", name=out.name))

    def _import_subtitles(self, body, segments, path, saved_lbl):
        """feature/edit-pack: імпортувати відредаговані .srt/.vtt назад у розшифровку.

        Читає файл, розбирає в кʼю (парсер спільний на обидва формати), звіряє
        кількість сегментів із поточною й попереджає при розбіжності (розшифровку
        могли редагувати вручну — краще перепитати, ніж мовчки перезаписати).
        Оновлює текст картки, сегменти (на місці — export-лямбди бачать той самий
        список) і сховище історії тим самим шляхом, що й ручна правка."""
        from whisper_core import export
        start_dir = str(Path(path).parent) if path else ""
        src, _ = QFileDialog.getOpenFileName(
            self, tr("files_import_subs"), start_dir, tr("files_filt_subs"))
        if not src:
            return
        try:
            content = Path(src).read_text(encoding="utf-8-sig")
        except OSError:
            logging.exception("Не вдалося прочитати субтитри %s", anonymize_path(src))
            self._saved_note(saved_lbl, tr("files_import_read_fail"), error=True)
            return
        cues = export.parse_subtitles(content)
        if not cues:
            self._saved_note(saved_lbl, tr("files_import_empty"), error=True)
            return
        # звірка кількості сегментів — попередження, а не блокування
        if segments and len(cues) != len(segments):
            resp = QMessageBox.question(
                self, tr("files_import_subs"),
                tr("files_import_mismatch", have=len(segments), got=len(cues)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return
        new_text = " ".join(c["text"] for c in cues).strip()
        old = getattr(body, "_stored_final", body._final_text)
        body._final_text = new_text
        body.setText(self._render_fix_html(body, new_text))
        segments[:] = cues                      # на місці — export бачить оновлене
        self.controller.update_file_transcript(old, new_text)
        body._stored_final = new_text
        self._saved_note(saved_lbl, tr("files_import_ok", n=len(cues)))

    @staticmethod
    def _saved_note(lbl, text: str, error: bool = False):
        """Підпис стану збереження: успіх — muted, помилка — стиль badge=error
        (інакше помилка виглядає так само тихо, як успіх, і її не помічають)."""
        lbl.setText(text)
        lbl.setProperty("badge", "error" if error else "")
        lbl.setProperty("muted", not error)
        lbl.style().unpolish(lbl)
        lbl.style().polish(lbl)
        lbl.show()


class MainWindow(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle(tr("app_title"))
        self.setMinimumSize(1000, 640)
        # геометрія переживає перезапуск (QSettings → реєстр Windows)
        self._settings = QSettings("Balachky", "Balachky")
        geo = self._settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(860, 540)

        self._backdrop_done = False   # Mica вмикаємо один раз, у showEvent
        self._mica_active = False     # чи ліг Mica-оверлей (для перезастосування теми)
        motion.init_config(controller.cfg)   # прапорець animations для анімацій

        # навігація: колона скляних кнопок (glow за мишею, золотий маркер активної)
        self._nav = QButtonGroup(self)
        self._nav.setExclusive(True)
        navcol = QVBoxLayout()
        navcol.setSpacing(4)
        # Нульові поля: nav-кнопки на всю ширину сайдбара — як кнопка «Налаштування»
        # (яка йде прямо у sv). Інакше типові поля QVBoxLayout крали ~18px і кнопки
        # були вужчі за «Налаштування» на 18px та інсетнуті від розділювачів; довгий
        # напис («Screen recording») не влазив і обрізався (візуальний гейт, сайдбар).
        navcol.setContentsMargins(0, 0, 0, 0)
        # «Налаштування» лишається у групі (той самий id = індекс сторінки), але
        # виноситься ВНИЗ сайдбара, окремо від контентних сторінок (патерн desktop).
        self._settings_nav_btn = None
        for i, (icon_name, key) in enumerate(_PAGES):
            btn = GlassButton(tr(key), icon=_nav_icon(icon_name), nav=True)
            self._nav.addButton(btn, i)
            if key == "nav_settings":
                self._settings_nav_btn = btn
                continue
            navcol.addWidget(btn)
        navcol.addStretch()

        # сайдбар-колона: логотип · навігація · слоган + версія
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")   # адресат скляного тонування (QSS_MICA)
        sidebar.setFixedWidth(204)
        sv = QVBoxLayout(sidebar)
        sv.setContentsMargins(14, 20, 14, 14)
        sv.setSpacing(0)
        # шапка бренду: жук-маскот + вордмарка «Балачки / у Коростені».
        # Клікабельна → «Про програму» (інформаційний хаб).
        brand_frame = ClickableFrame()
        brand_frame.setAccessibleName(tr("about_open"))
        brand_frame.setToolTip(tr("about_title"))
        brand_frame.clicked.connect(self._open_about)
        brand = QHBoxLayout(brand_frame)
        brand.setContentsMargins(6, 0, 0, 0)
        brand.setSpacing(10)
        mascot = QLabel()
        mpm = QPixmap(str(asset_root() / "assets" / "mascot-512.png"))
        if not mpm.isNull():
            # DPI-safe: рендеримо у фізичних пікселях, логічний розмір ~46dp
            dpr = self.devicePixelRatioF()
            mpm = mpm.scaled(round(46 * dpr), round(46 * dpr),
                             Qt.KeepAspectRatio, Qt.SmoothTransformation)
            mpm.setDevicePixelRatio(dpr)
            self._mascot_pm = mpm            # оригінал для перетінтування теми

            def _paint_mascot(w):            # нічний режим: жук у червоній гамі
                w.setPixmap(_red_tint(self._mascot_pm) if theme.is_night()
                            else self._mascot_pm)

            _paint_mascot(mascot)
            theme.register_restyle_call(mascot, _paint_mascot)
        brand.addWidget(mascot)
        words = QVBoxLayout()
        words.setSpacing(0)
        logo = QLabel(tr("brand_top"))
        logo.setProperty("logo", True)
        words.addWidget(logo)
        bottom = tr("brand_bottom")
        if bottom:
            logo_sub = QLabel(bottom)
            logo_sub.setProperty("logosub", True)
            words.addWidget(logo_sub)
        brand.addLayout(words)
        brand.addStretch()
        sv.addWidget(brand_frame)
        sv.addSpacing(16)
        top_div = QFrame()
        top_div.setProperty("divider", True)
        sv.addWidget(top_div)
        sv.addSpacing(8)
        # Навігація живе у скрол-області: коли вікно низьке (канон 1920 → 1044px і
        # менше), список віддає висоту нижньому блокові, тож слоган і РЯДОК ВЕРСІЇ
        # ніколи не підрізаються за нижнім краєм (аудит 1.2.1 №5 — було на всіх
        # сторінках). Смуга прокрутки зʼявляється лише за потреби.
        nav_host = QWidget()
        nav_host.setLayout(navcol)
        nav_scroll = QScrollArea()
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        nav_scroll.setWidget(nav_host)
        nav_scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        sv.addWidget(nav_scroll, stretch=1)
        # Індикатор фонового завантаження моделі AI-протоколу (E-бекграунд-
        # докачка, аудит 31.07.2026): між прокруткою навігації й «Налаштування»
        # — видимий з будь-якої сторінки, приховується сам, доки нема качання.
        self._download_indicator = SidebarDownloadIndicator()
        sv.addWidget(self._download_indicator)
        sv.addSpacing(8)
        # «Налаштування» — унизу, окремою кнопкою над футером (слоган/версія)
        if self._settings_nav_btn is not None:
            sv.addWidget(self._settings_nav_btn)
            sv.addSpacing(8)
        bot_div = QFrame()
        bot_div.setProperty("divider", True)
        sv.addWidget(bot_div)
        sv.addSpacing(8)
        slogan = QLabel(tr("brand_slogan"))
        slogan.setProperty("slogan", True)
        slogan.setWordWrap(True)          # страховка: краще перенос, ніж обрізати
        slogan.setContentsMargins(12, 0, 0, 0)
        sv.addWidget(slogan)
        sv.addSpacing(6)
        # позначка активного режиму тестування (hint-рівень; під версією).
        # Оновлюється живо через сигнал test_mode_changed при перемиканні в Налаштуваннях.
        self._test_indicator = QLabel(tr("sidebar_test_mode"))
        self._test_indicator.setProperty("version", True)
        self._test_indicator.setContentsMargins(12, 0, 0, 0)
        self._test_indicator.setWordWrap(True)
        sv.addWidget(self._test_indicator)
        from .crash import test_mode_active
        self._test_indicator.setVisible(test_mode_active())
        # getattr — контролери-заглушки смоук-тестів не мають цього сигналу
        _tmc = getattr(controller, "test_mode_changed", None)
        if _tmc is not None:
            _tmc.connect(self._test_indicator.setVisible)

        self.pages = _TiledStack()   # тайл «Б з жуком» на всю висоту (drawTiledPixmap)
        self.pages.reload_background(getattr(controller, "cfg", None))
        self.dictation = DictationPage(controller)
        self.pages.addWidget(self.dictation)
        self.files = FilesPage(controller)
        self.pages.addWidget(self.files)
        from .pages.meeting import MeetingPage   # feature/meeting-ui
        from .pages.screen import ScreenPage
        from .pages.history import HistoryPage
        from .pages.vocab import VocabPage
        from .pages.settings import SettingsPage
        from .pages.search import SearchPage   # feature/global-search
        self.meeting = MeetingPage(controller)   # index 2 (між Аудіофайли й Історія)
        self.pages.addWidget(self.meeting)
        self.screen = ScreenPage(controller)     # index 3 (кнопка «Запис екрана»)
        self.pages.addWidget(self.screen)
        self.history = HistoryPage(controller)
        self.pages.addWidget(self.history)
        self.vocab = VocabPage(controller)
        self.pages.addWidget(self.vocab)
        self.settings = SettingsPage(controller)
        self.pages.addWidget(self.settings)
        self.search = SearchPage(controller)   # index 7 (останній — після Налаштувань)
        self.pages.addWidget(self.search)
        self._nav.idClicked.connect(self._on_nav_click)   # ТЗ п.3: fade-перехід + test_log
        self.set_page(0)

        host = QWidget()
        host.setObjectName("centralHost")   # прозорий при Mica (QSS_MICA)
        lay = QHBoxLayout(host)
        self._host_lay = lay   # при Mica spacing→0, інакше гола смуга скла у шві
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(sidebar)
        lay.addWidget(self.pages, stretch=1)
        self.setCentralWidget(host)

        # Esc — сховати вікно у трей; Ctrl+Q — повний вихід звідусіль
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.activated.connect(self.hide)
        quit_sc = QShortcut(QKeySequence("Ctrl+Q"), self)
        quit_sc.setContext(Qt.ApplicationShortcut)
        # Через контролер: під час активного запису наради спитати підтвердження
        # (С7). Лямбда відкладає резолв методу до активації (тест-дублі без нього).
        quit_sc.activated.connect(lambda: self.controller.request_quit())

    def _on_nav_click(self, index: int):
        """Клік nav-кнопки: дія-логер (режим тестування) + fade-перехід."""
        from .crash import test_log
        test_log("nav_click", page=_PAGES[index][1], index=index)
        motion.fade_switch(self.pages, index)

    def _open_about(self):
        """Клік по шапці сайдбара → вкладка «Про програму» в Налаштуваннях
        (раніше відкривала окреме модальне вікно — власник просив прибрати
        спливання і вести напряму в потрібний пункт Налаштувань)."""
        self.set_page(self.pages.indexOf(self.settings))
        self.settings.select_about_tab()

    def set_page(self, index: int):
        """Перемкнути вкладку програмно (старт, скріншоти): кнопка + сторінка."""
        self._nav.button(index).setChecked(True)
        from .crash import test_log
        test_log("page_switch", page=_PAGES[index][1], index=index)
        motion.fade_switch(self.pages, index)   # ТЗ п.3: fade-перехід (no-op при вимк.)

    def maybe_show_test_log_text_reminder(self):
        return maybe_show_test_log_text_reminder(self, self.controller)

    def showEvent(self, event):
        """Темний системний titlebar Windows 11 (DWM); світлий на темному вікні
        виглядає чужорідно. Помилка не критична — просто лишиться світлий."""
        super().showEvent(event)
        try:
            hwnd = int(self.winId())
            value = ctypes.c_int(1)
            dwm = ctypes.windll.dwmapi
            dwm.DwmSetWindowAttribute.argtypes = (
                wintypes.HWND, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD)
            dwm.DwmSetWindowAttribute.restype = ctypes.c_long
            for attr in (20, 19):   # 20 = Win11/10 20H1+; 19 — старіші білди Win10
                res = dwm.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
                if res == 0:
                    break
        except Exception:
            pass
        apply_screen_protection = getattr(
            self.controller, "apply_screen_protection_to_window", None)
        if callable(apply_screen_protection):
            try:
                # Реєстрація потрібна і при вимкненому opt-in: тоді наступне
                # перемикання застосує захист до цього живого вікна й збере факт.
                apply_screen_protection(self)
            except Exception:
                logging.exception("Збій встановлення display affinity")
        # Mica-скло (Win11 22H2+): лише при backdrop="auto" і УСПІШНИХ DWM-викликах;
        # інакше вікно лишається твердим «оформленням» (тихий фолбек, нічого не міняємо)
        if (not self._backdrop_done
                and getattr(self.controller.cfg, "backdrop", "off") == "auto"):
            self._backdrop_done = True
            try:
                from .backdrop import enable_mica
                if enable_mica(int(self.winId())):
                    self.setStyleSheet(theme.QSS_MICA)
                    self._mica_active = True
                    self._host_lay.setSpacing(0)   # шов закриває border сайдбара
            except Exception:
                pass

    def reapply_theme(self, night: bool) -> None:
        """Нічний/денний режим НА ЛЬОТУ: свопнути палітру, перевстановити QSS
        застосунку, перебудувати острівці й іконки навігації, перемалювати все.
        Викликається з Налаштувань; без перезапуску (feature/night-mode)."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, night)        # палітра + QSS + restyle-hooks + repaint
            # іконка вікна/таскбару (QIcon самої .ico не читає тему) — під нову гаму
            app.setWindowIcon(app_icon(asset_root() / "assets" / "balachky.ico"))
        # іконки навігації створені один раз під стару гаму — перебудувати у новій
        for i, (icon_name, _key) in enumerate(_PAGES):
            btn = self._nav.button(i)
            if btn is not None:
                btn.setIcon(_nav_icon(icon_name))
        # під Mica власний QSS вікна перекриває застосунковий — покласти нову тему
        if self._mica_active:
            self.setStyleSheet(theme.QSS_MICA)
        self.update()

    def remember_geometry(self):
        """Запам'ятати розмір/позицію вікна (виклик: закриття вікна та вихід)."""
        self._settings.setValue("geometry", self.saveGeometry())

    def closeEvent(self, event):
        """Закриття вікна → у трей, застосунок продовжує слухати PTT.
        Підказку про фон показуємо ОДИН раз (прапорець у QSettings)."""
        self.remember_geometry()
        event.ignore()
        self.hide()
        if not self._settings.value("close_hint_shown", False, type=bool):
            self._settings.setValue("close_hint_shown", True)
            self.controller.tray.notify(tr("close_hint"))
