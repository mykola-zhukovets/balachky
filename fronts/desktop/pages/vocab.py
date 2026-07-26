"""Вкладка «Словники»: список словників, терміни активного, помічені нові слова.

Стилі — лише глобальний QSS через properties (card/muted/strong/accent).
Виділення активного рядка — QFrame[active="true"]: цей стиль додається
у theme.py під час інтеграції, тут лише setProperty("active", True).
"""
import os
import shutil
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QLabel, QFrame, QPushButton, QVBoxLayout, QHBoxLayout,
    QTableWidget, QTableWidgetItem, QAbstractItemView, QCheckBox,
    QHeaderView, QInputDialog, QMessageBox, QFileDialog, QScrollArea, QMenu,
    QDialog, QTextEdit, QLineEdit,
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor

import learn  # модуль у корені репо; PyInstaller пакує його як звичайний модуль
from .. import theme
from whisper_core import paths, profiles
from whisper_core.terms import (
    read_terms_dict, add_term, parse_bulk_terms, parse_csv_terms,
    editable_canons, delete_term, rename_term,
)
from whisper_core import macros as macros_mod   # feature/voice-macros
from whisper_core import error_diary             # feature/diary-calendar
from whisper_core import phrasebook              # feature/bilingual-memory
from whisper_core import self_learning           # feature/selflearn-dict

from .. import motion
from ..glass import GlassButton
from ..i18n import tr, plural
from . import page_header

# База профілів — через whisper_core.paths (dev: корінь репо; frozen: USER_DIR)
ROOT = paths.profiles_root()


def _profile_error_text(error: profiles.ProfileValidationError) -> str:
    """Локалізувати стабільний код ядра на межі UI."""
    if error.code == "already_exists":
        return tr("vocab_name_exists", name=error.name)
    if error.code == "is_default":               # feature/vocab-manage
        return tr("vocab_name_is_default")
    if error.code == "not_found":                # feature/vocab-manage
        return tr("vocab_name_not_found")
    return tr("vocab_name_invalid")


def _clear(lay):
    """Прибрати всі елементи layout (віджети й вкладені layout-и)."""
    while lay.count():
        item = lay.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


class _ProfileRow(QFrame):
    """Рядок-картка словника; клік будь-де по рядку → перемкнути."""

    def __init__(self, on_click):
        super().__init__()
        self._on_click = on_click
        self._on_context = None          # feature/vocab-manage: ПКМ-меню (опційно)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setProperty("profileRow", True)

    def set_context(self, on_context):
        """Увімкнути ПКМ-меню картки (перейменувати/видалити). Захищені
        профілі меню не отримують — тоді on_context лишається None."""
        self._on_context = on_context

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Space):
            self._on_click()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._on_click()
        else:
            super().mousePressEvent(event)

    def contextMenuEvent(self, event):    # feature/vocab-manage
        if self._on_context is not None:
            self._on_context(event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)
    # hover — миттєвий QSS (QFrame[profileRow]:hover у theme.py). Анімований
    # золотий оверлей прибрано (Kimi K3: «рідкий» ефект на кожному рядку дратує).


