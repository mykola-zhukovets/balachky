"""RecordActionBar — спільний рядок дій над записом.

Канон побудови сторінок (30.07, п.4) і специфікація спільних компонентів
(31.07): один компонент замість трьох різних реалізацій дій над записом
(перейменувати / показати в теці / надіслати-зберегти / [дія сторінки] —
addStretch() — видалити). Перший споживач — Запис екрана (найбільша дірка:
там дій не було ЗОВСІМ), далі — Аудіофайли.

Обов'язкові умови канону, втілені тут:
  - усі кнопки ЗАВЖДИ видимі (жодного hover-only QSS) — користувач часто на
    чужому ноутбуці, сенсорному екрані або з клавіатури;
  - «Видалити» фізично відокремлене від безпечних дій через addStretch();
  - підтвердження видалення називає наслідок словами (назва + шлях +
    «незворотно»), а не просто «Ви впевнені?».

Компонент НЕ виконує файлових операцій сам — він лише збирає намір
користувача (сигнали) і будує підтвердження за каноном. Справжню роботу
(перейменувати на диску, видалити, скопіювати) виконує власник картки —
контролер сторінки. Це навмисно: для Наради «видалити» — це не unlink
одного файлу, а видалення сесії з журналом цілісності/доказовим пакетом;
підключити той самий бар туди можна буде, підмінивши обробники сигналів,
без жодної зміни в самому барі.
"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QWidget

import qtawesome as qta

from .glass import FlowLayout, GlassButton
from .i18n import tr

_FORBIDDEN_NAME_CHARS = '\\/:*?"<>|'


def is_safe_display_name(name: str) -> bool:
    """Ім'я, яке безпечно перетворити на файлову назву (без розширення):
    непорожнє, без роздільників шляху й спецсимволів Windows, не самі крапки
    (".", ".." — traversal через Path.with_name, який САМ не блокує ".."). Ця
    сама перевірка знадобиться майбутнім споживачам бару (Аудіофайли/Нарада)."""
    name = (name or "").strip()
    if not name or name.strip(".") == "":
        return False
    return not any(ch in name for ch in _FORBIDDEN_NAME_CHARS)


class RecordActionBar(QWidget):
    """Рядок дій над одним записом. `display_name` — назва без розширення
    (показується в діалозі перейменування); `path_text` — шлях, що показується
    у підтвердженні видалення. `extra_widget` — специфічна дія сторінки
    («Дивитися відео» / «Транскрибувати» тощо), вставляється між «Надіслати»
    і роздільником."""

    rename_requested = Signal(str)       # нова назва (без розширення), вже перевірена
    show_in_folder_requested = Signal()
    save_as_requested = Signal()
    copy_path_requested = Signal()
    delete_requested = Signal()          # ЛИШЕ після підтвердження "Так"

    def __init__(self, display_name: str, path_text: str, *,
                 extra_widget: "QWidget | None" = None, parent=None):
        super().__init__(parent)
        self._display_name = display_name
        self._path_text = path_text

        # FlowLayout, а не QHBoxLayout: живий тест 31.07 на реальній ширині
        # вікна показав, що пʼять кнопок із довгими українськими підписами
        # («Перейменувати», «Надіслати / Зберегти») стискаються нижче свого
        # природного розміру і текст обрізається просто посеред слова.
        # Перенос на новий рядок лишає кожну кнопку читабельною за будь-якої
        # ширини — той самий рецепт, що вже застосований до чіпів наради.
        row = FlowLayout(self, spacing=8)

        self.rename_btn = GlassButton(tr("recact_rename"))
        self.rename_btn.setIcon(qta.icon("fa6s.pen"))
        self.rename_btn.setAccessibleName(tr("recact_rename"))
        self.rename_btn.clicked.connect(self._ask_rename)
        row.addWidget(self.rename_btn)

        self.folder_btn = GlassButton(tr("recact_show_in_folder"))
        self.folder_btn.setIcon(qta.icon("fa6s.folder-open"))
        self.folder_btn.setAccessibleName(tr("recact_show_in_folder"))
        self.folder_btn.clicked.connect(self.show_in_folder_requested)
        row.addWidget(self.folder_btn)

        self.share_btn = GlassButton(tr("recact_share"))
        self.share_btn.setIcon(qta.icon("fa6s.share-from-square"))
        self.share_btn.setAccessibleName(tr("recact_share"))
        menu = QMenu(self.share_btn)
        act_save = menu.addAction(tr("recact_save_as"))
        act_save.triggered.connect(self.save_as_requested)
        act_copy = menu.addAction(tr("recact_copy_path"))
        act_copy.triggered.connect(self.copy_path_requested)
        self.share_btn.setMenu(menu)
        row.addWidget(self.share_btn)

        if extra_widget is not None:
            row.addWidget(extra_widget)

        # Канон п.4 (незворотне — віддалене від безпечних дій): розпір тут
        # неможливий (FlowLayout його не має), тож «Видалити» лишається
        # ОСТАННЬОЮ кнопкою ряду і єдиною в ghost-стилі — випадково влучити
        # в неї так само важко, а текст більше не ріжеться.
        self.delete_btn = GlassButton(tr("recact_delete"))
        self.delete_btn.setProperty("ghost", True)
        self.delete_btn.setIcon(qta.icon("fa6s.trash-can"))
        self.delete_btn.setAccessibleName(tr("recact_delete"))
        self.delete_btn.clicked.connect(self._ask_delete)
        row.addWidget(self.delete_btn)

    # --- синхронізація підпису після успішної дії власника картки ---
    def set_display_name(self, name: str) -> None:
        self._display_name = name

    def set_path_text(self, text: str) -> None:
        self._path_text = text

    # --- діалоги за каноном ---
    def _ask_rename(self) -> None:
        text, ok = QInputDialog.getText(
            self, tr("recact_rename"), tr("recact_rename_prompt"),
            text=self._display_name)
        text = text.strip()
        if ok and text and text != self._display_name and is_safe_display_name(text):
            self.rename_requested.emit(text)

    def _ask_delete(self) -> None:
        resp = QMessageBox.question(
            self, tr("recact_delete"),
            tr("recact_delete_confirm", name=self._display_name, path=self._path_text),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if resp == QMessageBox.Yes:
            self.delete_requested.emit()
