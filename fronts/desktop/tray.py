"""System tray: значок-мікрофон кольору стану + меню з профілями пам'яті.

Стани (колір мікрофона): сірий (готовий) / червоний (запис) / золото (розпізнаю).
Усі посилання на Qt-об'єкти тримаємо на self — інакше python GC їх прибере.
"""
import time

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PySide6.QtGui import (QIcon, QPixmap, QPainter, QColor, QAction,
                           QActionGroup, QFontMetrics, QPen)
from PySide6.QtCore import Qt, QRect, QRectF, QTimer

import qtawesome as qta

from . import motion
from . import theme   # персоналізація кольору: idle/busy/loading у тон активної теми
from .i18n import tr

# Гама «Balachky»: готовий = theme.IDLE, розпізнаю = theme.GOLD (акцент) — обидва
# ЖИВІ, тож перефарбовуються під активний колір інтерфейсу (значок перебудовується
# на кожній зміні стану/кадрі спінера — окремий restyle-хук не потрібен). "recording"
# — ЄДИНИЙ фіксований колір (аудит 25.07, свідомо НЕ theme.ALERT): активний
# мікрофон = семантично критичний стан у треї ОС, впізнаваність важливіша за
# персоналізацію — не чіпати.
_RECORDING_COLOR = "#E52421"


def _tray_color(state: str) -> str:
    if state == "recording":
        return _RECORDING_COLOR
    if state in ("busy", "loading"):
        return theme.GOLD
    return theme.IDLE


# коди станів → ключі i18n (текст беремо через tr у момент показу — мова вже задана)
_LABEL_KEY = {"idle": "tray_ready", "recording": "tray_recording",
              "busy": "tray_transcribing", "loading": "tray_loading_model"}


def _state_icon(color: str) -> QIcon:
    """Значок стану у треї — суцільний мікрофон кольору стану (замість безликої
    крапки). Мікрофон малюємо на весь кадр (менше порожнечі, краще видно
    у дрібному треї), рендеримо у 32px для чіткості."""
    pm = qta.icon("fa6s.microphone", color=color).pixmap(32, 32)
    # трохи «розтягнути» гліф на весь кадр: qtawesome лишає поля ~10%
    canvas = QPixmap(32, 32)
    canvas.fill(Qt.transparent)
    p = QPainter(canvas)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    p.drawPixmap(QRect(1, 0, 30, 32), pm)
    p.end()
    return QIcon(canvas)


