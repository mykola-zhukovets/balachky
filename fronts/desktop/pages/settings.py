"""Вкладка «Налаштування»: модель, керування записом, куди текст, система.

Стиль «Просторий editorial»: секції — QFrame[card] без рамок із золотим
eyebrow-заголовком; форми — двоколонкова сітка label/control (QGridLayout,
лейбли QLabel[formlabel]); примітки — QLabel[muted]. Жодних інлайн-стилів —
усе з глобального QSS. Логіка (сигнали/хендлери) — без змін.
"""
import html
import json
import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, QSize, Signal, QTimer, QTime, QThread
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
    QRadioButton, QButtonGroup, QPushButton, QCheckBox, QFrame, QScrollArea, QTabWidget,
    QMessageBox, QFileDialog, QToolButton, QDialog, QProgressBar, QSlider, QMenu,
    QLineEdit, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QTimeEdit,   # feature/qol-pack: поля «тихих годин»
    QInputDialog,   # fix/stt-models: власна модель за HF-id
)

import qtawesome as qta

from whisper_core import __version__, updates, cuda_runtime, profiles as wc_profiles
from whisper_core import paths as wc_paths
from whisper_core import netlog   # доказова офлайновість: журнал вихідних з'єднань
from whisper_core.config import (            # feature/audio-qol + audio-center: дефолти
    VAD_THRESHOLD_DEFAULT, VAD_MIN_SILENCE_MS_DEFAULT, VAD_MIN_SPEECH_MS_DEFAULT,
    NOISE_GATE_THRESHOLD_DB_DEFAULT, AGC_TARGET_DB_DEFAULT,
)
from whisper_core.engine import cuda_runtime_available

from .. import context as ctx_mod
from ..context import ContextProfile, Behavior, AutoProfileRule

from .. import motion
from .. import links
from ..chip_popover import make_slider
from ..autostart import is_enabled, enable, disable
from ..crash import open_log_dir, copy_diagnostics
from ..glass import GlassButton, TipToolButton, sync_status_animations
from ..hotkey import normalize_name, pretty
from ..i18n import tr, current_language, human_size
from ..onboarding import GpuDownloadWorker, _reap_worker
from ..recorder import list_input_devices, list_output_devices
from .. import theme   # нічний режим: ⓘ-іконки читають палітру
from ..theme import spaced
from . import page_header

_LABEL_COL = 168   # ширина колонки лейблів у формах-сітках
_CTRL_MAX = 420    # комбобокси не розтягуються на всю колонку


def _format_voice_date(updated_at) -> str:
    """Дата останнього оновлення голосу (voices.json зберігає ``updated_at`` як
    int epoch). Повертає ``YYYY-MM-DD`` або "" при відсутньому/некоректному
    значенні. Раніше тут було ``updated_at[:10]`` → TypeError на int."""
    if not isinstance(updated_at, (int, float)) or updated_at <= 0:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(updated_at))
    except (OSError, ValueError, OverflowError):
        return ""

# Точність обчислень моделі (compute_type faster-whisper/ctranslate2). CPU
# підтримує лише int8 (float16/int8_float16 — CUDA-only). GPU дає вибір: int8
# (найменше VRAM) → int8_float16 (баланс, дефолт) → float16 (максимум якості).
_COMPUTE_CPU = (("int8", "set_compute_int8"),)
_COMPUTE_GPU = (
    ("int8", "set_compute_int8"),
    ("int8_float16", "set_compute_balanced"),
    ("float16", "set_compute_float16"),
)

# Приблизний пік ВІДЕОпам’яті (діапазон, без одиниці — її додає tr('unit_gb'))
# за пресетом і точністю. int8-ваги вдвічі легші за float16; int8_float16 =
# int8-ваги + float16-обчислення (проміжний). Орієнтир large-v3 int8_float16
# ~3.7 ГБ — з whisper_core/cuda_runtime.py; решта масштабована за розміром моделі.
_GPU_VRAM = {
    ("small", "int8"): "0.5-1", ("small", "int8_float16"): "1",
    ("small", "float16"): "1-1.5",
    ("medium", "int8"): "1-1.5", ("medium", "int8_float16"): "1.5-2",
    ("medium", "float16"): "2-3",
    ("large-v3-turbo", "int8"): "1-1.5", ("large-v3-turbo", "int8_float16"): "1.5-2",
    ("large-v3-turbo", "float16"): "2-3",
    ("large-v3", "int8"): "2-3", ("large-v3", "int8_float16"): "3-4",
    ("large-v3", "float16"): "4-5",
}

# feature/audio-qol: код вердикту тесту мікрофона → i18n-ключ підпису результату
_MIC_VERDICT_KEYS = {
    "good": "set_mic_good",
    "quiet": "set_mic_quiet",
    "silence": "set_mic_silence",
    "error": "set_mic_error",
}


class InfoButton(TipToolButton):
    """ⓘ-кнопка з ГАРАНТОВАНИМ показом підказки при наведенні — тонкий підклас
    спільного TipToolButton (glass.py): та сама логіка enterEvent→QToolTip."""


def info_hint(text_key: str, clickable: bool = False,
              title_key: str = None) -> QToolButton:
    """Маленька ⓘ-іконка з простим поясненням «для бабусі».

    За замовчуванням — QToolTip при наведенні (для дрібних підказок). З
    clickable=True клік відкриває невеликий QMessageBox із трохи довшим
    поясненням (для найважливішого — вибору моделі). Текст — з i18n (двомовно).
    """
    text = tr(text_key)
    btn = InfoButton(text)
    btn.setFixedSize(22, 22)
    btn.setAutoRaise(True)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFocusPolicy(Qt.StrongFocus)
    btn.setIcon(qta.icon("fa6s.circle-info", color=theme.TEXT_MUTED, color_active=theme.GOLD))
    theme.register_restyle_call(btn, lambda w: w.setIcon(  # нічний режим: перечитати палітру
        qta.icon("fa6s.circle-info", color=theme.TEXT_MUTED, color_active=theme.GOLD)))
    btn.setIconSize(QSize(14, 14))
    btn.setStyleSheet(
        "QToolButton { border: 2px solid transparent; background: transparent; }"
        f"QToolButton:focus {{ border-color: {theme.FOCUS}; border-radius: 4px; }}")
    # setToolTip — для доступності; показ на hover гарантує enterEvent.
    # Rich-text (<qt>…</qt>): QToolTip САМ переносить довгі рядки (plain-text не
    # переносив) → підказки багаторядкові й компактні (max-width у QToolTip QSS).
    btn.setToolTip(f"<qt>{html.escape(text)}</qt>")
    if clickable:
        title = tr(title_key) if title_key else tr("nav_settings")

        def _show(_checked=False):
            box = QMessageBox(btn.window())
            box.setIcon(QMessageBox.NoIcon)
            box.setWindowTitle(title)
            box.setText(text)
            box.setStandardButtons(QMessageBox.Ok)
            box.exec()

        btn.clicked.connect(_show)
    return btn


def _labeled_hint(text: str, hint_key: str, clickable: bool = False,
                  title_key: str = None) -> QWidget:
    """Лейбл форми + ⓘ-підказка поруч → віджет для колонки 0 сітки."""
    w = QWidget()
    row = QHBoxLayout(w)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(_form_label(text))
    row.addWidget(info_hint(hint_key, clickable, title_key))
    row.addStretch()
    return w


def _card(title: str, hint_key: str = None, clickable: bool = False,
          title_key: str = None):
    """Секція-картка з eyebrow-заголовком → (frame, layout).

    hint_key — за бажанням додає ⓘ-підказку поруч із заголовком секції."""
    frame = QFrame()
    frame.setProperty("card", True)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(22, 18, 22, 20)
    lay.setSpacing(14)
    head = QLabel(title)
    head.setProperty("level", "block")   # назва картки — блок (16/600), не дрібний eyebrow
    if hint_key:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(head)
        row.addWidget(info_hint(hint_key, clickable, title_key))
        row.addStretch()
        lay.addLayout(row)
    else:
        lay.addWidget(head)
    return frame, lay


def _grid() -> QGridLayout:
    """Двоколонкова сітка label/control зі спільним вирівнюванням."""
    g = QGridLayout()
    g.setHorizontalSpacing(24)
    g.setVerticalSpacing(12)
    g.setColumnMinimumWidth(0, _LABEL_COL)
    g.setColumnStretch(1, 1)
    return g


def _form_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("formlabel", True)
    return lbl


def _note(text: str) -> QLabel:
    """Прихована muted-примітка; показується після зміни налаштування."""
    lbl = QLabel(text)
    lbl.setProperty("muted", True)
    lbl.setWordWrap(True)
    lbl.hide()
    return lbl


def _soft_break_long(text: str, chunk: int = 12) -> str:
    """Вставити м'які переноси (U+200B) у довгі неперервні токени.

    QLabel[wordWrap] переносить лише по пробілах. Довгий неперервний токен —
    напр. URL чи клас винятку в мережевій помилці завантаження компонента —
    ширший за колонку обрізається по горизонталі (пробілу для переносу немає).
    Вставляємо невидимий м'який перенос після роздільників шляху/URL і не рідше
    ніж кожні `chunk` символів, щоб рушій переносу міг ламати такий токен. На
    короткі токени (статуси на кшталт «Готово») не впливає — вони коротші за chunk."""
    zwsp = "​"
    parts = []
    for token in text.split(" "):
        if len(token) <= chunk:
            parts.append(token)
            continue
        buf = []
        run = 0
        for ch in token:
            buf.append(ch)
            run += 1
            if ch in "/\\._-,:;=&?@" or run >= chunk:
                buf.append(zwsp)
                run = 0
        parts.append("".join(buf))
    return " ".join(parts)


def _human_size(nbytes: int) -> str:
    """Розмір по-людськи: ГБ від 1 ГБ, інакше МБ (одиниці з i18n).  # feature/delete-model"""
    return human_size(nbytes)


def _color_swatch_icon(color_hex_or_rgb):
    """Маленький закруглений квадратик-зразок кольору для QComboBox."""
    from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QBrush
    from PySide6.QtCore import Qt
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    if isinstance(color_hex_or_rgb, str):
        color = QColor(color_hex_or_rgb)
    elif isinstance(color_hex_or_rgb, (tuple, list)):
        color = QColor(color_hex_or_rgb[0], color_hex_or_rgb[1], color_hex_or_rgb[2])
    else:
        color = QColor("#888888")
    painter.setBrush(QBrush(color))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(0, 0, 16, 16, 4, 4)
    painter.end()
    return QIcon(pixmap)

def _tts_engine_available() -> bool:
    """Чи є в ЦІЙ збірці рушій озвучення (whisper_core/tts/sidecar.py).
    Обгортка — щоб картка «Голоси озвучення» перевірялась тим самим способом,
    що майстер перших кроків, і щоб тест міг підмінити відповідь."""
    from whisper_core.tts.sidecar import engine_available
    return engine_available()


def _social_link_qss() -> str:
    """QSS круглого значка-посилання. Спокій — нейтральні токени палітри (_LINE_SOFT/
    _WHITE_06: біле скло вдень, ЧЕРВОНЕ вночі — теж «не-біле світло»). Hover-акцент —
    з ЖИВОЇ палітри (theme.ACCENT_RGB): золото вдень, червоне вночі (вимога «жодного
    не-червоного світла» — на hover рамка/тло не мають світити золотом уночі). Pressed
    — нейтральний чорний натиск (той самий патерн, що QToolButton:pressed у theme.py:
    натиск затемнює, кольору не додає — свідомо не токенізовано). Рядок будується в
    setStyleSheet, тож на живому свопі теми його перебудовує restyle-хук (див.
    round_social). Альфи ті самі, що були — денний вигляд байт-у-байт незмінний."""
    r, g, b = theme.ACCENT_RGB
    return (
        f"QToolButton {{ border: 1px solid {theme._LINE_SOFT};"
        f" border-radius: 17px; background: {theme._WHITE_06}; }}"
        f"QToolButton:hover {{ border-color: rgba({r},{g},{b},0.65);"
        f" background: rgba({r},{g},{b},0.12); }}"
        "QToolButton:pressed { background: rgba(0,0,0,0.28); }")


def build_support_menu(btn: QWidget) -> QMenu:
    """Меню способів підтримки автора (гривня, долари, євро, крипта) — ПОБУДОВА
    без показу.

    Розділено на побудову й показ тим самим патерном, що `_build_models_folder_menu`:
    `menu.exec()` в offscreen блокує прогін назавжди, а перевіряти склад меню
    треба. Кожна дія несе властивість «supportTarget» — адресу або посилання з
    fronts/desktop/links.py: єдине джерело правди, за яким тест бачить, що з
    інтерфейсу досяжні ВСІ шість способів."""
    from PySide6.QtGui import QDesktopServices
    from .. import motion

    menu = QMenu(btn)
    menu.setObjectName("supportMenu")

    # 0. Інтро-заголовок
    act_intro = menu.addAction(tr("support_menu_intro"))
    act_intro.setObjectName("supportActIntro")
    act_intro.setEnabled(False)
    menu.addSeparator()

    # 1. Monobank (гривня)
    act_mono = menu.addAction(tr("support_menu_mono"))
    act_mono.setObjectName("supportActMono")
    act_mono.setToolTip(tr("support_menu_mono"))
    act_mono.setProperty("supportTarget", links.SUPPORT_MONO_UAH)
    act_mono.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(links.SUPPORT_MONO_UAH)))

    # 2. PrivatBank (долари)
    act_usd = menu.addAction(tr("support_menu_privat_usd"))
    act_usd.setObjectName("supportActPrivatUsd")
    act_usd.setToolTip(tr("support_menu_privat_usd"))
    act_usd.setProperty("supportTarget", links.SUPPORT_PRIVAT_USD)
    act_usd.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(links.SUPPORT_PRIVAT_USD)))

    # 3. PrivatBank (євро)
    act_eur = menu.addAction(tr("support_menu_privat_eur"))
    act_eur.setObjectName("supportActPrivatEur")
    act_eur.setToolTip(tr("support_menu_privat_eur"))
    act_eur.setProperty("supportTarget", links.SUPPORT_PRIVAT_EUR)
    act_eur.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(links.SUPPORT_PRIVAT_EUR)))

    menu.addSeparator()

    def _copy_crypto(addr: str):
        QApplication.clipboard().setText(addr)
        target = btn.window() if hasattr(btn, "window") and btn.window() else btn
        motion.toast(target, tr("toast_address_copied"))

    # 4. USDT (TRC-20)
    act_usdt = menu.addAction(tr("support_menu_usdt"))
    act_usdt.setObjectName("supportActUsdt")
    act_usdt.setToolTip(tr("support_menu_usdt"))
    act_usdt.setProperty("supportTarget", links.SUPPORT_USDT_TRC20)
    act_usdt.triggered.connect(lambda: _copy_crypto(links.SUPPORT_USDT_TRC20))

    # 5. Bitcoin
    act_btc = menu.addAction(tr("support_menu_btc"))
    act_btc.setObjectName("supportActBtc")
    act_btc.setToolTip(tr("support_menu_btc"))
    act_btc.setProperty("supportTarget", links.SUPPORT_BTC)
    act_btc.triggered.connect(lambda: _copy_crypto(links.SUPPORT_BTC))

    # 6. Ethereum
    act_eth = menu.addAction(tr("support_menu_eth"))
    act_eth.setObjectName("supportActEth")
    act_eth.setToolTip(tr("support_menu_eth"))
    act_eth.setProperty("supportTarget", links.SUPPORT_ETH)
    act_eth.triggered.connect(lambda: _copy_crypto(links.SUPPORT_ETH))

    return menu


def support_targets() -> tuple:
    """Адреси й посилання, які меню підтримки віддає користувачу — з побудованого
    меню, а не з окремого списку (інакше перелік розійшовся б із меню)."""
    # Меню — дитина host; окремий deleteLater тут НЕ ставимо: host гине разом із
    # локальною змінною і забирає меню з собою, а відкладене видалення дало б
    # другий прохід по вже знищеному об'єкту (падіння процесу на наступному exec).
    host = QWidget()
    menu = build_support_menu(host)
    return tuple(act.property("supportTarget") for act in menu.actions()
                 if act.property("supportTarget"))


def show_support_menu(btn: QWidget) -> QMenu:
    """Показати меню способів підтримки автора під кнопкою."""
    from PySide6.QtCore import QPoint

    menu = build_support_menu(btn)
    menu.exec(btn.mapToGlobal(QPoint(0, btn.height())))
    return menu



def round_social(icon_name: str, url: str, *, name: str = None,
                 tooltip: str = None) -> QToolButton:
    """Круглий значок-посилання: скляне коло, золото на hover; відкриває
    посилання в браузері або меню підтримки. icon_name — набір qtawesome (напр. fa6b.github).
    name — accessibleName для скрінрідера; tooltip — підказка (з маркером ↗).
    URL кладемо і у властивість linkUrl — щоб тести могли його перевірити."""
    btn = QToolButton()
    btn.setFixedSize(34, 34)
    btn.setAutoRaise(True)                       # → QIcon.Active на hover (золото)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setIcon(qta.icon(icon_name, color=theme.TEXT_MUTED, color_active=theme.GOLD))
    theme.register_restyle_call(btn, lambda w: w.setIcon(  # нічний режим: перечитати палітру
        qta.icon(icon_name, color=theme.TEXT_MUTED, color_active=theme.GOLD)))
    btn.setIconSize(QSize(16, 16))
    # QSS будуємо з живої палітри й перебудовуємо на свопі теми — той самий патерн,
    # що restyle іконки вище (інакше hover уночі світив би золотом із хардкоду).
    btn.setStyleSheet(_social_link_qss())
    theme.register_restyle_call(btn, lambda w: w.setStyleSheet(_social_link_qss()))
    btn.setProperty("linkUrl", url)
    if name:
        btn.setAccessibleName(name)
    if tooltip:
        btn.setToolTip(tooltip)
    if icon_name == "fa6s.heart" or url in (links.SUPPORT_URL, links.SUPPORT_MONO_UAH):
        btn.clicked.connect(lambda _=False: show_support_menu(btn))
    else:
        btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
    return btn



class NetworkLogDialog(QDialog):
    """Журнал мережевої активності: наші вихідні з'єднання (факт + хост, БЕЗ
    вмісту). Доказова офлайновість — у нормі порожній. Внизу — довідка «Як
    перевірити самому» (Resource Monitor / Wireshark).

    entries можна передати ззовні (тести / візуальний гейт); за замовчуванням
    читаємо реальний журнал whisper_core.netlog."""

    _KIND_KEYS = {
        netlog.MODEL: "set_offline_kind_model",
        netlog.UPDATE: "set_offline_kind_update",
        netlog.OTHER: "set_offline_kind_other",
    }

    def __init__(self, parent=None, entries=None):
        super().__init__(parent)
        self.setWindowTitle(tr("set_offline_log_title"))
        self.setModal(True)
        rows = list(netlog.entries()) if entries is None else list(entries)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(14)

        intro = QLabel(tr("set_offline_log_intro"))
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        if rows:
            lay.addWidget(self._build_table(rows))
        else:
            empty = QLabel(tr("set_offline_log_empty"))
            empty.setWordWrap(True)
            lay.addWidget(empty)

        # --- довідка: як перевірити самому (незалежно від нашого журналу) ---
        vhead = QLabel(tr("set_offline_verify_title"))
        vhead.setProperty("level", "block")
        lay.addWidget(vhead)
        verify = QLabel(tr("set_offline_verify"))
        verify.setProperty("muted", True)
        verify.setWordWrap(True)
        lay.addWidget(verify)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = QPushButton(tr("common_close"))
        close_btn.setAccessibleName(tr("common_close"))
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)
        self.resize(560, 480)

    def _build_table(self, rows) -> QTableWidget:
        table = QTableWidget(len(rows), 3, self)
        theme.setup_table(table)
        table.setHorizontalHeaderLabels([
            tr("set_offline_col_time"), tr("set_offline_col_host"),
            tr("set_offline_col_kind")])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setFocusPolicy(Qt.NoFocus)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for i, e in enumerate(reversed(rows)):          # найновіші зверху
            ts = e.get("ts")
            when = (time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                    if ts else "")
            kind_key = self._KIND_KEYS.get(e.get("kind"), "set_offline_kind_other")
            table.setItem(i, 0, QTableWidgetItem(when))
            table.setItem(i, 1, QTableWidgetItem(str(e.get("host", ""))))
            table.setItem(i, 2, QTableWidgetItem(tr(kind_key)))
        table.setMinimumHeight(160)
        return table


class KeyCaptureDialog(QDialog):
    """Захоплення клавіші «як у Discord»: слухаємо клавіатуру, показуємо натиснуте
    live і валідовуємо ПІД ЧАС захоплення.

    Правило: рівно ОДНА основна клавіша + >=1 модифікатор (Ctrl/Shift/Alt). Дві
    звичайні клавіші (P+F8), лише-модифікатори, Copilot/F23 та Win-комбо/Alt+Tab —
    відхиляємо з живою підказкою.

    feature/native-hotkeys: глобальний хук тут НЕ потрібен — діалог модальний і
    у фокусі, тож досить Qt keyPressEvent/keyReleaseEvent (+grabKeyboard, щоб
    жодна фокусна дрібниця не перехопила клавішу). Ім'я клавіші беремо з
    nativeVirtualKey() — VK незалежний від розкладки (укр/англ дають те саме).
    Контролер перед показом кличе hotkeys_native.capture_suspend, інакше
    RegisterHotKey «з'їв» би натиск поточної PTT-комбінації."""

    _MODS = ("ctrl", "shift", "alt")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.result_key = None
        self._mods = set()          # затиснуті модифікатори (норм. імена)
        self._mains = set()         # затиснуті основні (не-модифікаторні) клавіші
        self._pending = None        # остання ВАЛІДНА комбінація (її й збережемо)
        self._hint_key = "keycap_listening"

        self.setWindowTitle(tr("keycap_title"))
        self.setModal(True)
        self.setMinimumWidth(380)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)

        prompt = QLabel(tr("keycap_prompt"))
        prompt.setWordWrap(True)
        prompt.setProperty("muted", True)
        lay.addWidget(prompt)

        self._combo = QLabel("…")
        self._combo.setProperty("kbd", True)
        self._combo.setAlignment(Qt.AlignCenter)
        self._combo.setMinimumHeight(44)
        lay.addWidget(self._combo)

        self._hint = QLabel(tr(self._hint_key))
        self._hint.setWordWrap(True)
        self._hint.setProperty("muted", True)
        lay.addWidget(self._hint)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        # NoFocus: інакше Space/Enter із комбінації «клікнув» би фокусну кнопку
        cancel.setFocusPolicy(Qt.NoFocus)
        cancel.clicked.connect(self.reject)
        self._save = QPushButton(tr("keycap_save"))
        self._save.setFocusPolicy(Qt.NoFocus)
        self._save.setEnabled(False)
        self._save.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(self._save)
        lay.addLayout(btns)

        # нативні реєстрації тимчасово відпустити, інакше RegisterHotKey «з'їсть»
        # натиск поточної комбінації і діалог його не побачить (resume в accept/reject)
        from ..hotkeys_native import capture_suspend
        capture_suspend()
        self.grabKeyboard()   # усі клавіші — сюди (навіть якщо фокус «поплив»)

    # --- Qt-події клавіатури (GUI-потік; глобального хука нема) ---
    def keyPressEvent(self, ev):
        if not ev.isAutoRepeat():
            self._on_event("down", self._key_name(ev))
        ev.accept()

    def keyReleaseEvent(self, ev):
        if not ev.isAutoRepeat():
            self._on_event("up", self._key_name(ev))
        ev.accept()

    @staticmethod
    def _key_name(ev) -> str:
        """QKeyEvent → канонічне ім'я через VK (незалежно від розкладки)."""
        from ..hotkeys_native import NAME_BY_VK
        return NAME_BY_VK.get(int(ev.nativeVirtualKey() or 0), "")

    def _on_event(self, event_type: str, name: str):
        name = normalize_name(name)          # ліві/праві модифікатори → канонічні
        if not name:
            return
        down = event_type == "down"
        if down and name == "esc":
            self.reject()
            return
        if name in self._MODS or name == "windows":
            (self._mods.add if down else self._mods.discard)(name)
        else:
            (self._mains.add if down else self._mains.discard)(name)
        self._analyze()
        self._render()

    def _current(self):
        """(комбінація|None, ключ-підказки) з ПОТОЧНО затиснутих клавіш."""
        mods = [m for m in self._MODS if m in self._mods]
        mains = sorted(self._mains)
        if "windows" in self._mods:
            return None, "keycap_reserved"            # Win-комбо зарезервовані
        if any(m == "f23" or m.startswith("copilot") for m in mains):
            return None, "keycap_unsupported"         # Copilot/F23 перехоплює Windows
        if len(mains) >= 2:
            return None, "keycap_one_main"            # дві звичайні клавіші (P+F8)
        if len(mains) == 1:
            main = mains[0]
            if main == "tab" and "alt" in mods:
                return None, "keycap_reserved"        # Alt+Tab
            if not mods:
                return None, "keycap_need_mod"        # звичайна клавіша без модифікатора
            return "+".join(mods + [main]), None
        if mods:
            return None, "keycap_need_key"            # лише модифікатори — чекаємо клавішу
        return None, "keycap_listening"

    def _analyze(self):
        combo, hint = self._current()
        if combo:
            self._pending = combo                     # зафіксувати валідну комбінацію
            self._hint_key = "keycap_ready"
        elif self._pending:
            self._hint_key = "keycap_ready"           # уже маємо валідну — тримаємо її
        else:
            self._hint_key = hint

    def _partial(self) -> str:
        """Прев'ю поточно затиснутих клавіш (поки валідної комбінації ще нема)."""
        keys = [m for m in ("ctrl", "shift", "alt", "windows") if m in self._mods]
        keys += sorted(self._mains)
        return pretty("+".join(keys)) if keys else ""

    def _render(self):
        shown = pretty(self._pending) if self._pending else self._partial()
        self._combo.setText(shown or "…")
        self._hint.setText(tr(self._hint_key))
        self._save.setEnabled(self._pending is not None)

    def _accept(self):
        if self._pending:
            self.result_key = self._pending
            self.accept()

    def accept(self):
        self._finish_capture()
        super().accept()

    def reject(self):
        self._finish_capture()
        super().reject()

    def _finish_capture(self):
        self.releaseKeyboard()
        from ..hotkeys_native import capture_resume
        capture_resume()


class ContextProfileDialog(QDialog):
    """Додати профіль застосунку (feature/context-profiles). Мінімум полів:
    назва, програми (exe через кому), словник, авто-Enter, чи вставляти. Кнопка
    «Взяти з активного вікна» дає 3 с перемкнутись на цільове вікно, потім
    знімає exe (патерн espanso #detect). title_regex у MVP не редагуємо тут —
    лише через файл профілів."""

    _DELAY = 3   # с до знімка активного вікна

    def __init__(self, resolver, dict_names, parent=None):
        super().__init__(parent)
        self._resolver = resolver
        self.result_profile = None
        self._count = 0
        self._timer = None

        self.setWindowTitle(tr("ctx_dlg_add_title"))
        self.setModal(True)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        lay.addWidget(_form_label(tr("ctx_dlg_name")))
        self._name = QLineEdit()
        lay.addWidget(self._name)

        lay.addWidget(_form_label(tr("ctx_dlg_apps")))
        self._apps = QLineEdit()
        self._apps.setPlaceholderText(tr("ctx_dlg_apps_hint"))
        lay.addWidget(self._apps)
        self._take = QPushButton(tr("ctx_take_active"))
        self._take.setFocusPolicy(Qt.NoFocus)
        self._take.clicked.connect(self._start_take)
        lay.addWidget(self._take)
        apps_hint = QLabel(tr("ctx_dlg_apps_hint"))
        apps_hint.setProperty("muted", True)
        apps_hint.setWordWrap(True)
        lay.addWidget(apps_hint)

        lay.addWidget(_form_label(tr("ctx_dlg_dictionary")))
        self._dict = QComboBox()
        self._dict.addItem(tr("ctx_dict_active"), None)   # None → активний словник
        for name in dict_names:
            self._dict.addItem(name, name)
        lay.addWidget(self._dict)

        # feature/output-formats: детермінований профіль форматування виводу
        lay.addWidget(_form_label(tr("ctx_dlg_formatting")))
        self._fmt = QComboBox()
        self._fmt.setAccessibleName(tr("ctx_dlg_formatting"))
        for mode, key in (("plain", "ctx_fmt_plain"), ("markdown", "ctx_fmt_markdown"),
                          ("code", "ctx_fmt_code"), ("letter", "ctx_fmt_letter")):
            self._fmt.addItem(tr(key), mode)
        lay.addWidget(self._fmt)

        self._enabled = QCheckBox(tr("ctx_dlg_enabled"))
        self._enabled.setChecked(True)
        lay.addWidget(self._enabled)
        self._auto = QCheckBox(tr("ctx_dlg_auto_enter"))
        lay.addWidget(self._auto)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        save = QPushButton(tr("keycap_save"))
        save.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _start_take(self):
        """3-секундний відлік → знімок активного вікна (патерн espanso #detect)."""
        self._count = self._DELAY
        self._take.setEnabled(False)
        self._take.setText(tr("ctx_take_counting", n=self._count))
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._count -= 1
        if self._count > 0:
            self._take.setText(tr("ctx_take_counting", n=self._count))
            return
        self._timer.stop()
        self._take.setEnabled(True)
        self._take.setText(tr("ctx_take_active"))
        try:
            ctx = self._resolver.get_window_context()
        except Exception:
            ctx = None
        if ctx and ctx.exe:
            existing = self._apps.text().strip()
            self._apps.setText(f"{existing}, {ctx.exe}" if existing else ctx.exe)
            if not self._name.text().strip():
                self._name.setText(ctx.exe.rsplit(".", 1)[0])

    def _accept(self):
        apps = [a.strip() for a in self._apps.text().split(",") if a.strip()]
        name = self._name.text().strip() or (apps[0] if apps else "")
        self.result_profile = ContextProfile(
            name=name, apps=apps, title_regex=None,
            behavior=Behavior(auto_enter=self._auto.isChecked(),
                              dictionary=self._dict.currentData(),
                              enabled=self._enabled.isChecked(),
                              formatting=self._fmt.currentData() or "plain"))
        self.accept()