# feature/bulk-import
class _BulkImportDialog(QDialog):
    """Масовий імпорт термінів: багаторядкове поле, один термін на рядок."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("vocab_bulk_title"))
        self.setModal(True)
        self.setMinimumWidth(520)   # дозволити рости вширину (~65 симв.), не тісний
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)

        prompt = QLabel(tr("vocab_bulk_prompt"))
        prompt.setWordWrap(True)
        prompt.setProperty("muted", True)
        lay.addWidget(prompt)

        self._edit = QTextEdit()
        self._edit.setPlaceholderText(tr("vocab_bulk_placeholder"))
        self._edit.setMinimumHeight(220)
        lay.addWidget(self._edit)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("vocab_bulk_submit"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self.accept)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def text(self) -> str:
        return self._edit.toPlainText()


# feature/voice-macros
class _MacroAddDialog(QDialog):
    """Додати макрос: тригер (однорядковий) + розгортка (багаторядкова, може
    містити {дата}/{час})."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("vocab_macros_add_title"))
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        t_label = QLabel(tr("vocab_macros_trigger_label"))
        t_label.setProperty("strong", True)
        lay.addWidget(t_label)
        self._trigger = QLineEdit()
        self._trigger.setPlaceholderText(tr("vocab_macros_trigger_ph"))
        lay.addWidget(self._trigger)

        lay.addSpacing(6)
        e_label = QLabel(tr("vocab_macros_expansion_label"))
        e_label.setProperty("strong", True)
        lay.addWidget(e_label)
        self._expansion = QTextEdit()
        self._expansion.setPlaceholderText(tr("vocab_macros_expansion_ph"))
        self._expansion.setMinimumHeight(150)
        lay.addWidget(self._expansion)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("vocab_macros_submit"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _submit(self):
        # тригер і розгортка обидва обов'язкові — інакше макрос нінащо
        if not self._trigger.text().strip() or not self._expansion.toPlainText().strip():
            QMessageBox.warning(self, tr("vocab_macros_add_title"),
                                tr("vocab_macros_need_both"))
            return
        self.accept()

    def values(self):
        """(тригер, розгортка) без обрізання розгортки — переноси зберігаємо."""
        return self._trigger.text().strip(), self._expansion.toPlainText()


# feature/bilingual-memory
class _PhraseAddDialog(QDialog):
    """Додати пару в пам'ять фраз: як ЧУЄТЬСЯ (кирилицею) + як ПИСАТИ (латиницею).
    Обидва поля — однорядкові; можна задати початкові значення (для кандидата
    зі щоденника помилок)."""

    def __init__(self, parent=None, heard="", write=""):
        super().__init__(parent)
        self.setWindowTitle(tr("vocab_phrase_add_title"))
        self.setModal(True)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        h_label = QLabel(tr("vocab_phrase_heard_label"))
        h_label.setProperty("strong", True)
        lay.addWidget(h_label)
        self._heard = QLineEdit(heard)
        self._heard.setPlaceholderText(tr("vocab_phrase_heard_ph"))
        self._heard.setAccessibleName(tr("vocab_phrase_heard_label"))
        lay.addWidget(self._heard)

        lay.addSpacing(6)
        w_label = QLabel(tr("vocab_phrase_write_label"))
        w_label.setProperty("strong", True)
        lay.addWidget(w_label)
        self._write = QLineEdit(write)
        self._write.setPlaceholderText(tr("vocab_phrase_write_ph"))
        self._write.setAccessibleName(tr("vocab_phrase_write_label"))
        lay.addWidget(self._write)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("vocab_phrase_submit"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _submit(self):
        # обидва поля обов'язкові — інакше пара нінащо
        if not self._heard.text().strip() or not self._write.text().strip():
            QMessageBox.warning(self, tr("vocab_phrase_add_title"),
                                tr("vocab_phrase_need_both"))
            return
        self.accept()

    def values(self):
        """(як_чується, як_писати)."""
        return self._heard.text().strip(), self._write.text().strip()


class VocabPage(QWidget):
    """Словники: перемикання, терміни, помічені нові слова, пам'ять."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        # Сторінка вища за клієнтську область на 1080p (minimumSizeHint ~1153 >
        # ~1044): без скролу QStackedWidget тисне вміст нижче мінімуму й РІЖЕ
        # рядки-картки словників (§1 рубрики). Тому весь вміст — у QScrollArea,
        # як і на сторінці «Налаштування»: переповнення прокручується, не ріжеться.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        _scroll = QScrollArea()
        _scroll.setWidgetResizable(True)
        _scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(_scroll)
        _content = QWidget()
        _scroll.setWidget(_content)
        root = QVBoxLayout(_content)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        # --- шапка сторінки: заголовок + підзаголовок на ПОВНУ ширину ---
        # Окремим рядком від дій (канон DESIGN-TYPOGRAPHY §3): підзаголовок
        # переноситься нормальними рядками, а не стискається в колонку по слову.
        root.addLayout(page_header(tr("nav_dictionaries"), tr("vocab_subtitle")))
        root.addSpacing(16)

        # --- окремий ряд-тулбар дій під шапкою ---
        # Головна «+ Новий» — кнопка; три вторинні дії згруповано в меню-кнопку
        # «Додати…», щоб повні підписи не обрізались на 1000px (§3, п.5).
        toolbar = QHBoxLayout()
        toolbar.setSpacing(10)
        new_btn = QPushButton(tr("vocab_new"))
        new_btn.setProperty("accent", True)
        new_btn.clicked.connect(self._new_profile)
        add_btn = QPushButton(tr("vocab_add_menu"))
        add_menu = QMenu(add_btn)
        add_menu.addAction(tr("vocab_add_from_file_item"), self._add_from_file)
        add_menu.addAction(tr("vocab_add_bulk_item"), self._add_bulk)   # feature/bulk-import
        add_menu.addAction(tr("vocab_add_csv_item"), self._import_csv)  # CSV з файлу
        add_btn.setMenu(add_menu)
        save_btn = QPushButton(tr("vocab_save_file"))
        save_btn.clicked.connect(self._save_to_file)
        self._header_buttons = (new_btn, add_btn, save_btn)   # для render-smoke
        for b in self._header_buttons:
            toolbar.addWidget(b)
        toolbar.addStretch()
        root.addLayout(toolbar)
        root.addSpacing(20)

        # --- список словників (перебудовується у refresh) ---
        self._list_box = QVBoxLayout()
        self._list_box.setSpacing(10)
        root.addLayout(self._list_box)
        root.addSpacing(24)

        # --- вивчені виправлення активного словника (feature/selflearn-dict) ---
        # «Виправив раз — назавжди»: правила, які програма запам'ятала з виправлень
        # користувача САМЕ для цього словника. Над ручним керуванням термінами/
        # фразами; прибрати запис — і він одразу перестає діяти (з undo).
        self._learned_title = QLabel()
        self._learned_title.setProperty("strong", True)
        root.addWidget(self._learned_title)
        root.addSpacing(4)
        learned_hint = QLabel(tr("sl_manage_hint"))
        learned_hint.setProperty("muted", True)
        learned_hint.setWordWrap(True)
        root.addWidget(learned_hint)
        root.addSpacing(10)
        self._learned_search = QLineEdit()
        self._learned_search.setPlaceholderText(tr("sl_manage_search"))
        self._learned_search.setClearButtonEnabled(True)
        self._learned_search.setAccessibleName(tr("sl_manage_search"))
        self._learned_search.textChanged.connect(self._filter_learned)
        root.addWidget(self._learned_search)
        root.addSpacing(10)
        self._learned_box = QVBoxLayout()
        self._learned_box.setSpacing(8)
        self._learned_box.setAlignment(Qt.AlignTop)
        root.addLayout(self._learned_box)
        self._learned_cards = []      # (card, haystack) — для фільтра пошуку
        root.addSpacing(24)

        # --- дві колонки: терміни | помічені слова ---
        cols = QHBoxLayout()
        cols.setSpacing(28)
        left = QVBoxLayout()
        left.setSpacing(12)
        self._terms_title = QLabel()
        self._terms_title.setProperty("strong", True)
        left.addWidget(self._terms_title)
        self._table = QTableWidget(0, 2)
        theme.setup_table(self._table)
        self._table.setHorizontalHeaderLabels(
            [tr("vocab_col_spell"), tr("vocab_col_sound")])
        # feature/vocab-manage: подвійний клік редагує лише машинні терміни
        # (людські лишаються без прапорця ItemIsEditable — див. refresh)
        self._table.setEditTriggers(QAbstractItemView.DoubleClicked)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._terms_menu)
        self._table.itemChanged.connect(self._on_term_edited)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(38)
        # обидві колонки ділять ширину — без горизонтального скролбара
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.horizontalHeader().setDefaultAlignment(
            Qt.AlignLeft | Qt.AlignVCenter)
        self._table.setShowGrid(False)
        left.addWidget(self._table, stretch=1)
        open_btn = QPushButton(tr("vocab_open_file"))
        open_btn.clicked.connect(self._open_terms_file)
        left.addWidget(open_btn, alignment=Qt.AlignLeft)
        cols.addLayout(left, stretch=3)

        right = QVBoxLayout()
        right.setSpacing(12)
        cand_title = QLabel(tr("vocab_spotted"))
        cand_title.setProperty("strong", True)
        right.addWidget(cand_title)
        # Пояснення терміна на всю ширину колонки (канон DESIGN-TYPOGRAPHY §3:
        # підказка переноситься по словах, а не стискається/обрізається).
        cand_explain = QLabel(tr("vocab_spotted_explain"))
        cand_explain.setObjectName("vocabSpottedExplain")
        cand_explain.setProperty("muted", True)
        cand_explain.setWordWrap(True)
        right.addWidget(cand_explain)
        right.addSpacing(4)

        self._cand_box = QVBoxLayout()
        self._cand_box.setSpacing(10)
        self._cand_box.setAlignment(Qt.AlignTop)

        cand_container = QWidget()
        cand_container.setLayout(self._cand_box)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(cand_container)

        right.addWidget(scroll, stretch=1)
        cols.addLayout(right, stretch=2)
        root.addLayout(cols, stretch=1)
        root.addSpacing(24)

        # --- макроси (feature/voice-macros) ---
        # Підсекція на всю ширину: заголовок + підказка окремими рядками (канон
        # DESIGN-TYPOGRAPHY §3 — підказка переноситься, а не стискається).
        m_title = QLabel(tr("vocab_macros_title"))
        m_title.setProperty("strong", True)
        root.addWidget(m_title)
        root.addSpacing(4)
        m_hint = QLabel(tr("vocab_macros_hint"))
        m_hint.setProperty("muted", True)
        m_hint.setWordWrap(True)
        root.addWidget(m_hint)
        root.addSpacing(12)

        self._macros_table = QTableWidget(0, 2)
        theme.setup_table(self._macros_table)
        self._macros_table.setHorizontalHeaderLabels(
            [tr("vocab_macros_col_trigger"), tr("vocab_macros_col_expansion")])
        self._macros_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._macros_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._macros_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._macros_table.verticalHeader().setVisible(False)
        self._macros_table.verticalHeader().setDefaultSectionSize(38)
        self._macros_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents)
        self._macros_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch)
        self._macros_table.horizontalHeader().setDefaultAlignment(
            Qt.AlignLeft | Qt.AlignVCenter)
        self._macros_table.setShowGrid(False)
        self._macros_table.setMinimumHeight(140)
        root.addWidget(self._macros_table)
        root.addSpacing(10)

        m_bar = QHBoxLayout()
        m_bar.setSpacing(10)
        m_add = QPushButton(tr("vocab_macros_add"))
        m_add.setProperty("accent", True)
        m_add.clicked.connect(self._add_macro)
        m_del = QPushButton(tr("vocab_macros_delete"))
        m_del.clicked.connect(self._delete_macro)
        m_open = QPushButton(tr("vocab_macros_open"))
        m_open.clicked.connect(self._open_macros_file)
        for b in (m_add, m_del, m_open):
            m_bar.addWidget(b)
        m_bar.addStretch()
        root.addLayout(m_bar)
        root.addSpacing(24)

        # --- щоденник помилок (feature/diary-calendar) ---
        # Повторювані виправлення з корпусу: “було → стало × N”. Кнопка біля
        # кожного рядка додає правило у словник активного профілю. Список
        # перебудовується у refresh() (той самий патерн, що макроси/кандидати).
        d_title = QLabel(tr("diary_title"))
        d_title.setProperty("strong", True)
        root.addWidget(d_title)
        root.addSpacing(4)
        d_hint = QLabel(tr("diary_hint"))
        d_hint.setProperty("muted", True)
        d_hint.setWordWrap(True)
        root.addWidget(d_hint)
        root.addSpacing(12)

        self._diary_box = QVBoxLayout()
        self._diary_box.setSpacing(10)
        self._diary_box.setAlignment(Qt.AlignTop)
        root.addLayout(self._diary_box)
        root.addSpacing(24)

        # --- білінгвальна пам'ять фраз (feature/bilingual-memory) ---
        # Окреме сховище (phrases.toml профілю) для укр/англ-сумішей, які STT
        # калічить кирилицею («ворктрі» → «worktree»). Свій тумблер, незалежний
        # від «Не виправляй мою мову»: це терміни, а не стиль мовлення.
        ph_title = QLabel(tr("vocab_phrase_title"))
        ph_title.setProperty("strong", True)
        root.addWidget(ph_title)
        root.addSpacing(4)
        ph_hint = QLabel(tr("vocab_phrase_hint"))
        ph_hint.setProperty("muted", True)
        ph_hint.setWordWrap(True)
        root.addWidget(ph_hint)
        root.addSpacing(10)

        self._phrase_cb = QCheckBox(tr("vocab_phrase_enable"))
        self._phrase_cb.setAccessibleName(tr("vocab_phrase_enable"))
        self._phrase_cb.toggled.connect(self._on_phrase_enabled)
        root.addWidget(self._phrase_cb)
        root.addSpacing(10)

        self._phrase_table = QTableWidget(0, 2)
        theme.setup_table(self._phrase_table)
        self._phrase_table.setHorizontalHeaderLabels(
            [tr("vocab_phrase_col_sound"), tr("vocab_phrase_col_spell")])
        self._phrase_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._phrase_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._phrase_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._phrase_table.setContextMenuPolicy(Qt.NoContextMenu)
        self._phrase_table.verticalHeader().setVisible(False)
        self._phrase_table.verticalHeader().setDefaultSectionSize(38)
        self._phrase_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._phrase_table.horizontalHeader().setDefaultAlignment(
            Qt.AlignLeft | Qt.AlignVCenter)
        self._phrase_table.setShowGrid(False)
        self._phrase_table.setMinimumHeight(140)
        self._phrase_table.setAccessibleName(tr("vocab_phrase_title"))
        root.addWidget(self._phrase_table)
        root.addSpacing(10)

        ph_bar = QHBoxLayout()
        ph_bar.setSpacing(10)
        ph_add = QPushButton(tr("vocab_phrase_add"))
        ph_add.setProperty("accent", True)
        ph_add.clicked.connect(self._add_phrase)
        ph_del = QPushButton(tr("vocab_phrase_delete"))
        ph_del.clicked.connect(self._delete_phrase)
        ph_imp = QPushButton(tr("vocab_phrase_import"))
        ph_imp.clicked.connect(self._import_phrases)
        ph_open = QPushButton(tr("vocab_phrase_open"))
        ph_open.clicked.connect(self._open_phrases_file)
        for b in (ph_add, ph_del, ph_imp, ph_open):
            ph_bar.addWidget(b)
        ph_bar.addStretch()
        root.addLayout(ph_bar)
        root.addSpacing(10)

        # авто-навчання: кандидати з мовного щоденника (латинська правильна форма)
        ph_sug_title = QLabel(tr("vocab_phrase_sug_title"))
        ph_sug_title.setProperty("muted", True)
        ph_sug_title.setWordWrap(True)
        root.addWidget(ph_sug_title)
        root.addSpacing(6)
        self._phrase_sug_box = QVBoxLayout()
        self._phrase_sug_box.setSpacing(10)
        self._phrase_sug_box.setAlignment(Qt.AlignTop)
        root.addLayout(self._phrase_sug_box)
        root.addSpacing(24)

        # --- пам'ять ---
        self._profile_meta_warning = QLabel()
        self._profile_meta_warning.setProperty("badge", "error")
        self._profile_meta_warning.setWordWrap(True)
        self._profile_meta_warning.hide()
        root.addWidget(self._profile_meta_warning)
        root.addSpacing(8)
        bottom = QHBoxLayout()
        self._memory_cb = QCheckBox(tr("vocab_mem_cb"))
        self._memory_cb.setAccessibleName(tr("vocab_mem_cb"))
        self._memory_cb.toggled.connect(self._on_memory_toggled)
        bottom.addWidget(self._memory_cb)
        bottom.addStretch()
        # «Очистити історію» живе на сторінці «Історія» — тут лише підказка, щоб
        # не дублювати ту саму дію у двох місцях (плутанина, де «дім» функції).
        clear_hint = QLabel(tr("vocab_clear_hint"))
        clear_hint.setProperty("muted", True)
        clear_hint.setWordWrap(True)
        bottom.addWidget(clear_hint)
        root.addLayout(bottom)

        self.refresh()

    # --- повна перебудова під актуальний профіль (контролер кличе після трею) ---
    def refresh(self):
        profile = self.controller.profile
        # список словників
        _clear(self._list_box)
        for p in profiles.list_profiles(ROOT):
            row = _ProfileRow(lambda name=p.name: self._switch(name))
            row.setProperty("card", True)
            row.setProperty("active", p.name == profile.name)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(18, 13, 18, 13)
            name = QLabel(p.name)
            name.setProperty("strong", True)
            mem = tr("vocab_mem_on") if p.memory_enabled else tr("vocab_mem_off")
            n = len(read_terms_dict(p.terms_path))
            if p.name != profiles.DEFAULT_PROFILE:   # feature/vocab-manage: ПКМ-меню
                row.set_context(
                    lambda pos, nm=p.name, cnt=n: self._profile_menu(nm, cnt, pos))
            # плюралізація: 1 слово / 2-4 слова / 5+ слів (uk), 1 word / N words (en)
            word = plural(n, ("слово", "слова", "слів"), ("word", "words"))
            meta = f"{n} {word} · {mem}"
            if p.name == profile.name:
                meta += tr("vocab_active_suffix")
            row.setAccessibleName(p.name)
            row.setAccessibleDescription(meta)
            m = QLabel(meta)
            m.setProperty("muted", True)
            rl.addWidget(name)
            rl.addStretch()
            rl.addWidget(m)
            self._list_box.addWidget(row)
        # терміни активного
        self._terms_title.setText(tr("vocab_terms_title", name=profile.name))
        terms = read_terms_dict(profile.terms_path, on_recovered=self._notify_recovered)
        # feature/vocab-manage: правити/видаляти можна лише машинні терміни
        editable = editable_canons(profile.terms_path)
        # заповнення НЕ має будити itemChanged (це не правка користувача)
        self._table.blockSignals(True)
        self._table.clearSpans()
        if terms:
            self._table.setRowCount(len(terms))
            for i, (canon, variants) in enumerate(sorted(terms.items())):
                spelling = QTableWidgetItem(canon)
                spelling.setData(Qt.UserRole, canon)   # стара форма — для перейменування
                if canon in editable:
                    spelling.setToolTip(canon)
                    spelling.setFlags(spelling.flags() | Qt.ItemIsEditable)
                else:                                  # людський terms.toml → read-only
                    spelling.setToolTip(tr("vocab_term_readonly"))
                    spelling.setFlags(spelling.flags() & ~Qt.ItemIsEditable)
                sounds_text = ", ".join(variants)
                sounds = QTableWidgetItem(sounds_text)
                sounds.setToolTip(sounds_text)
                # варіанти («як чується») тут не редагуємо — лише канон і видалення
                sounds.setFlags(sounds.flags() & ~Qt.ItemIsEditable)
                self._table.setItem(i, 0, spelling)
                self._table.setItem(i, 1, sounds)
        else:
            self._table.setRowCount(1)
            self._table.setSpan(0, 0, 1, 2)
            empty = QTableWidgetItem(tr("vocab_terms_empty"))
            empty.setTextAlignment(Qt.AlignCenter)
            empty.setToolTip(tr("vocab_terms_empty"))
            empty.setFlags(empty.flags() & ~Qt.ItemIsEditable)
            self._table.setItem(0, 0, empty)
        self._table.blockSignals(False)
        # помічені нові слова
        _clear(self._cand_box)
        known = learn.load_known_variants(profile.terms_path) | profile.ignored_words()
        cands = learn.analyze(profile.history_path, known=known, min_count=2)[:10]
        if not cands:
            hint = QLabel(tr("vocab_cand_empty"))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            self._cand_box.addWidget(hint)
        for word, count in cands:
            card = QFrame()
            card.setProperty("card", True)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 10, 12, 10)
            cl.setSpacing(8)
            cl.addWidget(QLabel(f"{word} ×{count}"))
            add = GlassButton(tr("vocab_add"))
            add.clicked.connect(lambda _=False, w=word: self._add_candidate(w))
            skip = QPushButton(tr("vocab_ban"))
            skip.setProperty("ghost", True)   # тихіша другорядна дія
            skip.clicked.connect(lambda _=False, w=word: self._ignore_candidate(w))
            cl.addWidget(add, alignment=Qt.AlignLeft)
            cl.addWidget(skip, alignment=Qt.AlignLeft)
            self._cand_box.addWidget(card)
        # вивчені виправлення активного профілю (feature/selflearn-dict)
        self._refresh_learned()
        # макроси активного профілю (feature/voice-macros)
        self._refresh_macros_table()
        # щоденник помилок (feature/diary-calendar)
        self._refresh_diary()
        # білінгвальна пам'ять фраз (feature/bilingual-memory)
        self._phrase_cb.blockSignals(True)
        self._phrase_cb.setChecked(
            bool(getattr(self.controller.cfg, "phrase_memory_enabled", False)))
        self._phrase_cb.blockSignals(False)
        self._refresh_phrase_table()
        self._refresh_phrase_suggestions()
        # пам'ять (без сигналу — це не дія користувача)
        self._memory_cb.blockSignals(True)
        self._memory_cb.setChecked(profile.memory_enabled)
        self._memory_cb.blockSignals(False)
        self._refresh_profile_meta_warning()

    def _notify_recovered(self, path):
        """Колбек для read_terms_dict: побитий terms.auto.toml підняли з
        резервної копії (whisper_core.dict_backup) — повідомити користувача."""
        QMessageBox.information(
            self, tr("vocab_dict_recovered_title"), tr("vocab_dict_recovered_body"))

    def _refresh_profile_meta_warning(self):
        corrupt = bool(getattr(self.controller.profile, "meta_corrupt", False))
        if not corrupt:
            self._profile_meta_warning.hide()
            return
        text = tr("profile_meta_corrupt")
        self._profile_meta_warning.setText(text)
        self._profile_meta_warning.setAccessibleName(text)
        self._profile_meta_warning.show()

    # --- дії ---
    def _switch(self, name):
        self.controller.switch_profile(name)
        self.refresh()

    def _new_profile(self):
        name, ok = QInputDialog.getText(
            self, tr("vocab_new_title"), tr("vocab_new_prompt"))
        if not ok or not name.strip():
            return
        try:
            profiles.create_profile(ROOT, name.strip())
        except profiles.ProfileValidationError as e:
            QMessageBox.warning(self, tr("vocab_create_fail"),
                                _profile_error_text(e))
            return
        self.controller.switch_profile(name.strip())
        self.refresh()

    def _add_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("vocab_file_title"), "", tr("vocab_filter"))
        if not path:
            return
        name, ok = QInputDialog.getText(
            self, tr("vocab_new_from_file"), tr("vocab_new_name"),
            text=Path(path).stem)
        if not ok or not name.strip():
            return
        try:
            p = profiles.create_profile(ROOT, name.strip())
        except profiles.ProfileValidationError as e:
            QMessageBox.warning(self, tr("vocab_create_fail"),
                                _profile_error_text(e))
            return
        shutil.copy2(path, p.terms_path)
        self.refresh()

    # feature/bulk-import
    def _add_bulk(self):
        dlg = _BulkImportDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        profile = self.controller.profile
        existing = read_terms_dict(profile.terms_path)
        new_terms, skipped = parse_bulk_terms(dlg.text(), existing)
        for canon, variant in new_terms:
            add_term(profile.terms_path, canon, variant)
        self.controller.reload_terms()
        self.refresh()
        motion.toast(self, tr("vocab_bulk_result", added=len(new_terms), skipped=skipped))

    # CSV-імпорт з файлу: «термін;вимова1;вимова2»
    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr("vocab_csv_title"), "", tr("vocab_csv_filter"))
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, ValueError):
            QMessageBox.warning(self, tr("vocab_csv_title"), tr("vocab_csv_read_fail"))
            return
        profile = self.controller.profile
        existing = read_terms_dict(profile.terms_path)
        new_terms, skipped = parse_csv_terms(text, existing)
        if not new_terms:
            QMessageBox.information(
                self, tr("vocab_csv_title"),
                tr("vocab_csv_result", added=0, skipped=skipped))
            return
        resp = QMessageBox.question(
            self, tr("vocab_csv_title"),
            tr("vocab_csv_confirm", added=len(new_terms), skipped=skipped))
        if resp != QMessageBox.Yes:
            return
        for canon, variant in new_terms:
            add_term(profile.terms_path, canon, variant)
        self.controller.reload_terms()
        self.refresh()
        motion.toast(self, tr("vocab_csv_result", added=len(new_terms), skipped=skipped))

    def _save_to_file(self):
        src = self.controller.profile.terms_path
        if not src.exists():
            QMessageBox.warning(self, tr("vocab_nothing_title"),
                                tr("vocab_nothing_body"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self, tr("vocab_save_copy"),
            tr("vocab_save_filename", name=self.controller.profile.name),
            tr("vocab_filter_save"))
        if path:
            shutil.copy2(src, path)

    def _open_terms_file(self):
        p = self.controller.profile.terms_path
        if p.exists():
            os.startfile(p)

    def _add_candidate(self, word):
        canon, ok = QInputDialog.getText(
            self, tr("vocab_add_title"), tr("fixdlg_prompt", word=word), text=word)
        if not ok or not canon.strip():
            return
        add_term(self.controller.profile.terms_path, canon.strip(), word)
        self.controller.reload_terms()
        self.refresh()

    def _ignore_candidate(self, word):
        self.controller.profile.add_ignored([word])
        self.refresh()

    # --- вивчені виправлення (feature/selflearn-dict) ---
    _LEARNED_BADGE = {"term-bias": "sl_badge_bias", "term-replace": "sl_badge_term",
                      "phrase-replace": "sl_badge_phrase"}

    @staticmethod
    def _fmt_learned_date(iso: str) -> str:
        """ISO 2026-07-23T… → 23.07.2026 (як у картках Історії). Порожньо при збої."""
        parts = (iso or "")[:10].split("-")
        return f"{parts[2]}.{parts[1]}.{parts[0]}" if len(parts) == 3 and parts[0] else ""

    def _refresh_learned(self):
        """Перебудувати список вивчених виправлень активного словника."""
        profile = self.controller.profile
        self._learned_title.setText(tr("sl_manage_title", name=profile.name))
        _clear(self._learned_box)
        self._learned_cards = []
        try:
            entries = self_learning.list_learned(profile)
        except Exception:
            entries = []
        if not entries:
            hint = QLabel(tr("sl_manage_empty"))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            self._learned_box.addWidget(hint)
            return
        for e in entries:
            card = QFrame()
            card.setProperty("card", True)
            row = QHBoxLayout(card)
            row.setContentsMargins(16, 9, 12, 9)
            row.setSpacing(12)
            badge_text = tr(self._LEARNED_BADGE.get(e.kind, "sl_badge_term"))
            badge = QLabel(badge_text)
            badge.setProperty("eyebrow", True)
            row.addWidget(badge)
            pair = f"{e.heard} → {e.write}"
            lbl = QLabel(pair)
            lbl.setWordWrap(True)
            row.addWidget(lbl, 1)
            date = QLabel(self._fmt_learned_date(e.created_at))
            date.setProperty("muted", True)
            row.addWidget(date)
            rm = QPushButton(tr("sl_manage_remove"))
            rm.setProperty("ghost", True)
            rm.setAccessibleName(f"{tr('sl_manage_remove')}: {badge_text} {pair}")
            rm.clicked.connect(lambda _=False, ent=e: self._remove_learned(ent))
            row.addWidget(rm)
            card.setAccessibleName(f"{badge_text} {pair}")
            self._learned_box.addWidget(card)
            self._learned_cards.append((card, f"{pair}\n{badge_text}".lower()))
        self._filter_learned(self._learned_search.text())

    def _filter_learned(self, query):
        q = (query or "").strip().lower()
        for card, hay in self._learned_cards:
            card.setVisible(not q or q in hay)

    def _remove_learned(self, entry):
        """Прибрати вивчене правило (без підтвердження) + тост із «Скасувати»:
        undo повертає ТЕ САМЕ правило назад і перечитує терміни активного словника."""
        profile = self.controller.profile
        if not self.controller.revoke_learned(profile, entry.id):
            return
        self._refresh_learned()

        def _undo(kind=entry.kind, heard=entry.heard, write=entry.write, prof=profile):
            self_learning.relearn(prof, kind, heard, write)
            if prof.name == self.controller.profile.name:
                self.controller.reload_terms()
            self._refresh_learned()

        try:
            motion.undo_toast(
                self, tr("sl_removed", name=profile.name, heard=entry.heard,
                         write=entry.write), _undo, undo_label=tr("sl_undo"),
                seconds=10)
        except Exception:
            pass

    # --- щоденник помилок (feature/diary-calendar) ---
    def _refresh_diary(self):
        """Перебудувати список повторюваних виправлень із корпусу АКТИВНОГО
        словника. Фільтр за профілем (feature/selflearn-dict): щоденник показує
        лише свої пари, тож клік «Додати» не може дописати чужу пару в цей
        словник (спека «never become one-click suggestions for a selected
        profile»)."""
        _clear(self._diary_box)
        prof = self.controller.profile
        try:
            rows = error_diary.aggregate(profile=prof.name)
        except Exception:
            rows = []
        rows = [r for r in rows if r["count"] >= 2]     # лише повторювані
        if not rows:
            hint = QLabel(tr("diary_empty"))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            self._diary_box.addWidget(hint)
            return
        for r in rows[:20]:
            card = QFrame()
            card.setProperty("card", True)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 10, 12, 10)
            cl.setSpacing(8)
            times = plural(r["count"], ("раз", "рази", "разів"),
                           ("time", "times"))
            text = tr("diary_row", was=r["was"], now=r["now"],
                      n=r["count"], times=times)
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setAccessibleName(text)
            cl.addWidget(lbl)
            add = GlassButton(tr("diary_add"))
            add.setAccessibleName(tr("diary_add"))
            add.clicked.connect(
                lambda _=False, was=r["was"], now=r["now"], p=prof:
                self._diary_add(p, was, now))
            cl.addWidget(add, alignment=Qt.AlignLeft)
            self._diary_box.addWidget(card)

    def _diary_add(self, profile, was, now):
        """Правило зі щоденника у ЗАХОПЛЕНИЙ словник (той, чиї пари показано):
        канон=виправлене, варіант=почуте (щоб рушій «почув was» → підставив now).
        Перечитуємо терміни лише якщо цей словник досі активний — перемикання з
        трею не має переадресувати запис на новоактивний профіль."""
        add_term(profile.terms_path, now, was)
        if profile.name == self.controller.profile.name:
            self.controller.reload_terms()
        try:
            motion.toast(self, tr("diary_added", now=now))
        except Exception:
            pass
        self.refresh()

    # --- керування словниками: перейменувати / видалити (feature/vocab-manage) ---
    def _profile_menu(self, name, word_count, global_pos):
        """ПКМ по картці незахищеного словника."""
        menu = QMenu(self)
        act_rename = menu.addAction(tr("vocab_rename"))
        act_delete = menu.addAction(tr("vocab_delete"))
        chosen = menu.exec(global_pos)
        if chosen == act_rename:
            self._rename_profile(name)
        elif chosen == act_delete:
            self._delete_profile(name, word_count)

    def _rename_profile(self, old):
        new, ok = QInputDialog.getText(
            self, tr("vocab_rename_title"), tr("vocab_rename_prompt"), text=old)
        if not ok or not new.strip() or new.strip() == old:
            return
        active_renamed = self.controller.profile.name == old
        try:
            profiles.rename_profile(ROOT, old, new.strip())
        except profiles.ProfileValidationError as e:
            QMessageBox.warning(self, tr("vocab_rename_fail"),
                                _profile_error_text(e))
            return
        # перейменували активний → пересинхронити контролер тим самим шляхом,
        # що й перемикання (switch_profile сам викличе vocab.refresh)
        if active_renamed:
            self.controller.switch_profile(new.strip())
        else:
            self.refresh()

    def _delete_profile(self, name, word_count):
        word = plural(word_count, ("слово", "слова", "слів"), ("word", "words"))
        confirm = QMessageBox.question(
            self, tr("vocab_delete_title"),
            tr("vocab_delete_body", name=name, count=word_count, word=word),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        active_deleted = self.controller.profile.name == name
        try:
            profiles.delete_profile(ROOT, name)
        except profiles.ProfileValidationError as e:
            QMessageBox.warning(self, tr("vocab_delete_fail"),
                                _profile_error_text(e))
            return
        # видалили активний → ядро вже перемкнуло на default; синхронимо контролер
        if active_deleted:
            self.controller.switch_profile(profiles.get_active(ROOT).name)
        else:
            self.refresh()

    # --- керування термінами: видалити / правка канона (feature/vocab-manage) ---
    def _terms_menu(self, _pos):
        """ПКМ по рядку таблиці термінів → «Видалити» для машинних термінів.
        Хіт-тест беремо через глобальну позицію курсора, щоб не залежати від того,
        у чиїх координатах (таблиця чи viewport) прийшов сигнал."""
        gpos = QCursor.pos()
        item = self._table.itemAt(self._table.viewport().mapFromGlobal(gpos))
        if item is None:
            return
        canon_item = self._table.item(item.row(), 0)
        canon = canon_item.data(Qt.UserRole) if canon_item else None
        # видаляти дозволено лише машинні терміни — та сама межа, що й для правки
        if not canon or canon not in editable_canons(
                self.controller.profile.terms_path):
            return
        menu = QMenu(self)
        act_delete = menu.addAction(tr("vocab_term_delete"))
        if menu.exec(gpos) == act_delete:
            delete_term(self.controller.profile.terms_path, canon)
            self.controller.reload_terms()
            self.refresh()

    def _on_term_edited(self, item):
        """Коміт правки канонічної форми (подвійний клік). Порожньо / без змін —
        просто перемальовуємо. refresh відкладаємо, бо не можна перебудовувати
        таблицю всередині обробки itemChanged (item видалиться під нами)."""
        if item.column() != 0:
            return
        old = item.data(Qt.UserRole)
        new = item.text().strip()
        if new and new != old:
            rename_term(self.controller.profile.terms_path, old, new)
            self.controller.reload_terms()
        QTimer.singleShot(0, self.refresh)

    # --- макроси (feature/voice-macros) ---
    def _macros_path(self):
        return self.controller.profile.macros_path

    def _sync_macros_cache(self):
        """Дати контролеру перечитати macros.toml, щоб наступне диктування
        побачило зміни одразу (не чекаючи mtime-рефрешу)."""
        reload_fn = getattr(self.controller, "_reload_macros", None)
        if callable(reload_fn):
            reload_fn()

    def _refresh_macros_table(self):
        data = macros_mod.load_macros(self._macros_path())
        table = self._macros_table
        table.clearSpans()
        if data:
            table.setRowCount(len(data))
            for i, (trigger, expansion) in enumerate(sorted(data.items())):
                t_item = QTableWidgetItem(trigger)
                t_item.setData(Qt.UserRole, trigger)   # нормалізований ключ для видалення
                t_item.setToolTip(trigger)
                # у таблиці один рядок — переноси розгортки показуємо як пробіли
                preview = " ".join(expansion.splitlines())
                e_item = QTableWidgetItem(preview)
                e_item.setToolTip(expansion)
                table.setItem(i, 0, t_item)
                table.setItem(i, 1, e_item)
        else:
            table.setRowCount(1)
            table.setSpan(0, 0, 1, 2)
            empty = QTableWidgetItem(tr("vocab_macros_empty"))
            empty.setTextAlignment(Qt.AlignCenter)
            table.setItem(0, 0, empty)

    def _add_macro(self):
        dlg = _MacroAddDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        trigger, expansion = dlg.values()
        macros_mod.add_macro(self._macros_path(), trigger, expansion)
        self._sync_macros_cache()
        self._refresh_macros_table()

    def _delete_macro(self):
        row = self._macros_table.currentRow()
        if row < 0:
            return
        item = self._macros_table.item(row, 0)
        trigger = item.data(Qt.UserRole) if item else None
        if not trigger:          # порожній рядок-заглушка
            return
        macros_mod.delete_macro(self._macros_path(), trigger)
        self._sync_macros_cache()
        self._refresh_macros_table()

    def _open_macros_file(self):
        """Відкрити macros.toml у системному редакторі. Немає файлу — засідаємо
        порожнім (заголовок із форматом), щоб було що редагувати."""
        path = self._macros_path()
        if not path.exists():
            macros_mod.save_macros(path, {})
        os.startfile(path)

    # --- білінгвальна пам'ять фраз (feature/bilingual-memory) ---
    def _phrases_path(self):
        return self.controller.profile.phrases_path

    def _reload_phrase_terms(self):
        """Дати контролеру перечитати словник (з підмішаною пам'яттю фраз), щоб
        наступне диктування побачило зміни одразу."""
        reload_fn = getattr(self.controller, "reload_terms", None)
        if callable(reload_fn):
            reload_fn()

    def _refresh_phrase_table(self):
        pairs = phrasebook.list_phrases(self._phrases_path())
        table = self._phrase_table
        table.clearSpans()
        if pairs:
            table.setRowCount(len(pairs))
            for i, (write, variants) in enumerate(pairs):
                heard_text = ", ".join(variants)
                heard = QTableWidgetItem(heard_text)
                heard.setToolTip(heard_text)
                write_item = QTableWidgetItem(write)
                write_item.setData(Qt.UserRole, write)   # ключ для видалення
                write_item.setToolTip(write)
                table.setItem(i, 0, heard)
                table.setItem(i, 1, write_item)
        else:
            table.setRowCount(1)
            table.setSpan(0, 0, 1, 2)
            empty = QTableWidgetItem(tr("vocab_phrase_empty"))
            empty.setTextAlignment(Qt.AlignCenter)
            empty.setToolTip(tr("vocab_phrase_empty"))
            table.setItem(0, 0, empty)

    def _refresh_phrase_suggestions(self):
        """Кандидати у пам'ять фраз зі щоденника помилок АКТИВНОГО словника
        (латинська правильна форма). Той самий патерн карток, що й у діарі; фільтр
        за профілем (feature/selflearn-dict) — щоб клік «Додати» не запхав чужу
        пару в цей словник."""
        _clear(self._phrase_sug_box)
        prof = self.controller.profile
        try:
            rows = phrasebook.bilingual_suggestions(profile=prof.name)
        except Exception:
            rows = []
        # уже додані пари не пропонуємо повторно
        known = {v.lower() for _w, vs in phrasebook.list_phrases(self._phrases_path())
                 for v in vs}
        rows = [r for r in rows if r["was"].lower() not in known]
        if not rows:
            hint = QLabel(tr("vocab_phrase_sug_empty"))
            hint.setProperty("muted", True)
            hint.setWordWrap(True)
            self._phrase_sug_box.addWidget(hint)
            return
        for r in rows:
            card = QFrame()
            card.setProperty("card", True)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 10, 12, 10)
            cl.setSpacing(8)
            text = tr("vocab_phrase_sug_row", was=r["was"], now=r["now"])
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setAccessibleName(text)
            cl.addWidget(lbl)
            add = GlassButton(tr("vocab_phrase_sug_add"))
            add.setAccessibleName(tr("vocab_phrase_sug_add"))
            add.clicked.connect(
                lambda _=False, h=r["was"], w=r["now"], p=prof:
                self._add_phrase_pair(p, h, w))
            cl.addWidget(add, alignment=Qt.AlignLeft)
            self._phrase_sug_box.addWidget(card)

    def _add_phrase_pair(self, profile, heard, write):
        """Записати пару в пам'ять фраз ЗАХОПЛЕНОГО словника; тихо ігноруємо
        дублікат. Перечитуємо терміни лише якщо словник досі активний."""
        if phrasebook.add_phrase(profile.phrases_path, write, heard):
            if profile.name == self.controller.profile.name:
                self._reload_phrase_terms()
            try:
                motion.toast(self, tr("vocab_phrase_added", write=write))
            except Exception:
                pass
        self.refresh()

    def _on_phrase_enabled(self, on):
        self.controller.cfg.phrase_memory_enabled = bool(on)
        self.controller.save_config()
        self._reload_phrase_terms()      # тумблер діє одразу на наступне диктування

    def _add_phrase(self):
        dlg = _PhraseAddDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        heard, write = dlg.values()
        self._add_phrase_pair(self.controller.profile, heard, write)

    def _delete_phrase(self):
        row = self._phrase_table.currentRow()
        if row < 0:
            return
        item = self._phrase_table.item(row, 1)
        write = item.data(Qt.UserRole) if item else None
        if not write:                    # порожній рядок-заглушка
            return
        phrasebook.delete_phrase(self._phrases_path(), write)
        self._reload_phrase_terms()
        self.refresh()

    def _import_phrases(self):
        """Масовий імпорт пар «чується = писати» (той самий формат, що й bulk
        термінів: variant=почуто, canon=як писати)."""
        dlg = _BulkImportDialog(self)
        dlg.setWindowTitle(tr("vocab_phrase_import_title"))
        if dlg.exec() != QDialog.Accepted:
            return
        existing = phrasebook.read_phrases(self._phrases_path())
        # parse_bulk_terms повертає (canon=як_писати, variant=почуто)
        new_pairs, skipped = parse_bulk_terms(dlg.text(), existing)
        added = 0
        for write, heard in new_pairs:
            if heard and phrasebook.add_phrase(self._phrases_path(), write, heard):
                added += 1
            else:
                skipped += 1          # рядок без «= як писати» для фрази нінащо
        self._reload_phrase_terms()
        self.refresh()
        motion.toast(self, tr("vocab_phrase_import_result",
                              added=added, skipped=skipped))

    def _open_phrases_file(self):
        path = self._phrases_path()
        if not path.exists():
            phrasebook.write_phrases(path, phrasebook.read_phrases(path))
            if not path.exists():        # порожньо → write видаляє; сідимо шаблоном
                path.write_text(
                    "# Білінгвальна пам'ять фраз.\n"
                    "# Ключ — як ПИСАТИ (латиниця), список — як ЧУЄТЬСЯ кирилицею.\n"
                    "\n[phrases]\n", encoding="utf-8")
        os.startfile(path)

    def _on_memory_toggled(self, on):
        self.controller.toggle_memory(on)
        self.refresh()
