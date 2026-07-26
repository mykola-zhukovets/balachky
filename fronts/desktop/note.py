"""feature/scratchpad-note — плаваюча нотатка: диктування у власне вікно.

Fallback до PTT: коли нема активного текстового поля (або просто хочеш зібрати
думки), диктуєш у невелике always-on-top вікно, а не в чужий застосунок. Текст
додається (append) у буфер, що живе в контролері (переживає закриття вікна в
межах сесії, але НЕ перезапуск — жодних нових файлів стану).

Тут:
- `append_note` — чиста логіка додавання рядка в буфер (тестується без Qt);
- `NoteHotkey` — необовʼязкова глобальна комбінація, що відкриває вікно (opt-in);
- `NoteWindow` — саме вікно у гамі «Мундір».
"""
import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QVBoxLayout,
    QWidget,
)

from .glass import GlassButton, RecButton
from .i18n import tr
from . import theme   # нічний режим: локальний стиль редактора теж перечитує палітру


def append_note(buffer: str, text: str) -> str:
    """Додати продиктований рядок у буфер нотатки. Кожна фраза — з нового рядка
    (нотатка читається як список реплік). Порожній/пробільний text — без змін."""
    text = (text or "").strip()
    if not text:
        return buffer
    if not buffer:
        return text
    sep = "" if buffer.endswith("\n") else "\n"
    return buffer + sep + text


class NoteHotkey(QObject):
    """Одна глобальна комбінація → сигнал triggered (відкрити нотатку). На відміну
    від PTT-Hotkey тут лише «натиснуто» (вікно відкривається одним разом), і stop()
    знімає ЛИШЕ свій хук (keyboard.remove_hotkey), не чіпаючи PTT."""

    triggered = Signal()

    def __init__(self, key: str):
        super().__init__()
        self._key = key
        self._handle = None

    def start(self) -> bool:
        import keyboard   # лениво: legacy-бекенд; native цю бібліотеку не вантажить
        try:
            # suppress=True: клавіша комбінації не «протікає» у активне вікно
            self._handle = keyboard.add_hotkey(
                self._key, self.triggered.emit, suppress=True)
            return True
        except Exception:
            logging.exception("Не вдалося повісити хук нотатки на «%s»", self._key)
            self._handle = None
            return False

    def stop(self):
        if self._handle is not None:
            try:
                import keyboard
                keyboard.remove_hotkey(self._handle)
            except (KeyError, ValueError):
                pass                # уже знято (напр. після PTT-rebind → unhook_all)
            except Exception:
                logging.exception("Не вдалося зняти хук нотатки")
            self._handle = None


