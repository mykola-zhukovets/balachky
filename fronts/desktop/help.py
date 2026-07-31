"""Спільне відкриття довідки: сторінка репозиторію в браузері, а офлайн —
вшитий README поруч із програмою.

Один вхід для трею і Налаштувань: нетех-користувач має дістатись інструкції
одним натисканням. Мова інтерфейсу визначає, яку версію відкрити.

ЧОМУ САМЕ ТАКИЙ ПОРЯДОК (знахідка живого тесту 25.07). Спершу код відкривав
локальний README.md через системну асоціацію — і у власника НЕ ВІДКРИВАЛОСЬ
НІЧОГО. Причина буденна: у Windows зазвичай немає програми за замовчуванням для
файлів .md, тож система тихо нічого не робила. Тепер порядок такий:
  1) сторінка репозиторію — браузер є завжди, і markdown там показано як
     людський текст із заголовками й картинками;
  2) якщо браузер не відкрився (немає мережі чи він не налаштований) — віддаємо
     локальний файл;
  3) якщо й це не спрацювало — кажемо людині, де файл лежить, замість тишею
     вдавати, що все гаразд.
"""
import logging

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from whisper_core import paths
from .i18n import current_language, tr

# Локальні файли (пакуються в збірку через datas у balachky.spec).
# Канонічний README.md — АНГЛІЙСЬКИЙ: це головна сторінка публічного
# репозиторію, і саме англійська там основна (рішення власника; так було
# від першої публікації). Українська версія — README.uk.md; README.en.md
# лишається копією англійської для сумісності з наявними посиланнями.
_LOCAL = {"uk": "README.uk.md", "en": "README.md"}
# Сторінки репо з якорем розділу «як користуватися»
_REMOTE = {
    "uk": ("https://github.com/mykola-zhukovets/balachky/"
           "blob/master/README.uk.md#використання"),
    "en": ("https://github.com/mykola-zhukovets/balachky/"
           "blob/master/README.md#usage"),
}


def open_user_guide(parent=None) -> bool:
    """Відкрити інструкцію. Повертає True, якщо щось справді відкрилось.

    ``parent`` — вікно для повідомлення, коли не вдалося нічого відкрити.
    """
    lang = "en" if current_language() == "en" else "uk"

    if QDesktopServices.openUrl(QUrl(_REMOTE[lang])):
        return True
    logging.info("Довідка: браузер не відкрився, пробуємо локальний файл")

    local = paths.bundled_doc(_LOCAL[lang])
    if local is not None and QDesktopServices.openUrl(QUrl.fromLocalFile(str(local))):
        return True

    # Ні браузер, ні системна асоціація не спрацювали — не мовчимо.
    where = str(local) if local is not None else _REMOTE[lang]
    logging.warning("Довідку відкрити не вдалося; шлях для людини: %s",
                     paths.anonymize_path(where))
    if parent is None:
        # Немає вікна — нема кому показувати. Модальне вікно без батька блокує
        # потік, а в середовищі без екрана валить процес: саме так повний прогін
        # 25.07 обривався аварією, коли тест кликав довідку напряму.
        return False
    try:
        from PySide6.QtWidgets import QMessageBox
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(tr("help_open_failed_title"))
        box.setText(tr("help_open_failed_body"))
        box.setInformativeText(where)
        box.exec()
    except Exception:
        logging.exception("Не вдалося показати повідомлення про довідку")
    return False
