"""Ручна render-проба waveform; цей файл навмисно поза unittest discover."""
import os
import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if __name__ == "__main__":
    from PySide6.QtWidgets import QApplication
    from fronts.desktop.audio_editor import Waveform
    app = QApplication.instance() or QApplication([])
    w = Waveform(np.sin(np.linspace(0, 80, 32000, dtype=np.float32))[:, None], 16000)
    w.resize(760, 140); w.show(); app.processEvents()
    w.grab().save("waveform-render.png")
