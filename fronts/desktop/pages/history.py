"""Вкладка «Історія»: минулі розшифровки активного словника (history.jsonl).

Список перебудовується на кожен показ сторінки (showEvent) — історію дописує
кожне диктування і кожен файл. Видалення одного запису — перезапис файлу без
цього рядка (точне співпадіння рядка JSON). «Очистити історію» — через
controller.reset_memory(): history перейменовується у бекап, дані не губляться.
"""
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
    QStackedWidget, QFrame, QPushButton, QMessageBox, QApplication,
)

import qtawesome as qta

from whisper_core.history import delete_line, read_recent
from whisper_core.stats import summarize, estimate_saved_minutes, streak_days

from .. import motion
from ..glass import GlassButton
from ..empty_state import EmptyState
from ..i18n import tr, plural
from .. import theme   # нічний режим: іконка статистики читає палітру
from . import page_header

_MAX_SHOWN = 200   # найновіші; глибша історія лишається у файлі


class HistoryPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addLayout(page_header(tr("nav_history"), tr("hist_subtitle")), 1)
        clear_btn = GlassButton(tr("hist_clear"))
        stats_btn = GlassButton(tr("hist_statistics"),
                                icon=qta.icon("fa6s.chart-line", color=theme.IDLE))
        theme.register_restyle_call(stats_btn, lambda w: w.setIcon(   # нічний режим
            qta.icon("fa6s.chart-line", color=theme.IDLE)))
        stats_btn.clicked.connect(self._toggle_stats)
        head.addWidget(stats_btn, alignment=Qt.AlignTop)

        clear_btn.clicked.connect(self._clear_all)
        head.addWidget(clear_btn, alignment=Qt.AlignTop)
        root.addLayout(head)
        root.addSpacing(16)

        # Статистика (зведення + дашборд економії) прихована при вході (зауваж. 8):
        # список записів одразу вгорі, а кнопка «Статистика» показує/ховає панель.
        # Прихований QWidget не займає місця в layout, тож список піднімається вгору.
        self._stats_panel = QWidget()
        stats_lay = QVBoxLayout(self._stats_panel)
        stats_lay.setContentsMargins(0, 0, 0, 16)     # відступ до пошуку, лише коли видно
        stats_lay.setSpacing(12)
        self._sum_cells = {}      # період -> (число-слів, підпис-записів)
        stats_lay.addWidget(self._build_summary())
        # дашборд «економія часу»: зекономлено часу + стрік (feature/ux-center)
        self._dashboard = self._build_dashboard()
        stats_lay.addWidget(self._dashboard)
        self._stats_panel.hide()
        root.addWidget(self._stats_panel)

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("hist_search"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)
        root.addWidget(self._search)
        root.addSpacing(12)

        # пам'ять вимкнена, але записи є — попереджаємо, не ховаючи список
        self._memory_note = QLabel(tr("hist_mem_off_note"))
        self._memory_note.setProperty("muted", True)
        self._memory_note.setWordWrap(True)
        self._memory_note.hide()
        root.addWidget(self._memory_note)

        # стрічка записів
        self._feedbox = QVBoxLayout()
        self._feedbox.setSpacing(12)
        self._feedbox.addStretch()
        feedhost = QWidget()
        feedhost.setLayout(self._feedbox)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(feedhost)

        # порожній стан ⇄ стрічка
        empty = EmptyState("fa6s.clock-rotate-left", tr("common_empty_here"),
                           tr("hist_empty_hint"))
        self._empty_title = empty.title_label
        self._empty_hint = empty.hint_label

        self._stack = QStackedWidget()
        self._stack.addWidget(empty)    # 0 — порожній стан
        self._stack.addWidget(scroll)   # 1 — стрічка
        root.addWidget(self._stack, stretch=1)

        self._cards = []   # (card_widget, текст у нижньому регістрі) — для фільтра

    def focus_query(self, text: str) -> None:
        """feature/global-search: підставити запит у поле пошуку історії, щоб
        перехід із глобального «Пошуку» одразу відфільтрував потрібні записи."""
        self._search.setText(text or "")

    def _toggle_stats(self):
        """Показати/сховати панель статистики (стартово прихована, зауваж. 8)."""
        self._stats_panel.setVisible(self._stats_panel.isHidden())

    def _build_summary(self) -> QFrame:
        """Картка-зведення «скільки наговорив»: три колонки з великим числом
        слів і підписом «N записів». Числа виставляє _update_summary()."""
        card = QFrame()
        card.setProperty("card", True)
        row = QHBoxLayout(card)
        row.setContentsMargins(24, 18, 24, 18)
        row.setSpacing(12)
        for period, caption in (("today", tr("stats_today")),
                                ("week", tr("stats_week")),
                                ("all", tr("stats_all"))):
            col = QVBoxLayout()
            col.setSpacing(4)
            cap = QLabel(caption)
            cap.setProperty("eyebrow", True)
            cap.setAlignment(Qt.AlignCenter)
            num = QLabel("0")
            num.setProperty("level", "stat")
            num.setAlignment(Qt.AlignCenter)
            sub = QLabel("—")
            sub.setProperty("muted", True)
            sub.setAlignment(Qt.AlignCenter)
            col.addWidget(cap)
            col.addWidget(num)
            col.addWidget(sub)
            row.addLayout(col, 1)
            self._sum_cells[period] = (num, sub)
        return card

    def _build_dashboard(self) -> QFrame:
        """Дашборд «економія часу»: дві колонки — приблизно зекономлений час проти
        набору руками (з підказкою-припущенням) і стрік днів поспіль. Дані —
        з уже накопиченої історії, без нового трекінгу (feature/ux-center)."""
        card = QFrame()
        card.setProperty("card", True)
        row = QHBoxLayout(card)
        row.setContentsMargins(24, 18, 24, 18)
        row.setSpacing(12)

        # зекономлено часу (з ⓘ-підказкою про припущення формули)
        saved_col = QVBoxLayout()
        saved_col.setSpacing(4)
        cap_row = QHBoxLayout()
        cap_row.setSpacing(6)
        cap_row.addStretch()
        saved_cap = QLabel(tr("stats_saved"))
        saved_cap.setProperty("eyebrow", True)
        cap_row.addWidget(saved_cap)
        hint = QLabel("ⓘ")
        hint.setProperty("muted", True)
        hint.setToolTip(tr("stats_saved_hint"))
        cap_row.addWidget(hint)
        cap_row.addStretch()
        self._saved_num = QLabel("—")
        self._saved_num.setProperty("level", "stat")
        self._saved_num.setAlignment(Qt.AlignCenter)
        saved_col.addLayout(cap_row)
        saved_col.addWidget(self._saved_num)
        row.addLayout(saved_col, 1)

        # стрік
        streak_col = QVBoxLayout()
        streak_col.setSpacing(4)
        streak_cap = QLabel(tr("stats_streak"))
        streak_cap.setProperty("eyebrow", True)
        streak_cap.setAlignment(Qt.AlignCenter)
        self._streak_num = QLabel("—")
        self._streak_num.setProperty("level", "stat")
        self._streak_num.setAlignment(Qt.AlignCenter)
        streak_col.addWidget(streak_cap)
        streak_col.addWidget(self._streak_num)
        row.addLayout(streak_col, 1)
        return card

    def _update_summary(self):
        """Перерахувати зведення з history.jsonl активного словника."""
        data = summarize(self.controller.profile)
        for period, (num, sub) in self._sum_cells.items():
            words = data[period]["words"]
            records = data[period]["records"]
            num.setText(str(words))
            word_form = plural(words, ("слово", "слова", "слів"),
                               ("word", "words"))
            rec_form = plural(records, ("запис", "записи", "записів"),
                              ("record", "records"))
            sub.setText(f"{word_form} · {records} {rec_form}")
        # дашборд «економія часу» (feature/ux-center)
        mins = round(estimate_saved_minutes(data["all"]["words"]))
        self._saved_num.setText(tr("stats_saved_unit", mins=mins))
        days = streak_days(self.controller.profile)
        self._streak_num.setText(tr("stats_streak_unit", days=days) if days
                                 else tr("stats_streak_none"))

    def showEvent(self, event):
        """Перебудувати список: історія росте з кожним диктуванням."""
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        self._update_summary()
        # прибрати старі картки (лишається лише stretch)
        while self._feedbox.count() > 1:
            item = self._feedbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cards = []

        memory_on = self.controller.profile.memory_enabled
        # найновіші першими, найбільше _MAX_SHOWN (спільний парсер із треєм)
        records = read_recent(self.controller.profile, _MAX_SHOWN)
        if not records:
            if memory_on:
                self._empty_title.setText(tr("common_empty_here"))
                self._empty_hint.setText(tr("hist_empty_hint"))
            else:
                self._empty_title.setText(tr("hist_mem_off_title"))
                self._empty_hint.setText(tr("hist_mem_off_hint"))
            self._memory_note.hide()
            self._stack.setCurrentIndex(0)
            return

        self._memory_note.setVisible(not memory_on)
        self._stack.setCurrentIndex(1)
        for line, rec in records:          # read_recent уже дає найновіші зверху
            self._add_card(line, rec)
        self._apply_filter(self._search.text())

    def _add_card(self, line: str, rec: dict):
        text = (rec.get("final") or rec.get("raw") or "").strip()
        if not text:
            return
        card = QFrame()
        card.setProperty("card", True)
        motion.lift_on_hover(card)   # наведення «підйом + тінь» на картку-плитку
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 15)
        lay.setSpacing(8)

        meta_parts = []
        ts = rec.get("ts")
        if ts:
            meta_parts.append(time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)))
        if rec.get("source") == "file":
            meta_parts.append(tr("hist_from_file"))
        if rec.get("edited"):          # feature/reverse-dictation: позначка виправлення
            meta_parts.append(tr("revdict_edited_badge"))
        meta_text = "  ·  ".join(meta_parts)
        meta = QLabel(meta_text or "—")
        meta.setProperty("muted", True)
        lay.addWidget(meta)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(body)

        btns = QHBoxLayout()
        btns.setSpacing(10)

        # feature/reverse-dictation: «Переслухати» (грає збережене аудіо цього
        # диктування) + «Виправити» (переслухати й виправити текст голосом/
        # клавіатурою). Аудіо резолвиться через контролер; немає (старі/файлові
        # записи чи пам'ять без аудіо) → кнопка неактивна з підказкою, текст
        # усе одно можна правити.
        resolve = getattr(self.controller, "dictation_audio_path", None)
        audio_path = resolve(rec) if resolve else None
        replay = GlassButton(tr("revdict_replay"))
        replay.setAccessibleName(tr("revdict_replay"))
        # clicked під'єднуємо ЗАВЖДИ (§17 — не «мертва» кнопка), навіть коли
        # неактивна: без збереженого аудіо кнопка лишається disabled із підказкою.
        replay.clicked.connect(
            lambda _=False, r=rec, l=line: self._reverse_dictation(
                r, l, autoplay=True))
        if audio_path is None:
            replay.setEnabled(False)
            replay.setToolTip(tr("revdict_replay_tip_none"))
        btns.addWidget(replay)
        correct = GlassButton(tr("revdict_correct"))
        correct.setAccessibleName(tr("revdict_correct"))
        correct.clicked.connect(
            lambda _=False, r=rec, l=line: self._reverse_dictation(
                r, l, autoplay=False))
        btns.addWidget(correct)

        copy = GlassButton(tr("common_copy"))
        copy.clicked.connect(
            lambda _=False, t=text: QApplication.clipboard().setText(t))
        btns.addWidget(copy)
        # feature/processing-slider (спека §7): відновлення дослівного — окрема
        # кнопка копіює незайманий raw, коли обробка (чистка/пунктуація) змінила
        # вивід. Показуємо лише за наявності відмінного raw, щоб не дублювати «Копіювати».
        raw_text = (rec.get("raw") or "").strip()
        if raw_text and raw_text != text:
            copy_raw = GlassButton(tr("common_copy_verbatim"))
            copy_raw.clicked.connect(
                lambda _=False, t=raw_text: QApplication.clipboard().setText(t))
            btns.addWidget(copy_raw)
        # feature/accuracy-corpus: позначити запис як погано розпізнаний. Аудіо-
        # кліп доступний лише для диктувань ПОТОЧНОЇ сесії (буфер у пам'яті за ts);
        # для давніших/файлових — зразок піде текстовим (чесно: A/B його пропустить).
        bad = QPushButton(tr("corpus_menu_bad"))
        bad.setProperty("ghost", True)
        bad.setAccessibleName(tr("corpus_menu_bad"))
        bad.clicked.connect(
            lambda _=False, t=text, r=rec: self._report_bad(t, r))
        rm = QPushButton(tr("hist_delete"))
        rm.setProperty("ghost", True)   # тиха другорядна дія
        rm.setAccessibleName(tr("hist_delete"))
        rm.clicked.connect(lambda _=False, r=rec: self._delete(line, r, card))
        btns.addStretch()
        lay.addLayout(btns)
        # ─── ДРУГИЙ ряд: тихі дії над записом ───
        # П'ять-шість підписаних кнопок в один ряд не вміщались у картку на
        # мінімумі вікна (1000): «Розпізнано погано…» просила 140 точок при 98
        # доступних, «Видалити з історії» — 121 при 86. Qt тиснув їх нижче
        # minimumSizeHint і різав підписи. Тихі дії (позначити погане
        # розпізнавання, видалити) винесено окремим рядом — той самий рецепт,
        # що в картці стрічки диктування; підписи не скорочуємо.
        quiet = QHBoxLayout()
        quiet.setContentsMargins(0, 0, 0, 0)
        quiet.setSpacing(10)
        quiet.addWidget(bad)
        quiet.addWidget(rm)
        quiet.addStretch()
        lay.addLayout(quiet)

        self._feedbox.insertWidget(self._feedbox.count() - 1, card)
        # Пошук матчить і текст, і мету (дата запису, «з файлу») — щоб запит із
        # датою, як показано в картці (напр. «17.07.2026» чи «17.07»), знаходив
        # запис (зауваж. 9). До цього шукали лише по тексту розшифровки.
        self._cards.append((card, f"{text}\n{meta_text}".lower()))

    # --- дії ---
    def _apply_filter(self, query: str):
        query = query.strip().lower()
        for card, text in self._cards:
            card.setVisible(not query or query in text)

    def _report_bad(self, text: str, rec: dict):
        """«Розпізнано погано…» над карткою історії → діалог збирача корпусу.
        Аудіо-кліп резолвиться за ts (буфер диктування поточної сесії)."""
        from ..corpus_dialog import report_bad
        report_bad(self, self.controller, text, ts=rec.get("ts"),
                   source=rec.get("source") or "desktop")

    def _reverse_dictation(self, rec: dict, line: str, *, autoplay: bool):
        """feature/reverse-dictation: відкрити діалог «переслухати й виправити».
        Після збереження виправлення — перебудувати список (оновлений текст +
        позначка «виправлено»)."""
        from .reverse_dictation import ReverseDictationDialog
        resolve = getattr(self.controller, "dictation_audio_path", None)
        audio_path = resolve(rec) if resolve else None
        dlg = ReverseDictationDialog(self.controller, rec, audio_path=audio_path,
                                     autoplay=autoplay, parent=self)
        dlg.exec()
        if getattr(dlg, "saved", False):
            self.refresh()
            # feature/selflearn-dict: підсумок самонавчання (тост + «Скасувати»)
            # показуємо на сторінці Історії — модалка вже закрилась
            result = getattr(dlg, "_result", None)
            if result is not None:
                from .. import learn_feedback
                learn_feedback.show(self, self.controller, result,
                                    getattr(dlg, "_profile", None))

    def _delete(self, line: str, rec: dict, card: QFrame):
        """Прибрати ОДИН запис: перезаписати файл без цього рядка + видалити його
        збережене аудіо диктування (feature/reverse-dictation), щоб не лишати
        осиротілі WAV."""
        delete_line(self.controller.profile.history_path, line)
        drop_audio = getattr(self.controller, "delete_dictation_audio", None)
        if drop_audio:
            drop_audio(rec)
        card.deleteLater()
        self._cards = [(c, t) for c, t in self._cards if c is not card]
        if not self._cards:
            self.refresh()

    def _clear_all(self):
        resp = QMessageBox.question(
            self, tr("hist_clear"), tr("hist_clear_confirm"))
        if resp == QMessageBox.Yes:
            self.controller.reset_memory()
            self.refresh()
