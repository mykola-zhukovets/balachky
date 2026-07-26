"""Сторінки головного вікна (по файлу на вкладку) + спільна шапка сторінки."""
from PySide6.QtWidgets import QLabel, QVBoxLayout

from ..theme import spaced


def page_header(title: str, subtitle: str) -> QVBoxLayout:
    """Editorial-шапка сторінки: великий H1 + приглушений підзаголовок."""
    box = QVBoxLayout()
    box.setSpacing(6)
    h1 = QLabel(title)
    h1.setProperty("h1", True)
    h1.setObjectName("pageHeaderH1")
    h1.setWordWrap(True)            # канон §3: заголовок не обрізається на вузькому вікні
    sub = QLabel(subtitle)
    sub.setProperty("pagesub", True)
    spaced(sub)                     # просторіший міжрядковий у підзаголовку сторінки
    box.addWidget(h1)
    box.addWidget(sub)
    return box