def _spinner_pixmap(phase: float, color: str, size: int = 64) -> QPixmap:
    """Один кадр обертового спінера для трею — ТА САМА геометрія, що й у пілюлі
    «Жива кромка» (glass.StatusTag._paint_spinner): кільце з 8 РІВНИХ дуг
    (span = 360/8 − gap, gap = 24°), круглий капелюшок пера, обертання на phase.
    Малюємо у 64px QPixmap → чітко на HiDPI (Qt масштабує вниз під трей)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.translate(size / 2.0, size / 2.0)
    p.rotate(phase)
    pen = QPen(QColor(color), size * 0.10)       # товщина пропорційна кадру (≈ пілюля)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    r = size * 0.36                              # радіус кільця
    rect = QRectF(-r, -r, 2 * r, 2 * r)
    n = 8
    gap = 24.0
    span = 360.0 / n - gap                        # 21° — ідентично пілюлі
    for i in range(n):
        start = i * (360.0 / n) + gap / 2.0
        p.drawArc(rect, int(round(start * 16)), int(round(span * 16)))
    p.end()
    return pm


class Tray:
    def __init__(self, *, profile_names, active, memory_on,
                 on_switch_profile, on_toggle_memory, on_reset_memory,
                 on_reload_terms, on_quit, on_open_window=None, on_recent=None,
                 on_undo_paste=None, on_insert_last=None, on_cheat_sheet=None,
                 on_open_note=None, on_command_edit=None,
                 on_recent_pastes=None, on_paste_recent=None, on_help=None,
                 on_listen_selection=None, on_open_voices=None, on_pron_dict=None):
        self.icon = QSystemTrayIcon(_state_icon(_tray_color("idle")))
        self.icon.setToolTip(tr("app_title"))

        # обертовий спінер стану «розпізнаю»: фаза + таймер (батько = icon → GC)
        self._spin_phase = 0.0
        self._spin_timer = None
        self._state = "idle"

        self._menu = QMenu()
        self._status = QAction(tr("tray_ready"))
        self._status.setEnabled(False)
        self._menu.addAction(self._status)
        self._menu.addSeparator()
        if on_open_window:
            self._open = QAction(tr("tray_open"))
            self._open.triggered.connect(on_open_window)
            self._menu.addAction(self._open)
            self.icon.activated.connect(
                lambda reason: on_open_window()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
            self._menu.addSeparator()

        # feature/scratchpad-note: плаваюча нотатка (диктування у власне вікно)
        if on_open_note is not None:
            self._note = QAction(tr("note_tray"))
            self._note.triggered.connect(on_open_note)
            self._menu.addAction(self._note)
            self._menu.addSeparator()

        # feature/voice-edit-selection: Command Mode — редагувати виділене голосом
        # (діє завжди; глобальний хоткей опційний).
        if on_command_edit is not None:
            self._command_edit = QAction(tr("cmdedit_menu"))
            self._command_edit.triggered.connect(lambda _=False: on_command_edit())
            self._menu.addAction(self._command_edit)
            self._menu.addSeparator()

        # feature/tts-listen: «Прослухати виділене» — озвучити системне виділення
        # (діє завжди; глобальний хоткей опційний).
        if on_listen_selection is not None:
            self._listen = QAction(tr("tts_listen_selection"))
            self._listen.triggered.connect(lambda _=False: on_listen_selection())
            self._menu.addAction(self._listen)
            self._menu.addSeparator()

        # feature/tts-listen (Хвиля 3): менеджер голосів озвучення
        if on_open_voices is not None:
            self._voices = QAction(tr("tts_voices_menu"))
            self._voices.triggered.connect(lambda _=False: on_open_voices())
            self._menu.addAction(self._voices)
            self._menu.addSeparator()

        # feature/tts-listen (Хвиля 4): словник вимови
        if on_pron_dict is not None:
            self._pron = QAction(tr("tts_pron_menu"))
            self._pron.triggered.connect(lambda _=False: on_pron_dict())
            self._menu.addAction(self._pron)
            self._menu.addSeparator()

        # Підменю «Останні розшифровки»: 3-5 найновіших записів активного профілю.
        # Список змінюється, тож будуємо його ЩОРАЗУ перед показом (aboutToShow),
        # а не раз при старті. Дані бере провайдер on_recent (свіжий активний профіль).
        if on_recent is not None:
            self._on_recent = on_recent
            self._recent_menu = self._menu.addMenu(tr("tray_recent"))
            self._recent_menu.setToolTipsVisible(True)   # показати «клік — копіювати»
            self._recent_actions = []       # тримаємо ref на дії (GC!)
            self._recent_menu.aboutToShow.connect(self._rebuild_recent)
            self._menu.addSeparator()

        # feature/qol-pack: скасувати / повторити останню вставку (діють завжди,
        # хоткеї опційні). Тримаємо ref на дії (GC).
        if on_undo_paste is not None:
            self._undo_action = QAction(tr("tray_undo_paste"))
            self._undo_action.triggered.connect(lambda _=False: on_undo_paste())
            self._menu.addAction(self._undo_action)
        if on_insert_last is not None:
            self._insert_action = QAction(tr("tray_insert_last"))
            self._insert_action.triggered.connect(lambda _=False: on_insert_last())
            self._menu.addAction(self._insert_action)
        # feature/paste-safety: підменю «Останні вставки» — останні N доставлених
        # текстів сесії (НЕ історія розшифровок), клік вставляє обраний ще раз.
        # Будуємо щоразу перед показом (aboutToShow) — список змінюється.
        if on_recent_pastes is not None and on_paste_recent is not None:
            self._on_recent_pastes = on_recent_pastes
            self._on_paste_recent = on_paste_recent
            self._pastes_menu = self._menu.addMenu(tr("tray_recent_pastes"))
            self._paste_actions = []       # тримаємо ref на дії (GC!)
            self._pastes_menu.aboutToShow.connect(self._rebuild_pastes)
        if (on_undo_paste is not None or on_insert_last is not None
                or on_recent_pastes is not None):
            self._menu.addSeparator()

        # Підменю «Словник»: radio-список + пам'ять + очищення
        self._pmenu = self._menu.addMenu(tr("tray_dictionary"))
        self._pgroup = QActionGroup(self._pmenu)
        self._pgroup.setExclusive(True)
        self._pactions = {}
        for name in profile_names:
            a = QAction(name, self._pmenu)
            a.setCheckable(True)
            a.setChecked(name == active)
            a.triggered.connect(lambda checked=False, n=name: on_switch_profile(n))
            self._pgroup.addAction(a)
            self._pmenu.addAction(a)
            self._pactions[name] = a
        self._pmenu.addSeparator()
        self._mem = QAction(tr("tray_mem"), self._pmenu)
        self._mem.setCheckable(True)
        self._mem.setChecked(memory_on)
        self._mem.toggled.connect(on_toggle_memory)
        self._pmenu.addAction(self._mem)
        self._reset = QAction(tr("common_clear_mem"), self._pmenu)
        self._reset.triggered.connect(on_reset_memory)
        self._pmenu.addAction(self._reset)

        self._menu.addSeparator()
        # Довідка: коротка інструкція (локальний README, інакше сторінка репо)
        if on_help is not None:
            self._help = QAction(tr("tray_help"))
            self._help.triggered.connect(on_help)
            self._menu.addAction(self._help)
        # feature/ux-center: шпаргалка гарячих клавіш (компактна картка-довідка)
        if on_cheat_sheet is not None:
            self._cheat = QAction(tr("hotkeys_title"))
            self._cheat.triggered.connect(on_cheat_sheet)
            self._menu.addAction(self._cheat)
        self._reload = QAction(tr("tray_reload"))
        self._reload.triggered.connect(on_reload_terms)
        self._menu.addAction(self._reload)
        self._quit = QAction(tr("tray_quit"))
        self._quit.triggered.connect(on_quit)
        self._menu.addAction(self._quit)

        self.icon.setContextMenu(self._menu)
        self.icon.show()

    def set_state(self, state: str, text: str | None = None):
        """Викликати ЛИШЕ з GUI-потоку (через слот, під'єднаний до сигналу).
        Стан «busy» (розпізнаю) — обертовий спінер; решта — статичний значок."""
        self._state = state
        if state in ("busy", "loading"):
            self._start_spin()                   # анімований золотий спінер
        else:
            self._stop_spin()                    # спинити таймер → 0% CPU у спокої
            self.icon.setIcon(_state_icon(_tray_color(state)))
        self._status.setText(text or tr(_LABEL_KEY.get(state, "tray_ready")))

    # --- обертовий спінер стану «розпізнаю» ---
    def _start_spin(self):
        """Запустити обертання (стан busy). Анімації вимкнені (конфіг/система) →
        один статичний кадр спінера, без таймера. Повторний busy — не смикаємо
        фазу (спінер уже крутиться)."""
        if not motion.animations_enabled():
            self._stop_spin()
            self.icon.setIcon(QIcon(_spinner_pixmap(0.0, _tray_color("busy"))))
            return
        if self._spin_timer is None:
            self._spin_timer = QTimer(self.icon)     # батько = icon → ref (GC)
            self._spin_timer.setInterval(70)
            self._spin_timer.timeout.connect(self._spin_tick)
        if not self._spin_timer.isActive():
            self._spin_phase = 0.0
            self.icon.setIcon(QIcon(_spinner_pixmap(self._spin_phase, _tray_color("busy"))))
            self._spin_timer.start()

    def _spin_tick(self):
        if not motion.animations_enabled():
            self._stop_spin()
            self.icon.setIcon(QIcon(_spinner_pixmap(0.0, _tray_color("busy"))))
            return
        # 21°/кадр × 70мс ≈ 1200мс/оберт — та сама кутова швидкість, що й у пілюлі
        self._spin_phase = (self._spin_phase + 21.0) % 360.0
        self.icon.setIcon(QIcon(_spinner_pixmap(self._spin_phase, _tray_color("busy"))))

    def _stop_spin(self):
        if self._spin_timer is not None and self._spin_timer.isActive():
            self._spin_timer.stop()

    def sync_animations(self):
        """Негайно stop/start busy-спінер після зміни налаштування."""
        if self._state in ("busy", "loading"):
            self._start_spin()
        elif not motion.animations_enabled():
            self._stop_spin()

    def set_memory_checked(self, on: bool):
        """Оновити чекбокс без повторного спрацювання колбека."""
        self._mem.blockSignals(True)
        self._mem.setChecked(on)
        self._mem.blockSignals(False)

    def notify(self, text: str):
        self.icon.showMessage(tr("app_title"), text)

    # --- підменю «Останні розшифровки» ---
    def _rebuild_recent(self):
        """Перебудувати підменю ПЕРЕД показом: історія росте, профіль може
        перемкнутися. Дані — свіжі, з провайдера. Порожньо/пам'ять вимкнена →
        один вимкнений пункт «Історія порожня»."""
        self._recent_menu.clear()          # старі дії видаляє Qt
        self._recent_actions = []          # і ми відпускаємо ref на них
        records = self._on_recent() or []
        fm = QFontMetrics(self._recent_menu.font())
        for _line, rec in records:
            full = (rec.get("final") or rec.get("raw") or "").strip()
            if not full:
                continue
            a = QAction(self._recent_label(rec, full, fm), self._recent_menu)
            a.setToolTip(tr("tray_recent_tip"))   # афорданс дії
            # клік копіює ПОВНИЙ текст (не елідований) + нативний тост
            a.triggered.connect(
                lambda checked=False, t=full: self._copy_recent(t))
            self._recent_menu.addAction(a)
            self._recent_actions.append(a)
        if not self._recent_actions:       # історія справді порожня (файл без записів)
            a = QAction(tr("common_empty_here"), self._recent_menu)
            a.setEnabled(False)
            self._recent_menu.addAction(a)
            self._recent_actions.append(a)

    def _rebuild_pastes(self):
        """feature/paste-safety: перебудувати «Останні вставки» перед показом.
        Дані — з провайдера (найновіша першою). Клік вставляє повний текст ще раз.
        Порожньо → один вимкнений пункт-заглушка."""
        self._pastes_menu.clear()
        self._paste_actions = []
        fm = QFontMetrics(self._pastes_menu.font())
        for text in (self._on_recent_pastes() or []):
            body = (text or "").strip()
            if not body:
                continue
            label = fm.elidedText(
                body.replace("\n", " ").replace("\r", " "),
                Qt.ElideRight, 320).replace("&", "&&")
            a = QAction(label, self._pastes_menu)
            a.setToolTip(tr("tray_recent_pastes_tip"))
            a.triggered.connect(
                lambda checked=False, t=body: self._on_paste_recent(t))
            self._pastes_menu.addAction(a)
            self._paste_actions.append(a)
        if not self._paste_actions:
            a = QAction(tr("common_empty_here"), self._pastes_menu)
            a.setEnabled(False)
            self._pastes_menu.addAction(a)
            self._paste_actions.append(a)

    def _recent_label(self, rec: dict, full: str, fm: QFontMetrics) -> str:
        """Однорядкова елідована мітка «ГГ:ХХ  початок тексту…».
        Переноси прибираємо; «&» подвоюємо, щоб QAction не з'їв як мнемоніку."""
        ts = rec.get("ts")
        when = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
        body = full.replace("\n", " ").replace("\r", " ")
        text = f"{when}  {body}" if when else body
        return fm.elidedText(text, Qt.ElideRight, 320).replace("&", "&&")

    def _copy_recent(self, text: str):
        QApplication.clipboard().setText(text)
        self.icon.showMessage(tr("app_title"), tr("tray_copied"))