class AutoProfileRuleDialog(QDialog):
    """Правило «активне вікно → профіль» (feature/auto-profile). Поля: процес
    (wildcard), фрагмент заголовка (опційно), профіль. Кнопка «Взяти з поточного
    вікна» дає 3 с перемкнутись на цільове вікно й забирає і процес, і заголовок —
    щоб правило створювалось у два кліки."""

    _DELAY = 3

    def __init__(self, resolver, profile_names, parent=None):
        super().__init__(parent)
        self._resolver = resolver
        self.result_rule = None
        self._count = 0
        self._timer = None

        self.setWindowTitle(tr("auto_dlg_title"))
        self.setModal(True)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        lay.addWidget(_form_label(tr("auto_dlg_process")))
        self._process = QLineEdit()
        self._process.setPlaceholderText(tr("auto_dlg_process_hint"))
        lay.addWidget(self._process)

        lay.addWidget(_form_label(tr("auto_dlg_title_frag")))
        self._title = QLineEdit()
        self._title.setPlaceholderText(tr("auto_dlg_title_hint"))
        lay.addWidget(self._title)

        self._take = QPushButton(tr("auto_take_active"))
        self._take.setFocusPolicy(Qt.NoFocus)
        self._take.clicked.connect(self._start_take)
        lay.addWidget(self._take)

        lay.addWidget(_form_label(tr("auto_dlg_profile")))
        self._profile = QComboBox()
        self._profile.setAccessibleName(tr("auto_dlg_profile"))
        for name in profile_names:
            self._profile.addItem(name, name)
        lay.addWidget(self._profile)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        save = QPushButton(tr("keycap_save"))
        save.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _start_take(self):
        self._count = self._DELAY
        self._take.setEnabled(False)
        self._take.setText(tr("auto_take_counting", n=self._count))
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self):
        self._count -= 1
        if self._count > 0:
            self._take.setText(tr("auto_take_counting", n=self._count))
            return
        self._timer.stop()
        self._take.setEnabled(True)
        self._take.setText(tr("auto_take_active"))
        try:
            ctx = self._resolver.get_window_context()
        except Exception:
            ctx = None
        if ctx and ctx.exe:
            self._process.setText(ctx.exe)
            if ctx.title and not self._title.text().strip():
                self._title.setText(ctx.title)

    def _accept(self):
        self.result_rule = AutoProfileRule(
            process=self._process.text().strip(),
            title=self._title.text().strip(),
            profile=self._profile.currentData() or "")
        self.accept()


class OfflineExportWorker(QThread):
    """Фоновий воркер створення офлайн-пакета."""
    progress = Signal(object, object, str)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, target_dir, cfg, selected_ids=None, parent=None):
        super().__init__(parent)
        self.target_dir = target_dir
        self.cfg = cfg
        self.selected_ids = selected_ids
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        from whisper_core.offline_package import export_package, OfflinePackageCancelled

        def _cancel_check():
            return self._is_cancelled

        def _progress_cb(copied, total, fname):
            self.progress.emit(copied, total, fname)

        try:
            pkg_dir = export_package(
                self.target_dir,
                self.cfg,
                selected_component_ids=self.selected_ids,
                progress_cb=_progress_cb,
                cancel_check=_cancel_check,
            )
            if self._is_cancelled:
                self.cancelled.emit()
            else:
                self.finished_ok.emit(pkg_dir)
        except OfflinePackageCancelled:
            self.cancelled.emit()
        except Exception as e:
            self.failed.emit(str(e))


