"""feature/selflearn-dict: показ підсумку самонавчання після збереження
виправлення — спільне для зворотного диктування та діалогу «Розпізнано погано».

Тримає формулювання й undo в одному місці, щоб обидва потоки казали те саме.
Для успішного навчання тост несе дію «Скасувати» (10с) — справжній undo
(revoke + перечит терміни активного профілю через controller.revoke_learned)."""
from __future__ import annotations

import logging

from . import motion
from .i18n import tr


def result_text(result, name: str) -> str:
    """Людський підсумок LearnResult укр/англ (профіль name у формулюванні)."""
    st, reason, kind = result.status, result.reason, result.kind
    if st == "learned":
        if kind == "term-bias":
            return tr("sl_learned_bias", name=name, write=result.write)
        return tr("sl_learned_replace", name=name, heard=result.heard, write=result.write)
    if st == "already_learned":
        return tr("sl_already", name=name)
    if st == "failed":
        return tr("sl_failed")
    # not_learned — пояснюємо, чому правило не додано (виправлення все одно збережено)
    return {
        "contradiction": lambda: tr("sl_conflict", name=name, heard=result.heard),
        "multi_hunk": lambda: tr("sl_not_multi"),
        "ordinary_word": lambda: tr("sl_not_ordinary", heard=result.heard),
        "overlap": lambda: tr("sl_overlap"),
        "at_cap": lambda: tr("sl_cap"),
    }.get(reason, lambda: tr("sl_not_generic"))()


def show(page, controller, result, profile) -> None:
    """Показати тост підсумку на сторінці page. Успішне навчання → тост з «Скасувати»."""
    if page is None or result is None:
        return
    name = getattr(profile, "name", "")
    text = result_text(result, name)
    try:
        if result.status == "learned" and result.entry_id:
            def _undo(eid=result.entry_id, prof=profile):
                controller.revoke_learned(prof, eid)
                try:
                    motion.toast(page, tr("sl_undone"))
                except Exception:
                    logging.exception("Не вдалося показати тост скасування")
            motion.undo_toast(page, text, _undo, undo_label=tr("sl_undo"), seconds=10)
        else:
            motion.toast(page, text)
    except Exception:
        logging.exception("Не вдалося показати тост самонавчання")
