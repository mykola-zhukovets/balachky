"""Уніфікований діалог згоди перед завантаженням локального компонента.

Один спільний хелпер для ВСІХ кнопок завантаження (розпізнавання мовців у
Нараді, словник автокорекції та пунктуатор у Налаштуваннях): показує, що саме
завантажується, приблизний розмір і нагадує, що компонент працює повністю
локально й його можна видалити. Так користувач один раз бачить однаковий,
чесний екран згоди замість трьох різних мовчазних кнопок.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QLabel

from .i18n import tr


def confirm_download(parent, *, name: str, size_mb, license_name=None,
                     license_url=None, license_links=None) -> bool:
    """Показати діалог згоди. name — людська назва компонента (вже локалізована),
    size_mb — приблизний розмір у МБ (число). license_name/license_url — назва
    ліцензії моделі й клікабельне посилання (обидва або жоден). ``license_links``
    — список ``(назва, url)`` для компонентів із КІЛЬКОХ моделей (діаризація: дві).
    → True, якщо користувач підтвердив завантаження; False — скасував/закрив."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Information)
    box.setWindowTitle(tr("dl_consent_title"))
    body = tr("dl_consent_body", name=name, size=size_mb)
    pairs = list(license_links or [])
    if not pairs and license_name and license_url:
        pairs = [(license_name, license_url)]
    if pairs:
        # rich-text із клікабельними посиланнями на сторінки моделей; переноси
        # звичайного тексту зберігаємо як <br>
        box.setTextFormat(Qt.RichText)
        licenses = "<br>".join(
            tr("dl_consent_license", license=n, url=u) for n, u in pairs)
        box.setText(body.replace("\n", "<br>") + "<br><br>" + licenses)
        for lbl in box.findChildren(QLabel):
            lbl.setOpenExternalLinks(True)   # клік по ліцензії → браузер
    else:
        box.setText(body)
    ok = box.addButton(tr("dl_consent_ok"), QMessageBox.AcceptRole)
    box.addButton(tr("common_cancel"), QMessageBox.RejectRole)
    box.setDefaultButton(ok)
    box.exec()
    return box.clickedButton() is ok
