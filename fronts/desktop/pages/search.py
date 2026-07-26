"""Вкладка «Пошук»: глобальний пошук по всіх джерелах тексту застосунку —
диктування, розшифровки аудіофайлів (обидва з history.jsonl словників) і наради
(transcript.json). Індекс збирається В ПАМʼЯТІ з локальних файлів на кожен показ
сторінки (свіжий скан надійніший за кеш; нічого нікуди не відправляється —
whisper_core.search_index).

Результат клікабельний: диктування/файл → вкладка «Історія» з підставленим
запитом; нарада → вкладка «Нарада» з прокруткою до потрібної картки.
"""
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
    QStackedWidget, QFrame,
)

from whisper_core.search_index import KIND_DICTATION, KIND_FILE, KIND_MEETING

from ..glass import GlassButton, StatusTag
from ..empty_state import EmptyState
from ..i18n import tr
from . import page_header

# Вид джерела → (kind StatusTag, ключ-i18n підпису бейджа).
_KIND_BADGE = {
    KIND_DICTATION: ("queued", "search_kind_dictation"),
    KIND_FILE: ("done", "search_kind_file"),
    KIND_MEETING: ("busy", "search_kind_meeting"),
}
_MAX_RESULTS = 100


class SearchPage(QWidget):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._index = None
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        root.addLayout(page_header(tr("nav_search"), tr("search_subtitle")))
        root.addSpacing(16)

        self._search = QLineEdit()
        self._search.setPlaceholderText(tr("search_placeholder"))
        self._search.setAccessibleName(tr("search_placeholder"))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_query_changed)
        root.addWidget(self._search)
        root.addSpacing(12)

        self._count = QLabel("")
        self._count.setProperty("muted", True)
        root.addWidget(self._count)
        root.addSpacing(6)

        # стрічка результатів
        self._feedbox = QVBoxLayout()
        self._feedbox.setSpacing(12)
        self._feedbox.addStretch()
        feedhost = QWidget()
        feedhost.setLayout(self._feedbox)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setWidget(feedhost)

        empty = EmptyState("fa6s.magnifying-glass", tr("search_empty_title"),
                           tr("search_empty_hint"))
        self._empty_title = empty.title_label
        self._empty_hint = empty.hint_label

        self._stack = QStackedWidget()
        self._stack.addWidget(empty)      # 0 — порожній стан / підказка
        self._stack.addWidget(self._scroll)  # 1 — результати
        root.addWidget(self._stack, stretch=1)

        # Дебаунс: не перебудовувати результати на кожну натиснуту клавішу.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._run_search)

    def showEvent(self, event):
        """Свіжий індекс на кожен вхід: історія й наради ростуть постійно."""
        super().showEvent(event)
        self._rebuild_index()
        self._run_search()

    def _rebuild_index(self):
        try:
            self._index = self.controller.build_search_index()
        except Exception:
            from whisper_core.search_index import SearchIndex
            self._index = SearchIndex.build()

    def _on_query_changed(self, _text: str):
        self._debounce.start()

    def _run_search(self):
        self._clear_feed()
        query = self._search.text().strip()
        if self._index is None:
            self._rebuild_index()
        results = self._index.search(query, limit=_MAX_RESULTS) if query else []

        if not query:
            self._empty_title.setText(tr("search_empty_title"))
            self._empty_hint.setText(tr("search_empty_hint"))
            self._count.setText("")
            self._stack.setCurrentIndex(0)
            return
        if not results:
            self._empty_title.setText(tr("search_none_title"))
            self._empty_hint.setText(tr("search_none_hint", query=query))
            self._count.setText("")
            self._stack.setCurrentIndex(0)
            return

        self._count.setText(tr("search_count", n=len(results)))
        self._stack.setCurrentIndex(1)
        for res in results:
            self._add_card(res)

    def _clear_feed(self):
        while self._feedbox.count() > 1:
            item = self._feedbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _add_card(self, res):
        card = QFrame()
        card.setProperty("card", True)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 13, 18, 15)
        lay.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(10)
        kind_key, badge_key = _KIND_BADGE.get(res.kind, ("queued", "search_kind_dictation"))
        top.addWidget(StatusTag(kind_key, tr(badge_key)))
        meta = QLabel(self._meta_text(res))
        meta.setProperty("muted", True)
        top.addWidget(meta, stretch=1)
        lay.addLayout(top)

        body = QLabel(res.snippet)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(body)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        open_btn = GlassButton(tr("search_open"))
        open_btn.clicked.connect(lambda _=False, r=res: self._open_source(r))
        btns.addWidget(open_btn)
        btns.addStretch()
        lay.addLayout(btns)

        self._feedbox.insertWidget(self._feedbox.count() - 1, card)

    def _meta_text(self, res) -> str:
        parts = []
        if res.date:
            parts.append(time.strftime("%d.%m.%Y %H:%M", time.localtime(res.date)))
        if res.kind == KIND_MEETING:
            parts.append(res.title or tr("search_meeting_untitled"))
            if res.timecode is not None:
                parts.append(_fmt_ts(res.timecode))
        elif res.profile:
            parts.append(tr("search_from_dict", name=res.profile))
        return "  ·  ".join(parts)

    # --- навігація до джерела ---
    def _open_source(self, res):
        if res.kind == KIND_MEETING:
            self._go_to("nav_meeting")
            page = getattr(getattr(self.controller, "window", None), "meeting", None)
            if page is not None and hasattr(page, "focus_session"):
                page.focus_session(res.ref)
        else:
            self._go_to("nav_history")
            page = getattr(getattr(self.controller, "window", None), "history", None)
            if page is not None and hasattr(page, "focus_query"):
                page.focus_query(self._search.text().strip())

    def _go_to(self, page_key: str):
        """Перемкнути головне вікно на вкладку за ключем _PAGES (без хардкоду
        індексу: індекс береться з поточного порядку навігації)."""
        win = getattr(self.controller, "window", None)
        if win is None:
            return
        from ..main_window import _PAGES
        for i, (_icon, key) in enumerate(_PAGES):
            if key == page_key:
                win.set_page(i)
                return


def _fmt_ts(seconds: float) -> str:
    """Секунди → mm:ss (таймкод репліки наради)."""
    total = int(max(0.0, seconds))
    return f"{total // 60:02d}:{total % 60:02d}"