class NoteWindow(QWidget):
    """Плаваюче always-on-top вікно нотатки. Джерело правди для тексту —
    контролер (controller.note_text/note_set_buffer): вікно лише показує й
    редагує його. Кнопка мікрофона диктує через наявний рушій (взаємно виключно
    з PTT — гейти в контролері)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(tr("note_title"))
        # окреме верхнє вікно поверх усіх; рамку лишаємо рідну (перетягування/закриття)
        self.setWindowFlags(Qt.Window | Qt.WindowStaysOnTopHint)
        # закриття знищує вікно → знімає з'єднання note_state; reopen будує свіже
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(360, 380)
        self.resize(420, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        hint = QLabel(tr("note_hint"))
        hint.setProperty("muted", True)
        hint.setWordWrap(True)
        root.addWidget(hint)

        self._editor = QPlainTextEdit()
        self._editor.setPlaceholderText(tr("note_placeholder"))
        # QSS не стилізує QPlainTextEdit — локальний стиль у гамі «Мундір»
        self._apply_editor_style()
        theme.register_restyle(self._apply_editor_style)   # нічний режим
        self._editor.textChanged.connect(self._on_text_changed)
        root.addWidget(self._editor, stretch=1)

        # --- рядок диктування: мікрофон + статус ---
        recrow = QHBoxLayout()
        recrow.setSpacing(12)
        self._rec_btn = RecButton()
        self._rec_btn.setToolTip(tr("note_rec_start"))
        self._rec_btn.setAccessibleName(tr("note_rec_start"))
        self._rec_btn.clicked.connect(self._on_mic)
        recrow.addWidget(self._rec_btn)
        self._status = QLabel("")
        self._status.setProperty("muted", True)
        recrow.addWidget(self._status, stretch=1)
        root.addLayout(recrow)

        # --- дії над нотаткою ---
        actions = QHBoxLayout()
        actions.setSpacing(10)
        copy_btn = GlassButton(tr("note_copy_all"))
        copy_btn.clicked.connect(self._on_copy)
        save_btn = GlassButton(tr("note_save"))
        save_btn.clicked.connect(self._on_save)
        clear_btn = GlassButton(tr("note_clear"))
        clear_btn.clicked.connect(self._on_clear)
        actions.addWidget(copy_btn)
        actions.addWidget(save_btn)
        actions.addStretch()
        actions.addWidget(clear_btn)
        root.addLayout(actions)

        self.set_text(controller.note_text())
        controller.note_state.connect(self._on_state)
        self._on_state(controller.note_state_value())

    # --- текст (контролер ↔ редактор) ---
    def set_text(self, text: str):
        """Показати текст із буфера, не смикаючи textChanged (щоб не було петлі)."""
        self._editor.blockSignals(True)
        self._editor.setPlainText(text or "")
        self._editor.blockSignals(False)
        cur = self._editor.textCursor()
        cur.movePosition(cur.MoveOperation.End)
        self._editor.setTextCursor(cur)

    def _apply_editor_style(self):
        self._editor.setStyleSheet(
            f"QPlainTextEdit {{ background: {theme.DEEP}; color: {theme.TEXT_BODY};"
            f" border: 1px solid {theme._LINE_SOFT}; border-radius: 6px; padding: 10px; }}"
            f" QPlainTextEdit:focus {{ border: 2px solid {theme.FOCUS}; }}")

    def _on_text_changed(self):
        # ручні правки користувача → у буфер контролера (джерело правди)
        self.controller.note_set_buffer(self._editor.toPlainText())

    # --- диктування ---
    def _on_mic(self):
        self.controller.note_record_toggle()

    def _on_state(self, state: str):
        recording = state == "recording"
        busy = state == "busy"
        self._rec_btn.set_recording(recording)
        self._rec_btn.setEnabled(not busy)      # під час розшифровки — не чіпати
        self._rec_btn.setToolTip(tr("note_rec_stop") if recording
                                 else tr("note_rec_start"))
        if recording:
            self._status.setText(tr("note_status_recording"))
        elif busy:
            self._status.setText(tr("note_status_busy"))
        else:
            self._status.setText("")

    # --- дії ---
    def _on_copy(self):
        text = self.controller.note_text()
        QApplication.clipboard().setText(text)
        self.controller.tray.notify(tr("note_copied"))

    def _on_save(self):
        path, _flt = QFileDialog.getSaveFileName(
            self, tr("note_save_title"), "note.txt", tr("note_save_filter"))
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.controller.note_text())
        except OSError:
            logging.exception("Не вдалося зберегти нотатку у %s", path)
            self.controller.tray.notify(tr("note_save_fail"))
            return
        import os
        self.controller.tray.notify(tr("note_saved", name=os.path.basename(path)))

    def _on_clear(self):
        self.controller.note_clear()
        self.set_text("")

    def closeEvent(self, event):
        self.controller.note_on_window_closed()
        super().closeEvent(event)


def center_on_screen(win) -> None:
    """Розмістити вікно по центру доступного екрана (перше відкриття)."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        return
    geo = screen.availableGeometry()
    fr = win.frameGeometry()
    fr.moveCenter(geo.center())
    win.move(fr.topLeft())
