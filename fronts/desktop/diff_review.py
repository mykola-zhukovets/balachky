"""feature/player-pack — «Огляд перед дією»: діалог diff перед застосуванням
автоматичних змін розшифровки (чистка філерів / автокорекція одруків).

З наших досліджень довіри: коли автоматика мовчки переписує текст, користувач
втрачає контроль. Тут — opt-in режим: РУЧНА розшифровка файлу показує, ЩО саме
змінилось (було → стало), і дає обрати «Застосувати» чи «Лишити як було».

ЛИШЕ для ручних дій. Миттєве диктування (вставка/нотатка) цей режим ІГНОРУЄ —
там модальний діалог зламав би флоу (див. звіт/налаштування).

Чиста логіка (word_diff, _render_html, chosen_text) винесена й покрита unit-
тестами; QDialog — лише тонка оболонка над нею.
"""
import difflib
import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import theme
from .i18n import tr


def word_diff(before: str, after: str):
    """Поділити на слова й порівняти: список (op, token), op ∈ {equal, removed,
    added}. Порядок — уніфікований diff (у блоці заміни спершу видалені слова,
    потім додані). Це ПОДАННЯ для ока; застосовується завжди реальний ``after``."""
    a = before.split()
    b = after.split()
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += [("equal", t) for t in a[i1:i2]]
        elif tag == "delete":
            out += [("removed", t) for t in a[i1:i2]]
        elif tag == "insert":
            out += [("added", t) for t in b[j1:j2]]
        elif tag == "replace":
            out += [("removed", t) for t in a[i1:i2]]
            out += [("added", t) for t in b[j1:j2]]
    return out


def _render_html(tokens) -> str:
    """Токени word_diff → rich-text: видалені — ALERT (закреслені), додані —
    SUCCESS (жирні), решта — звичайні. Кольори з наявних токенів теми."""
    parts = []
    for op, tok in tokens:
        esc = html.escape(tok)
        if op == "removed":
            parts.append(f'<span style="color:{theme.ALERT}; '
                         f'text-decoration:line-through;">{esc}</span>')
        elif op == "added":
            parts.append(f'<span style="color:{theme.SUCCESS}; '
                         f'font-weight:700;">{esc}</span>')
        else:
            parts.append(esc)
    return " ".join(parts)


def chosen_text(before: str, after: str, applied: bool) -> str:
    """Підсумок вибору: «Застосувати» → after, «Лишити як було» → before."""
    return after if applied else before


class DiffReviewDialog(QDialog):
    """Модальний огляд змін. `review(parent, before, after)` повертає текст, який
    треба зберегти (after при «Застосувати», before при «Лишити як було»)."""

    def __init__(self, before: str, after: str, parent=None):
        super().__init__(parent)
        self._before = before
        self._after = after
        self.setWindowTitle(tr("review_title"))
        self.setModal(True)
        self.resize(560, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        intro = QLabel(tr("review_intro"))
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        root.addWidget(intro)

        # легенда кольорів
        legend = QLabel(
            f'<span style="color:{theme.ALERT}; text-decoration:line-through;">'
            f'{html.escape(tr("review_legend_removed"))}</span>'
            "&nbsp;&nbsp;&nbsp;"
            f'<span style="color:{theme.SUCCESS}; font-weight:700;">'
            f'{html.escape(tr("review_legend_added"))}</span>')
        legend.setTextFormat(Qt.RichText)
        root.addWidget(legend)

        body = QLabel()
        body.setTextFormat(Qt.RichText)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        body.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        body.setText('<p style="line-height:165%; margin:0;">'
                     + _render_html(word_diff(before, after)) + "</p>")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(4, 4, 4, 4)
        wl.addWidget(body)
        wl.addStretch()
        scroll.setWidget(wrap)
        root.addWidget(scroll, stretch=1)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch()
        keep = QPushButton(tr("review_keep"))
        keep.clicked.connect(self.reject)              # «Лишити як було»
        btns.addWidget(keep)
        apply_btn = QPushButton(tr("review_apply"))
        apply_btn.setProperty("accent", True)
        apply_btn.setDefault(True)
        apply_btn.clicked.connect(self.accept)         # «Застосувати»
        btns.addWidget(apply_btn)
        root.addLayout(btns)

    @classmethod
    def review(cls, parent, before: str, after: str) -> str:
        """Показати діалог; повернути обраний текст. Блокує (модальний)."""
        dlg = cls(before, after, parent)
        applied = dlg.exec() == QDialog.Accepted
        dlg.deleteLater()
        return chosen_text(before, after, applied)
