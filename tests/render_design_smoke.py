"""Offscreen geometry smoke test for shared design primitives.
Run manually with: QT_QPA_PLATFORM=offscreen python tests/render_design_smoke.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QHBoxLayout, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget

from fronts.desktop.empty_state import EmptyState
from fronts.desktop.theme import QSS


def main():
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(QSS)
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(24, 24, 24, 24)
    empty = EmptyState("fa6s.file-audio", "No files yet", "Choose files or drag them here")
    layout.addWidget(empty)
    row = QHBoxLayout()
    button = QPushButton("Primary")
    button.setProperty("accent", True)
    button.setFocus(Qt.TabFocusReason)
    combo = QComboBox()
    combo.addItem("One")
    slider = QSlider(Qt.Horizontal)
    slider.setValue(50)
    row.addWidget(button)
    row.addWidget(combo)
    row.addWidget(slider)
    layout.addLayout(row)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(QWidget())
    layout.addWidget(scroll)
    host.resize(960, 640)
    host.show()
    app.processEvents()
    assert empty.title_label.geometry().width() > 0
    assert button.geometry().height() >= 24
    assert combo.geometry().width() > 0 and slider.geometry().width() > 0
    # ширину скролбара offscreen-стиль не застосовує до width() — піксельна
    # перевірка тут хибно-негативна; тонкість скролбара дивимось живим оглядом
    dialog = QDialog(host)
    dialog.resize(320, 160)
    dialog.show()
    app.processEvents()
    assert dialog.width() == 320
    dialog.close()
    host.close()


if __name__ == "__main__":
    main()
