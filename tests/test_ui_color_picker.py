"""Тести розділу вибору кольору інтерфейсу у Налаштуваннях (feature/ui-color-picker)."""
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtGui import QColor

from whisper_core.config import Config
from fronts.desktop import theme, i18n
from fronts.desktop.i18n import STRINGS
from fronts.desktop.pages.settings import SettingsPage, _color_swatch_icon


@pytest.fixture
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


@pytest.fixture
def uk_language():
    language = i18n.current_language()
    i18n.set_language("uk")
    yield
    i18n.set_language(language)


@pytest.fixture(autouse=True)
def reset_theme():
    """Скинути тему до classic після кожного тесту."""
    yield
    theme.set_ui_color("classic")


class DummySignal:
    def connect(self, slot): pass
    def disconnect(self, slot=None): pass
    def emit(self, *args, **kwargs): pass
    def __call__(self, *args, **kwargs): return None


class DummyController:
    def __init__(self, cfg=None):
        self.cfg = cfg or Config()
        self.saved = False
        self.window = None
        self._ctx_matcher = None

    def save_config(self):
        self.saved = True

    def get_models_info(self): return {}
    def update_state(self): return "1.0.0", "1.0.0", "https://example/rel", False
    def delivery_state(self): return None, None, None
    def list_voice_memories(self): return []
    def list_meeting_screen_monitors(self): return []
    def list_meetings(self): return []
    def list_recordings(self): return []
    def corpus_count(self): return 0

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return DummySignal()


def test_i18n_language_parity_and_cleanup():
    """Перевірка паритету ключів set_ui_color_* між uk та en і відсутності set_night_mode."""
    uk_dict = STRINGS["uk"]
    en_dict = STRINGS["en"]

    assert "set_night_mode" not in uk_dict, "set_night_mode не має бути в uk"
    assert "set_night_mode" not in en_dict, "set_night_mode не має бути в en"

    ui_color_keys_uk = {k for k in uk_dict if k.startswith("set_ui_color_")}
    ui_color_keys_en = {k for k in en_dict if k.startswith("set_ui_color_")}

    assert len(ui_color_keys_uk) > 0, "Мають бути ключі set_ui_color_* в uk"
    assert ui_color_keys_uk == ui_color_keys_en, "Паритет ключів set_ui_color_* між uk та en"


def test_ui_color_controls_accessibility(qapp, uk_language):
    """Кожен новий контрол має accessibleName та toolTip."""
    ctrl = DummyController()
    page = SettingsPage(ctrl)

    choice = page._ui_color_choice
    assert choice.accessibleName() == "Колір інтерфейсу"
    assert choice.toolTip() == "Колір інтерфейсу"
    assert page._ui_color_hint.text() == (
        "Для роботи в темряві вибирайте червоний — решту кольорів видно збоку."
    )


def test_preset_selection_changes_palette_and_config(qapp):
    """Вибір пресету в UI змінює активну палітру та зберігає в конфіг."""
    cfg = Config(ui_color="classic")
    ctrl = DummyController(cfg)
    page = SettingsPage(ctrl)

    # Початковий стан — classic
    assert theme.current_ui_color() == "classic"
    gold_classic = theme._P["GOLD"]

    # Обираємо red (індекс 1)
    idx_red = page._ui_color_choice.findData("red")
    assert idx_red >= 0
    page._ui_color_choice.setCurrentIndex(idx_red)

    assert theme.current_ui_color() == "red"
    assert theme._P["GOLD"] != gold_classic
    assert cfg.ui_color == "red"
    assert ctrl.saved is True

    # Обираємо teal
    idx_teal = page._ui_color_choice.findData("teal")
    assert idx_teal >= 0
    page._ui_color_choice.setCurrentIndex(idx_teal)

    assert theme.current_ui_color() == "teal"
    assert cfg.ui_color == "teal"


def test_config_restore_on_page_load(qapp):
    """Вибір зберігається у конфізі й піднімається при створенні сторінки."""
    cfg = Config(ui_color="teal")
    ctrl = DummyController(cfg)
    page = SettingsPage(ctrl)

    assert page._ui_color_choice.currentData() == "teal"
    assert page._current_ui_color == "teal"


def test_custom_color_selection_success(qapp, monkeypatch):
    """Власний колір з валідним hue доходить до set_ui_color і зберігається."""
    cfg = Config(ui_color="classic")
    ctrl = DummyController(cfg)
    page = SettingsPage(ctrl)

    # Симулюємо діалог QColorDialog, який повертає колір з hue = 180 (бирюзово-зелений)
    fake_color = QColor.fromHsv(180, 200, 200)
    monkeypatch.setattr("PySide6.QtWidgets.QColorDialog.getColor", lambda *args, **kwargs: fake_color)

    idx_custom = page._ui_color_choice.findData("custom")
    page._ui_color_choice.setCurrentIndex(idx_custom)

    assert theme.current_ui_color() == 180
    assert cfg.ui_color == 180
    assert ctrl.saved is True


def test_gray_and_invalid_contrast_color_rejection(qapp, monkeypatch):
    """Сірий колір (hue -1) або колір без контрасту показують повідомлення і лишають вибір."""
    cfg = Config(ui_color="classic")
    ctrl = DummyController(cfg)
    page = SettingsPage(ctrl)

    warning_shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warning_shown.append(args))

    # Case 1: Сірий колір (hue -1)
    gray_color = QColor(128, 128, 128)
    assert gray_color.hue() == -1
    monkeypatch.setattr("PySide6.QtWidgets.QColorDialog.getColor", lambda *args, **kwargs: gray_color)

    idx_custom = page._ui_color_choice.findData("custom")
    page._ui_color_choice.setCurrentIndex(idx_custom)

    assert len(warning_shown) == 1
    assert theme.current_ui_color() == "classic"
    assert page._ui_color_choice.currentData() == "classic"

    # Case 2: Неможливий контраст (симулюємо RuntimeError при build_palette_for_hue)
    valid_color = QColor.fromHsv(100, 200, 200)
    monkeypatch.setattr("PySide6.QtWidgets.QColorDialog.getColor", lambda *args, **kwargs: valid_color)

    def fake_build_palette(hue):
        raise RuntimeError("неможливо підтягнути контраст")

    monkeypatch.setattr(theme, "build_palette_for_hue", fake_build_palette)

    page._ui_color_choice.setCurrentIndex(idx_custom)
    assert len(warning_shown) == 2
    assert theme.current_ui_color() == "classic"


def test_connection_proof_with_early_return(qapp, monkeypatch):
    """ДОКАЗ З'ЄДНАННЯ: ранній return у set_ui_color ламає сценарій вибору в Налаштуваннях."""
    cfg = Config(ui_color="classic")
    ctrl = DummyController(cfg)
    page = SettingsPage(ctrl)

    # Спочатку перевіряємо, що у нормальному стані вибір працює
    idx_red = page._ui_color_choice.findData("red")
    page._ui_color_choice.setCurrentIndex(idx_red)
    assert theme.current_ui_color() == "red"

    # Скидаємо в classic
    theme.set_ui_color("classic")
    page._sync_ui_color_choice_selection("classic")

    # Патчимо set_ui_color на ранній return (нічого не робить)
    monkeypatch.setattr(theme, "set_ui_color", lambda color: None)

    # Натискаємо у Налаштуваннях вибір "red"
    page._on_ui_color_choice_changed(idx_red)

    # ДОКАЗ: активний колір у theme ЛАМАЄТЬСЯ (лишився classic, бо контрол з'єднаний через set_ui_color)
    assert theme.current_ui_color() == "classic"
