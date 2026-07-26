"""Діалог «Розпізнано погано…» — збирач корпусу точності (feature/accuracy-corpus).

Показує розпізнаний текст (як є) і поле «Як мало бути» (передзаповнене тим самим
текстом) → зберігає пару в локальний корпус через controller.save_corpus_sample.
Уся дискова логіка й формат — у whisper_core.corpus (під юнітами); тут лише Qt.

report_bad() — спільна точка входу для стрічки Диктування, Аудіофайлів та Історії.
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QLabel, QVBoxLayout, QHBoxLayout, QPlainTextEdit, QPushButton,
)

from . import motion
from .glass import GlassButton
from .i18n import tr


class CorpusReportDialog(QDialog):
    """Модалка: розпізнаний текст (read-only) + правка «Як мало бути»."""

    def __init__(self, recognized: str, parent=None):
        super().__init__(parent)
        self.corrected = None            # заповнюється на «Зберегти»
        self.setWindowTitle(tr("corpus_dlg_title"))
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        intro = QLabel(tr("corpus_dlg_intro"))
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        lay.addWidget(intro)

        lay.addWidget(QLabel(tr("corpus_dlg_recognized")))
        heard = QLabel(recognized or "—")
        heard.setWordWrap(True)
        heard.setProperty("muted", True)
        heard.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(heard)

        lay.addWidget(QLabel(tr("corpus_dlg_corrected")))
        self._edit = QPlainTextEdit(recognized or "")
        self._edit.setMinimumHeight(90)
        lay.addWidget(self._edit)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        save = GlassButton(tr("corpus_dlg_save"))
        save.clicked.connect(self._accept)
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)

    def _accept(self):
        text = self._edit.toPlainText().strip()
        if not text:
            return                       # порожнє виправлення марне — не закриваємо
        self.corrected = text
        self.accept()


def report_bad(page, controller, recognized: str, *, ts=None, src_wav=None,
               source: str = "desktop") -> bool:
    """Відкрити діалог і — якщо збережено — покласти зразок у корпус. Повертає
    True, коли зразок записано. page — сторінка-власник (для toast/parent)."""
    # feature/selflearn-dict: ЗНІМОК активного словника на момент відкриття діалогу.
    # Тумблер-меню трею — це QMenu-попап, який Qt НЕ блокує під модальністю, тож
    # користувач може перемкнути активний словник, поки модалка «Розпізнано погано»
    # відкрита. Захоплюємо профіль ДО exec(), щоб і навчання, і його «Скасувати»
    # пішли саме в цей словник, а не в новоактивний (ізоляція по словниках —
    # дзеркало зворотного диктування).
    profile = getattr(controller, "profile", None)
    dlg = CorpusReportDialog(recognized, parent=page)
    if dlg.exec() != QDialog.Accepted or not dlg.corrected:
        return False
    rec = controller.save_corpus_sample(
        recognized, dlg.corrected, ts=ts, src_wav=src_wav, source=source,
        profile=profile)
    if rec is None:
        try:
            motion.toast(page, tr("corpus_toast_failed"))
        except Exception:
            logging.exception("Не вдалося показати toast корпусу")
        return False
    # feature/selflearn-dict: та сама дія САМЕ вчить ЗАХОПЛЕНИЙ словник безпечним
    # правилом (diff recognized→corrected). Якщо правило вивчено — показуємо його
    # підсумок (тост + «Скасувати»); інакше лишаємо звичайний тост «приклад додано».
    learn = getattr(controller, "learn_from_report", None)
    result = (learn(recognized, dlg.corrected, source=source, profile=profile)
              if learn else None)
    try:
        if result is not None and result.status in ("learned", "already_learned"):
            from . import learn_feedback
            learn_feedback.show(page, controller, result, profile)
        else:
            motion.toast(page, tr("corpus_toast_saved"))
    except Exception:
        logging.exception("Не вдалося показати toast корпусу")
    return True