class OfflineExportDialog(QDialog):
    """Діалог створення офлайн-пакета моделей з прогресом і можливістю скасування."""

    def __init__(self, target_dir, components, cfg, parent=None):
        super().__init__(parent)
        self.target_dir = Path(target_dir)
        self.components = components
        self.cfg = cfg
        self.pkg_result = None
        self._worker = None

        self.setWindowTitle(tr("offline_pkg_export_title"))
        self.setModal(True)
        self.setMinimumWidth(500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        path_lbl = QLabel(tr("offline_pkg_export_target_label", path=str(self.target_dir)))
        path_lbl.setWordWrap(True)
        lay.addWidget(path_lbl)

        head_lbl = QLabel(tr("offline_pkg_export_components_head", count=len(components)))
        head_lbl.setProperty("strong", True)
        lay.addWidget(head_lbl)

        total_bytes = sum(c.size_bytes for c in components)
        for comp in components:
            comp_lbl = QLabel(f"• {comp.display_name} ({human_size(comp.size_bytes)})")
            comp_lbl.setProperty("muted", True)
            lay.addWidget(comp_lbl)

        total_lbl = QLabel(tr("offline_pkg_export_total_size", size=human_size(total_bytes)))
        total_lbl.setProperty("strong", True)
        lay.addWidget(total_lbl)

        from whisper_core.offline_package import check_fat32_warning
        fat_warn = check_fat32_warning(self.target_dir, components)
        if fat_warn:
            fat_lbl = QLabel(tr(fat_warn))
            # Колір з ЖИВОЇ палітри, а не літералом: інакше попередження світило б
            # тим самим відтінком у будь-якому кольорі інтерфейсу. Окремого правила
            # QSS для попереджень немає, тому беремо токен так само, як це робить
            # _social_link_qss() для свого акценту.
            fat_lbl.setStyleSheet(f"color: {theme._P['DANGER_MUTED']};")
            fat_lbl.setWordWrap(True)
            lay.addWidget(fat_lbl)

        self._status = QLabel(tr("offline_pkg_export_status_preparing"))
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        total_mb = int(total_bytes // (1024 * 1024)) if total_bytes > 0 else 100
        self._bar = QProgressBar()
        self._bar.setRange(0, max(1, total_mb))
        self._bar.setValue(0)
        lay.addWidget(self._bar)

        btns = QHBoxLayout()
        btns.addStretch()

        self._cancel_btn = QPushButton(tr("common_cancel"))
        self._cancel_btn.setAccessibleName(tr("common_cancel"))
        self._cancel_btn.setToolTip(tr("common_cancel"))
        self._cancel_btn.clicked.connect(self._on_cancel)

        self._start_btn = QPushButton(tr("offline_pkg_export_btn_start"))
        self._start_btn.setAccessibleName(tr("offline_pkg_export_btn_start_acc"))
        self._start_btn.setToolTip(tr("offline_pkg_export_btn_start_tip"))
        self._start_btn.setProperty("accent", True)
        self._start_btn.clicked.connect(self._start_export)

        btns.addWidget(self._cancel_btn)
        btns.addWidget(self._start_btn)
        lay.addLayout(btns)

    def _start_export(self):
        self._start_btn.setEnabled(False)
        self._worker = OfflineExportWorker(self.target_dir, self.cfg, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _on_cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
        else:
            self.reject()

    def _on_progress(self, copied: int, total: int, fname: str):
        total_mb = int(total // (1024 * 1024)) if total and total > 0 else 100
        copied_mb = int(copied // (1024 * 1024)) if copied else 0
        self._bar.setRange(0, max(1, total_mb))
        self._bar.setValue(min(total_mb, copied_mb))
        self._status.setText(tr("offline_pkg_export_status_copying", file=fname))

    def _on_finished(self, pkg_path: Path):
        self.pkg_result = pkg_path
        self._status.setText(tr("offline_pkg_export_success"))
        self._bar.setValue(self._bar.maximum())
        self._start_btn.hide()
        self._cancel_btn.setText(tr("common_ok"))
        self._cancel_btn.clicked.disconnect()
        self._cancel_btn.clicked.connect(self.accept)

    def _on_failed(self, err: str):
        from fronts.desktop.i18n import STRINGS
        err_msg = tr(err) if err in STRINGS["uk"] else err
        self._status.setText(f"{tr('badge_error')}: {err_msg}")
        self._start_btn.setEnabled(True)

    def _on_cancelled(self):
        self._status.setText(tr("offline_pkg_export_cancelled"))
        self.reject()


class OfflineImportWorker(QThread):
    """Фоновий воркер імпорту моделей з офлайн-пакета."""
    progress = Signal(object, object, str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, src_dir, cfg, parent=None):
        super().__init__(parent)
        self.src_dir = src_dir
        self.cfg = cfg

    def run(self):
        from whisper_core.offline_package import import_package

        def _progress_cb(copied, total, fname):
            self.progress.emit(copied, total, fname)

        try:
            res = import_package(self.src_dir, self.cfg, progress=_progress_cb)
            self.finished_ok.emit(res)
        except Exception as e:
            self.failed.emit(str(e))


class OfflineImportDialog(QDialog):
    """Діалог імпорту моделей з офлайн-пакета з прев'ю вмісту та перевіркою сум."""

    def __init__(self, src_dir, cfg, parent=None):
        super().__init__(parent)
        self.src_dir = Path(src_dir)
        self.cfg = cfg
        self.import_result = None
        self._worker = None

        self.setWindowTitle(tr("offline_pkg_import_title"))
        self.setModal(True)
        self.setMinimumWidth(500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(14)

        from whisper_core.offline_package import read_manifest
        from fronts.desktop.i18n import STRINGS
        try:
            self.manifest = read_manifest(self.src_dir)
        except Exception as e:
            err_key = str(e)
            err_msg = tr(err_key) if err_key in STRINGS["uk"] else str(e)
            QMessageBox.warning(parent or self, tr("offline_pkg_import_title"), err_msg)
            raise

        components = [c for c in (self.manifest.get("components") or []) if isinstance(c, dict)]
        total_bytes = sum(int(c.get("size_bytes") or 0) for c in components)

        path_lbl = QLabel(tr("offline_pkg_import_source_label", path=str(self.src_dir)))
        path_lbl.setWordWrap(True)
        lay.addWidget(path_lbl)

        head_lbl = QLabel(tr("offline_pkg_import_components_head", count=len(components)))
        head_lbl.setProperty("strong", True)
        lay.addWidget(head_lbl)

        for comp in components:
            dname = comp.get("display_name") or comp.get("id")
            sz = int(comp.get("size_bytes") or 0)
            comp_lbl = QLabel(f"• {dname} ({human_size(sz)})")
            comp_lbl.setProperty("muted", True)
            lay.addWidget(comp_lbl)

        total_lbl = QLabel(tr("offline_pkg_import_total_size", size=human_size(total_bytes)))
        total_lbl.setProperty("strong", True)
        lay.addWidget(total_lbl)

        self._status = QLabel(tr("offline_pkg_export_status_preparing"))
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        total_mb = int(total_bytes // (1024 * 1024)) if total_bytes > 0 else 100
        self._bar = QProgressBar()
        self._bar.setRange(0, max(1, total_mb))
        self._bar.setValue(0)
        lay.addWidget(self._bar)

        btns = QHBoxLayout()
        btns.addStretch()

        self._cancel_btn = QPushButton(tr("common_cancel"))
        self._cancel_btn.setAccessibleName(tr("common_cancel"))
        self._cancel_btn.setToolTip(tr("common_cancel"))
        self._cancel_btn.clicked.connect(self.reject)

        self._start_btn = QPushButton(tr("offline_pkg_import_btn_start"))
        self._start_btn.setAccessibleName(tr("offline_pkg_import_btn_start_acc"))
        self._start_btn.setToolTip(tr("offline_pkg_import_btn_start_tip"))
        self._start_btn.setProperty("accent", True)
        self._start_btn.clicked.connect(self._start_import)

        btns.addWidget(self._cancel_btn)
        btns.addWidget(self._start_btn)
        lay.addLayout(btns)

    def _start_import(self):
        self._start_btn.setEnabled(False)
        self._worker = OfflineImportWorker(self.src_dir, self.cfg, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, copied: int, total: int, fname: str):
        total_mb = int(total // (1024 * 1024)) if total and total > 0 else 100
        copied_mb = int(copied // (1024 * 1024)) if copied else 0
        self._bar.setRange(0, max(1, total_mb))
        self._bar.setValue(min(total_mb, copied_mb))
        self._status.setText(tr("offline_pkg_import_status_copying", file=fname))

    def _on_finished(self, result):
        self.import_result = result
        self._bar.setValue(self._bar.maximum())
        msg = tr("offline_pkg_import_success_detail", count=len(result.installed), size=human_size(result.total_bytes))
        if result.bad_files:
            msg += "\n" + tr("offline_pkg_import_bad_files_warn") + " " + ", ".join(result.bad_files)
        self._status.setText(msg)
        self._start_btn.hide()
        self._cancel_btn.setText(tr("common_ok"))
        self._cancel_btn.clicked.disconnect()
        self._cancel_btn.clicked.connect(self.accept)

    def _on_failed(self, err: str):
        from fronts.desktop.i18n import STRINGS
        err_msg = tr(err) if err in STRINGS["uk"] else err
        self._status.setText(f"{tr('badge_error')}: {err_msg}")
        self._start_btn.setEnabled(True)


class GpuDownloadDialog(QDialog):
    """Модальна докачка CUDA-рантайму (cuBLAS) з прогресом (feature/gpu).
    exec() == Accepted → рантайм готовий. Патерн воркера/reaping — той самий,
    що в докачці моделі (onboarding.GpuDownloadWorker / _reap_worker)."""

    _MB = 1024 * 1024

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("gpu_dl_title"))
        self.setModal(True)
        self.setMinimumWidth(440)
        self._worker = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)

        self._status = QLabel(tr("gpu_dl_status"))
        self._status.setProperty("strong", True)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._bar = QProgressBar()
        self._bar.setTextVisible(False)
        lay.addWidget(self._bar)

        self._info = QLabel(tr("gpu_connecting"))
        self._info.setProperty("muted", True)
        self._info.setWordWrap(True)
        lay.addWidget(self._info)

        btns = QHBoxLayout()
        btns.addStretch()
        self._cancel = QPushButton(tr("common_cancel"))
        self._cancel.clicked.connect(self.reject)
        self._retry = QPushButton(tr("onb_retry"))
        self._retry.setProperty("accent", True)
        self._retry.clicked.connect(self._start)
        self._retry.hide()
        btns.addWidget(self._cancel)
        btns.addWidget(self._retry)
        lay.addLayout(btns)

        self._start()

    def _start(self):
        self._detach()
        self._status.setText(tr("gpu_dl_status"))
        self._info.setText(tr("gpu_connecting"))
        self._bar.setRange(0, 0)             # обсяг ще невідомий
        self._retry.hide()
        self._cancel.show()
        self._worker = GpuDownloadWorker()
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self._worker.start()

    def _detach(self):
        w = self._worker
        self._worker = None
        if w is None:
            return
        try:
            w.progress.disconnect()
            w.finished_ok.disconnect()
            w.failed.disconnect()
            w.cancelled.disconnect()
        except (RuntimeError, TypeError):
            pass
        w.cancel()
        _reap_worker(w)

    def _on_progress(self, done, total):
        if total:
            self._bar.setRange(0, 1000)
            self._bar.setValue(min(1000, int(done * 1000 / total)))
            self._info.setText(tr("onb_dl_progress",
                                  done=done // self._MB, total=total // self._MB))
        else:
            self._info.setText(tr("onb_dl_progress_indet", done=done // self._MB))

    def _on_done(self):
        self._bar.setRange(0, 1000)
        self._bar.setValue(1000)
        self.accept()

    def _on_failed(self, msg: str):
        logging.warning("Докачка прискорення GPU не вдалась: %s", msg)
        self._bar.setRange(0, 1000)
        self._status.setText(tr("gpu_failed"))
        self._info.setText(tr("gpu_failed_detail"))
        self._retry.show()

    def _on_cancelled(self):
        self.reject()

    def reject(self):
        self._detach()
        super().reject()


# --- feature/punctuation-plus: фонове завантаження компонентів постобробки ---
class _ComponentDownloadWorker(QThread):
    """Тягне завантажуваний компонент постобробки тексту у фоні. install_fn —
    whisper_core.*_download.download_and_install (частотний словник або ONNX-
    модель пунктуатора); target — шлях призначення. Помилку віддаємо рядком, а не
    трейсбеком, як діаризаційний воркер наради."""
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, install_fn, target, parent=None):
        super().__init__(parent)
        self._install_fn = install_fn
        self._target = target

    def run(self):
        try:
            self._install_fn(self._target)
            self.finished_ok.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class ProfileImportConfirmDialog(QDialog):
    """Діалог підтвердження імпорту профілю (Т42)."""

    def __init__(self, parent, info: dict, backup_dir_preview: str):
        super().__init__(parent)
        self.setWindowTitle(tr("set_backup_confirm_title"))
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)

        header = QLabel(tr("set_backup_confirm_header"))
        header.setWordWrap(True)
        lay.addWidget(header)

        if info.get("is_newer_version"):
            warn_ver = QLabel(tr("set_backup_warn_newer", archive_ver=info.get("app_version", "")))
            # WARN-акцент: у theme.py немає окремого токена — GOLD_PRESSED уже
            # застосовується як warn-акцент (glass.py::_tag_accent, суд-3 п.7).
            warn_ver.setStyleSheet(f"color: {theme.GOLD_PRESSED}; font-weight: bold;")
            warn_ver.setWordWrap(True)
            lay.addWidget(warn_ver)

        if info.get("missing_components"):
            missing_str = ", ".join(info["missing_components"])
            warn_miss = QLabel(tr("set_backup_warn_missing", missing=missing_str))
            warn_miss.setWordWrap(True)
            lay.addWidget(warn_miss)

        items_str = "\n• " + "\n• ".join(info.get("files", []))
        items_lbl = QLabel(tr("set_backup_will_replace", items=items_str))
        items_lbl.setWordWrap(True)
        lay.addWidget(items_lbl)

        bak_lbl = QLabel(tr("set_backup_backup_notice", backup_dir=backup_dir_preview))
        bak_lbl.setProperty("muted", True)
        bak_lbl.setWordWrap(True)
        lay.addWidget(bak_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        self.btn_confirm = QPushButton(tr("set_backup_import"))
        self.btn_confirm.setAccessibleName("btn_confirm_import")
        self.btn_confirm.clicked.connect(self.accept)

        self.btn_cancel = QPushButton(tr("set_backup_cancel"))
        self.btn_cancel.setAccessibleName("btn_cancel_import")
        self.btn_cancel.clicked.connect(self.reject)

        btn_row.addWidget(self.btn_confirm)
        btn_row.addWidget(self.btn_cancel)
        lay.addLayout(btn_row)


MAX_BG_FILE_SIZE = 20 * 1024 * 1024  # 20 MB


def validate_custom_bg_file(file_path: Path) -> tuple[bool, str | None]:
    """Перевірка розміру (<=20 МБ) та цілісності зображення (PNG/JPG/WEBP)."""
    try:
        path = Path(file_path)
        if not path.is_file():
            return False, "set_workspace_bg_err_corrupt"
        if path.stat().st_size > MAX_BG_FILE_SIZE:
            return False, "set_workspace_bg_err_size"
        pm = QPixmap(str(path))
        if pm.isNull():
            return False, "set_workspace_bg_err_corrupt"
        return True, None
    except Exception:
        return False, "set_workspace_bg_err_corrupt"


def cleanup_custom_bg_file(cfg) -> None:
    """Видалити скопійований файл тла з user_dir() при зміні режиму з custom."""
    path_str = getattr(cfg, "workspace_custom_bg_path", None)
    if path_str:
        try:
            from whisper_core.paths import user_dir, safe_under
            u_dir = user_dir()
            target = u_dir / path_str
            if target.is_file() and safe_under(u_dir, target):
                target.unlink()
        except OSError:
            pass
        cfg.workspace_custom_bg_path = None


class SettingsPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._restart_pending = False
        cfg = controller.cfg

        self._tabs = QTabWidget()
        self._tabs.addTab(self._tab_scroll(self._models_hub_group(cfg)),
                          tr("set_tab_models"))
        self._tabs.addTab(self._tab_scroll(self._model_group(cfg),
                                           self._corpus_group()),
                          tr("set_tab_recognition"))
        self._tabs.addTab(self._tab_scroll(
            self._record_group(cfg), self._sound_group(cfg), self._hotkeys_group(cfg)),
            tr("set_tab_recording"))
        self._tabs.addTab(self._tab_scroll(
            self._output_group(), self._note_group(cfg), self._context_group(),
            self._autoprofile_group(),
            self._export_group(cfg)), tr("set_tab_output"))
        self._tabs.addTab(self._tab_scroll(self._meeting_group(cfg),
                                           self._obsidian_group(cfg)),
                          tr("set_tab_meeting"))
        self._tabs.addTab(self._tab_scroll(
            self._author_group(),
            self._watch_group(cfg), self._backup_group(), self._system_group(cfg),
            self._protection_group(cfg),
            self._about_group()), tr("set_tab_system"))
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 20, 0)
        root.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 12, 0)
        head.addLayout(page_header(tr("nav_settings"), tr("set_subtitle")))
        root.addLayout(head)
        root.addSpacing(20)
        root.addWidget(self._tabs)
        root.addWidget(self._make_restart_bar())

    @staticmethod
    def _tab_scroll(*cards: QWidget) -> QScrollArea:
        """Окремий короткий скрол для кожної тематичної вкладки налаштувань."""
        content = QWidget()
        col = QVBoxLayout(content)
        col.setContentsMargins(0, 16, 12, 24)
        col.setSpacing(20)
        for card in cards:
            col.addWidget(card)
        col.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    # --- смужка перезапуску (пінована внизу, поза скролом) ---
    def _make_restart_bar(self) -> QWidget:
        """Прихована смужка «Деякі зміни діють після перезапуску» + кнопка
        «Перезапустити зараз». Показується motion.slide_fade_in при першій
        зміні, що набуває чинності лише після перезапуску (модель / обробка /
        тека моделей / мова інтерфейсу). Хост із верхнім відступом ховається
        цілком (layout пропускає прихований віджет) — у idle зайвого проміжку
        нема (як level-host у Диктуванні)."""
        host = QWidget()
        hl = QVBoxLayout(host)
        hl.setContentsMargins(0, 12, 0, 0)
        hl.setSpacing(0)
        bar = QFrame()
        bar.setProperty("glasspanel", True)
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(14)
        lbl = QLabel(tr("set_restart_bar"))
        lbl.setWordWrap(True)
        btn = GlassButton(tr("set_restart_now"))
        btn.clicked.connect(self.controller.restart_app)
        row.addWidget(lbl, stretch=1)
        row.addWidget(btn)
        hl.addWidget(bar)
        host.hide()
        self._restart_host = host
        return host

    def _mark_restart_pending(self):
        """Показати смужку перезапуску (idempotent: вже видиму не переанімовує)."""
        self._restart_pending = True
        motion.slide_fade_in(self._restart_host)   # ТЗ п.9

    # --- ПРО АВТОРА ---
    def _author_group(self):
        """Компактна картка автора вгорі вкладки «Система» (фідбек Миколи 22.07):
        підпис «Зробив Микола Жуковець» + два значки-посилання (GitHub і
        «Підтримати») у тон muted-тексту, тими самими, що в майстрі першого
        запуску. URL — з єдиного джерела fronts/desktop/links.py."""
        frame, lay = _card(tr("set_author_eyebrow"))
        frame.setObjectName("authorCard")

        row = QHBoxLayout()
        row.setSpacing(8)
        author = QLabel(tr("onb_author"))
        author.setWordWrap(True)
        row.addWidget(author)
        row.addStretch(1)
        gh = round_social("fa6b.github", links.GITHUB_URL,
                          name=tr("author_github_name"),
                          tooltip=tr("author_github_hint"))
        gh.setObjectName("authorGithubLink")
        support = round_social("fa6s.heart", links.SUPPORT_URL,
                               name=tr("about_support_link"),
                               tooltip=tr("author_support_hint"))
        support.setObjectName("authorSupportLink")
        row.addWidget(gh)
        row.addWidget(support)
        lay.addLayout(row)

        note = QLabel(tr("author_support_short"))
        note.setProperty("muted", True)
        note.setWordWrap(True)
        lay.addWidget(note)
        return frame

    # --- ПРО ПРОГРАМУ ---
    def _about_group(self):
        # URL соцмереж (усі 4 задані → усі значки показуються).
        # Порожнє значення → відповідний значок не малюється.
        X_URL = "https://x.com/zukovec20653"
        GITHUB_URL = links.GITHUB_URL           # єдине джерело (fronts/desktop/links.py)
        FACEBOOK_URL = "https://www.facebook.com/nikolia.zhukowets"
        INSTAGRAM_URL = "https://www.instagram.com/nikolia.zhukowets/"
        # підтримати автора: monobank (₴) для України + PrivatBank-конверти ($/€) для іноземців
        DONATE_UAH = links.SUPPORT_URL          # єдине джерело (fronts/desktop/links.py)
        DONATE_USD = links.SUPPORT_PRIVAT_USD
        DONATE_EUR = links.SUPPORT_PRIVAT_EUR

        frame, lay = _card(tr("set_about_eyebrow"))
        lead = QLabel(tr("set_about_lead"))
        lead.setProperty("strong", True)
        spaced(lead)
        lay.addWidget(lead)

        body = QLabel(tr("set_about_body"))
        spaced(body, rich=True)      # опис із <b>/<br> — просторіші рядки, HTML зберегти
        lay.addWidget(body)

        version = QLabel(tr("set_version", ver=__version__))
        version.setProperty("muted", True)
        lay.addWidget(version)

        author = QLabel(tr("set_author"))
        author.setWordWrap(True)
        lay.addWidget(author)

        socials = QHBoxLayout()
        socials.setSpacing(10)
        for icon_name, url in (
            ("fa6b.x-twitter", X_URL),
            ("fa6b.github", GITHUB_URL),
            ("fa6b.facebook", FACEBOOK_URL),
            ("fa6b.instagram", INSTAGRAM_URL),
        ):
            if url:
                socials.addWidget(round_social(icon_name, url))
        socials.addStretch()
        lay.addLayout(socials)

        # рядок підтримки автора — валютні лінки (₴ моно + $ приват), лише задані.
        # Колір лінка — inline (style="color:…"), НЕ QPalette.Link: жива зміна теми
        # НЕ перечитує Link у вже збудованих QLabel (перевірено — лишався б денним
        # золотом), а inline-колір перебудовуємо restyle-хуком під активний акцент.
        def _support_text():
            parts = []
            if DONATE_UAH:
                parts.append('<a href="{}" style="color:{};">monobank ₴</a>'
                             .format(DONATE_UAH, theme.GOLD))
            if DONATE_USD:
                parts.append('<a href="{}" style="color:{};">PrivatBank $</a>'
                             .format(DONATE_USD, theme.GOLD))
            if DONATE_EUR:
                parts.append('<a href="{}" style="color:{};">PrivatBank €</a>'
                             .format(DONATE_EUR, theme.GOLD))
            # Цифрові гроші — це АДРЕСА, не посилання: браузеру відкривати нічого.
            # Тому власна схема "balachky-copy:" — обробник нижче копіює адресу в
            # буфер обміну. Власник 25.07: гаманці мусять бути видні тут же, а не
            # лише в меню за круглою кнопкою.
            for label, addr in (("USDT (TRC-20)", links.SUPPORT_USDT_TRC20),
                                ("Bitcoin", links.SUPPORT_BTC),
                                ("Ethereum", links.SUPPORT_ETH)):
                if addr:
                    parts.append('<a href="balachky-copy:{}" style="color:{};">{}</a>'
                                 .format(addr, theme.GOLD, label))
            return tr("about_support") + " " + " · ".join(parts)
        if DONATE_UAH or DONATE_USD or DONATE_EUR:
            support = QLabel(_support_text())
            # НЕ openExternalLinks: обробляємо самі, бо адреси гаманців копіюються,
            # а банківські посилання відкриваються у браузері.
            support.setOpenExternalLinks(False)
            support.setTextInteractionFlags(Qt.TextBrowserInteraction)

            def _on_support_link(href: str, _w=None):
                if href.startswith("balachky-copy:"):
                    addr = href.split(":", 1)[1]
                    QApplication.clipboard().setText(addr)
                    motion.toast(self.window() or self, tr("toast_address_copied"))
                else:
                    QDesktopServices.openUrl(QUrl(href))

            support.linkActivated.connect(_on_support_link)
            support.setWordWrap(True)
            support.setProperty("muted", True)
            support.setProperty("gold", True)
            theme.register_restyle_call(support, lambda w: w.setText(_support_text()))

            # Крипто-гаманець — це адреса, а не посилання: у QLabel з
            # openExternalLinks він не працює (браузеру нічого відкривати), тож
            # решту способів віддаємо через уже наявне меню підтримки — одна
            # точка правди, копіювання в буфер там уже реалізоване.
            more = round_social("fa6s.heart", links.SUPPORT_MONO_UAH,
                                name=tr("about_support_more"),
                                tooltip=tr("about_support_more_hint"))
            more.setObjectName("aboutSupportMoreBtn")

            support_row = QHBoxLayout()
            support_row.setSpacing(8)
            support_row.addWidget(support, stretch=1)
            support_row.addWidget(more, alignment=Qt.AlignTop)
            lay.addLayout(support_row)

        license_lbl = QLabel(tr("set_license"))
        license_lbl.setOpenExternalLinks(True)
        license_lbl.setWordWrap(True)
        license_lbl.setProperty("muted", True)
        # нічний режим: inline %%ACCENT%% у лінку підставляється в tr() один раз —
        # re-run tr() на зміні теми дає свіжий акцент (золото вдень / червоне вночі)
        theme.register_restyle_call(license_lbl, lambda w: w.setText(tr("set_license")))
        lay.addWidget(license_lbl)

        # кнопка відкриває файл ліцензій третіх сторін у системному переглядачі
        tp_row = QHBoxLayout()
        self._third_party_btn = QPushButton(tr("set_third_party_btn"))
        self._third_party_btn.clicked.connect(self._open_third_party)
        tp_row.addWidget(self._third_party_btn)
        tp_row.addStretch()
        lay.addLayout(tp_row)
        return frame

    def _open_third_party(self, _checked=False):
        """Відкрити THIRD-PARTY-NOTICES.txt (ліцензії сторонніх компонентів) у
        системному переглядачі. Файл пакується поруч із програмою."""
        p = wc_paths.bundled_doc("THIRD-PARTY-NOTICES.txt")
        if p is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))
        else:
            QMessageBox.information(self, tr("set_third_party_btn"),
                                    tr("set_third_party_missing"))

    def _on_tab_changed(self, index: int):
        if index == 0:
            self._refresh_models_hub()

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        return human_size(size_bytes)

    def _find_tab_index(self, tab_key: str) -> int:
        """Пошук індексу вкладки за ключем локалізації назви."""
        label = tr(tab_key)
        for i in range(self._tabs.count()):
            if self._tabs.tabText(i) == label:
                return i
        return 0

    # --- ЦЕНТР МОДЕЛЕЙ ---
    def _models_hub_group(self, cfg):
        frame, lay = _card(tr("models_hub_eyebrow"), hint_key="models_hub_hint", title_key="models_hub_title")

        self._models_hub_rows = {}
        from whisper_core.models_hub import get_models_hub_status, get_total_models_disk_size

        items = get_models_hub_status(cfg)
        # Полегшена збірка йде без рушія озвучення — тоді картка голосів НЕ має
        # видавати «Активна: …» за робочий стан і НЕ пропонує активацію
        # (в решті програми кнопка «Прослухати» про це вже чесно попереджає).
        self._tts_engine_absent = not _tts_engine_available()
        for item in items:
            comp_box = QFrame()
            comp_box.setProperty("card", True)
            comp_lay = QVBoxLayout(comp_box)
            comp_lay.setContentsMargins(16, 12, 16, 14)
            comp_lay.setSpacing(8)

            top_row = QHBoxLayout()
            top_row.setSpacing(6)

            title_lbl = QLabel(tr(item.title_key))
            title_lbl.setProperty("strong", True)

            top_row.addWidget(title_lbl)

            if item.component_id == "tts":
                tts_info = info_hint("tts_engine_info_hint", clickable=False)
                tts_info.setToolTip(tr("tts_engine_info_hint"))
                tts_info.setAccessibleName(tr("tts_engine_info_hint"))
                top_row.addWidget(tts_info)

            top_row.addStretch(1)

            status_text = (
                tr("models_hub_status_downloaded", size=self._format_size(item.size_bytes))
                if item.is_downloaded
                else tr("models_hub_status_missing")
            )
            status_lbl = QLabel(status_text)
            status_lbl.setProperty("muted", True)
            top_row.addWidget(status_lbl)
            comp_lay.addLayout(top_row)

            mid_row = QHBoxLayout()
            mid_row.setSpacing(6)

            engine_absent = (item.component_id == "tts" and self._tts_engine_absent)
            active_name_str = (
                tr(item.active_name_key, name=item.active_name_param)
                if item.active_name_param
                else tr(item.active_name_key)
            )
            if engine_absent:
                active_lbl = QLabel(tr("models_hub_tts_engine_absent"))
                active_lbl.setWordWrap(True)
                active_lbl.setObjectName("models_hub_tts_engine_absent")
            else:
                active_lbl = QLabel(tr("models_hub_active_label", name=active_name_str))

            mem_lbl = QLabel(tr(item.memory_note_key))
            mem_lbl.setObjectName("models_hub_memory_note")
            mem_lbl.setProperty("muted", True)

            mid_row.addWidget(active_lbl, stretch=1)
            mid_row.addWidget(mem_lbl)
            comp_lay.addLayout(mid_row)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(10)

            rec_acc_key = f"models_hub_{item.component_id}_rec_acc"
            adv_acc_key = f"models_hub_{item.component_id}_adv_acc"

            cid = item.component_id
            btn_rec = None
            if not engine_absent:      # нема рушія — активувати нічого
                btn_rec = GlassButton(tr("models_hub_btn_recommended"))
                btn_rec.setAccessibleName(tr(rec_acc_key))
                btn_rec.setToolTip(tr(rec_acc_key))
                btn_rec.clicked.connect(lambda _checked=False, c=cid: self._on_models_hub_recommended(c))

            btn_adv = GlassButton(tr("models_hub_btn_advanced"))
            btn_adv.setAccessibleName(tr(adv_acc_key))
            btn_adv.setToolTip(tr(adv_acc_key))
            btn_adv.clicked.connect(lambda _checked=False, c=cid: self._on_models_hub_advanced(c))

            if btn_rec is not None:
                btn_row.addWidget(btn_rec)
            btn_row.addWidget(btn_adv)

            btn_engine_action = None
            engine_sub_lbl = None
            if item.component_id == "tts":
                from whisper_core import paths
                from whisper_core.tts import engine_manager
                arch_bytes, disk_bytes = engine_manager.expected_engine_sizes()
                dl_size = human_size(arch_bytes)
                disk_size = human_size(disk_bytes)
                engine_inst = paths.tts_engine_exe_path().exists()

                btn_engine_action = GlassButton(
                    tr("tts_engine_delete_btn") if engine_inst else tr("tts_engine_download_btn")
                )
                btn_engine_action.setObjectName("btn_tts_engine_action")
                btn_engine_action.setToolTip(btn_engine_action.text())
                btn_engine_action.setAccessibleName(btn_engine_action.text())
                btn_engine_action.clicked.connect(self._on_tts_engine_action_clicked)
                btn_row.addWidget(btn_engine_action)

                engine_sub_lbl = QLabel(
                    tr("tts_engine_delete_subtext", disk_size=disk_size) if engine_inst
                    else tr("tts_engine_download_subtext", dl_size=dl_size, disk_size=disk_size)
                )
                engine_sub_lbl.setWordWrap(True)
                engine_sub_lbl.setProperty("muted", True)
                engine_sub_lbl.setObjectName("tts_engine_subtext")

            btn_row.addStretch()
            comp_lay.addLayout(btn_row)

            if engine_sub_lbl is not None:
                comp_lay.addWidget(engine_sub_lbl)

            lay.addWidget(comp_box)

            self._models_hub_rows[item.component_id] = {
                "box": comp_box,
                "status_lbl": status_lbl,
                "active_lbl": active_lbl,
                "btn_rec": btn_rec,
                "btn_adv": btn_adv,
                "btn_engine_action": btn_engine_action,
                "engine_sub_lbl": engine_sub_lbl,
            }

        summary_row = QHBoxLayout()
        summary_row.setContentsMargins(4, 10, 4, 0)
        summary_row.setSpacing(16)

        total_bytes = get_total_models_disk_size(cfg)
        self._models_hub_total_lbl = QLabel(
            tr("models_hub_total_disk", size=self._format_size(total_bytes))
        )
        self._models_hub_total_lbl.setProperty("strong", True)
        self._models_hub_total_lbl.setWordWrap(True)

        btn_open_folder = GlassButton(tr("models_hub_open_folder"))
        btn_open_folder.setAccessibleName(tr("models_hub_open_folder_acc"))
        btn_open_folder.clicked.connect(self._on_open_models_folder)

        summary_row.addWidget(self._models_hub_total_lbl)
        summary_row.addStretch()
        summary_row.addWidget(btn_open_folder)

        lay.addLayout(summary_row)

        offline_row = QHBoxLayout()
        offline_row.setContentsMargins(4, 8, 4, 0)
        offline_row.setSpacing(10)

        self._btn_export_pkg = GlassButton(tr("offline_pkg_btn_export"))
        self._btn_export_pkg.setAccessibleName(tr("offline_pkg_btn_export_acc"))
        self._btn_export_pkg.setToolTip(tr("offline_pkg_btn_export_tip"))
        self._btn_export_pkg.clicked.connect(self._on_offline_export)

        self._btn_import_pkg = GlassButton(tr("offline_pkg_btn_import"))
        self._btn_import_pkg.setAccessibleName(tr("offline_pkg_btn_import_acc"))
        self._btn_import_pkg.setToolTip(tr("offline_pkg_btn_import_tip"))
        self._btn_import_pkg.clicked.connect(self._on_offline_import)

        offline_row.addWidget(self._btn_export_pkg)
        offline_row.addWidget(self._btn_import_pkg)
        offline_row.addStretch()

        lay.addLayout(offline_row)
        return frame

    def _on_offline_export(self, _checked=False):
        from whisper_core.offline_package import get_available_components
        cfg = self.controller.cfg
        components = get_available_components(cfg)
        if not components:
            QMessageBox.information(
                self,
                tr("offline_pkg_export_title"),
                tr("offline_pkg_export_no_components"),
            )
            return

        target_dir = QFileDialog.getExistingDirectory(
            self, tr("offline_pkg_select_export_dir")
        )
        if not target_dir:
            return

        dlg = OfflineExportDialog(target_dir, components, cfg, parent=self)
        dlg.exec()

    def _on_offline_import(self, _checked=False):
        src_dir = QFileDialog.getExistingDirectory(
            self, tr("offline_pkg_select_import_dir")
        )
        if not src_dir:
            return

        try:
            dlg = OfflineImportDialog(src_dir, self.controller.cfg, parent=self)
        except Exception:
            return

        if dlg.exec() == QDialog.Accepted:
            self._refresh_models_hub()

    def _on_models_hub_recommended(self, component_id: str):
        from whisper_core.models_hub import get_models_hub_status
        cfg = self.controller.cfg
        statuses = {item.component_id: item for item in get_models_hub_status(cfg)}
        item = statuses.get(component_id)
        if item and not item.is_downloaded:
            hint_keys = {
                "stt": "models_hub_download_stt_hint",
                "diarization": "models_hub_download_diar_hint",
                "protocol": "models_hub_download_proto_hint",
                "tts": "models_hub_download_tts_hint",
                "punctuator": "models_hub_download_punc_hint",
            }
            QMessageBox.information(
                self,
                tr("models_hub_title"),
                tr(hint_keys.get(component_id, "models_hub_status_missing"))
            )
            self._on_models_hub_advanced(component_id)
            return

        if component_id == "stt":
            cfg.model_name = "large-v3-turbo"
            cfg.save()
            if hasattr(self, "_refresh_model_meta"):
                self._refresh_model_meta()
        elif component_id == "diarization":
            cfg.diarization_enabled = True
            cfg.save()
        elif component_id == "protocol":
            cfg.protocol_model = "fast"
            cfg.save()
        elif component_id == "tts":
            cfg.tts_voice_uk = "styletts2_ua"
            cfg.tts_enabled = True
            cfg.save()
        elif component_id == "punctuator":
            cfg.punctuator_enabled = True
            cfg.save()
            if hasattr(self, "_refresh_textproc_controls"):
                self._refresh_textproc_controls()
        self._refresh_models_hub()

    def _on_models_hub_advanced(self, component_id: str):
        if component_id == "stt":
            self._tabs.setCurrentIndex(self._find_tab_index("set_tab_recognition"))
        elif component_id in ("diarization", "protocol"):
            win = self.window()
            if hasattr(win, "set_page"):
                win.set_page(2)
        elif component_id == "tts":
            if hasattr(self.controller, "open_voice_manager"):
                self.controller.open_voice_manager()
        elif component_id == "punctuator":
            self._tabs.setCurrentIndex(self._find_tab_index("set_tab_recording"))

    def _build_models_folder_menu(self):
        """Меню тек з моделями — ПОБУДОВА без показу.

        Розділено свідомо: тест, який патчив QMenu.exec через mock, у PySide6
        не перехоплював Shiboken-метод — меню реально відкривалось і в
        offscreen чекало вічно, тобто `unittest discover` не завершувався
        ніколи (суд 24.07 відтворив тричі). Тепер тест перевіряє побудоване
        меню і exec не кличе взагалі."""
        from PySide6.QtWidgets import QMenu
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from whisper_core.models_hub import get_model_dirs

        menu = QMenu(self)
        dirs = get_model_dirs(self.controller.cfg)
        for i, (key, path) in enumerate(dirs):
            if i == len(dirs) - 1:
                menu.addSeparator()
            label = tr(key)
            action = menu.addAction(label)
            p = path
            action.triggered.connect(
                lambda _c=False, p_path=p: (
                    p_path.mkdir(parents=True, exist_ok=True),
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(p_path)))
                )
            )

        return menu

    def _on_open_models_folder(self, _checked=False):
        from PySide6.QtGui import QCursor

        menu = self._build_models_folder_menu()
        btn = self.sender()
        if btn and hasattr(btn, "mapToGlobal"):
            pos = btn.mapToGlobal(btn.rect().bottomLeft())
            menu.exec(pos)
        else:
            menu.exec(QCursor.pos())

    def _on_tts_engine_action_clicked(self):
        from whisper_core import paths
        from whisper_core.tts import engine_manager

        # БЛОКЕР 4: Першим ділом перевірка наявності публічного ключа підпису!
        pub_key = engine_manager.get_public_key().strip()
        if not pub_key:
            QMessageBox.warning(self, tr("models_hub_title"), tr("tts_engine_no_key"))
            return

        engine_inst = paths.tts_engine_exe_path().exists()
        is_incompatible = False
        manifest_path = paths.tts_engine_dir() / "engine_manifest.json"
        if engine_inst and manifest_path.exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                engine_manager.validate_manifest_compatibility(manifest_data)
            except engine_manager.IncompatibleVersionError:
                is_incompatible = True

        if engine_inst and not is_incompatible:
            ctrl = getattr(self.controller, "_tts_controller", None)
            shutdown_fn = ctrl.shutdown if ctrl else None
            engine_manager.delete_engine(sidecar_shutdown_fn=shutdown_fn)
            self._refresh_models_hub()
            return

        # Завантажити опис пакета з мережі (БЛОКЕР 4)
        try:
            manifest = engine_manager.fetch_engine_manifest()
        except Exception as exc:
            QMessageBox.warning(self, tr("models_hub_title"), f"{tr('tts_engine_offline')}\n({exc})")
            return

        # БЛОКЕР 2: Перевірка сумісності в робочому шляху
        try:
            engine_manager.validate_manifest_compatibility(manifest)
        except engine_manager.IncompatibleVersionError as exc:
            QMessageBox.warning(self, tr("models_hub_title"), str(exc))
            return

        # Перевірка Ed25519-підпису
        try:
            engine_manager.verify_manifest_signature(manifest)
        except engine_manager.NoPublicKeyError:
            QMessageBox.warning(self, tr("models_hub_title"), tr("tts_engine_no_key"))
            return
        except engine_manager.SignatureError as exc:
            QMessageBox.warning(self, tr("models_hub_title"), str(exc))
            return

        # БЛОКЕР 5: Перевірка вільного місця на диску (архів + розпаковане)
        arch_fallback, ext_fallback = engine_manager.expected_engine_sizes()
        ext_size = manifest.get("extracted_size_bytes", ext_fallback)
        arch_size = manifest.get("archive_size_bytes", arch_fallback)
        try:
            engine_manager.check_disk_space(ext_size, arch_size)
        except engine_manager.InsufficientDiskSpaceError as exc:
            QMessageBox.warning(
                self, tr("models_hub_title"),
                tr("tts_engine_nospace", req_size=human_size(exc.required_bytes),
                   avail_size=human_size(exc.available_bytes))
            )
            return

        dl_url = manifest.get("download_url", "")
        sha256 = manifest.get("sha256", "")
        dest = paths.user_dir() / "temp" / "balachky-tts-worker.zip"

        try:
            engine_manager.download_file(dl_url, dest, arch_size)
            engine_manager.verify_archive_hash(dest, sha256)
            installed_dir = engine_manager.install_engine_from_manifest(manifest, dest)

            # Зберегти маніфест локально для подальших перевірок сумісності
            local_manifest_path = installed_dir / "engine_manifest.json"
            with open(local_manifest_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)

        except engine_manager.InsufficientDiskSpaceError as exc:
            QMessageBox.warning(
                self, tr("models_hub_title"),
                tr("tts_engine_nospace", req_size=human_size(exc.required_bytes),
                   avail_size=human_size(exc.available_bytes))
            )
        except Exception as exc:
            QMessageBox.warning(self, tr("models_hub_title"), f"{tr('tts_engine_offline')}\n({exc})")

        self._refresh_models_hub()

    def _refresh_models_hub(self):
        if not hasattr(self, "_models_hub_rows"):
            return
        from whisper_core.models_hub import get_models_hub_status, get_total_models_disk_size
        from whisper_core import paths
        from whisper_core.tts import engine_manager
        cfg = self.controller.cfg
        items = get_models_hub_status(cfg)
        for item in items:
            cid = item.component_id
            if cid in self._models_hub_rows:
                row = self._models_hub_rows[cid]
                status_text = (
                    tr("models_hub_status_downloaded", size=self._format_size(item.size_bytes))
                    if item.is_downloaded
                    else tr("models_hub_status_missing")
                )
                row["status_lbl"].setText(status_text)
                if cid == "tts" and getattr(self, "_tts_engine_absent", False):
                    row["active_lbl"].setText(tr("models_hub_tts_engine_absent"))
                    continue
                active_name_str = (
                    tr(item.active_name_key, name=item.active_name_param)
                    if item.active_name_param
                    else tr(item.active_name_key)
                )
                row["active_lbl"].setText(
                    tr("models_hub_active_label", name=active_name_str))

                if cid == "tts" and row.get("btn_engine_action") and row.get("engine_sub_lbl"):
                    arch_bytes, disk_bytes = engine_manager.expected_engine_sizes()
                    dl_size = human_size(arch_bytes)
                    disk_size = human_size(disk_bytes)
                    engine_inst = paths.tts_engine_exe_path().exists()
                    btn = row["btn_engine_action"]
                    sub = row["engine_sub_lbl"]
                    status_lbl = row["status_lbl"]

                    is_incompatible = False
                    manifest_path = paths.tts_engine_dir() / "engine_manifest.json"
                    if engine_inst and manifest_path.exists():
                        try:
                            with open(manifest_path, "r", encoding="utf-8") as f:
                                m_data = json.load(f)
                            engine_manager.validate_manifest_compatibility(m_data)
                        except engine_manager.IncompatibleVersionError:
                            is_incompatible = True

                    if not engine_inst:
                        status_lbl.setText(tr("tts_engine_status_missing"))
                        btn.setText(tr("tts_engine_download_btn"))
                        btn.setToolTip(tr("tts_engine_download_btn"))
                        btn.setAccessibleName(tr("tts_engine_download_btn"))
                        sub.setText(tr("tts_engine_download_subtext", dl_size=dl_size, disk_size=disk_size))
                    elif is_incompatible:
                        status_lbl.setText(tr("tts_engine_status_incompatible"))
                        btn.setText(tr("tts_engine_update_btn"))
                        btn.setToolTip(tr("tts_engine_update_btn"))
                        btn.setAccessibleName(tr("tts_engine_update_btn"))
                        sub.setText(tr("tts_engine_update_needed"))
                    else:
                        status_lbl.setText(tr("tts_engine_status_downloaded", size=disk_size))
                        btn.setText(tr("tts_engine_delete_btn"))
                        btn.setToolTip(tr("tts_engine_delete_btn"))
                        btn.setAccessibleName(tr("tts_engine_delete_btn"))
                        sub.setText(tr("tts_engine_delete_subtext", disk_size=disk_size))

        total_bytes = get_total_models_disk_size(cfg)
        if hasattr(self, "_models_hub_total_lbl"):
            self._models_hub_total_lbl.setText(
                tr("models_hub_total_disk", size=self._format_size(total_bytes))
            )

    # --- МОДЕЛЬ РОЗПІЗНАВАННЯ ---
    def _model_group(self, cfg):
        frame, lay = _card(tr("set_model_eyebrow"))
        g = _grid()

        # Пресети моделі розпізнавання (whisper_core/stt_presets.py): ширший вибір,
        # ніж два зашитих радіо — small/medium для слабких ПК + опція власної
        # моделі (тека або HF-id). Дефолт НЕ міняємо: активну задає cfg.model_name.
        from whisper_core import stt_presets
        self._stt_presets = stt_presets
        modelbox = QVBoxLayout()
        modelbox.setSpacing(8)
        self._model = QComboBox()
        self._model.setAccessibleName(tr("set_model_label"))
        for preset in stt_presets.PRESETS:
            self._model.addItem(tr(preset.label_key), preset.name)
        # власна модель (тека/HF-id) з попереднього запуску не є пресетом —
        # додаємо окремим пунктом, щоб вибір коректно round-trip’ився
        if not stt_presets.is_preset(cfg.model_name):
            self._model.addItem(self._custom_model_label(cfg.model_name),
                                cfg.model_name)
        idx = self._model.findData(cfg.model_name)
        if idx >= 0:
            self._model.setCurrentIndex(idx)
        self._model.currentIndexChanged.connect(self._on_model)
        self._model.setMaximumWidth(_CTRL_MAX)
        modelbox.addWidget(self._model)

        # підказка про розмір/відеопам’ять активного пресета + лінк «Про модель»
        self._model_hint = QLabel("")
        self._model_hint.setProperty("muted", True)
        self._model_hint.setWordWrap(True)
        modelbox.addWidget(self._model_hint)
        self._model_about = QLabel("")
        self._model_about.setProperty("muted", True)
        self._model_about.setOpenExternalLinks(True)
        self._model_about.setWordWrap(True)
        # нічний режим: лінк «Про модель» має inline-колір (не QPalette.Link, який
        # жива зміна теми не перечитує) — перебудовуємо текст на зміні теми
        theme.register_restyle(self._refresh_model_meta)
        modelbox.addWidget(self._model_about)

        # власна модель: тека з готовою моделлю faster-whisper або HF-id
        custrow = QHBoxLayout()
        custrow.setSpacing(10)
        add_folder = QPushButton(tr("stt_model_add_folder"))
        add_folder.setToolTip(tr("stt_model_add_folder_tip"))
        add_folder.clicked.connect(self._add_stt_folder)
        add_hf = QPushButton(tr("stt_model_add_hf"))
        add_hf.setToolTip(tr("stt_model_add_hf_tip"))
        add_hf.clicked.connect(self._add_stt_hf)
        custrow.addWidget(add_folder)
        custrow.addWidget(add_hf)
        custrow.addStretch()
        modelbox.addLayout(custrow)

        self._model_note = _note(tr("set_restart"))
        g.addWidget(_labeled_hint(tr("set_model_label"), "hint_model",
                                  clickable=True, title_key="hint_model_title"),
                    0, 0)
        g.addLayout(modelbox, 0, 1)
        g.addWidget(self._model_note, 1, 1)

        # feature/multilang-asr (Т44): мова розпізнавання — «Автоматично» або
        # будь-яка з ~99 мов Whisper. «Автоватично» → рушій передає language=None.
        from whisper_core import languages as wc_languages
        lang = QComboBox()
        lang.setAccessibleName(tr("set_dict_lang"))
        lang.addItem(tr("set_dict_lang_auto"), wc_languages.AUTO)
        for code, name in wc_languages.ordered_for_ui(current_language()):
            lang.addItem(name, code)
        idx = lang.findData(cfg.language)
        if idx < 0:                      # порожнє/невідоме → «Автоматично» (перший пункт)
            idx = 0
        lang.setCurrentIndex(idx)
        lang.currentIndexChanged.connect(
            lambda _i, c=lang: self.controller.set_language(c.currentData()))
        lang.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_form_label(tr("set_dict_lang")), 2, 0)
        g.addWidget(lang, 2, 1)

        # feature/gpu: рантайм може бути докачаний (cuda_runtime) або системний.
        # Радіо GPU активне, коли рантайм реально доступний. Коли NVIDIA є, а
        # рантайму ще нема — замість вимкненого радіо пропонуємо кнопку докачки.
        gpu_ok = cuda_runtime_available()
        show_gpu_download = cuda_runtime.gpu_present() and not gpu_ok
        devrow = QHBoxLayout()
        devrow.setSpacing(20)
        devs = QButtonGroup(self)
        self._devs = devs
        self._cuda_radio = None
        for dev, label in (("cpu", tr("set_cpu")), ("cuda", tr("set_gpu"))):
            rb = QRadioButton(label)
            rb.setProperty("dev", dev)
            rb.setChecked(cfg.device == dev)
            if dev == "cuda":
                self._cuda_radio = rb
                if not gpu_ok:
                    rb.setEnabled(False)
            devs.addButton(rb)
            devrow.addWidget(rb)
        devrow.addStretch()
        g.addWidget(_labeled_hint(tr("set_processing"), "hint_device"), 3, 0)
        g.addLayout(devrow, 3, 1)

        # рядок під радіо: кнопка докачки (є NVIDIA без рантайму) або пояснення
        self._gpu_dl_btn = None
        self._gpu_dl_hint = None
        extra = QVBoxLayout()
        extra.setSpacing(8)
        if show_gpu_download:
            self._gpu_dl_btn = GlassButton(tr("set_gpu_download"))
            self._gpu_dl_btn.clicked.connect(self._on_gpu_download)
            btnrow = QHBoxLayout()
            btnrow.addWidget(self._gpu_dl_btn)
            btnrow.addStretch()
            extra.addLayout(btnrow)
            self._gpu_dl_hint = QLabel(tr("set_gpu_download_hint"))
            self._gpu_dl_hint.setProperty("muted", True)
            self._gpu_dl_hint.setWordWrap(True)
            extra.addWidget(self._gpu_dl_hint)
        elif not gpu_ok:
            hint = QLabel(tr("set_gpu_hint"))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            extra.addWidget(hint)
        g.addLayout(extra, 4, 1)

        self._dev_note = _note(tr("set_restart"))
        g.addWidget(self._dev_note, 5, 1)
        devs.buttonClicked.connect(self._on_device)

        # точність обчислень (compute_type): керує відео-/оперативною пам’яттю та
        # якістю. Список залежить від режиму «Обробка» вище (CPU → лише int8);
        # діє після перезапуску, як device/model.
        self._compute = QComboBox()
        self._compute.setAccessibleName(tr("set_compute"))
        self._compute.setMaximumWidth(_CTRL_MAX)
        self._sync_compute_options(cfg.device, cfg.compute_type)
        self._compute.currentIndexChanged.connect(self._on_compute)
        g.addWidget(_labeled_hint(tr("set_compute"), "hint_compute",
                                  clickable=True, title_key="hint_compute_title"),
                    6, 0)
        g.addWidget(self._compute, 6, 1)
        self._compute_note = _note(tr("set_restart"))
        g.addWidget(self._compute_note, 7, 1)

        # feature/model-idle-unload: один таймер для STT і локальної AI-моделі.
        idlebox = QVBoxLayout()
        idlebox.setSpacing(6)
        self._idle_unload = QComboBox()
        self._idle_unload.setAccessibleName(tr("set_model_idle_label"))
        from whisper_core.config import MODEL_IDLE_UNLOAD_OPTIONS
        keys = ("never", "5m", "10m", "30m", "1h", "2h", "4h")
        for key, seconds in zip(keys, MODEL_IDLE_UNLOAD_OPTIONS):
            self._idle_unload.addItem(tr("set_model_idle_" + key), seconds)
        idx = self._idle_unload.findData(
            int(getattr(cfg, "model_idle_unload_seconds", 600) or 0))
        self._idle_unload.setCurrentIndex(idx if idx >= 0 else 2)
        self._idle_unload.currentIndexChanged.connect(self._on_idle_unload)
        self._idle_unload.setMaximumWidth(_CTRL_MAX)
        idlebox.addWidget(self._idle_unload)
        idle_hint = QLabel(tr("set_model_idle_hint"))
        idle_hint.setProperty("muted", True)
        idle_hint.setWordWrap(True)
        idlebox.addWidget(idle_hint)
        g.addWidget(_form_label(tr("set_model_idle_label")), 8, 0)
        g.addLayout(idlebox, 8, 1)

        # тека моделей: у людей можуть бути моделі від інших програм
        dirbox = QVBoxLayout()
        dirbox.setSpacing(8)
        self._dir_label = QLabel()
        self._dir_label.setWordWrap(True)
        self._set_dir_text(cfg.model_dir)
        pick_dir = QPushButton(tr("common_change"))
        pick_dir.clicked.connect(self._on_pick_model_dir)
        std_dir = QPushButton(tr("set_default"))
        std_dir.clicked.connect(self._on_std_model_dir)
        dirbox.addWidget(self._dir_label)
        diractions = QHBoxLayout()
        diractions.setSpacing(10)
        diractions.addWidget(pick_dir)
        diractions.addWidget(std_dir)
        diractions.addStretch()
        dirbox.addLayout(diractions)
        g.addWidget(_labeled_hint(tr("common_model_folder"), "hint_model_dir"),
                    9, 0)
        g.addLayout(dirbox, 9, 1)
        self._dir_note = _note(tr("set_restart"))
        g.addWidget(self._dir_note, 10, 1)

        # видалення невживаної моделі, щоб звільнити місце  # feature/delete-model
        delrow = QHBoxLayout()
        delrow.setSpacing(10)
        self._del_model = QPushButton(tr("set_model_delete"))
        self._del_model.clicked.connect(self._on_delete_model)
        delrow.addWidget(self._del_model)
        delrow.addStretch()
        g.addWidget(_form_label(tr("set_disk_label")), 11, 0)
        g.addLayout(delrow, 11, 1)
        self._del_note = _note("")
        g.addWidget(self._del_note, 12, 1)
        self._refresh_delete_button()

        # підпис моделі рахуємо ПІСЛЯ побудови радіо «Обробка» й комбо точності —
        # він залежить від їхнього стану (VRAM під режим+точність)
        self._refresh_model_meta()
        lay.addLayout(g)
        return frame

    def _set_dir_text(self, path):
        self._dir_label.setText(str(path) if path else tr("set_default_folder"))

    def _on_pick_model_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("common_model_folder"))
        if not path:
            return
        self.controller.set_model_dir(path)
        self._set_dir_text(path)
        motion.slide_fade_in(self._dir_note)   # ТЗ п.9
        self._refresh_delete_button()          # інша тека — інша наявність моделей
        self._mark_restart_pending()

    def _on_std_model_dir(self):
        self.controller.set_model_dir("")
        self._set_dir_text(None)
        motion.slide_fade_in(self._dir_note)   # ТЗ п.9
        self._refresh_delete_button()          # інша тека — інша наявність моделей
        self._mark_restart_pending()

    def _on_model(self, _index):
        self.controller.set_model(self._model.currentData())
        self._refresh_model_meta()               # оновити хінт розміру/лінк «Про модель»
        motion.slide_fade_in(self._model_note)   # ТЗ п.9
        self._refresh_delete_button()            # активна змінилась → інша ціль видалення
        self._mark_restart_pending()

    # --- пресети/власна модель розпізнавання (fix/stt-models) ---
    def _custom_model_label(self, name: str) -> str:
        """Людяна назва власної моделі для комбо: «Власна: <тека/репо>»."""
        short = os.path.basename(str(name).rstrip("/\\")) or str(name)
        return f"{tr('stt_model_custom_prefix')}: {short}"

    def _refresh_model_meta(self):
        """Підказка про розмір + залізо активного пресета + лінк «Про модель».
        Залізо ДИНАМІЧНЕ: залежить від режиму «Обробка» (CPU/GPU) і, для GPU, від
        обраної точності (compute_type). Для власної моделі підказки нема."""
        preset = self._stt_presets.get_preset(self._model.currentData())
        if preset is None:
            self._model_hint.clear()
            self._model_about.clear()
            return
        self._model_hint.setText(self._hardware_hint(preset))
        if preset.page_url:
            # inline-колір (не QPalette.Link — жива зміна теми його не перечитує);
            # хук register_restyle(_refresh_model_meta) перебудовує під активний акцент
            self._model_about.setText(
                '<a href="{}" style="color:{};">{}</a>'.format(
                    preset.page_url, theme.GOLD,
                    html.escape(tr("stt_model_about"))))
        else:
            self._model_about.clear()

    def _selected_device(self) -> str:
        """Обраний режим «Обробка»: за станом радіо, а до їх побудови / у легкому
        тесті без контролера — з конфігу (типово cpu)."""
        devs = getattr(self, "_devs", None)
        btn = devs.checkedButton() if devs is not None else None
        if btn is not None:
            return btn.property("dev")
        cfg = getattr(getattr(self, "controller", None), "cfg", None)
        return getattr(cfg, "device", "cpu")

    def _selected_compute(self) -> str:
        """Обрана точність (compute_type): за комбо, інакше — з конфігу (int8)."""
        combo = getattr(self, "_compute", None)
        data = combo.currentData() if combo is not None else None
        if data:
            return data
        cfg = getattr(getattr(self, "controller", None), "cfg", None)
        return getattr(cfg, "compute_type", "int8")

    def _hardware_hint(self, preset) -> str:
        """Базовий підпис (диск+характер) + речення про залізо під поточний режим:
        CPU → оперативна пам’ять/час; GPU → VRAM під обрану точність (int8/…)."""
        base = tr(preset.hint_key)
        if self._selected_device() == "cuda":
            compute = self._selected_compute()
            vram = _GPU_VRAM.get((preset.name, compute))
            if vram:
                tail = tr("stt_hw_gpu",
                          vram="~{} {}".format(vram, tr("unit_gb")), compute=compute)
            else:
                tail = ""
        else:
            tail = tr(preset.label_key + "_cpu")
        return "{} {}".format(base, tail).strip()

    def _sync_compute_options(self, device: str, current: str = None):
        """Наповнити комбо точності за режимом: CPU → лише int8 (комбо вимкнене);
        GPU → int8/int8_float16/float16. Зберегти поточний вибір, якщо доступний."""
        if current is None:
            current = self._compute.currentData()
        self._compute.blockSignals(True)
        self._compute.clear()
        for ctype, label_key in (_COMPUTE_CPU if device == "cpu" else _COMPUTE_GPU):
            self._compute.addItem(tr(label_key), ctype)
        idx = self._compute.findData(current)
        if idx < 0:
            idx = self._compute.findData("int8" if device == "cpu" else "int8_float16")
        self._compute.setCurrentIndex(max(idx, 0))
        self._compute.setEnabled(device != "cpu")   # CPU: точність фіксована (int8)
        self._compute.blockSignals(False)

    def _on_compute(self, _index):
        self.controller.set_compute_type(self._compute.currentData())
        self._refresh_model_meta()               # VRAM у підписі залежить від точності
        motion.slide_fade_in(self._compute_note)
        self._mark_restart_pending()

    def _select_custom_model(self, name: str):
        """Зробити власну модель активною: додати/оновити пункт комбо й вибрати
        його (currentIndexChanged → _on_model зробить решту: set_model, restart)."""
        idx = self._model.findData(name)
        if idx < 0:
            self._model.addItem(self._custom_model_label(name), name)
            idx = self._model.findData(name)
        self._model.setCurrentIndex(idx)

    def _add_stt_folder(self):
        """Власна модель із теки: готовий каталог моделі faster-whisper
        (model.bin/model.safetensors + config.json). Шлях іде в cfg.model_name —
        WhisperModel приймає локальну теку напряму."""
        path = QFileDialog.getExistingDirectory(self, tr("stt_model_add_folder"))
        if not path:
            return
        has_weights = any(os.path.isfile(os.path.join(path, w))
                          for w in ("model.bin", "model.safetensors"))
        if not (has_weights and os.path.isfile(os.path.join(path, "config.json"))):
            QMessageBox.warning(self, tr("stt_model_add_folder"),
                                tr("stt_model_folder_invalid"))
            return
        self._select_custom_model(path)

    def _add_stt_hf(self):
        """Власна модель за ідентифікатором репозиторію HuggingFace «власник/назва»
        (має бути faster-whisper/CTranslate2-сумісна). Ім'я репо іде у cfg.model_name."""
        repo, ok = QInputDialog.getText(self, tr("stt_model_add_hf"),
                                        tr("stt_model_hf_prompt"))
        if not ok:
            return
        repo = repo.strip()
        if not self._stt_presets.is_repo_id(repo):
            QMessageBox.warning(self, tr("stt_model_add_hf"),
                                tr("stt_model_hf_invalid"))
            return
        self._select_custom_model(repo)

    def _on_device(self, btn):
        dev = btn.property("dev")
        self.controller.set_device(dev)
        # точність мусить бути сумісна з режимом: CPU → лише int8; GPU →
        # рекомендований int8_float16 (звідти можна опустити до int8 чи підняти
        # до float16). Startup-нормалізація однаково відсіє несумісне.
        ctype = "int8" if dev == "cpu" else "int8_float16"
        self.controller.set_compute_type(ctype)
        self._sync_compute_options(dev, ctype)   # оновити доступні варіанти точності
        self._refresh_model_meta()               # підпис залежить від режиму+точності
        motion.slide_fade_in(self._dev_note)   # ТЗ п.9
        self._mark_restart_pending()

    def _on_idle_unload(self, _index):
        self.controller.set_model_idle_unload(
            int(self._idle_unload.currentData() or 0))

    def sync_device(self, device: str):
        """Синхронізувати UI після автоматичного runtime-fallback без restart-note."""
        for button in self._devs.buttons():
            button.blockSignals(True)
            button.setChecked(button.property("dev") == device)
            button.blockSignals(False)

    # --- видалення невживаної моделі (feature/delete-model) ---
    def _model_display_name(self, model_name: str) -> str:
        """Людяна назва моделі з комбобокса (та сама, яку бачить користувач)."""
        idx = self._model.findData(model_name)
        return self._model.itemText(idx) if idx >= 0 else model_name

    def _delete_target(self):
        """НЕактивна (не вибрана в конфігу) модель застосунку, що зараз є на диску
        і яку можна безпечно видалити; None — нема що видаляти. Активну
        (cfg.model_name) не чіпаємо: її model.bin може бути залочений завантаженим
        рушієм (memory-map) — тому «лише неактивна»."""
        from whisper_core.engine import MODEL_REVISIONS
        from whisper_core.models import repo_for, model_snapshot_size
        cfg = self.controller.cfg
        for name in MODEL_REVISIONS:
            if name == cfg.model_name:
                continue
            if model_snapshot_size(cfg.model_dir, repo_for(name)) > 0:
                return name
        return None

    def _refresh_delete_button(self):
        """Кнопка активна, лише коли є НЕактивна модель на диску; інакше — вимкнена
        з підказкою «спершу перемкнись на іншу модель»."""
        target = self._delete_target()
        self._del_model.setEnabled(target is not None)
        if target is None:
            self._del_model.setToolTip(tr("set_model_delete_tip"))
            return
        from whisper_core.models import repo_for, model_snapshot_size
        size = model_snapshot_size(self.controller.cfg.model_dir, repo_for(target))
        self._del_model.setToolTip(tr("set_model_delete_ready",
                                      name=self._model_display_name(target),
                                      size=_human_size(size)))

    def _on_delete_model(self, _checked=False):
        from whisper_core.models import repo_for, model_snapshot_size, delete_model
        cfg = self.controller.cfg
        target = self._delete_target()
        if target is None:                       # стан міг змінитись — просто оновити
            self._refresh_delete_button()
            return
        size = model_snapshot_size(cfg.model_dir, repo_for(target))
        name = self._model_display_name(target)
        resp = QMessageBox.question(
            self, tr("set_model_delete_title"),
            tr("set_model_delete_body", name=name, size=_human_size(size)))
        if resp != QMessageBox.Yes:
            return
        try:
            freed = delete_model(cfg.model_dir, target)
        except Exception:
            logging.exception("Не вдалося видалити модель %s", target)
            QMessageBox.warning(self, tr("set_model_delete_title"),
                                tr("set_model_delete_fail"))
            self._refresh_delete_button()
            return
        self._del_note.setText(tr("set_model_deleted", size=_human_size(freed)))
        motion.slide_fade_in(self._del_note)     # ТЗ п.9
        self._refresh_delete_button()

    # --- докачка прискорення GPU (feature/gpu) ---
    def _on_gpu_download(self):
        """Кнопка «Завантажити прискорення»: модальна докачка з прогресом.
        Успіх → увімкнути й обрати радіо GPU + показати смужку перезапуску
        (рушій уже запущено на CPU, тож GPU запрацює після рестарту)."""
        dlg = GpuDownloadDialog(self.window())
        if dlg.exec():
            self._enable_gpu_after_download()

    def _enable_gpu_after_download(self):
        if self._cuda_radio is not None:
            self._cuda_radio.setEnabled(True)
            self._cuda_radio.blockSignals(True)
            self._cuda_radio.setChecked(True)
            self._cuda_radio.blockSignals(False)
        self.controller.set_device("cuda")     # діятиме після перезапуску
        if self._gpu_dl_btn is not None:
            self._gpu_dl_btn.hide()
        if self._gpu_dl_hint is not None:
            self._gpu_dl_hint.setText(tr("set_gpu_ready"))
        motion.slide_fade_in(self._dev_note)
        self._mark_restart_pending()

    # --- ГАРЯЧІ КЛАВІШІ (feature/ux-center: усі комбінації на одній сторінці) ---
    def _hotkeys_group(self, cfg):
        """Централізована картка: усі активні гарячі клавіші застосунку в одному
        місці (диктування-клавіатура, режим, бічна кнопка миші PTT). Захоплення
        нової комбінації — наявний Discord-діалог (controller.start_key_capture)."""
        frame, lay = _card(tr("set_hotkeys_eyebrow"))
        intro = QLabel(tr("hotkeys_intro"))
        intro.setProperty("muted", True)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        g = _grid()

        keyrow = QHBoxLayout()
        keyrow.setSpacing(12)
        self._key_label = QLabel(pretty(cfg.ptt_key))
        self._key_label.setProperty("kbd", True)
        change = QPushButton(tr("common_change"))
        change.clicked.connect(self.controller.start_key_capture)
        reset = QPushButton(tr("hotkeys_reset"))
        reset.setProperty("ghost", True)
        reset.clicked.connect(self.controller.reset_ptt_key)
        keyrow.addWidget(self._key_label)
        keyrow.addWidget(change)
        keyrow.addWidget(reset)
        keyrow.addStretch()
        g.addWidget(_labeled_hint(tr("hotkeys_dictate"), "hint_key"), 0, 0)
        g.addLayout(keyrow, 0, 1)
        self._key_note = _note(tr("set_key_changed"))
        g.addWidget(self._key_note, 1, 1)
        combo_hint = QLabel(tr("set_key_hint"))
        combo_hint.setProperty("muted", True)
        combo_hint.setWordWrap(True)
        g.addWidget(combo_hint, 2, 1)
        self.controller.key_captured.connect(self._on_key)

        modebox = QVBoxLayout()
        modebox.setSpacing(4)
        modes = QButtonGroup(self)
        for mode, label in (("hold", tr("set_mode_hold")),
                            ("toggle", tr("set_mode_toggle")),
                            ("double_tap", tr("set_mode_double"))):
            rb = QRadioButton(label)
            rb.setProperty("ptt_mode", mode)
            rb.setChecked(cfg.ptt_mode == mode)
            modes.addButton(rb)
            modebox.addWidget(rb)
        modes.buttonClicked.connect(self._on_ptt_mode)
        g.addWidget(_labeled_hint(tr("hotkeys_mode"), "hint_mode"), 3, 0,
                    Qt.AlignTop)
        g.addLayout(modebox, 3, 1)

        # feature/mouse-ptt: бічна кнопка миші як ДОДАТКОВА кнопка запису (opt-in);
        # діє одразу (контролер перезапускає хук)
        self._mouse_btn = QComboBox()
        self._mouse_btn.addItem(tr("set_mouse_none"), "none")
        self._mouse_btn.addItem(tr("set_mouse_x1"), "x1")
        self._mouse_btn.addItem(tr("set_mouse_x2"), "x2")
        idx = self._mouse_btn.findData(getattr(cfg, "ptt_mouse_button", "none"))
        self._mouse_btn.setCurrentIndex(idx if idx >= 0 else 0)
        self._mouse_btn.currentIndexChanged.connect(
            lambda _i: self.controller.set_ptt_mouse_button(
                self._mouse_btn.currentData()))
        self._mouse_btn.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_labeled_hint(tr("hotkeys_mouse"), "hint_mouse_btn"), 4, 0)
        g.addWidget(self._mouse_btn, 4, 1)
        mouse_hint = QLabel(tr("set_mouse_hint"))
        mouse_hint.setProperty("muted", True)
        mouse_hint.setWordWrap(True)
        g.addWidget(mouse_hint, 5, 1)

        lay.addLayout(g)
        return frame

    # --- КЕРУВАННЯ ЗАПИСОМ ---
    def _record_group(self, cfg):
        frame, lay = _card(tr("set_rec_eyebrow"))

        # feature/dictation-queue (запит Миколи №10): видимий тумблер угорі картки.
        # Типово увімкнено; діє одразу для наступного диктування (читається з cfg).
        qbox = QVBoxLayout()
        qbox.setSpacing(2)
        self._dictation_queue = QCheckBox(tr("set_dictation_queue"))
        self._dictation_queue.setChecked(
            bool(getattr(cfg, "dictation_queue_enabled", True)))
        self._dictation_queue.setAccessibleName(tr("set_dictation_queue"))
        self._dictation_queue.toggled.connect(self._on_dictation_queue)
        qbox.addWidget(self._dictation_queue)
        queue_hint = QLabel(tr("set_dictation_queue_hint"))
        queue_hint.setProperty("muted", True)
        queue_hint.setWordWrap(True)
        qbox.addWidget(queue_hint)
        # feature/live-transcription (перенесено з Наради, аудит Миколи 22.07): на
        # вкладці Нарада цей тумблер був чужим — він керує показом тексту під час
        # ДИКТУВАННЯ. Config-ключ live_transcription збережено, тож значення
        # користувачів не губиться. sync_live_transcription() тримає його синхронним,
        # коли фон програмно вимикає живий режим (старт наради тощо).
        qbox.addSpacing(8)
        self._live_transcription = QCheckBox(tr("meeting_live_dictation_label"))
        self._live_transcription.setChecked(bool(getattr(cfg, "live_transcription", False)))
        self._live_transcription.setAccessibleName(tr("meeting_live_dictation_label"))
        self._live_transcription.toggled.connect(self.controller.set_live_transcription)
        qbox.addWidget(self._live_transcription)
        live_hint = QLabel(tr("meeting_live_dictation_hint"))
        live_hint.setProperty("muted", True)
        live_hint.setWordWrap(True)
        qbox.addWidget(live_hint)
        # feature/model-bottlenecks (під-хвиля 2): підсвітка непевних слів у стрічці.
        # Керований, бо word_timestamps додає DTW-прохід (трохи повільніше на слабкому
        # CPU). Дефолт — увімкнено (рекомендовано; Т72 «прозорість + контроль»).
        qbox.addSpacing(8)
        self._highlight_uncertain = QCheckBox(tr("set_highlight_uncertain"))
        self._highlight_uncertain.setChecked(
            bool(getattr(cfg, "highlight_uncertain_words", True)))
        self._highlight_uncertain.setAccessibleName(tr("set_highlight_uncertain"))
        self._highlight_uncertain.toggled.connect(self._on_highlight_uncertain)
        qbox.addWidget(self._highlight_uncertain)
        hl_hint = QLabel(tr("set_highlight_uncertain_hint"))
        hl_hint.setProperty("muted", True)
        hl_hint.setWordWrap(True)
        qbox.addWidget(hl_hint)
        lay.addLayout(qbox)

        g = _grid()

        # feature/voice-punctuation: opt-in, діє одразу для наступного диктування;
        # свій ⓘ-хінт поруч (що саме робить), як у решти пунктів картки
        self._voice_punct = QCheckBox(tr("set_voice_punct"))
        self._voice_punct.setChecked(bool(getattr(cfg, "voice_punctuation", False)))
        self._voice_punct.toggled.connect(self._on_voice_punct)
        g.addWidget(self._voice_punct, 6, 0, 1, 2)
        punct_hint = QLabel(tr("set_voice_punct_hint"))
        punct_hint.setProperty("muted", True)
        punct_hint.setWordWrap(True)
        g.addWidget(punct_hint, 7, 1)

        # feature/processing-slider: колишній комбо «Автоочистка тексту» ПРИБРАНО —
        # рівень обробки тепер задає пер-профільний повзунок на вкладках Диктування
        # та Нарада (Дослівно / Без слів-паразитів / Під документ). Єдиний контроль,
        # без двох конкурентних (спека §5). Стара глобальна cleanup_level лишається
        # у config.toml лише для сумісності/міграції та шляху ручної розшифровки файлів.

        # feature/punctuation-plus: підсекція постобробки тексту — два opt-in
        # ЕКСПЕРИМЕНТАЛЬНІ кроки із завантажуваними компонентами (за зразком
        # діаризації в Нараді: чекбокс вимкнено, поки компонент не завантажено)
        # feature/edit-pack: «Не виправляй мою мову» — майстер-тумблер над
        # текстовою постобробкою. Коли увімкнено — обходить автокорекцію й
        # пунктуатор (не чіпає суржик/діалект); словники профілю лишаються.
        # Кладемо разом із підзаголовком в один VBox, щоб не перенумеровувати сітку.
        tp_head = QVBoxLayout()
        tp_head.setSpacing(2)
        tp_head.addWidget(self._subhead(tr("set_textproc_head")))
        self._preserve_speech = QCheckBox(tr("set_preserve_speech"))
        self._preserve_speech.setChecked(bool(getattr(cfg, "preserve_speech", False)))
        self._preserve_speech.setAccessibleName(tr("set_preserve_speech"))
        self._preserve_speech.toggled.connect(self._on_preserve_speech)
        tp_head.addWidget(self._preserve_speech)
        ps_hint = QLabel(tr("set_preserve_speech_hint"))
        ps_hint.setProperty("muted", True)
        ps_hint.setWordWrap(True)
        tp_head.addWidget(ps_hint)
        g.addLayout(tp_head, 10, 0, 1, 2)

        # (1) автокорекція одруків
        self._autocorrect = QCheckBox(tr("set_autocorrect"))
        self._autocorrect.setChecked(bool(getattr(cfg, "autocorrect_enabled", False)))
        self._autocorrect.toggled.connect(self._on_autocorrect)
        g.addWidget(self._autocorrect, 11, 0, 1, 2)
        ac_hint = QLabel(tr("set_autocorrect_hint"))
        ac_hint.setProperty("muted", True)
        ac_hint.setWordWrap(True)
        g.addWidget(ac_hint, 12, 1)
        self._autocorrect_dl = GlassButton(tr("set_autocorrect_download"))
        self._autocorrect_dl.clicked.connect(self._download_autocorrect)
        self._autocorrect_status = QLabel()
        self._autocorrect_status.setProperty("muted", True)
        self._autocorrect_status.setWordWrap(True)
        ac_row = QHBoxLayout()
        ac_row.addWidget(self._autocorrect_dl)
        # stretch=1 замість трейлінг-addStretch: інакше wordWrap-підпис у HBox не
        # отримує ширини й довгий статус («Спочатку завантажте компонент») обрізається.
        ac_row.addWidget(self._autocorrect_status, 1)
        g.addLayout(ac_row, 13, 1)

        # (2) пунктуатор/ITN
        self._punctuator = QCheckBox(tr("set_punctuator"))
        self._punctuator.setChecked(bool(getattr(cfg, "punctuator_enabled", False)))
        self._punctuator.toggled.connect(self._on_punctuator)
        g.addWidget(self._punctuator, 14, 0, 1, 2)
        pn_hint = QLabel(tr("set_punctuator_hint"))
        pn_hint.setProperty("muted", True)
        pn_hint.setWordWrap(True)
        g.addWidget(pn_hint, 15, 1)
        self._punctuator_dl = GlassButton(tr("set_punctuator_download"))
        self._punctuator_dl.clicked.connect(self._download_punctuator)
        self._punctuator_status = QLabel()
        self._punctuator_status.setProperty("muted", True)
        self._punctuator_status.setWordWrap(True)
        pn_row = QHBoxLayout()
        pn_row.addWidget(self._punctuator_dl)
        # stretch=1: див. коментар до ac_row — wordWrap-статус має отримати ширину.
        pn_row.addWidget(self._punctuator_status, 1)
        g.addLayout(pn_row, 16, 1)

        # feature/player-pack: «Огляд перед дією» — opt-in, вимкнено за замовч.;
        # діє одразу для наступної РУЧНОЇ розшифровки файлу (диктування ігнорує).
        # Чекбокс + хінт в одну комірку (рядок 17 вільний, 18+ зайняті) — щоб не
        # перенумеровувати решту сітки.
        self._review_changes = QCheckBox(tr("set_review_changes"))
        self._review_changes.setChecked(bool(getattr(cfg, "review_text_changes", False)))
        self._review_changes.toggled.connect(self._on_review_changes)
        rc_hint = QLabel(tr("set_review_changes_hint"))
        rc_hint.setProperty("muted", True)
        rc_hint.setWordWrap(True)
        rc_box = QVBoxLayout()
        rc_box.setSpacing(2)
        rc_box.addWidget(self._review_changes)
        rc_box.addWidget(rc_hint)
        g.addLayout(rc_box, 17, 0, 1, 2)
        self._refresh_textproc_controls()

        # feature/qol-pack: автостоп по тиші (0..30 с; 0 = вимкнено) — діє одразу
        self._autostop = make_slider()
        self._autostop.setRange(0, 30)
        self._autostop.setSingleStep(1)
        self._autostop.setPageStep(1)
        self._autostop.setValue(int(getattr(cfg, "dictation_autostop_silence_s", 0) or 0))
        self._autostop.setMaximumWidth(_CTRL_MAX)
        self._autostop.valueChanged.connect(self._on_autostop_changed)
        self._autostop.sliderReleased.connect(self._on_autostop_released)
        self._autostop_val = QLabel()
        self._autostop_val.setProperty("muted", True)
        g.addWidget(_labeled_hint(tr("set_autostop_label"), "set_autostop_hint"), 18, 0)
        g.addLayout(self._slider_row(self._autostop, self._autostop_val), 18, 1)

        # feature/qol-pack: ліміт тривалості (0..60 хв; 0 = без ліміту) — діє одразу
        self._maxdur = make_slider()
        self._maxdur.setRange(0, 60)
        self._maxdur.setSingleStep(1)
        self._maxdur.setPageStep(5)
        self._maxdur.setValue(int((getattr(cfg, "dictation_max_duration_s", 0) or 0) // 60))
        self._maxdur.setMaximumWidth(_CTRL_MAX)
        self._maxdur.valueChanged.connect(self._on_maxdur_changed)
        self._maxdur.sliderReleased.connect(self._on_maxdur_released)
        self._maxdur_val = QLabel()
        self._maxdur_val.setProperty("muted", True)
        g.addWidget(_labeled_hint(tr("set_maxdur_label"), "set_maxdur_hint"), 19, 0)
        g.addLayout(self._slider_row(self._maxdur, self._maxdur_val), 19, 1)
        self._sync_qol_slider_labels()

        # feature/qol-pack: опційні глобальні хоткеї дій (за замовч. вимкнені)
        g.addWidget(self._action_key_row("undo", tr("set_undo_key_label"), cfg), 20, 0, 1, 2)
        g.addWidget(self._action_key_row("insert", tr("set_insert_key_label"), cfg),
                    21, 0, 1, 2)
        qol_keys_hint = QLabel(tr("set_qol_keys_hint"))
        qol_keys_hint.setProperty("muted", True)
        qol_keys_hint.setWordWrap(True)
        g.addWidget(qol_keys_hint, 22, 1)

        # feature/office-voice-nav: голосова навігація полями зовнішніх документів
        # (Word/Excel). Opt-in, діє одразу для наступного диктування. Чекбокс +
        # хінт + кнопка «Список команд» одним VBox (рядок 23 вільний) — щоб не
        # перенумеровувати решту сітки.
        self._voice_nav = QCheckBox(tr("set_voice_nav"))
        self._voice_nav.setChecked(bool(getattr(cfg, "voice_nav_enabled", False)))
        self._voice_nav.setAccessibleName(tr("set_voice_nav"))
        self._voice_nav.toggled.connect(self._on_voice_nav)
        nav_hint = QLabel(tr("set_voice_nav_hint"))
        nav_hint.setProperty("muted", True)
        nav_hint.setWordWrap(True)
        self._nav_cmds_btn = GlassButton(tr("nav_cmds_open"))
        self._nav_cmds_btn.setAccessibleName(tr("nav_cmds_open"))
        self._nav_cmds_btn.clicked.connect(self._open_nav_commands)
        nav_btn_row = QHBoxLayout()
        nav_btn_row.addWidget(self._nav_cmds_btn)
        nav_btn_row.addStretch()
        nav_box = QVBoxLayout()
        nav_box.setSpacing(2)
        nav_box.addWidget(self._voice_nav)
        nav_box.addWidget(nav_hint)
        nav_box.addLayout(nav_btn_row)
        g.addLayout(nav_box, 23, 0, 1, 2)

        lay.addLayout(g)
        return frame

    # --- ЗВУК (feature/audio-center): пристрої / чутливість (VAD) / обробка ---
    def _sound_group(self, cfg):
        """Аудіо-центр рівня Teams/Discord: вибір пристроїв вводу+виводу з
        оновленням списку, розширена чутливість VAD і опційна обробка (gate/AGC).
        Три підсекції в одній картці; тест мікрофона — поруч із пристроями."""
        frame, lay = _card(tr("set_sound_eyebrow"))

        # ── Підсекція «Пристрої» ──────────────────────────────────────────
        lay.addWidget(self._subhead(tr("set_sound_devices")))
        g = _grid()

        # мікрофон: перший пункт = системний дефолт (None); далі — читабельні імена
        # без дублів хостапі. Зберігаємо ІМʼЯ; змінюється НЕГАЙНО (recorder перевідкриє
        # потік), тож примітки «після перезапуску» тут не треба.
        self._mic = QComboBox()
        self._fill_device_combo(self._mic, list_input_devices(), cfg.input_device)
        self._mic.currentIndexChanged.connect(
            lambda _i: self.controller.set_input_device(self._mic.currentData()))
        self._mic.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_labeled_hint(tr("set_mic"), "hint_mic"), 0, 0)
        g.addWidget(self._mic, 0, 1)
        mic_hint = QLabel(tr("set_mic_hint"))
        mic_hint.setProperty("muted", True)
        mic_hint.setWordWrap(True)
        g.addWidget(mic_hint, 1, 1)

        # пристрій ВИВОДУ — для відтворення тесту мікрофона (і майбутнього
        # програвання). Зберігаємо ІМʼЯ; діє одразу (застосовується при відтворенні).
        self._out = QComboBox()
        self._fill_device_combo(self._out, list_output_devices(),
                                getattr(cfg, "output_device", None))
        self._out.currentIndexChanged.connect(
            lambda _i: self.controller.set_output_device(self._out.currentData()))
        self._out.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_labeled_hint(tr("set_output_device"), "hint_output_device"), 2, 0)
        g.addWidget(self._out, 2, 1)

        # «Оновити список» — перечитати пристрої (від'єднали/під'єднали мікрофон/навушники)
        refreshrow = QHBoxLayout()
        refreshrow.setSpacing(10)
        refresh = QPushButton(tr("set_devices_refresh"))
        refresh.clicked.connect(self._on_refresh_devices)
        refreshrow.addWidget(refresh)
        refreshrow.addStretch()
        g.addLayout(refreshrow, 3, 1)

        # feature/audio-qol: тест мікрофона (патерн Discord/Teams). Клік → 3 с запису
        # з обраного пристрою → відтворення собі на обраний вивід → вердикт за піком.
        testrow = QHBoxLayout()
        testrow.setSpacing(10)
        self._mic_test_btn = QPushButton(tr("set_mic_test"))
        self._mic_test_btn.clicked.connect(self._on_mic_test)
        testrow.addWidget(self._mic_test_btn)
        testrow.addStretch()
        g.addWidget(_form_label(tr("set_mic_test_label")), 4, 0)
        g.addLayout(testrow, 4, 1)
        self._mic_test_note = _note("")
        g.addWidget(self._mic_test_note, 5, 1)
        self.controller.mic_test_result.connect(self._on_mic_test_result)
        self.controller.rec_state.connect(self._on_rec_state)
        lay.addLayout(g)

        # ── Підсекція «Чутливість (VAD)» ──────────────────────────────────
        lay.addWidget(self._subhead(tr("set_sound_sensitivity")))
        vg = _grid()
        vad_intro = QLabel(tr("set_vad_intro"))
        vad_intro.setProperty("muted", True)
        vad_intro.setWordWrap(True)
        vg.addWidget(vad_intro, 0, 0, 1, 2)

        self._vad_thr = make_slider()
        self._vad_thr.setRange(10, 90)          # 0.10..0.90
        self._vad_thr.setSingleStep(5)
        self._vad_thr.setPageStep(5)
        self._vad_thr.setValue(round(float(getattr(
            cfg, "vad_threshold", VAD_THRESHOLD_DEFAULT)) * 100))
        self._vad_thr.setMaximumWidth(_CTRL_MAX)
        self._vad_thr.setToolTip(tr("tip_vad_threshold"))
        self._vad_thr.valueChanged.connect(self._on_vad_changed)
        self._vad_thr.sliderReleased.connect(self._on_vad_released)
        self._vad_thr_val = QLabel()
        self._vad_thr_val.setProperty("muted", True)
        vg.addWidget(_form_label(tr("set_vad_threshold")), 1, 0)
        vg.addLayout(self._slider_row(self._vad_thr, self._vad_thr_val), 1, 1)

        # feature/audio-center: мінімальна тривалість мовлення — коротші сплески
        # (клацання клавіш) VAD відкидає як не-мову
        self._vad_speech = make_slider()
        self._vad_speech.setRange(0, 1000)      # мс
        self._vad_speech.setSingleStep(50)
        self._vad_speech.setPageStep(50)
        self._vad_speech.setValue(int(getattr(
            cfg, "vad_min_speech_ms", VAD_MIN_SPEECH_MS_DEFAULT)))
        self._vad_speech.setMaximumWidth(_CTRL_MAX)
        self._vad_speech.valueChanged.connect(self._on_vad_changed)
        self._vad_speech.sliderReleased.connect(self._on_vad_released)
        self._vad_speech_val = QLabel()
        self._vad_speech_val.setProperty("muted", True)
        vg.addWidget(_form_label(tr("set_vad_speech")), 2, 0)
        vg.addLayout(self._slider_row(self._vad_speech, self._vad_speech_val), 2, 1)

        self._vad_ms = make_slider()
        self._vad_ms.setRange(100, 2000)        # мс
        self._vad_ms.setSingleStep(100)
        self._vad_ms.setPageStep(100)
        self._vad_ms.setValue(int(getattr(
            cfg, "vad_min_silence_ms", VAD_MIN_SILENCE_MS_DEFAULT)))
        self._vad_ms.setMaximumWidth(_CTRL_MAX)
        self._vad_ms.valueChanged.connect(self._on_vad_changed)
        self._vad_ms.sliderReleased.connect(self._on_vad_released)
        self._vad_ms_val = QLabel()
        self._vad_ms_val.setProperty("muted", True)
        vg.addWidget(_form_label(tr("set_vad_silence")), 3, 0)
        vg.addLayout(self._slider_row(self._vad_ms, self._vad_ms_val), 3, 1)

        vad_reset_row = QHBoxLayout()
        vad_reset_row.setSpacing(10)
        vad_reset = QPushButton(tr("set_vad_reset"))
        vad_reset.clicked.connect(self._on_vad_reset)
        vad_reset_row.addWidget(vad_reset)
        vad_reset_row.addStretch()
        vg.addLayout(vad_reset_row, 4, 1)
        self._sync_vad_labels()                 # намалювати поточні значення
        lay.addLayout(vg)

        # ── Підсекція «Обробка» (gate/AGC — opt-in, вимкнено за замовчуванням) ──
        lay.addWidget(self._subhead(tr("set_sound_processing")))
        pg = _grid()

        self._gate = QCheckBox(tr("set_gate"))
        self._gate.setChecked(bool(getattr(cfg, "noise_gate_enabled", False)))
        self._gate.toggled.connect(self._on_gate_changed)
        pg.addWidget(self._gate, 0, 0, 1, 2)
        gate_hint = QLabel(tr("set_gate_hint"))
        gate_hint.setProperty("muted", True)
        gate_hint.setWordWrap(True)
        pg.addWidget(gate_hint, 1, 1)

        self._gate_db = make_slider()
        self._gate_db.setRange(-70, -20)        # dBFS поріг
        self._gate_db.setSingleStep(1)
        self._gate_db.setPageStep(5)
        self._gate_db.setValue(round(float(getattr(
            cfg, "noise_gate_threshold_db", NOISE_GATE_THRESHOLD_DB_DEFAULT))))
        self._gate_db.setMaximumWidth(_CTRL_MAX)
        self._gate_db.setToolTip(tr("tip_gate_threshold"))
        self._gate_db.valueChanged.connect(self._on_gate_changed)
        self._gate_db.sliderReleased.connect(self._on_gate_released)
        self._gate_db_val = QLabel()
        self._gate_db_val.setProperty("muted", True)
        pg.addWidget(_form_label(tr("set_gate_threshold")), 2, 0)
        pg.addLayout(self._slider_row(self._gate_db, self._gate_db_val), 2, 1)

        self._agc = QCheckBox(tr("set_agc"))
        self._agc.setChecked(bool(getattr(cfg, "agc_enabled", False)))
        self._agc.toggled.connect(self._on_agc_changed)
        pg.addWidget(self._agc, 3, 0, 1, 2)
        agc_hint = QLabel(tr("set_agc_hint"))
        agc_hint.setProperty("muted", True)
        agc_hint.setWordWrap(True)
        pg.addWidget(agc_hint, 4, 1)
        self._sync_processing_labels()
        lay.addLayout(pg)
        # ── Підсекція «Звук» (усі звукові тумблери в одному аудіо-центрі) ──
        lay.addWidget(self._subhead(tr("set_system_sound")))

        self._paste_sound = QCheckBox(tr("set_paste_sound"))
        self._paste_sound.setChecked(bool(getattr(cfg, "paste_confirm_sound", True)))
        self._paste_sound.toggled.connect(self._on_paste_sound)
        lay.addWidget(self._paste_sound)
        paste_sound_hint = QLabel(tr("set_paste_sound_hint"))
        paste_sound_hint.setProperty("muted", True)
        paste_sound_hint.setWordWrap(True)
        lay.addWidget(paste_sound_hint)

        self._quiet_enabled = QCheckBox(tr("set_quiet_enable"))
        self._quiet_enabled.setChecked(bool(getattr(cfg, "quiet_hours_enabled", False)))
        self._quiet_enabled.toggled.connect(self._on_quiet_toggled)
        lay.addWidget(self._quiet_enabled)
        qrow = QHBoxLayout()
        qrow.setSpacing(8)
        qrow.addWidget(QLabel(tr("set_quiet_from")))
        self._quiet_from = QTimeEdit(self._parse_time(getattr(cfg, "quiet_hours_start", "22:00")))
        self._quiet_from.setDisplayFormat("HH:mm")
        self._quiet_from.timeChanged.connect(self._on_quiet_time)
        qrow.addWidget(self._quiet_from)
        qrow.addWidget(QLabel(tr("set_quiet_to")))
        self._quiet_to = QTimeEdit(self._parse_time(getattr(cfg, "quiet_hours_end", "07:00")))
        self._quiet_to.setDisplayFormat("HH:mm")
        self._quiet_to.timeChanged.connect(self._on_quiet_time)
        qrow.addWidget(self._quiet_to)
        qrow.addStretch()
        lay.addLayout(qrow)
        quiet_hint = QLabel(tr("set_quiet_hint"))
        quiet_hint.setProperty("muted", True)
        quiet_hint.setWordWrap(True)
        lay.addWidget(quiet_hint)
        self._sync_quiet_enabled()

        # ── Підсекція «Плеєр»: авто-відкат після паузи ──
        lay.addWidget(self._subhead(tr("set_player_head")))
        self._backstep = QComboBox()
        self._backstep.addItem(tr("set_backstep_off"), 0.0)
        self._backstep.addItem(tr("set_backstep_05"), 0.5)
        self._backstep.addItem(tr("set_backstep_15"), 1.5)
        self._backstep.addItem(tr("set_backstep_3"), 3.0)
        idx = self._backstep.findData(float(getattr(cfg, "player_resume_backstep_s", 1.5)))
        self._backstep.setCurrentIndex(idx if idx >= 0 else 2)
        self._backstep.currentIndexChanged.connect(self._on_backstep)
        self._backstep.setMaximumWidth(_CTRL_MAX)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addWidget(QLabel(tr("set_backstep")))
        brow.addWidget(self._backstep)
        brow.addStretch()
        lay.addLayout(brow)
        backstep_hint = QLabel(tr("set_backstep_hint"))
        backstep_hint.setProperty("muted", True)
        backstep_hint.setWordWrap(True)
        lay.addWidget(backstep_hint)
        return frame


    # --- feature/qol-pack: автостоп / ліміт тривалості ---
    def _sync_qol_slider_labels(self):
        s = self._autostop.value()
        self._autostop_val.setText(tr("set_autostop_off") if s == 0
                                   else tr("set_autostop_value", n=s))
        m = self._maxdur.value()
        self._maxdur_val.setText(tr("set_maxdur_off") if m == 0
                                 else tr("set_maxdur_value", n=m))

    def _on_autostop_changed(self):
        self._sync_qol_slider_labels()
        if not self._autostop.isSliderDown():
            self.controller.set_autostop_silence(self._autostop.value())

    def _on_autostop_released(self):
        self.controller.set_autostop_silence(self._autostop.value())

    def _on_maxdur_changed(self):
        self._sync_qol_slider_labels()
        if not self._maxdur.isSliderDown():
            self.controller.set_max_duration(self._maxdur.value() * 60)

    def _on_maxdur_released(self):
        self.controller.set_max_duration(self._maxdur.value() * 60)

    # --- feature/qol-pack: опційні хоткеї дій (скасувати / вставити ще раз) ---
    def _action_key_row(self, which: str, label: str, cfg) -> QWidget:
        """Рядок «лейбл: <комбо або “Вимкнено”>  [Змінити…] [Прибрати]».
        Захоплення — тим самим KeyCaptureDialog, що й клавіша запису."""
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        row.addWidget(_form_label(label))
        key = getattr(cfg, "undo_paste_key" if which == "undo" else "insert_last_key", "")
        shown = QLabel(pretty(key) if key else tr("set_qol_key_none"))
        shown.setProperty("kbd", True)
        change = QPushButton(tr("common_change"))
        change.clicked.connect(lambda _=False, w=which, l=shown: self._capture_action_key(w, l))
        clear = QPushButton(tr("set_qol_key_clear"))
        clear.clicked.connect(lambda _=False, w=which, l=shown: self._clear_action_key(w, l))
        row.addWidget(shown)
        row.addStretch()
        row.addWidget(change)
        row.addWidget(clear)
        return w

    def _capture_action_key(self, which: str, label: QLabel):
        """Захоплення хоткея дії. _capturing — той самий гейт, що у
        start_key_capture: поки модальний діалог слухає клавіатуру, PTT-хук не
        має стартувати диктування за ним. Конфлікт (PTT-комбо чи інший хоткей
        дії) контролер відхиляє з тостом — напис тоді не міняємо."""
        self.controller._capturing = True
        try:
            dlg = KeyCaptureDialog(self.window())
            if dlg.exec() and dlg.result_key:
                if self.controller.set_action_hotkey(which, dlg.result_key):
                    label.setText(pretty(dlg.result_key))
        finally:
            self.controller._capturing = False

    def _clear_action_key(self, which: str, label: QLabel):
        self.controller.set_action_hotkey(which, "")
        label.setText(tr("set_qol_key_none"))

    # --- ПЛАВАЮЧА НОТАТКА (feature/scratchpad-note) ---
    def _note_group(self, cfg):
        frame, lay = _card(tr("set_note_eyebrow"))
        g = _grid()
        combo = getattr(cfg, "note_hotkey", "") or ""
        keyrow = QHBoxLayout()
        keyrow.setSpacing(12)
        self._note_key_label = QLabel(pretty(combo) if combo else tr("set_note_none"))
        self._note_key_label.setProperty("kbd", True)
        change = QPushButton(tr("common_change"))
        change.clicked.connect(self.controller.start_note_key_capture)
        self._note_clear_btn = QPushButton(tr("note_clear"))
        self._note_clear_btn.setProperty("ghost", True)
        self._note_clear_btn.clicked.connect(self.controller.clear_note_hotkey)
        self._note_clear_btn.setEnabled(bool(combo))
        keyrow.addWidget(self._note_key_label)
        keyrow.addWidget(change)
        keyrow.addWidget(self._note_clear_btn)
        keyrow.addStretch()
        g.addWidget(_labeled_hint(tr("set_note_hotkey"), "hint_note_hotkey"), 0, 0)
        g.addLayout(keyrow, 0, 1)
        note_hint = QLabel(tr("set_note_hint"))
        note_hint.setProperty("muted", True)
        note_hint.setWordWrap(True)
        g.addWidget(note_hint, 1, 1)
        self.controller.note_key_captured.connect(self._on_note_key)

        # feature/voice-edit-selection: глобальний хоткей Command Mode (редагувати
        # виділене голосом). Опційний — трей-пункт діє завжди.
        ce_combo = getattr(cfg, "command_edit_hotkey", "") or ""
        cerow = QHBoxLayout()
        cerow.setSpacing(12)
        self._command_edit_key_label = QLabel(
            pretty(ce_combo) if ce_combo else tr("set_note_none"))
        self._command_edit_key_label.setProperty("kbd", True)
        ce_change = QPushButton(tr("common_change"))
        ce_change.setAccessibleName(tr("cmdedit_hotkey_label"))
        ce_change.clicked.connect(self.controller.start_command_edit_key_capture)
        self._command_edit_clear_btn = QPushButton(tr("note_clear"))
        self._command_edit_clear_btn.setProperty("ghost", True)
        self._command_edit_clear_btn.clicked.connect(
            self.controller.clear_command_edit_hotkey)
        self._command_edit_clear_btn.setEnabled(bool(ce_combo))
        cerow.addWidget(self._command_edit_key_label)
        cerow.addWidget(ce_change)
        cerow.addWidget(self._command_edit_clear_btn)
        cerow.addStretch()
        g.addWidget(_labeled_hint(tr("cmdedit_hotkey_label"), "hint_cmdedit_hotkey"), 2, 0)
        g.addLayout(cerow, 2, 1)
        ce_hint = QLabel(tr("cmdedit_hotkey_hint"))
        ce_hint.setProperty("muted", True)
        ce_hint.setWordWrap(True)
        g.addWidget(ce_hint, 3, 1)
        self.controller.command_edit_key_captured.connect(self._on_command_edit_key)

        lay.addLayout(g)
        return frame

    def _on_command_edit_key(self, combo: str):
        self._command_edit_key_label.setText(
            pretty(combo) if combo else tr("set_note_none"))
        self._command_edit_clear_btn.setEnabled(bool(combo))

    def _on_note_key(self, combo: str):
        """Оновити напис комбінації нотатки після захоплення/зняття."""
        self._note_key_label.setText(pretty(combo) if combo else tr("set_note_none"))
        self._note_clear_btn.setEnabled(bool(combo))

    @staticmethod
    def _subhead(text: str) -> QLabel:
        """Назва групи всередині картки — блок (16/600), не дрібний eyebrow."""
        lbl = QLabel(text)
        lbl.setProperty("level", "block")
        return lbl

    @staticmethod
    def _fill_device_combo(combo: QComboBox, names, current):
        """Заповнити комбобокс пристроїв: перший пункт = системний дефолт (None),
        далі — імена; відновити поточний вибір. Сигнали блокуємо, щоб заповнення
        не смикнуло обробник вибору."""
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr("set_mic_default"), None)
        for name in names:
            combo.addItem(name, name)
        idx = combo.findData(current) if current else 0
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_refresh_devices(self, _checked=False):
        """«Оновити список»: перечитати пристрої вводу й виводу, зберігши вибір,
        якщо він ще доступний. Якщо збережений пристрій зник — комбобокс покаже
        системний дефолт (сам запис усе одно відкотиться на дефолт у recorder)."""
        cfg = self.controller.cfg
        self._fill_device_combo(self._mic, list_input_devices(), cfg.input_device)
        self._fill_device_combo(self._out, list_output_devices(),
                                getattr(cfg, "output_device", None))

    @staticmethod
    def _slider_row(slider: QSlider, value_label: QLabel) -> QHBoxLayout:
        """Повзунок + підпис поточного значення поруч (feature/audio-qol)."""
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(slider, stretch=1)
        value_label.setMinimumWidth(64)
        row.addWidget(value_label)
        return row

    # --- feature/audio-qol: тест мікрофона ---
    def _on_mic_test(self, _checked=False):
        """Запустити тест: заглушити кнопку на час запису+відтворення, показати
        «Записую…». Busy-стан ставимо ДО start_mic_test: у гілці «мікрофона
        немає» вердикт емітиться синхронно (той самий GUI-потік →
        DirectConnection), тож слот результату має спрацювати ПІСЛЯ заглушення —
        інакше «Записую…» перезатерло б фінальний стан і кнопка залипла б
        вимкненою назавжди (воркер не стартував, другого сигналу не буде).
        Вмикаємо назад у _on_mic_test_result (з воркера — сигналом у GUI-потік)."""
        self._mic_test_note.hide()
        self._mic_test_btn.setEnabled(False)
        self._mic_test_btn.setText(tr("set_mic_testing"))
        if not self.controller.start_mic_test():
            # зайнято (страхувальний гейт — rec_state і так тримає неактивною):
            # тест не стартував, сигнал не прийде — повертаємо кнопку самі
            self._mic_test_btn.setEnabled(True)
            self._mic_test_btn.setText(tr("set_mic_test"))

    def _on_mic_test_result(self, verdict: str):
        self._mic_test_btn.setEnabled(True)
        self._mic_test_btn.setText(tr("set_mic_test"))
        self._mic_test_note.setText(tr(_MIC_VERDICT_KEYS.get(verdict, "set_mic_error")))
        motion.slide_fade_in(self._mic_test_note)

    def _on_rec_state(self, state: str):
        """Кнопка тесту неактивна під час запису/розшифровки (диктування/файли).
        Під час самого тесту rec_state лишається idle — кнопку тоді тримає
        вимкненою _on_mic_test, тож конфлікту немає."""
        if hasattr(self, "_mic_test_btn") and not self.controller._mic_testing:
            self._mic_test_btn.setEnabled(state == "idle")

    # --- feature/audio-qol + audio-center: чутливість VAD (три параметри) ---
    def _sync_vad_labels(self):
        self._vad_thr_val.setText(f"{self._vad_thr.value() / 100:.2f}")
        self._vad_speech_val.setText(tr("set_vad_ms_value", ms=self._vad_speech.value()))
        self._vad_ms_val.setText(tr("set_vad_ms_value", ms=self._vad_ms.value()))

    def _save_vad(self):
        self.controller.set_vad(self._vad_thr.value() / 100.0,
                                self._vad_ms.value(), self._vad_speech.value())

    def _on_vad_changed(self):
        """Оновити підписи live; зберегти одразу для клавіш/кліку по треку. Під час
        перетягування мишею збереже _on_vad_released (щоб не писати конфіг на кожен
        тік). Діє з наступної транскрипції — без перезапуску."""
        self._sync_vad_labels()
        if not (self._vad_thr.isSliderDown() or self._vad_ms.isSliderDown()
                or self._vad_speech.isSliderDown()):
            self._save_vad()

    def _on_vad_released(self):
        self._save_vad()

    def _on_vad_reset(self, _checked=False):
        self._vad_thr.setValue(round(VAD_THRESHOLD_DEFAULT * 100))
        self._vad_speech.setValue(VAD_MIN_SPEECH_MS_DEFAULT)
        self._vad_ms.setValue(VAD_MIN_SILENCE_MS_DEFAULT)
        self._sync_vad_labels()
        self.controller.set_vad(VAD_THRESHOLD_DEFAULT, VAD_MIN_SILENCE_MS_DEFAULT,
                                VAD_MIN_SPEECH_MS_DEFAULT)

    # --- feature/audio-center: обробка (шумовий гейт + AGC) ---
    def _sync_processing_labels(self):
        self._gate_db_val.setText(tr("set_gate_db_value", db=self._gate_db.value()))

    def _save_gate(self):
        self.controller.set_noise_gate(self._gate.isChecked(), self._gate_db.value())

    def _on_gate_changed(self, *_):
        """Тумблер/повзунок гейта: оновити підпис live, зберегти (крім активного
        перетягування — тоді збереже _on_gate_released). Діє з наступного диктування."""
        self._sync_processing_labels()
        if not self._gate_db.isSliderDown():
            self._save_gate()

    def _on_gate_released(self):
        self._save_gate()

    def _on_agc_changed(self, *_):
        self.controller.set_agc(self._agc.isChecked(),
                                getattr(self.controller.cfg, "agc_target_db",
                                        AGC_TARGET_DB_DEFAULT))

    def _on_key(self, key: str):
        self._key_label.setText(pretty(key))
        motion.slide_fade_in(self._key_note)   # ТЗ п.9

    def _on_ptt_mode(self, btn):
        self.controller.set_ptt_mode(btn.property("ptt_mode"))

    def _on_dictation_queue(self, on: bool):
        # feature/dictation-queue: діє одразу (наступне диктування читає cfg).
        # Вимкнення НЕ чіпає фрази, що вже в черзі — вони чесно доопрацьовуються.
        self.controller.cfg.dictation_queue_enabled = bool(on)
        self.controller.save_config()

    def _on_highlight_uncertain(self, on: bool):
        # feature/model-bottlenecks (під-хвиля 2): діє одразу (наступне диктування
        # читає cfg). Вимкнення прибирає DTW-прохід word_timestamps — трохи швидше
        # на слабкому CPU, ціною відсутності золотої підсвітки непевних слів.
        self.controller.cfg.highlight_uncertain_words = bool(on)
        self.controller.save_config()

    def sync_live_transcription(self, on):
        # feature/live-transcription (перенесено з Наради): фон програмно вимкнув
        # живий режим (напр. старт наради / збій воркера) → відобразити в чекбоксі
        # без повторного save (blockSignals, щоб toggled не викликав set_ ще раз).
        self._live_transcription.blockSignals(True)
        try:
            self._live_transcription.setChecked(bool(on))
        finally:
            self._live_transcription.blockSignals(False)

    def _on_voice_punct(self, on: bool):
        # feature/voice-punctuation: діє одразу (наступне диктування читає cfg)
        self.controller.cfg.voice_punctuation = bool(on)
        self.controller.save_config()

    def _on_voice_nav(self, on: bool):
        # feature/office-voice-nav: діє одразу (наступне диктування читає cfg)
        self.controller.cfg.voice_nav_enabled = bool(on)
        self.controller.save_config()

    def _open_nav_commands(self):
        # feature/office-voice-nav: показати шпаргалку команд навігації (вбудовані
        # + користувацькі аліаси з navcommands.toml)
        from ..navcommands_dialog import NavCommandsDialog
        aliases = getattr(self.controller, "_nav_aliases", None)
        NavCommandsDialog(self.controller.cfg.language, aliases, self).exec()

    def _on_review_changes(self, on: bool):
        # feature/player-pack: діє одразу (наступна ручна розшифровка читає cfg)
        self.controller.cfg.review_text_changes = bool(on)
        self.controller.save_config()

    # --- feature/punctuation-plus: постобробка тексту ------------------------
    def _autocorrect_ready(self) -> bool:
        from whisper_core import autocorrect, paths as wc_paths
        return autocorrect.available(wc_paths.autocorrect_dict_path())

    def _punctuator_ready(self) -> bool:
        from whisper_core import punctuator, paths as wc_paths
        return punctuator.available(wc_paths.punctuator_model_dir())

    def _refresh_textproc_controls(self):
        """Синхронізувати чекбокси/статуси з фактичною наявністю компонентів.
        Пакет відсутній → кнопка недоступна й окреме пояснення; лише даних немає
        → пропонуємо завантажити (як діаризація)."""
        from whisper_core import autocorrect, punctuator
        # feature/edit-pack: коли «Не виправляй мою мову» ввімкнено — обидва кроки
        # постобробки обходяться конвеєром, тож глушимо чекбокси, щоб не збивати з пантелику.
        preserve = bool(getattr(self.controller.cfg, "preserve_speech", False))
        # (1) автокорекція
        pkg_ac = autocorrect.symspell_available()
        ready_ac = self._autocorrect_ready()
        self._autocorrect.setEnabled(ready_ac and not preserve)
        self._autocorrect_dl.setEnabled(pkg_ac and not ready_ac)
        if not pkg_ac:
            self._autocorrect_status.setText(tr("set_component_no_package"))
        elif ready_ac:
            self._autocorrect_status.setText(tr("set_component_ready"))
        else:
            self._autocorrect_status.setText(tr("set_component_missing"))
        # (2) пунктуатор
        pkg_pn = punctuator.punctuators_available()
        ready_pn = self._punctuator_ready()
        self._punctuator.setEnabled(ready_pn and not preserve)
        self._punctuator_dl.setEnabled(pkg_pn and not ready_pn)
        if not pkg_pn:
            self._punctuator_status.setText(tr("set_component_no_package"))
        elif ready_pn:
            self._punctuator_status.setText(tr("set_component_ready"))
        else:
            self._punctuator_status.setText(tr("set_component_missing"))

    def _on_preserve_speech(self, on: bool):
        # feature/edit-pack: діє одразу (наступне диктування читає cfg). Оновлюємо
        # доступність чекбоксів постобробки, бо вони обходяться при увімкненому режимі.
        self.controller.cfg.preserve_speech = bool(on)
        self.controller.save_config()
        self._refresh_textproc_controls()

    def _on_autocorrect(self, on: bool):
        # діє одразу (наступне диктування читає cfg)
        self.controller.cfg.autocorrect_enabled = bool(on)
        self.controller.save_config()

    def _on_punctuator(self, on: bool):
        self.controller.cfg.punctuator_enabled = bool(on)
        self.controller.save_config()

    def _download_autocorrect(self):
        from ..dl_consent import confirm_download
        if not confirm_download(self.window(), name=tr("dl_name_autocorrect"),
                                size_mb=1):
            return
        from whisper_core.autocorrect_download import download_and_install
        from whisper_core import paths as wc_paths
        self._autocorrect_dl.setEnabled(False)
        self._autocorrect_status.setText(tr("set_component_downloading"))
        self._ac_worker = _ComponentDownloadWorker(
            download_and_install, wc_paths.autocorrect_dict_path(), self)
        self._ac_worker.finished_ok.connect(self._refresh_textproc_controls)
        self._ac_worker.failed.connect(lambda err: (
            self._set_component_error(self._autocorrect_status, err),
            self._autocorrect_dl.setEnabled(True)))
        self._ac_worker.start()

    def _download_punctuator(self):
        from ..dl_consent import confirm_download
        if not confirm_download(self.window(), name=tr("dl_name_punctuator"),
                                size_mb=234):
            return
        from whisper_core.punctuator import download_and_install
        from whisper_core import paths as wc_paths
        self._punctuator_dl.setEnabled(False)
        self._punctuator_status.setText(tr("set_component_downloading"))
        self._pn_worker = _ComponentDownloadWorker(
            download_and_install, wc_paths.punctuator_model_dir(), self)
        self._pn_worker.finished_ok.connect(self._refresh_textproc_controls)
        self._pn_worker.failed.connect(lambda err: (
            self._set_component_error(self._punctuator_status, err),
            self._punctuator_dl.setEnabled(True)))
        self._pn_worker.start()

    @staticmethod
    def _set_component_error(label: QLabel, err: str):
        """Показати помилку завантаження компонента без обрізання.

        Текст помилки містить довільний рядок винятку (часто довгий URL/мережевий
        клас без пробілів). Три захисти проти обрізання wordWrap-статусу в сітці:
        (1) `_soft_break_long` дає рушію переносу точки розриву в довгих токенах —
        інакше токен, ширший за колонку, ріжеться по горизонталі; (2) повний
        оригінальний текст — у tooltip (копіювати/прочитати цілком); (3)
        `updateGeometry` штовхає перерахунок висоти рядка сітки під перенесений
        текст (страховка: у деяких розкладках висота не росте сама після setText)."""
        msg = tr("set_component_failed", error=err)
        label.setText(_soft_break_long(msg))
        label.setToolTip(msg)
        label.updateGeometry()

    # --- КУДИ ТЕКСТ ---
    def _output_group(self):
        frame, lay = _card(tr("set_output_eyebrow"), "hint_output")

        restore = QCheckBox(tr("set_restore_clipboard"))
        restore.setChecked(bool(self.controller.cfg.restore_clipboard))
        restore.toggled.connect(self._on_restore_clipboard)
        lay.addWidget(restore)
        restore_hint = QLabel(tr("set_restore_clipboard_hint"))
        restore_hint.setProperty("muted", True)
        restore_hint.setWordWrap(True)
        lay.addWidget(restore_hint)

        # feature/cascade-paste: opt-in — у звичайні вікна доставляти текст
        # набором замість Ctrl+V (для полів, де вставка заборонена). Діє одразу.
        typing = QCheckBox(tr("set_paste_typing"))
        typing.setChecked(bool(getattr(self.controller.cfg, "paste_typing_fallback", False)))
        typing.toggled.connect(self._on_paste_typing)
        lay.addWidget(typing)
        typing_hint = QLabel(tr("set_paste_typing_hint"))
        typing_hint.setProperty("muted", True)
        typing_hint.setWordWrap(True)
        lay.addWidget(typing_hint)

        # feature/paste-preview: opt-in — після розпізнавання показати картку
        # перегляду/правки перед вставкою. Діє одразу (наступне диктування читає cfg).
        preview = QCheckBox(tr("set_paste_preview"))
        preview.setChecked(bool(getattr(self.controller.cfg, "paste_preview", False)))
        preview.toggled.connect(self._on_paste_preview)
        lay.addWidget(preview)
        preview_hint = QLabel(tr("set_paste_preview_hint"))
        preview_hint.setProperty("muted", True)
        preview_hint.setWordWrap(True)
        lay.addWidget(preview_hint)

        # feature/transcript-editing: opt-in — кнопка «Редагувати» на картках
        # Файлів і Нарад (правка тексту + пошук). Діє одразу для нових карток.
        editing = QCheckBox(tr("set_transcript_edit"))
        editing.setChecked(bool(getattr(self.controller.cfg,
                                        "transcript_editing_enabled", False)))
        editing.toggled.connect(self._on_transcript_editing)
        lay.addWidget(editing)
        editing_hint = QLabel(tr("set_transcript_edit_hint"))
        editing_hint.setProperty("muted", True)
        editing_hint.setWordWrap(True)
        lay.addWidget(editing_hint)

        # feature/paste-safety: тумблер буфера останніх вставок (трей-підменю).
        # Вимкнення чистить буфер — щоб чутливі диктування не лишались у меню.
        history = QCheckBox(tr("set_paste_history"))
        history.setChecked(bool(getattr(self.controller.cfg,
                                        "paste_history_enabled", True)))
        history.toggled.connect(self._on_paste_history)
        lay.addWidget(history)
        history_hint = QLabel(tr("set_paste_history_hint"))
        history_hint.setProperty("muted", True)
        history_hint.setWordWrap(True)
        lay.addWidget(history_hint)
        return frame

    def _on_paste_history(self, on: bool):
        self.controller.set_paste_history_enabled(bool(on))

    def _on_transcript_editing(self, on: bool):
        self.controller.cfg.transcript_editing_enabled = bool(on)
        self.controller.save_config()

    def _on_restore_clipboard(self, on: bool):
        self.controller.cfg.restore_clipboard = bool(on)
        self.controller.save_config()

    def _on_paste_preview(self, on: bool):
        self.controller.cfg.paste_preview = bool(on)
        self.controller.save_config()

    # --- НАРАДА (feature/meeting-ui) ---
    def _meeting_group(self, cfg):
        """Лише локальна тека нарад; керування записом і розшифровкою — на «Нараді»."""
        frame, lay = _card(tr("set_meeting_eyebrow"))
        g = _grid()

        # Канонічний multi-select: кожен mic і системний loopback = окрема доріжка.
        from whisper_core.config import (MEETING_SYSTEM_SOURCE,
                                         meeting_microphone_token,
                                         meeting_record_source_specs)
        specs = meeting_record_source_specs(cfg)
        selected_tokens = {
            (MEETING_SYSTEM_SOURCE if spec.kind == "system"
             else meeting_microphone_token(spec.device_name))
            for spec in specs
        }
        self._meeting_source_checks = []
        sourcebox = QVBoxLayout()
        sourcebox.setSpacing(4)
        choices = [(meeting_microphone_token(None), tr("set_meeting_default_mic"))]
        try:
            from whisper_core.meeting.capture import list_input_devices as list_meeting_inputs
            meeting_devices = [item.get("name") for item in list_meeting_inputs()]
        except Exception:
            meeting_devices = list_input_devices()
        choices.extend((meeting_microphone_token(device), device)
                       for device in meeting_devices if device)
        choices.append((MEETING_SYSTEM_SOURCE, tr("set_meeting_system_audio")))
        seen = set()
        for token, label in choices:
            if token in seen:
                continue
            seen.add(token)
            check = QCheckBox(label)
            check.setProperty("meetingSourceToken", token)
            check.setAccessibleName(label)
            check.setChecked(token in selected_tokens)
            check.toggled.connect(self._on_meeting_record_sources)
            self._meeting_source_checks.append(check)
            sourcebox.addWidget(check)
        g.addWidget(_labeled_hint(tr("set_meeting_record_sources"), "hint_meeting_sources"),
                    2, 0, Qt.AlignTop)
        g.addLayout(sourcebox, 2, 1)

        self._meeting_chunk_minutes = QComboBox()
        configured_minutes = int(getattr(cfg, "meeting_export_segment_minutes", 10))
        values = [5, 10, 15, 30]
        if configured_minutes not in values:
            values.append(configured_minutes)
            values.sort()
        for minutes in values:
            self._meeting_chunk_minutes.addItem(
                tr("set_meeting_chunk_minutes", minutes=minutes), minutes)
        self._meeting_chunk_minutes.setCurrentIndex(
            self._meeting_chunk_minutes.findData(configured_minutes))
        self._meeting_chunk_minutes.setAccessibleName(tr("set_meeting_chunk_size"))
        self._meeting_chunk_minutes.currentIndexChanged.connect(
            self._on_meeting_chunk_minutes)
        g.addWidget(_labeled_hint(tr("set_meeting_chunk_size"),
                                  "hint_meeting_chunk_size"), 4, 0)
        g.addWidget(self._meeting_chunk_minutes, 4, 1)

        # feature/evidence-plus: «хто зафіксував» — вільний текст у подію created
        # журналу цілісності (порожньо → не пишемо). Єдиний UI-вхід для поля.
        self._meeting_operator = QLineEdit(getattr(cfg, "operator_name", "") or "")
        self._meeting_operator.setPlaceholderText(tr("set_meeting_operator_placeholder"))
        self._meeting_operator.setAccessibleName(tr("set_meeting_operator"))
        self._meeting_operator.setMaximumWidth(_CTRL_MAX)
        self._meeting_operator.editingFinished.connect(self._on_meeting_operator)
        g.addWidget(_labeled_hint(tr("set_meeting_operator"),
                                  "hint_meeting_operator"), 5, 0)
        g.addWidget(self._meeting_operator, 5, 1)

        self._meeting_encrypt = QCheckBox(tr("set_meeting_encrypt"))
        self._meeting_encrypt.setAccessibleName(tr("set_meeting_encrypt"))
        self._meeting_encrypt.setChecked(bool(getattr(cfg, "meeting_encrypt", False)))
        self._meeting_encrypt.toggled.connect(self._on_meeting_encrypt)
        encryption_box = QVBoxLayout(); encryption_box.setSpacing(4)
        encryption_box.addWidget(self._meeting_encrypt)
        encryption_hint = QLabel(tr("set_meeting_encrypt_hint"))
        encryption_hint.setProperty("muted", True); encryption_hint.setWordWrap(True)
        encryption_box.addWidget(encryption_hint)
        password_warning = QLabel(tr("set_meeting_encrypt_password_warn"))
        password_warning.setProperty("level", "body")
        password_warning.setWordWrap(True)
        password_warning.setAccessibleName(
            tr("set_meeting_encrypt_password_warn"))
        encryption_box.addWidget(password_warning)
        g.addWidget(_labeled_hint(tr("set_meeting_encrypt_label"),
                                  "hint_meeting_encrypt"), 6, 0, Qt.AlignTop)
        g.addLayout(encryption_box, 6, 1)

        vaultbox = QVBoxLayout(); vaultbox.setSpacing(8)
        self._vault_state_lbl = QLabel(); self._vault_state_lbl.setProperty("muted", True)
        self._vault_state_lbl.setWordWrap(True); vaultbox.addWidget(self._vault_state_lbl)
        # ─── ТРИ ряди кнопок захисту ───
        # Вісім кнопок оголошені тут, але видимі завжди щонайбільше чотири
        # (vault_controls_for_state). В одному ряду вони не вміщались навіть
        # учотирьох: на мінімумі вікна (1000) колонка дає ~450 точок, а підписи
        # просять 805 — Qt тиснув кнопки до 130 точок і різав «Захистити
        # файлом-ключем…» (197/130), «Пароль + файл-ключ…», «Створити
        # файл-ключ…»; на ноутбучних 1280 вони теж були нижчі за sizeHint.
        # Підписи не скорочуємо — розкладаємо на ряди так, щоб у БУДЬ-ЯКОМУ
        # стані сховища кожен ряд просив не більше, ніж колонка дає (452 точки
        # українською, 422 англійською — англійська вужча й вирішальна):
        #   без секрету → [задати пароль, файл-ключ]  [двофактор, створити]
        #   парольне    → [змінити, заблокувати]      [зняти, створити]  [відновлення]
        #   відкрите    → [заблокувати]               [зняти, створити]  [відновлення]
        # «Створити новий код відновлення…» — найдовший підпис (307 точок), тож
        # він завжди сам у ряду: у парі з будь-ким знову різався б.
        # Порядок кнопок у ряду — той самий, що в коді (стани не міняють його).
        vrow = QHBoxLayout(); vrow.setSpacing(8)
        vrow2 = QHBoxLayout(); vrow2.setSpacing(8)
        vrow3 = QHBoxLayout(); vrow3.setSpacing(8)
        self._vault_set_btn = GlassButton(tr("set_vault_pw_set"))
        self._vault_set_btn.clicked.connect(lambda: self._open_vault_dialog("set"))
        self._vault_keyfile_btn = GlassButton(tr("set_vault_keyfile_set"))
        self._vault_keyfile_btn.clicked.connect(
            lambda: self._open_vault_protect_keyfile(False))
        self._vault_twofactor_btn = GlassButton(tr("set_vault_twofactor_set"))
        self._vault_twofactor_btn.clicked.connect(
            lambda: self._open_vault_protect_keyfile(True))
        self._vault_change_btn = GlassButton(tr("set_vault_pw_change"))
        self._vault_change_btn.clicked.connect(lambda: self._open_vault_dialog("change"))
        self._vault_remove_btn = GlassButton(tr("set_vault_pw_remove"))
        self._vault_remove_btn.clicked.connect(self._on_vault_remove)
        self._vault_lock_btn = GlassButton(tr("set_vault_lock_now"))
        self._vault_lock_btn.clicked.connect(self._on_vault_lock)
        self._vault_recovery_btn = GlassButton(tr("set_vault_pw_recovery"))
        self._vault_recovery_btn.clicked.connect(self._on_vault_regenerate)
        self._vault_create_keyfile_btn = GlassButton(tr("set_vault_keyfile_create"))
        self._vault_create_keyfile_btn.clicked.connect(self._on_vault_create_keyfile)
        for button in (self._vault_set_btn, self._vault_keyfile_btn,
                       self._vault_change_btn, self._vault_lock_btn):
            button.setAccessibleName(button.text())
            vrow.addWidget(button)
        for button in (self._vault_twofactor_btn, self._vault_remove_btn,
                       self._vault_create_keyfile_btn):
            button.setAccessibleName(button.text())
            vrow2.addWidget(button)
        self._vault_recovery_btn.setAccessibleName(self._vault_recovery_btn.text())
        vrow3.addWidget(self._vault_recovery_btn)
        vrow.addStretch(); vaultbox.addLayout(vrow)
        vrow2.addStretch(); vaultbox.addLayout(vrow2)
        vrow3.addStretch(); vaultbox.addLayout(vrow3)
        g.addWidget(_labeled_hint(tr("set_vault_pw_label"), "hint_vault_password"),
                    7, 0, Qt.AlignTop)
        g.addLayout(vaultbox, 7, 1)
        self._refresh_vault_ui()

        # тека записів: локальна, поза хмарою (посилена OPSEC-вимога)
        dirbox = QVBoxLayout()
        dirbox.setSpacing(8)
        self._meeting_dir_label = QLabel()
        self._meeting_dir_label.setWordWrap(True)
        self._set_meeting_dir_text(cfg.meeting_dir)
        pick = QPushButton(tr("common_change"))
        pick.clicked.connect(self._on_pick_meeting_dir)
        std = QPushButton(tr("set_default"))
        std.clicked.connect(self._on_std_meeting_dir)
        dirbox.addWidget(self._meeting_dir_label)
        acts = QHBoxLayout()
        acts.setSpacing(10)
        acts.addWidget(pick)
        acts.addWidget(std)
        acts.addStretch()
        dirbox.addLayout(acts)
        note = QLabel(tr("set_meeting_dir_local_note"))
        note.setProperty("muted", True)
        note.setWordWrap(True)
        dirbox.addWidget(note)
        g.addWidget(_labeled_hint(tr("set_meeting_dir"), "hint_meeting_dir"), 0, 0)
        g.addLayout(dirbox, 0, 1)

        hotrow = QHBoxLayout(); hotrow.setSpacing(10)
        combo = getattr(cfg, "meeting_bookmark_hotkey", "") or ""
        self._meeting_bookmark_key = QLabel(pretty(combo) if combo else tr("set_meeting_bookmark_none"))
        self._meeting_bookmark_key.setProperty("kbd", True)
        change = QPushButton(tr("common_change")); change.clicked.connect(self.controller.start_meeting_bookmark_key_capture)
        clear = QPushButton(tr("set_qol_key_clear")); clear.setProperty("ghost", True); clear.clicked.connect(self.controller.clear_meeting_bookmark_hotkey)
        hotrow.addWidget(self._meeting_bookmark_key); hotrow.addWidget(change); hotrow.addWidget(clear); hotrow.addStretch()
        g.addWidget(_form_label(tr("set_meeting_bookmark_hotkey")), 1, 0)
        g.addLayout(hotrow, 1, 1)
        self.controller.meeting_bookmark_key_captured.connect(self._on_meeting_bookmark_key)

        # feature/diary-calendar: опційний файл календаря (.ics) для авто-назви
        icsrow = QHBoxLayout(); icsrow.setSpacing(10)
        self._meeting_ics_label = QLabel()
        self._meeting_ics_label.setWordWrap(True)
        self._set_meeting_ics_text(getattr(cfg, "meeting_ics_path", None))
        ics_pick = QPushButton(tr("common_change"))
        ics_pick.clicked.connect(self._on_pick_meeting_ics)
        ics_clear = QPushButton(tr("set_qol_key_clear"))
        ics_clear.setProperty("ghost", True)
        ics_clear.clicked.connect(self._on_clear_meeting_ics)
        icsrow.addWidget(self._meeting_ics_label, stretch=1)
        icsrow.addWidget(ics_pick)
        icsrow.addWidget(ics_clear)
        g.addWidget(_labeled_hint(tr("set_meeting_ics"), "hint_meeting_ics"), 3, 0)
        g.addLayout(icsrow, 3, 1)

        lay.addLayout(g)

        # feature/voice-memory (Т41): секція «Збережені голоси»
        vmem_frame, vmem_lay = _card(tr("voice_memory_title"), "voice_memory_hint")

        self._vmem_enabled_check = QCheckBox(tr("voice_memory_enabled"))
        self._vmem_enabled_check.setChecked(bool(getattr(cfg, "voice_memory_enabled", False)))
        self._vmem_enabled_check.setAccessibleName(tr("voice_memory_enabled"))
        self._vmem_enabled_check.toggled.connect(self._on_vmem_toggled)
        vmem_lay.addWidget(self._vmem_enabled_check)

        vmem_hint = QLabel(tr("voice_memory_hint"))
        vmem_hint.setProperty("muted", True)
        vmem_hint.setWordWrap(True)
        vmem_lay.addWidget(vmem_hint)

        self._vmem_table = QTableWidget(0, 4)
        theme.setup_table(self._vmem_table)
        self._vmem_table.setHorizontalHeaderLabels([
            tr("common_name"),
            tr("voice_memory_samples", n="#"),
            tr("common_date"),
            tr("common_actions")
        ])
        self._vmem_table.verticalHeader().setVisible(False)
        self._vmem_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._vmem_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self._vmem_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self._vmem_table.setMinimumHeight(120)
        vmem_lay.addWidget(self._vmem_table)

        self._vmem_empty = QLabel(tr("voice_memory_empty"))
        self._vmem_empty.setWordWrap(True)
        self._vmem_empty.setProperty("muted", True)
        vmem_lay.addWidget(self._vmem_empty)

        btn_row = QHBoxLayout()
        self._vmem_clear_btn = QPushButton(tr("voice_memory_clear_all"))
        self._vmem_clear_btn.setAccessibleName(tr("voice_memory_clear_all"))
        self._vmem_clear_btn.clicked.connect(self._on_vmem_clear_all)
        btn_row.addWidget(self._vmem_clear_btn)
        btn_row.addStretch()
        vmem_lay.addLayout(btn_row)

        self._refresh_voice_memory_list()
        lay.addWidget(vmem_frame)
        return frame

    def _on_vmem_toggled(self, checked: bool):
        self.controller.cfg.voice_memory_enabled = bool(checked)
        self.controller.save_config()

    def _refresh_voice_memory_list(self):
        try:
            voices = self.controller.list_voice_memories()
        except Exception:
            voices = []
        self._vmem_table.setRowCount(0)
        for v in voices:
            r = self._vmem_table.rowCount()
            self._vmem_table.insertRow(r)
            self._vmem_table.setItem(r, 0, QTableWidgetItem(v.get("name", "")))
            samples = v.get("samples_count", 1)
            self._vmem_table.setItem(r, 1, QTableWidgetItem(tr("voice_memory_samples", n=samples)))
            date_str = _format_voice_date(v.get("updated_at"))
            updated = tr("voice_memory_updated", date=date_str) if date_str else ""
            self._vmem_table.setItem(r, 2, QTableWidgetItem(updated))

            del_btn = QPushButton(tr("voice_memory_delete"))
            name = v.get("name", "")
            del_btn.setAccessibleName(f"{tr('voice_memory_delete')} {name}")
            del_btn.clicked.connect(lambda _=False, n=name: self._on_vmem_delete_one(n))
            self._vmem_table.setCellWidget(r, 3, del_btn)

        has_voices = bool(voices)
        self._vmem_table.setVisible(has_voices)
        self._vmem_empty.setVisible(not has_voices)
        self._vmem_clear_btn.setEnabled(has_voices)

    def _on_vmem_delete_one(self, name: str):
        if self.controller.delete_voice_memory(name):
            self._refresh_voice_memory_list()

    def _on_vmem_clear_all(self):
        resp = QMessageBox.question(
            self, tr("voice_memory_title"),
            tr("voice_memory_clear_confirm"))
        if resp == QMessageBox.Yes:
            self.controller.clear_voice_memories()
            self._refresh_voice_memory_list()

    def _refresh_vault_ui(self):
        if getattr(self, "_vault_state_lbl", None) is None:
            return
        from .vault_dialogs import VAULT_STATE_LABELS, vault_controls_for_state
        try:
            state = self.controller.meeting_vault_state()
        except Exception:
            state = "none"
        self._vault_state_lbl.setText(
            tr(VAULT_STATE_LABELS.get(state, "set_vault_state_none")))
        show = vault_controls_for_state(state)
        self._vault_set_btn.setVisible(show["set"])
        self._vault_keyfile_btn.setVisible(show["set_keyfile"])
        self._vault_twofactor_btn.setVisible(show["set_twofactor"])
        self._vault_change_btn.setVisible(show["change"])
        self._vault_remove_btn.setVisible(show["remove"])
        self._vault_lock_btn.setVisible(show["lock"])
        self._vault_recovery_btn.setVisible(show["recovery"])
        self._vault_create_keyfile_btn.setVisible(state != "lost")
        self._vault_remove_btn.setText(tr(
            "set_vault_pw_remove" if state in ("password", "locked")
            else "set_vault_remove_protection"))
        self._vault_remove_btn.setAccessibleName(self._vault_remove_btn.text())
        self._vault_keyfile_state = state

    def _vault_notify(self, key):
        tray = getattr(self.controller, "tray", None)
        if tray is not None:
            tray.notify(tr(key))

    def _open_vault_dialog(self, mode):
        from .vault_dialogs import VaultPasswordDialog
        if VaultPasswordDialog(self.controller, mode, self).exec() == QDialog.Accepted:
            self._vault_notify("vault_pw_removed" if mode == "remove" else "vault_pw_saved")
        self._refresh_vault_ui()

    def _open_vault_protect_keyfile(self, two_factor):
        from .vault_dialogs import VaultProtectKeyfileDialog
        if VaultProtectKeyfileDialog(
                self.controller, two_factor, self).exec() == QDialog.Accepted:
            self._vault_notify("vault_keyfile_saved")
        self._refresh_vault_ui()

    def _on_vault_create_keyfile(self):
        from .vault_dialogs import _save_keyfile
        QMessageBox.information(self, tr("vault_keyfile_create_title"),
                                tr("vault_keyfile_create_warning"))
        path = _save_keyfile(self)
        if path:
            error = self.controller.meeting_vault_generate_keyfile(path)
            self._vault_notify(error or "vault_keyfile_created")

    def _on_vault_remove(self):
        if getattr(self, "_vault_keyfile_state", "none") in ("password", "locked"):
            self._open_vault_dialog("remove")
            return
        error = self.controller.meeting_vault_remove_keyfile()
        self._vault_notify(error or "vault_keyfile_removed")
        self._refresh_vault_ui()

    def _on_vault_lock(self):
        self.controller.meeting_vault_lock()
        self._refresh_vault_ui()

    def _on_vault_regenerate(self):
        state = getattr(self, "_vault_keyfile_state", "none")
        if state in ("keyfile", "twofactor"):
            error, code = self.controller.meeting_vault_regenerate_recovery()
            if error:
                self._vault_notify(error)
            elif code:
                from .vault_dialogs import VaultRecoveryCodeDialog
                VaultRecoveryCodeDialog(code, self).exec()
                self._vault_notify("vault_recovery_saved")
            self._refresh_vault_ui()
            return
        from .vault_dialogs import VaultRegenerateRecoveryDialog
        if VaultRegenerateRecoveryDialog(
                self.controller, self).exec() == QDialog.Accepted:
            self._vault_notify("vault_recovery_saved")
        self._refresh_vault_ui()

    def _set_meeting_ics_text(self, path):
        self._meeting_ics_label.setText(str(path) if path
                                        else tr("set_meeting_ics_none"))

    def _on_pick_meeting_ics(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("set_meeting_ics"), "", tr("set_meeting_ics_filter"))
        if not path:
            return
        self.controller.cfg.meeting_ics_path = path
        self.controller.save_config()
        self._set_meeting_ics_text(path)

    def _on_clear_meeting_ics(self):
        self.controller.cfg.meeting_ics_path = None
        self.controller.save_config()
        self._set_meeting_ics_text(None)

    def _on_meeting_bookmark_key(self, combo: str):
        self._meeting_bookmark_key.setText(pretty(combo) if combo else tr("set_meeting_bookmark_none"))

    def _set_meeting_dir_text(self, path):
        self._meeting_dir_label.setText(str(path) if path else tr("set_default_folder"))

    def _on_meeting_record_sources(self, _checked=False):
        from whisper_core.config import (MEETING_MIC_SOURCE_PREFIX,
                                         MEETING_MULTIMIC_MAX,
                                         MEETING_SYSTEM_SOURCE)
        sender = self.sender()
        if _checked and sender is not None:
            token = sender.property("meetingSourceToken")
            for check in self._meeting_source_checks:
                other = check.property("meetingSourceToken")
                mutually_exclusive = (
                    token == MEETING_MIC_SOURCE_PREFIX
                    and other.startswith(MEETING_MIC_SOURCE_PREFIX)
                    and other != token
                ) or (
                    token.startswith(MEETING_MIC_SOURCE_PREFIX)
                    and token != MEETING_MIC_SOURCE_PREFIX
                    and other == MEETING_MIC_SOURCE_PREFIX
                )
                if mutually_exclusive and check.isChecked():
                    check.blockSignals(True)
                    check.setChecked(False)
                    check.blockSignals(False)
            selected_microphones = [
                check for check in self._meeting_source_checks
                if check.isChecked()
                and str(check.property("meetingSourceToken")).startswith(
                    MEETING_MIC_SOURCE_PREFIX)
            ]
            if len(selected_microphones) > MEETING_MULTIMIC_MAX:
                sender.blockSignals(True)
                sender.setChecked(False)
                sender.blockSignals(False)
                motion.toast(
                    self,
                    tr("meeting_mic_limit", max=MEETING_MULTIMIC_MAX))
                return
        selected = [check.property("meetingSourceToken")
                    for check in self._meeting_source_checks if check.isChecked()]
        if not selected:
            if sender is not None:
                sender.blockSignals(True)
                sender.setChecked(True)
                sender.blockSignals(False)
                selected = [sender.property("meetingSourceToken")]
        self.controller.cfg.meeting_record_sources = selected
        microphones = [token[len(MEETING_MIC_SOURCE_PREFIX):]
                       for token in selected
                       if token.startswith(MEETING_MIC_SOURCE_PREFIX)
                       and token[len(MEETING_MIC_SOURCE_PREFIX):]]
        self.controller.cfg.meeting_mic_devices = microphones
        has_system = MEETING_SYSTEM_SOURCE in selected
        self.controller.cfg.meeting_sources = (
            "multimic" if len(microphones) > 1 else "mic+sys" if has_system else "mic")
        self.controller.save_config()

    def _on_meeting_chunk_minutes(self, index):
        minutes = self._meeting_chunk_minutes.itemData(index)
        if minutes is None:
            return
        self.controller.cfg.meeting_export_segment_minutes = int(minutes)
        self.controller.save_config()

    def _on_meeting_operator(self):
        # feature/evidence-plus: зберегти «хто зафіксував» у config (порожньо → "").
        self.controller.cfg.operator_name = self._meeting_operator.text().strip()
        self.controller.save_config()

    def _on_pick_meeting_dir(self):
        # НІКОЛИ не пропонуємо хмарну теку як дефолт — стартова тека системного
        # діалогу порожня (безпека Миколи); вибір робить користувач свідомо.
        path = QFileDialog.getExistingDirectory(self, tr("set_meeting_dir"))
        if not path:
            return
        self.controller.set_meeting_dir(path)
        self._set_meeting_dir_text(path)

    def _on_std_meeting_dir(self):
        self.controller.set_meeting_dir("")
        self._set_meeting_dir_text(None)

    def _on_meeting_encrypt(self, on: bool):
        if not self.controller.set_meeting_encryption(bool(on)):
            self._meeting_encrypt.blockSignals(True)
            self._meeting_encrypt.setChecked(not bool(on))
            self._meeting_encrypt.blockSignals(False)
        self._refresh_vault_ui()
    def _on_paste_typing(self, on: bool):
        self.controller.cfg.paste_typing_fallback = bool(on)
        self.controller.save_config()

    # --- ПРОФІЛІ ЗАСТОСУНКІВ (feature/context-profiles) ---
    def _context_group(self):
        frame, lay = _card(tr("ctx_eyebrow"), "ctx_hint", clickable=True,
                           title_key="ctx_eyebrow")
        intro = QLabel(tr("ctx_intro"))
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)

        self._ctx_path = wc_paths.context_profiles_path()
        # Контролер уже завантажив профілі на старті (reload_context_profiles) —
        # переиспользуємо їх, щоб не читати той самий файл удруге при побудові
        # вікна (інакше зайвий парсинг + дубль лог-шуму). Копія списку: правки в
        # таблиці не чіпають матчер (він перебудовується через reload на save).
        _m = getattr(self.controller, "_ctx_matcher", None)
        if _m is not None:
            self._ctx_items, self._ctx_default = list(_m.profiles), _m.default
        else:
            self._ctx_items, self._ctx_default = ctx_mod.load_profiles(self._ctx_path)

        self._ctx_table = QTableWidget(0, 3)
        theme.setup_table(self._ctx_table)
        self._ctx_table.setHorizontalHeaderLabels(
            [tr("ctx_col_name"), tr("ctx_col_apps"), tr("ctx_col_behavior")])
        self._ctx_table.verticalHeader().setVisible(False)
        self._ctx_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._ctx_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._ctx_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._ctx_table.setWordWrap(True)
        hh = self._ctx_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._ctx_table.setMinimumHeight(140)
        lay.addWidget(self._ctx_table)

        self._ctx_empty = QLabel(tr("ctx_empty"))
        self._ctx_empty.setWordWrap(True)
        self._ctx_empty.setProperty("muted", True)
        lay.addWidget(self._ctx_empty)

        row = QHBoxLayout()
        add = GlassButton(tr("ctx_add"))
        add.clicked.connect(self._ctx_add)
        delete = QPushButton(tr("ctx_delete"))
        delete.clicked.connect(self._ctx_delete)
        openf = QPushButton(tr("ctx_open_file"))
        openf.clicked.connect(self._ctx_open_file)
        row.addWidget(add)
        row.addWidget(delete)
        row.addStretch()
        row.addWidget(openf)
        lay.addLayout(row)

        self._ctx_populate()
        return frame

    @staticmethod
    def _ctx_behavior_text(prof) -> str:
        b = prof.behavior
        if not b.enabled:
            return tr("ctx_beh_disabled")
        parts = []
        if b.auto_enter:
            parts.append(tr("ctx_beh_auto_enter"))
        if b.dictionary:
            parts.append(tr("ctx_beh_dict", name=b.dictionary))
        fmt = getattr(b, "formatting", "plain")
        if fmt and fmt != "plain":
            parts.append(tr("ctx_beh_fmt", name=tr(f"ctx_fmt_{fmt}")))
        return ", ".join(parts) if parts else tr("ctx_beh_default")

    def _ctx_populate(self):
        self._ctx_table.setRowCount(0)
        for prof in self._ctx_items:
            r = self._ctx_table.rowCount()
            self._ctx_table.insertRow(r)
            self._ctx_table.setItem(r, 0, QTableWidgetItem(prof.name))
            self._ctx_table.setItem(r, 1, QTableWidgetItem(", ".join(prof.apps)))
            self._ctx_table.setItem(
                r, 2, QTableWidgetItem(self._ctx_behavior_text(prof)))
        self._ctx_table.resizeRowsToContents()
        has = bool(self._ctx_items)
        self._ctx_table.setVisible(has)
        self._ctx_empty.setVisible(not has)

    def _ctx_save(self):
        """Записати файл і перебудувати матчер контролера (порядок=пріоритет).
        Правила «вікно → профіль» (feature/auto-profile) живуть у тому ж файлі —
        передаємо їх, щоб правка профілів їх не стерла."""
        ctx_mod.save_profiles(
            self._ctx_path, self._ctx_items, self._ctx_default,
            auto_rules=getattr(self, "_auto_rules", None),
            auto_enabled=getattr(self, "_auto_enabled", None))
        self.controller.reload_context_profiles()
        self._ctx_populate()

    def _ctx_dict_names(self):
        try:
            return [p.name for p in
                    wc_profiles.list_profiles(wc_paths.profiles_root())]
        except Exception:
            return []

    def _ctx_add(self):
        dlg = ContextProfileDialog(
            self.controller._ctx_resolver, self._ctx_dict_names(), self)
        if dlg.exec() and dlg.result_profile is not None:
            self._ctx_items.append(dlg.result_profile)
            self._ctx_save()

    def _ctx_delete(self):
        r = self._ctx_table.currentRow()
        if 0 <= r < len(self._ctx_items):
            del self._ctx_items[r]
            self._ctx_save()

    def _ctx_open_file(self):
        """Відкрити context_profiles.toml у системному редакторі (створивши за
        потреби файл із поточним станом, щоб було що редагувати)."""
        if not self._ctx_path.exists():
            ctx_mod.save_profiles(
                self._ctx_path, self._ctx_items, self._ctx_default,
                auto_rules=getattr(self, "_auto_rules", None),
                auto_enabled=getattr(self, "_auto_enabled", None))
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._ctx_path)))

    # --- АВТО-ВИБІР ПРОФІЛЮ ЗА ВІКНОМ (feature/auto-profile) ---
    def _autoprofile_group(self):
        """Прапорець «Авто за вікном» + таблиця правил процес | заголовок →
        профіль. За замовчуванням прапорець увімкнено, лише якщо правила вже є."""
        frame, lay = _card(tr("auto_eyebrow"), "auto_hint", clickable=True,
                           title_key="auto_eyebrow")
        intro = QLabel(tr("auto_intro"))
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)

        self._auto_rules, self._auto_enabled = ctx_mod.load_auto_rules(self._ctx_path)
        self._auto_toggle = QCheckBox(tr("auto_toggle"))
        checked = self._auto_enabled if self._auto_enabled is not None \
            else bool(self._auto_rules)
        self._auto_toggle.setChecked(checked)
        self._auto_toggle.toggled.connect(self._auto_toggled)
        lay.addWidget(self._auto_toggle)

        self._auto_table = QTableWidget(0, 3)
        theme.setup_table(self._auto_table)
        self._auto_table.setHorizontalHeaderLabels(
            [tr("auto_col_process"), tr("auto_col_title"), tr("auto_col_profile")])
        self._auto_table.verticalHeader().setVisible(False)
        self._auto_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._auto_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._auto_table.setSelectionMode(QAbstractItemView.SingleSelection)
        hh = self._auto_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._auto_table.setMinimumHeight(120)
        lay.addWidget(self._auto_table)

        self._auto_empty = QLabel(tr("auto_empty"))
        self._auto_empty.setWordWrap(True)
        self._auto_empty.setProperty("muted", True)
        lay.addWidget(self._auto_empty)

        row = QHBoxLayout()
        add = GlassButton(tr("auto_add"))
        add.clicked.connect(self._auto_add)
        delete = QPushButton(tr("auto_delete"))
        delete.clicked.connect(self._auto_delete)
        row.addWidget(add)
        row.addWidget(delete)
        row.addStretch()
        lay.addLayout(row)

        self._auto_populate()
        return frame

    def _auto_populate(self):
        self._auto_table.setRowCount(0)
        for rule in self._auto_rules:
            r = self._auto_table.rowCount()
            self._auto_table.insertRow(r)
            self._auto_table.setItem(r, 0, QTableWidgetItem(rule.process))
            self._auto_table.setItem(r, 1, QTableWidgetItem(rule.title))
            self._auto_table.setItem(r, 2, QTableWidgetItem(rule.profile))
        has = bool(self._auto_rules)
        self._auto_table.setVisible(has)
        self._auto_empty.setVisible(not has)

    def _auto_save(self):
        """Записати правила (в тому ж файлі, що й профілі) і перебудувати матчер."""
        ctx_mod.save_profiles(
            self._ctx_path, self._ctx_items, self._ctx_default,
            auto_rules=self._auto_rules, auto_enabled=self._auto_enabled)
        self.controller.reload_context_profiles()
        self._auto_populate()

    def _auto_toggled(self, on: bool):
        self._auto_enabled = bool(on)
        self._auto_save()

    def _auto_add(self):
        dlg = AutoProfileRuleDialog(
            self.controller._ctx_resolver, self._ctx_dict_names(), self)
        if dlg.exec() and dlg.result_rule is not None:
            self._auto_rules.append(dlg.result_rule)
            if self._auto_enabled is None:
                self._auto_enabled = True          # перше правило → вмикаємо
                self._auto_toggle.setChecked(True)
            self._auto_save()

    def _auto_delete(self):
        r = self._auto_table.currentRow()
        if 0 <= r < len(self._auto_rules):
            del self._auto_rules[r]
            self._auto_save()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_del_model"):        # наявність моделей могла змінитись
            self._refresh_delete_button()      # feature/delete-model
        if hasattr(self, "_vault_state_lbl"):
            self._refresh_vault_ui()
        if hasattr(self, "_vmem_table"):
            self._refresh_voice_memory_list()

    # --- АВТОЗБЕРЕЖЕННЯ НОТАТОК (feature/auto-export) ---
    def _export_group(self, cfg):
        """Чекбокс «зберігати розшифровки в теку» + поле шляху з «Обрати…» +
        випадачка формату (md/txt). Поля неактивні, поки чекбокс знятий (патерн
        теки моделей/watch). Діє одразу (без перезапуску)."""
        frame, lay = _card(tr("set_export_eyebrow"))

        self._export_enabled = QCheckBox(tr("set_export_enable"))
        self._export_enabled.setChecked(bool(getattr(cfg, "auto_export_enabled", False)))
        self._export_enabled.toggled.connect(self._on_export_toggled)
        lay.addWidget(self._export_enabled)

        g = _grid()
        dirbox = QVBoxLayout()
        dirbox.setSpacing(8)
        self._export_dir_label = QLabel()
        self._export_dir_label.setWordWrap(True)
        self._set_export_dir_text(getattr(cfg, "auto_export_dir", None))
        self._export_pick = QPushButton(tr("common_change"))
        self._export_pick.clicked.connect(self._on_pick_export_dir)
        dirbox.addWidget(self._export_dir_label)
        pickrow = QHBoxLayout()
        pickrow.setSpacing(10)
        pickrow.addWidget(self._export_pick)
        pickrow.addStretch()
        dirbox.addLayout(pickrow)
        g.addWidget(_form_label(tr("set_export_folder")), 0, 0)
        g.addLayout(dirbox, 0, 1)

        self._export_format = QComboBox()
        self._export_format.addItem(tr("set_export_md"), "md")
        self._export_format.addItem(tr("set_export_txt"), "txt")
        idx = self._export_format.findData(getattr(cfg, "auto_export_format", "md"))
        self._export_format.setCurrentIndex(idx if idx >= 0 else 0)
        self._export_format.currentIndexChanged.connect(self._on_export_format)
        self._export_format.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_form_label(tr("set_export_format")), 1, 0)
        g.addWidget(self._export_format, 1, 1)

        export_hint = QLabel(tr("set_export_hint"))
        export_hint.setProperty("muted", True)
        export_hint.setWordWrap(True)
        g.addWidget(export_hint, 2, 1)
        lay.addLayout(g)
        self._sync_export_enabled()
        return frame

    def _set_export_dir_text(self, path):
        self._export_dir_label.setText(str(path) if path else tr("set_export_none"))

    def _sync_export_enabled(self):
        """Поля теки й формату активні лише коли автозбереження ввімкнене."""
        on = self._export_enabled.isChecked()
        self._export_dir_label.setEnabled(on)
        self._export_pick.setEnabled(on)
        self._export_format.setEnabled(on)

    def _on_export_toggled(self, on: bool):
        self.controller.set_auto_export_enabled(bool(on))
        self._sync_export_enabled()

    def _on_pick_export_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("set_export_folder"))
        if not path:
            return
        self.controller.set_auto_export_dir(path)
        self._set_export_dir_text(path)

    def _on_export_format(self, _index):
        self.controller.set_auto_export_format(self._export_format.currentData())

    # --- КАНАЛ OBSIDIAN (feature/obsidian-channel) ---
    def _obsidian_group(self, cfg):
        """Чекбокс «Надсилати до Obsidian» + поле папки сховища з «Обрати…» + поле
        шаблону імені файлу. Поля неактивні, поки чекбокс знятий (патерн
        автозбереження/стеження). Діє одразу (без перезапуску)."""
        frame, lay = _card(tr("set_obsidian_eyebrow"))

        self._obsidian_enabled = QCheckBox(tr("set_obsidian_enable"))
        self._obsidian_enabled.setChecked(bool(getattr(cfg, "obsidian_enabled", False)))
        self._obsidian_enabled.toggled.connect(self._on_obsidian_toggled)
        lay.addWidget(self._obsidian_enabled)

        g = _grid()
        dirbox = QVBoxLayout()
        dirbox.setSpacing(8)
        self._obsidian_dir_label = QLabel()
        self._obsidian_dir_label.setWordWrap(True)
        self._set_obsidian_dir_text(getattr(cfg, "obsidian_dir", None))
        self._obsidian_pick = QPushButton(tr("common_change"))
        self._obsidian_pick.clicked.connect(self._on_pick_obsidian_dir)
        dirbox.addWidget(self._obsidian_dir_label)
        pickrow = QHBoxLayout()
        pickrow.setSpacing(10)
        pickrow.addWidget(self._obsidian_pick)
        pickrow.addStretch()
        dirbox.addLayout(pickrow)
        g.addWidget(_form_label(tr("set_obsidian_folder")), 0, 0)
        g.addLayout(dirbox, 0, 1)

        self._obsidian_template = QLineEdit(
            getattr(cfg, "obsidian_filename_template", None) or "{дата}-{назва}")
        self._obsidian_template.setAccessibleName(tr("set_obsidian_name"))
        self._obsidian_template.setMaximumWidth(_CTRL_MAX)
        self._obsidian_template.editingFinished.connect(self._on_obsidian_template)
        g.addWidget(_form_label(tr("set_obsidian_name")), 1, 0)
        g.addWidget(self._obsidian_template, 1, 1)
        tmpl_hint = QLabel(tr("set_obsidian_name_hint"))
        tmpl_hint.setProperty("muted", True)
        tmpl_hint.setWordWrap(True)
        g.addWidget(tmpl_hint, 2, 1)

        obsidian_hint = QLabel(tr("set_obsidian_hint"))
        obsidian_hint.setProperty("muted", True)
        obsidian_hint.setWordWrap(True)
        g.addWidget(obsidian_hint, 3, 1)
        lay.addLayout(g)
        self._sync_obsidian_enabled()
        return frame

    def _set_obsidian_dir_text(self, path):
        self._obsidian_dir_label.setText(str(path) if path else tr("set_obsidian_none"))

    def _sync_obsidian_enabled(self):
        """Поля папки й шаблону активні лише коли канал увімкнено."""
        on = self._obsidian_enabled.isChecked()
        self._obsidian_dir_label.setEnabled(on)
        self._obsidian_pick.setEnabled(on)
        self._obsidian_template.setEnabled(on)

    def _on_obsidian_toggled(self, on: bool):
        self.controller.set_obsidian_enabled(bool(on))
        self._sync_obsidian_enabled()

    def _on_pick_obsidian_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("set_obsidian_folder"))
        if not path:
            return
        self.controller.set_obsidian_dir(path)
        self._set_obsidian_dir_text(path)

    def _on_obsidian_template(self):
        self.controller.set_obsidian_filename_template(self._obsidian_template.text())

    # --- СТЕЖЕННЯ ЗА ТЕКОЮ (feature/watch-folder) ---
    def _watch_group(self, cfg):
        """Чекбокс «стежити за текою» + поле шляху з «Обрати…» (патерн теки
        моделей). Поле неактивне, поки чекбокс знятий. Діє одразу (без перезапуску)."""
        frame, lay = _card(tr("set_watch_eyebrow"))

        self._watch_enabled = QCheckBox(tr("set_watch_enable"))
        self._watch_enabled.setChecked(bool(getattr(cfg, "watch_enabled", False)))
        self._watch_enabled.toggled.connect(self._on_watch_toggled)
        lay.addWidget(self._watch_enabled)

        g = _grid()
        dirbox = QVBoxLayout()
        dirbox.setSpacing(8)
        self._watch_dir_label = QLabel()
        self._watch_dir_label.setWordWrap(True)
        self._set_watch_dir_text(getattr(cfg, "watch_dir", None))
        self._watch_pick = QPushButton(tr("common_change"))
        self._watch_pick.clicked.connect(self._on_pick_watch_dir)
        dirbox.addWidget(self._watch_dir_label)
        pickrow = QHBoxLayout()
        pickrow.setSpacing(10)
        pickrow.addWidget(self._watch_pick)
        pickrow.addStretch()
        dirbox.addLayout(pickrow)
        g.addWidget(_form_label(tr("set_watch_folder")), 0, 0)
        g.addLayout(dirbox, 0, 1)
        watch_hint = QLabel(tr("set_watch_hint"))
        watch_hint.setProperty("muted", True)
        watch_hint.setWordWrap(True)
        g.addWidget(watch_hint, 1, 1)
        lay.addLayout(g)
        self._sync_watch_enabled()
        return frame

    def _set_watch_dir_text(self, path):
        self._watch_dir_label.setText(str(path) if path else tr("set_watch_none"))

    def _sync_watch_enabled(self):
        """Поле теки активне лише коли стеження увімкнене (як тека моделей)."""
        on = self._watch_enabled.isChecked()
        self._watch_dir_label.setEnabled(on)
        self._watch_pick.setEnabled(on)

    def _on_watch_toggled(self, on: bool):
        self.controller.set_watch_enabled(bool(on))
        self._sync_watch_enabled()

    def _on_pick_watch_dir(self):
        path = QFileDialog.getExistingDirectory(self, tr("set_watch_folder"))
        if not path:
            return
        self.controller.set_watch_dir(path)
        self._set_watch_dir_text(path)

    # --- РЕЗЕРВНА КОПІЯ НАЛАШТУВАНЬ (feature/ux-center) ---
    def _corpus_group(self):
        """Збирач корпусу точності (feature/accuracy-corpus): лічильник зібраних
        зразків + підказка + кнопка «Відкрити папку корпусу». Наповнюється дією
        «Розпізнано погано…» на картках Диктування, Аудіофайлів та Історії."""
        frame, lay = _card(tr("set_corpus_eyebrow"))
        hint = QLabel(tr("set_corpus_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)

        try:
            n = self.controller.corpus_count()
        except Exception:
            n = 0
        self._corpus_count_lbl = QLabel(tr("set_corpus_count", n=n))
        lay.addWidget(self._corpus_count_lbl)

        row = QHBoxLayout()
        row.setSpacing(10)
        open_btn = GlassButton(tr("set_corpus_open"))
        open_btn.clicked.connect(self.controller.open_corpus_folder)
        row.addWidget(open_btn)
        row.addStretch()
        lay.addLayout(row)
        return frame

    def _backup_group(self):
        """Експорт/імпорт усіх налаштувань одним .zip. Експорт — вибір файлу й
        запис; імпорт — підтвердження, автобекап поточного стану, розпакування
        і пропозиція перезапуску."""
        frame, lay = _card(tr("set_backup_eyebrow"))
        hint = QLabel(tr("set_backup_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(10)
        export = GlassButton(tr("set_backup_export"))
        export.setAccessibleName("btn_export_profile")
        export.clicked.connect(self._on_backup_export)
        imp = QPushButton(tr("set_backup_import"))
        imp.setAccessibleName("btn_import_profile")
        imp.clicked.connect(self._on_backup_import)
        row.addWidget(export)
        row.addWidget(imp)
        row.addStretch()
        lay.addLayout(row)

        self._backup_note = _note("")
        lay.addWidget(self._backup_note)
        return frame

    def _on_backup_export(self, _checked=False):
        default_name = "balachky-profile-" + time.strftime("%Y%m%d") + ".zip"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("set_backup_export_title"), default_name,
            tr("set_backup_filter"))
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            self.controller.export_settings_to(path)
        except OSError:
            logging.exception("Експорт профілю не вдався")
            QMessageBox.warning(self, tr("set_backup_eyebrow"),
                                tr("set_backup_failed"))
            return
        self._backup_note.setText(tr("set_backup_done", path=path))
        motion.slide_fade_in(self._backup_note)

    def _on_backup_import(self, _checked=False):
        from whisper_core.settings_io import SettingsArchiveError
        path, _ = QFileDialog.getOpenFileName(
            self, tr("set_backup_import_title"), "", tr("set_backup_filter"))
        if not path:
            return

        try:
            info = self.controller.inspect_profile_archive(path)
        except (SettingsArchiveError, Exception):
            QMessageBox.warning(self, tr("set_backup_import_title"),
                                tr("set_backup_invalid"))
            return

        today_str = time.strftime("%Y-%m-%d")
        backup_preview_dir = f"backup-{today_str}"

        dlg = ProfileImportConfirmDialog(self, info, backup_preview_dir)
        if dlg.exec() != QDialog.Accepted:
            return

        try:
            backup_dir = self.controller.import_profile_with_backup(path)
        except SettingsArchiveError:
            QMessageBox.warning(self, tr("set_backup_import_title"),
                                tr("set_backup_invalid"))
            return
        except Exception:
            logging.exception("Імпорт профілю не вдався")
            QMessageBox.warning(self, tr("set_backup_import_title"),
                                tr("set_backup_failed"))
            return

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(tr("set_backup_success_title"))
        msg_box.setText(tr("set_backup_success_body", backup_dir=str(backup_dir)))
        msg_box.setIcon(QMessageBox.Information)
        restart_btn = msg_box.addButton(tr("set_backup_restart_now"), QMessageBox.AcceptRole)
        restart_btn.setAccessibleName("btn_restart_now")
        close_btn = msg_box.addButton(tr("set_backup_restart_later"), QMessageBox.RejectRole)
        close_btn.setAccessibleName("btn_restart_later")
        msg_box.exec()
        if msg_box.clickedButton() == restart_btn:
            self.controller.restart_app()

    # --- СИСТЕМА ---
    def _system_group(self, cfg):
        frame, lay = _card(tr("set_system_eyebrow"))

        self._autostart = QCheckBox(tr("set_autostart"))
        lay.addWidget(self._subhead(tr("set_system_start_appearance")))
        self._autostart.setChecked(is_enabled())
        self._autostart.toggled.connect(self._on_autostart)
        lay.addWidget(self._autostart)

        # Повторний онбординг: реєстр «onboarded» лишається=1 після першого разу,
        # тож майстер сам не з'явиться — кнопка дає надійно пройти його ще раз.
        rerun_row = QHBoxLayout()
        rerun_row.setContentsMargins(0, 0, 0, 0)
        self._rerun_onb = QPushButton(tr("set_rerun_onboarding"))
        self._rerun_onb.setAccessibleName(tr("set_rerun_onboarding"))
        self._rerun_onb.clicked.connect(self._rerun_onboarding)
        rerun_row.addWidget(self._rerun_onb)
        rerun_row.addStretch()
        lay.addLayout(rerun_row)
        rerun_hint = QLabel(tr("set_rerun_onboarding_hint"))
        rerun_hint.setProperty("muted", True)
        rerun_hint.setWordWrap(True)
        lay.addWidget(rerun_hint)

        anims = QCheckBox(tr("set_animations"))
        anims.setChecked(bool(cfg.animations))
        anims.toggled.connect(self._on_animations)
        lay.addWidget(anims)

        mica = QCheckBox(tr("set_mica"))
        mica.setChecked(getattr(cfg, "backdrop", "auto") == "auto")
        mica.toggled.connect(self._on_mica)
        # feature/text-rewrite: ⓘ поруч — пояснює, що Mica не впливає на запис
        mica_row = QHBoxLayout()
        mica_row.setContentsMargins(0, 0, 0, 0)
        mica_row.setSpacing(6)
        mica_row.addWidget(mica)
        mica_row.addWidget(info_hint("hint_mica"))
        mica_row.addStretch()
        lay.addLayout(mica_row)

        # --- Тло робочої зони (Т76) ---
        lay.addSpacing(6)
        bg_lbl = QLabel(tr("set_workspace_bg_title"))
        bg_lbl.setProperty("subhead", True)
        bg_lbl.setWordWrap(True)
        lay.addWidget(bg_lbl)

        bg_row = QHBoxLayout()
        bg_row.setContentsMargins(0, 0, 0, 0)
        bg_row.setSpacing(10)

        self._bg_choice = QComboBox()
        self._bg_choice.setAccessibleName(tr("set_workspace_bg_title"))
        self._bg_choice.addItem(tr("set_workspace_bg_mascot"), "mascot")
        self._bg_choice.addItem(tr("set_workspace_bg_solid"), "solid")
        self._bg_choice.addItem(tr("set_workspace_bg_custom"), "custom")

        current_bg = getattr(cfg, "workspace_bg", "mascot")
        idx = self._bg_choice.findData(current_bg)
        if idx >= 0:
            self._bg_choice.setCurrentIndex(idx)
        bg_row.addWidget(self._bg_choice)

        self._bg_choose_btn = QPushButton(tr("set_workspace_bg_choose_file"))
        self._bg_choose_btn.setAccessibleName(tr("set_workspace_bg_choose_file"))
        self._bg_choose_btn.clicked.connect(self._on_bg_choose_file)
        bg_row.addWidget(self._bg_choose_btn)
        bg_row.addStretch()
        lay.addLayout(bg_row)

        self._bg_hint = QLabel(tr("set_workspace_bg_custom_hint"))
        self._bg_hint.setProperty("muted", True)
        self._bg_hint.setWordWrap(True)
        lay.addWidget(self._bg_hint)

        self._update_bg_controls_visibility(current_bg == "custom")
        self._bg_choice.currentIndexChanged.connect(self._on_bg_choice_changed)

        # --- Колір інтерфейсу (feature/ui-color-picker) ---
        lay.addSpacing(6)
        color_lbl = QLabel(tr("set_ui_color_title"))
        color_lbl.setProperty("subhead", True)
        color_lbl.setWordWrap(True)
        lay.addWidget(color_lbl)

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(10)

        self._ui_color_choice = QComboBox()
        self._ui_color_choice.setAccessibleName(tr("set_ui_color_title"))
        self._ui_color_choice.setToolTip(tr("set_ui_color_title"))

        _PRESET_KEYS = ["classic", "red", "amber", "green", "teal", "blue", "purple", "pink"]
        for key in _PRESET_KEYS:
            pal = theme.palette_for(key)
            gold_hex = pal.get("GOLD", "#F39200")
            self._ui_color_choice.addItem(_color_swatch_icon(gold_hex), tr(f"set_ui_color_{key}"), key)

        self._ui_color_choice.addItem(_color_swatch_icon("#888888"), tr("set_ui_color_custom"), "custom")

        current_ui_color = theme.resolve_ui_color(cfg)
        self._current_ui_color = current_ui_color
        self._sync_ui_color_choice_selection(current_ui_color)

        color_row.addWidget(self._ui_color_choice)
        color_row.addStretch()
        lay.addLayout(color_row)

        self._ui_color_hint = QLabel(tr("set_ui_color_hint"))
        self._ui_color_hint.setProperty("muted", True)
        self._ui_color_hint.setWordWrap(True)
        lay.addWidget(self._ui_color_hint)

        self._ui_color_choice.currentIndexChanged.connect(self._on_ui_color_choice_changed)

        # --- Режим тестування (детальний журнал для живих тестів) ---
        lay.addWidget(self._subhead(tr("set_test_mode_eyebrow")))
        self._test_mode = QCheckBox(tr("set_test_mode"))
        self._test_mode.setChecked(bool(getattr(cfg, "test_mode", False)))
        self._test_mode.toggled.connect(self._on_test_mode)
        lay.addWidget(self._test_mode)
        self._test_include_text = QCheckBox(tr("set_test_include_text"))
        self._test_include_text.setChecked(bool(getattr(cfg, "test_mode_include_text", False)))
        self._test_include_text.setEnabled(self._test_mode.isChecked())
        self._test_include_text.toggled.connect(self._on_test_include_text)
        # відступ праворуч — підпорядкований другий чекбокс усередині режиму
        incl_row = QHBoxLayout()
        incl_row.setContentsMargins(22, 0, 0, 0)
        incl_row.addWidget(self._test_include_text)
        incl_row.addStretch()
        lay.addLayout(incl_row)
        test_hint = QLabel(tr("set_test_mode_hint"))
        test_hint.setProperty("muted", True)
        test_hint.setWordWrap(True)
        lay.addWidget(test_hint)

        # --- Приватність / Офлайн: доказова офлайновість (мілітарі-довіра) ---
        # Не декларація, а перевірюваний факт: бейдж + журнал вихідних з'єднань
        # (у нормі 0, крім завантажень, які користувач запускає сам) + довідка
        # «як звірити самому» всередині діалогу журналу.
        lay.addWidget(self._subhead(tr("set_offline_eyebrow")))
        offline_badge = QLabel(tr("set_offline_badge"))
        offline_badge.setProperty("badge", "done")
        badge_row = QHBoxLayout()
        badge_row.setContentsMargins(0, 0, 0, 0)
        badge_row.addWidget(offline_badge)
        badge_row.addStretch()
        lay.addLayout(badge_row)
        offline_text = QLabel(tr("set_offline_text"))
        offline_text.setProperty("muted", True)
        offline_text.setWordWrap(True)
        lay.addWidget(offline_text)
        net_row = QHBoxLayout()
        net_row.setContentsMargins(0, 0, 0, 0)
        net_log_btn = QPushButton(tr("set_offline_show_log"))
        net_log_btn.setAccessibleName(tr("set_offline_show_log"))
        net_log_btn.clicked.connect(self._show_network_log)
        net_row.addWidget(net_log_btn)
        net_row.addStretch()
        lay.addLayout(net_row)

        g = _grid()
        # мова інтерфейсу: тепер увімкнено (UK/EN); застосовується після перезапуску
        self._ui_lang = QComboBox()
        self._ui_lang.addItem(tr("common_ukrainian"), "uk")
        self._ui_lang.addItem(tr("common_english"), "en")
        idx = self._ui_lang.findData(getattr(cfg, "ui_language", "uk"))
        self._ui_lang.setCurrentIndex(idx if idx >= 0 else 0)
        self._ui_lang.currentIndexChanged.connect(self._on_ui_lang)
        self._ui_lang.setMaximumWidth(_CTRL_MAX)
        g.addWidget(_form_label(tr("set_ui_lang")), 0, 0)
        g.addWidget(self._ui_lang, 0, 1)
        self._ui_lang_note = _note(tr("set_restart"))
        g.addWidget(self._ui_lang_note, 1, 1)

        # Раніше 4 контролі тіснились в один ряд без проміжків — «Відкрити папку
        # логів» упиралась у підпис «Рівень логування». Розбито на два рядки
        # (кнопки-дії / вибір рівня) зі spacing 10px, щоб нічого не злипалось.
        logs_box = QVBoxLayout()
        logs_box.setSpacing(10)
        logs_actions = QHBoxLayout()
        logs_actions.setSpacing(10)
        logs_btn = QPushButton(tr("set_open_logs"))
        logs_btn.setAccessibleName(tr("set_open_logs"))
        logs_btn.clicked.connect(lambda _=False: open_log_dir())
        logs_actions.addWidget(logs_btn)
        copy_diag = QPushButton(tr("set_copy_diagnostics"))
        copy_diag.setAccessibleName(tr("set_copy_diagnostics"))
        copy_diag.clicked.connect(self._copy_diagnostics)
        logs_actions.addWidget(copy_diag)
        logs_actions.addStretch()
        logs_box.addLayout(logs_actions)
        # «Повідомити про проблему» — окремим рядом: утрьох кнопки просили 491
        # точку при 386 доступних у колонці на мінімумі вікна (1000), і Qt різав
        # усі три підписи («Відкрити папку логів» 142/101, «Скопіювати
        # діагностику» 164/122, «Повідомити про проблему» 185/143). Удвох перший
        # ряд просить 316 — вміщається; підписи лишаються повними.
        report_row = QHBoxLayout()
        report_row.setSpacing(10)
        report_btn = QPushButton(tr("set_report_problem"))
        report_btn.setAccessibleName(tr("set_report_problem"))
        report_btn.clicked.connect(self._report_problem)
        report_row.addWidget(report_btn)
        report_row.addStretch()
        logs_box.addLayout(report_row)
        level_row = QHBoxLayout()
        level_row.setSpacing(10)
        self._log_level = QComboBox()
        for level in ("INFO", "DEBUG", "WARNING"):
            self._log_level.addItem(level, level)
        idx = self._log_level.findData(getattr(cfg, "log_level", "INFO"))
        self._log_level.setCurrentIndex(idx if idx >= 0 else 0)
        self._log_level.currentIndexChanged.connect(self._on_log_level)
        level_row.addWidget(QLabel(tr("set_log_level")))
        level_row.addWidget(self._log_level)
        level_row.addStretch()
        logs_box.addLayout(level_row)
        g.addWidget(self._subhead(tr("set_system_diagnostics")), 2, 0, 1, 2)
        g.addWidget(_form_label(tr("set_diagnostics")), 3, 0, Qt.AlignTop)
        g.addLayout(logs_box, 3, 1)
        logs_hint = QLabel(tr("set_logs_hint"))
        logs_hint.setProperty("muted", True)
        logs_hint.setWordWrap(True)
        g.addWidget(logs_hint, 4, 1)

        # --- оновлення (GitHub Releases; завантаження — лише за дією/opt-in) ---
        self._upd_status = QLabel()
        self._upd_status.setWordWrap(True)
        self._upd_url = None            # сторінка релізу (fallback у браузер)
        self._upd_installer_url = None  # прямий asset-інсталятор
        self._upd_sha = None            # очікуваний SHA-256
        self._upd_notes = None          # «що нового»
        self._upd_ready_path = None     # шлях до вже завантаженого інсталятора
        updrow = QHBoxLayout()
        updrow.setSpacing(10)
        # feature/auto-update: завантажити інсталятор у теку оновлень (з прогресом)
        self._upd_get = GlassButton(tr("set_upd_download"))
        self._upd_get.clicked.connect(self._on_get_update)
        self._upd_get.hide()
        # запустити завантажений інсталятор і коректно вийти
        self._upd_install = GlassButton(tr("set_upd_install_now"))
        self._upd_install.clicked.connect(self._on_install_update)
        self._upd_install.hide()
        # показати release notes текстом
        self._upd_notes_btn = GlassButton(tr("set_upd_whatsnew"))
        self._upd_notes_btn.clicked.connect(self._on_whats_new)
        self._upd_notes_btn.hide()
        # відкриває сторінку релізу в браузері (резерв, коли asset недоступний)
        self._upd_download = GlassButton(tr("set_open_download"))
        self._upd_download.clicked.connect(self._on_download_update)
        self._upd_download.hide()
        upd_check = GlassButton(tr("set_check_now"))
        upd_check.clicked.connect(self.controller.check_updates_now)
        updrow.addWidget(self._upd_get)
        updrow.addWidget(self._upd_install)
        updrow.addWidget(self._upd_notes_btn)
        updrow.addWidget(self._upd_download)
        updrow.addWidget(upd_check)
        updrow.addStretch()
        g.addWidget(self._subhead(tr("set_system_updates")), 5, 0, 1, 2)
        g.addWidget(_labeled_hint(tr("set_updates"), "hint_updates"), 6, 0)
        g.addWidget(self._upd_status, 6, 1)
        g.addLayout(updrow, 7, 1)
        upd_hint = QLabel(tr("set_upd_hint"))
        upd_hint.setProperty("muted", True)
        upd_hint.setWordWrap(True)
        upd_hint.setOpenExternalLinks(True)
        # нічний режим: inline %%ACCENT%% у лінку «Releases» — re-run tr() на зміні теми
        theme.register_restyle_call(upd_hint, lambda w: w.setText(tr("set_upd_hint")))
        g.addWidget(upd_hint, 8, 1)
        # opt-in — за замовчуванням вимкнено (застосунок офлайн з коробки); тут же,
        # поруч зі своїм предметом (не вгорі секції)
        self._auto_upd = QCheckBox(tr("set_auto_upd"))
        self._auto_upd.setChecked(bool(getattr(cfg, "check_updates", False)))
        self._auto_upd.toggled.connect(self._on_auto_updates)
        g.addWidget(self._auto_upd, 9, 1)
        # feature/auto-update: тихе автозавантаження — окремий opt-in, теж ВИМК
        # за замовчуванням (гігабайти без згоди не качаємо)
        self._auto_download = QCheckBox(tr("set_auto_download"))
        self._auto_download.setChecked(bool(getattr(cfg, "auto_download_updates", False)))
        self._auto_download.toggled.connect(self._on_auto_download)
        g.addWidget(self._auto_download, 10, 1)

        # --- довідка: коротка інструкція у браузері (нетех-користувачу) ---
        help_row = QHBoxLayout()
        help_row.setSpacing(10)
        help_btn = GlassButton(tr("set_help"))
        help_btn.clicked.connect(self._on_open_help)
        help_row.addWidget(help_btn)
        help_row.addStretch()
        g.addWidget(_form_label(tr("set_help_label")), 11, 0)
        g.addLayout(help_row, 11, 1)
        help_hint = QLabel(tr("set_help_hint"))
        help_hint.setProperty("muted", True)
        help_hint.setWordWrap(True)
        g.addWidget(help_hint, 12, 1)

        lay.addLayout(g)
        self._refresh_update_row()
        self.controller.update_result.connect(self._on_update_result)
        # feature/auto-update: прогрес/готовність/помилка завантаження
        self.controller.download_progress.connect(self._on_download_progress)
        self.controller.update_downloaded.connect(self._on_downloaded)
        self.controller.download_failed.connect(self._on_download_failed)
        return frame

    def _protection_group(self, cfg):
        from whisper_core.win_hardening import is_display_affinity_supported
        frame, lay = _card(tr("set_protection_eyebrow"))

        supported = is_display_affinity_supported()
        self._screen_protection = QCheckBox(tr("set_screen_protection"))
        self._screen_protection.setAccessibleName(tr("set_screen_protection"))
        self._screen_protection.setChecked(bool(getattr(cfg, "screen_protection", False)) and supported)
        self._screen_protection.setEnabled(supported)
        self._screen_protection.toggled.connect(self._on_screen_protection)

        sp_row = QHBoxLayout()
        sp_row.setContentsMargins(0, 0, 0, 0)
        sp_row.setSpacing(6)
        sp_row.addWidget(self._screen_protection)
        sp_row.addWidget(info_hint("hint_screen_protection"))
        sp_row.addStretch()
        lay.addLayout(sp_row)

        if not supported:
            unsupp_lbl = QLabel(tr("set_screen_protection_unsupported"))
            unsupp_lbl.setProperty("muted", True)
            unsupp_lbl.setWordWrap(True)
            lay.addWidget(unsupp_lbl)

        g = _grid()
        combo = getattr(cfg, "panic_lock_hotkey", "") or ""
        keyrow = QHBoxLayout()
        keyrow.setSpacing(12)
        self._panic_key_label = QLabel(pretty(combo) if combo else tr("set_note_none"))
        self._panic_key_label.setProperty("kbd", True)
        change = QPushButton(tr("common_change"))
        change.setAccessibleName(tr("set_panic_hotkey"))
        change.clicked.connect(self.controller.start_panic_key_capture)
        self._panic_clear_btn = QPushButton(tr("note_clear"))
        # a11y: РІЗНІ accessibleName для «Змінити»/«Очистити» (як у command_edit) —
        # інакше скрінрідер називає обидві кнопки однаково «set_panic_hotkey».
        self._panic_clear_btn.setAccessibleName(
            f"{tr('note_clear')} — {tr('set_panic_hotkey')}")
        self._panic_clear_btn.setProperty("ghost", True)
        self._panic_clear_btn.clicked.connect(self.controller.clear_panic_lock_hotkey)
        self._panic_clear_btn.setEnabled(bool(combo))
        keyrow.addWidget(self._panic_key_label)
        keyrow.addWidget(change)
        keyrow.addWidget(self._panic_clear_btn)
        keyrow.addStretch()
        g.addWidget(_labeled_hint(tr("set_panic_hotkey"), "hint_panic_hotkey"), 0, 0)
        g.addLayout(keyrow, 0, 1)

        panic_hint = QLabel(tr("set_panic_hotkey_hint"))
        panic_hint.setProperty("muted", True)
        panic_hint.setWordWrap(True)
        g.addWidget(panic_hint, 1, 1)
        lay.addLayout(g)

        self.controller.panic_lock_key_captured.connect(self._on_panic_key)
        return frame

    def _on_screen_protection(self, checked: bool):
        self.controller.set_screen_protection(checked)

    def _on_panic_key(self, combo: str):
        self._panic_key_label.setText(pretty(combo) if combo else tr("set_note_none"))
        self._panic_clear_btn.setEnabled(bool(combo))

    # --- оновлення: стан і дії ---
    def _hide_update_buttons(self):
        for b in (self._upd_get, self._upd_install, self._upd_notes_btn,
                  self._upd_download):
            b.hide()

    def _show_available_buttons(self):
        """Кнопки для стану «є нова версія»: Завантажити / Оновити зараз /
        Що нового / Сторінка релізу — залежно від наявних даних доставки."""
        from whisper_core import updater
        self._upd_download.setVisible(bool(self._upd_url))
        self._upd_notes_btn.setVisible(bool(self._upd_notes))
        installable = bool(self._upd_installer_url and self._upd_sha)
        ready = installable and updater.installer_ready(self._upd_installer_url)
        self._upd_ready_path = str(ready) if ready else None
        self._upd_install.setVisible(bool(ready))
        self._upd_get.setVisible(installable and not ready)

    def _refresh_update_row(self):
        """Намалювати збережений стан оновлень (без мережі)."""
        current, latest, url, checked = self.controller.update_state()
        self._upd_url = url
        self._upd_installer_url, self._upd_sha, self._upd_notes = \
            self.controller.delivery_state()
        if latest:
            self._set_update_text(tr("set_upd_available", ver=latest), gold=True)
            self._show_available_buttons()
        elif checked:
            self._set_update_text(tr("set_upd_current", ver=current), gold=False)
            self._hide_update_buttons()
        else:
            self._set_update_text(tr("set_upd_never", ver=current), gold=False)
            self._hide_update_buttons()

    def _on_update_result(self, res):
        """Живий результат фонової/ручної перевірки → оновити рядок."""
        if res.status == updates.OFFLINE:
            # не стираємо корисний відомий стан («є нова версія» + кнопки):
            # якщо є що показати — відновлюємо збережене; лише коли нема
            # відомого оновлення, кажемо нейтральне «Не вдалося перевірити»
            _current, latest, _url, _checked = self.controller.update_state()
            if latest:
                self._refresh_update_row()
            else:
                self._set_update_text(tr("set_upd_failed"), gold=False)
                self._hide_update_buttons()
        else:  # UPDATE_AVAILABLE / UP_TO_DATE / NOT_MODIFIED → перемалювати зі стану
            self._refresh_update_row()

    def _set_update_text(self, text: str, gold: bool):
        self._upd_status.setText(text)
        self._upd_status.setProperty("gold", gold)
        # динамічна властивість → примусова переполіровка стилю
        self._upd_status.style().unpolish(self._upd_status)
        self._upd_status.style().polish(self._upd_status)

    def _on_get_update(self):
        """«Завантажити»: старт фонового завантаження інсталятора з прогресом."""
        self._upd_get.setEnabled(False)
        self._set_update_text(tr("set_upd_downloading", pct=0), gold=True)
        self.controller.start_installer_download(
            self._upd_installer_url, self._upd_sha)

    def _on_download_progress(self, done: int, total: int):
        if not self._upd_get.isVisible() and self._upd_ready_path:
            return
        pct = int(done * 100 / total) if total and total > 0 else 0
        self._set_update_text(tr("set_upd_downloading", pct=pct), gold=True)

    def _on_downloaded(self, path: str):
        """Інсталятор завантажено й перевірено → показати «Оновити зараз»."""
        self._upd_ready_path = path
        self._upd_get.setEnabled(True)
        self._upd_get.hide()
        self._upd_install.show()
        self._set_update_text(tr("set_upd_ready"), gold=True)

    def _on_download_failed(self, _msg: str):
        self._upd_get.setEnabled(True)
        self._set_update_text(tr("set_upd_dl_failed"), gold=False)
        # лишаємо кнопку «Завантажити» видимою — можна повторити (докачає .part)
        self._upd_get.show()
        self._upd_install.hide()

    def _on_install_update(self):
        """«Оновити зараз»: запустити завантажений інсталятор і вийти."""
        if self._upd_ready_path:
            self.controller.launch_installer_and_quit(self._upd_ready_path)

    def _show_network_log(self, _checked=False):
        """Відкрити журнал мережевої активності (доказова офлайновість)."""
        NetworkLogDialog(self).exec()

    def _on_whats_new(self):
        """Показати release notes звичайним текстом."""
        if not self._upd_notes:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.NoIcon)
        box.setWindowTitle(tr("set_upd_whatsnew"))
        box.setText(self._upd_notes)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()

    def _on_download_update(self):
        if self._upd_url:
            QDesktopServices.openUrl(QUrl(self._upd_url))

    def _on_auto_updates(self, on: bool):
        self.controller.cfg.check_updates = bool(on)
        self.controller.save_config()

    def _on_auto_download(self, on: bool):
        self.controller.cfg.auto_download_updates = bool(on)
        self.controller.save_config()

    def _on_autostart(self, on: bool):
        try:
            enable() if on else disable()
        except Exception:
            logging.exception("Не вдалося змінити автозапуск")
            QMessageBox.warning(
                self, tr("set_autostart_title"),
                tr("set_autostart_fail"))
            self._autostart.blockSignals(True)
            self._autostart.setChecked(not on)
            self._autostart.blockSignals(False)

    def _rerun_onboarding(self, _checked=False):
        """Знову показати майстер першого запуску (кнопка «пройти ще раз»).

        Ключ «onboarded» уже=1 після першого проходу, тож майстер сам не
        з'явиться — ця кнопка гарантує повторний прохід. НЕ чіпаємо ключ до
        показу: якщо користувач скасує майстер, «onboarded» лишається=1 і
        застосунок стартує нормально (інакше один клік «Скасувати» ламав би
        запуск). Поля майстра передзаповнюємо з поточного cfg, щоб скасування
        чи випадковий «Далі» не перезаписали модель/теку на дефолти.
        """
        cfg = self.controller.cfg
        from ..onboarding import FirstRunWizard
        wizard = FirstRunWizard(
            self,
            model_name=cfg.model_name,
            model_dir=cfg.model_dir,
            language=cfg.ui_language or cfg.language,
            ptt_key=cfg.ptt_key,
        )
        if not wizard.exec():                   # скасував — нічого не міняємо
            return
        cfg.model_name = wizard.model_name
        cfg.model_dir = wizard.model_dir
        cfg.language = wizard.language
        cfg.ui_language = wizard.language
        cfg.ptt_key = wizard.ptt_key
        if getattr(wizard, "use_gpu", False):
            cfg.device = "cuda"
            cfg.compute_type = "int8_float16"
        self.controller.save_config()
        self._mark_restart_pending()

    def _on_open_help(self, _checked=False):
        """Відкрити коротку інструкцію: локальний README зі збірки (за мовою
        інтерфейсу), а якщо його немає поруч — сторінку репозиторію в браузері.
        Спільна логіка з пунктом трею «Довідка» (fronts.desktop.help)."""
        from ..help import open_user_guide
        open_user_guide(self)

    def _on_log_level(self, _index):
        self.controller.set_log_level(self._log_level.currentData())

    def _on_test_mode(self, on: bool):
        """Увімкнути/вимкнути режим тестування. Другий чекбокс (тексти) активний
        лише в межах режиму; при вимиканні його теж скидаємо у стан «вимк.»."""
        self._test_include_text.setEnabled(on)
        include = on and self._test_include_text.isChecked()
        self.controller.set_test_mode(on, include)

    def _on_test_include_text(self, on: bool):
        """Тексти розшифровок у журнал — з попередженням про приватність при
        вмиканні. Відмова у діалозі знімає галочку назад."""
        if on:
            confirmed = QMessageBox.warning(
                self, tr("set_test_include_text_title"),
                tr("set_test_include_text_warn"),
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if confirmed != QMessageBox.Ok:
                self._test_include_text.blockSignals(True)
                self._test_include_text.setChecked(False)
                self._test_include_text.blockSignals(False)
                return
        self.controller.set_test_mode(self._test_mode.isChecked(), on)

    def _copy_diagnostics(self):
        QApplication.clipboard().setText(copy_diagnostics(self.controller.cfg))

    def _report_problem(self, toast_target=None):
        """Зібрати zip-звіт на Робочому столі й показати toast зі шляхом.
        toast_target — видимий віджет для toast (напр. головне вікно, коли звіт
        викликано з хабу «Про програму», а сторінка Налаштувань прихована)."""
        from ..report import build_report_zip
        from ..crash import LOG_DIR
        target = toast_target if toast_target is not None else self
        desktop = Path(os.environ.get("USERPROFILE") or Path.home()) / "Desktop"
        dpi = None
        try:
            scr = self.screen()
            if scr is not None:
                dpi = round(scr.logicalDotsPerInch())
        except Exception:
            dpi = None
        try:
            zip_path = build_report_zip(
                desktop, app_version=__version__, cfg=self.controller.cfg,
                log_dir=LOG_DIR, extra_info={"DPI": dpi} if dpi else None)
        except Exception as e:
            logging.error("Звіт про проблему не створено: %s", e)
            motion.toast(target, tr("set_report_fail"))
            return
        motion.toast(target, tr("set_report_done", path=str(zip_path)))

    def _on_ui_lang(self, _index):
        """Мова інтерфейсу: зберегти вибір; застосується після перезапуску."""
        self.controller.set_ui_language(self._ui_lang.currentData())
        motion.slide_fade_in(self._ui_lang_note)   # ТЗ п.9
        self._mark_restart_pending()

    # --- feature/qol-pack: звук вставки + «тихі години» ---
    @staticmethod
    def _parse_time(value) -> QTime:
        """«HH:MM» → QTime; невалідне → 00:00 (безпечний дефолт для поля)."""
        t = QTime.fromString((value or "").strip(), "HH:mm")
        return t if t.isValid() else QTime(0, 0)

    def _on_paste_sound(self, on: bool):
        self.controller.set_paste_confirm_sound(bool(on))

    def _on_backstep(self):
        self.controller.set_player_backstep(float(self._backstep.currentData()))

    def _sync_quiet_enabled(self):
        on = self._quiet_enabled.isChecked()
        self._quiet_from.setEnabled(on)
        self._quiet_to.setEnabled(on)

    def _push_quiet_hours(self):
        self.controller.set_quiet_hours(
            self._quiet_enabled.isChecked(),
            self._quiet_from.time().toString("HH:mm"),
            self._quiet_to.time().toString("HH:mm"))

    def _on_quiet_toggled(self, on: bool):
        self._sync_quiet_enabled()
        self._push_quiet_hours()

    def _on_quiet_time(self, _t=None):
        self._push_quiet_hours()

    def _on_animations(self, on: bool):
        self.controller.cfg.animations = bool(on)   # діє одразу (motion читає cfg)
        sync_status_animations()
        self.controller.tray.sync_animations()
        pill = getattr(self.controller, "pill", None)
        if pill is not None:                       # feature/status-tags-soul
            pill.sync_animations()
        window = getattr(self.controller, "window", None)
        if window is not None:
            window.dictation.sync_animations()
            if getattr(window, "meeting", None) is not None:   # feature/meeting-ui
                window.meeting.sync_animations()
        self.controller.save_config()

    def _on_mica(self, on: bool):
        """Mica застосовується лише при створенні вікна (showEvent) — як і модель
        чи мову інтерфейсу, зміну видно після перезапуску."""
        self.controller.cfg.backdrop = "auto" if on else "off"
        self.controller.save_config()
        self._mark_restart_pending()

    def _sync_ui_color_choice_selection(self, color_val):
        """Синхронізувати обраний пункт комбобокса вибору кольору без викликання сигналів."""
        if not hasattr(self, "_ui_color_choice"):
            return
        self._ui_color_choice.blockSignals(True)
        if isinstance(color_val, str) and color_val in ("classic", "red", "amber", "green", "teal", "blue", "purple", "pink"):
            idx = self._ui_color_choice.findData(color_val)
            if idx >= 0:
                self._ui_color_choice.setCurrentIndex(idx)
        else:
            idx = self._ui_color_choice.findData("custom")
            if idx >= 0:
                self._ui_color_choice.setCurrentIndex(idx)
                try:
                    pal = theme.palette_for(color_val)
                    gold_hex = pal.get("GOLD", "#888888")
                    self._ui_color_choice.setItemIcon(idx, _color_swatch_icon(gold_hex))
                except Exception:
                    pass
        self._ui_color_choice.blockSignals(False)

    def _on_ui_color_choice_changed(self, index: int):
        """Обробка вибору кольору інтерфейсу у Налаштуваннях."""
        mode = self._ui_color_choice.itemData(index)
        cfg = self.controller.cfg
        prev_color = getattr(self, "_current_ui_color", theme.resolve_ui_color(cfg))

        target_color = mode

        if mode == "custom":
            from PySide6.QtWidgets import QColorDialog
            from PySide6.QtGui import QColor

            init_color = QColor("#F39200")
            try:
                pal = theme.palette_for(prev_color)
                init_color = QColor(pal.get("GOLD", "#F39200"))
            except Exception:
                pass

            dlg_color = QColorDialog.getColor(init_color, self, tr("set_ui_color_custom"))
            if not dlg_color.isValid():
                self._sync_ui_color_choice_selection(prev_color)
                return

            hue = dlg_color.hue()
            if hue == -1:
                # Сірий, чорний і білий не мають відтінку — тут проблема не в
                # контрасті, а в самій неможливості побудувати монохром. Текст
                # мусить казати саме це, інакше людина шукатиме, що не так із
                # читабельністю.
                self._sync_ui_color_choice_selection(prev_color)
                QMessageBox.warning(
                    self,
                    tr("set_ui_color_err_title"),
                    tr("set_ui_color_err_no_hue"),
                )
                return

            try:
                theme.build_palette_for_hue(hue)
                target_color = hue
            except RuntimeError:
                self._sync_ui_color_choice_selection(prev_color)
                QMessageBox.warning(
                    self,
                    tr("set_ui_color_err_title"),
                    tr("set_ui_color_err_contrast"),
                )
                return

        try:
            theme.set_ui_color(target_color)
        except (ValueError, RuntimeError):
            self._sync_ui_color_choice_selection(prev_color)
            QMessageBox.warning(
                self,
                tr("set_ui_color_err_title"),
                tr("set_ui_color_err_contrast"),
            )
            return

        self._current_ui_color = target_color
        cfg.ui_color = target_color
        cfg.night_mode = (target_color == "red")
        self.controller.save_config()

        self._apply_ui_color_live(target_color)

    def _apply_ui_color_live(self, color):
        """Перебудувати та перемалювати інтерфейс наживо."""
        window = getattr(self.controller, "window", None)
        if window is not None and hasattr(window, "reapply_theme"):
            window.reapply_theme(color == "red")
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.QSS)
            theme.apply_link_colors(app)
            theme._run_restyle_hooks()
            for w in app.allWidgets():
                try:
                    w.update()
                except RuntimeError:
                    pass

    def _on_night_mode(self, on: bool):
        """Сумісність зі старим нічним режимом."""
        new_color = "red" if on else "classic"
        self._on_ui_color_choice_changed(self._ui_color_choice.findData(new_color))

    # --- Тло робочої зони (Т76) ---
    def _update_bg_controls_visibility(self, is_custom: bool) -> None:
        self._bg_choose_btn.setVisible(is_custom)
        self._bg_hint.setVisible(is_custom)

    def _on_bg_choice_changed(self, index: int) -> None:
        mode = self._bg_choice.itemData(index)
        cfg = self.controller.cfg
        prev_mode = getattr(cfg, "workspace_bg", "mascot")

        if mode == "custom":
            self._update_bg_controls_visibility(True)
            custom_path = getattr(cfg, "workspace_custom_bg_path", None)
            from whisper_core.paths import user_dir
            if not custom_path or not (user_dir() / custom_path).is_file():
                success = self._on_bg_choose_file()
                if not success:
                    m_idx = self._bg_choice.findData("mascot")
                    self._bg_choice.blockSignals(True)
                    self._bg_choice.setCurrentIndex(m_idx if m_idx >= 0 else 0)
                    self._bg_choice.blockSignals(False)
                    self._update_bg_controls_visibility(False)
                    return
            else:
                cfg.workspace_bg = "custom"
                self.controller.save_config()
                self._notify_bg_update()
        else:
            self._update_bg_controls_visibility(False)
            if prev_mode == "custom" or getattr(cfg, "workspace_custom_bg_path", None):
                cleanup_custom_bg_file(cfg)
            cfg.workspace_bg = mode
            self.controller.save_config()
            self._notify_bg_update()

    def _on_bg_choose_file(self) -> bool:
        fn, _ = QFileDialog.getOpenFileName(
            self,
            tr("set_workspace_bg_choose_file"),
            "",
            "Зображення (*.png *.jpg *.jpeg *.webp);;Усі файли (*.*)",
        )
        if not fn:
            return False
        src_path = Path(fn)
        valid, err_key = validate_custom_bg_file(src_path)
        if not valid:
            QMessageBox.warning(
                self,
                tr("set_workspace_bg_err_title"),
                tr(err_key or "set_workspace_bg_err_corrupt"),
            )
            return False

        try:
            import shutil
            from whisper_core.paths import user_dir, safe_under

            u_dir = user_dir()
            for old_bg in u_dir.glob("workspace_custom_bg.*"):
                if old_bg.is_file() and safe_under(u_dir, old_bg):
                    try:
                        old_bg.unlink()
                    except OSError:
                        pass

            ext = src_path.suffix.lower() or ".png"
            dest_filename = f"workspace_custom_bg{ext}"
            dest_path = u_dir / dest_filename
            shutil.copy2(src_path, dest_path)

            cfg = self.controller.cfg
            cfg.workspace_custom_bg_path = dest_filename
            cfg.workspace_bg = "custom"
            self.controller.save_config()
            self._notify_bg_update()
            return True
        except Exception as e:
            logging.exception("Failed to copy custom background file: %s", e)
            QMessageBox.warning(
                self,
                tr("set_workspace_bg_err_title"),
                tr("set_workspace_bg_err_corrupt"),
            )
            return False

    def _notify_bg_update(self) -> None:
        win = getattr(self.controller, "window", None)
        if win and hasattr(win, "pages") and hasattr(win.pages, "reload_background"):
            win.pages.reload_background(self.controller.cfg)
