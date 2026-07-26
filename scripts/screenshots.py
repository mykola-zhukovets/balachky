"""Чисті скріншоти вікна для README → docs/screenshots/*.png.

Жодного приватного вмісту: профілі — тимчасова пісочниця з фейковими даними,
конфіг — дефолтний Config() (config.toml користувача НЕ читається).
Знімок — системний (PIL.ImageGrab), кроп по DWMWA_EXTENDED_FRAME_BOUNDS:
на відміну від widget.grab(), показує Mica-скло.

Запуск із кореня репо:
  .venv\\Scripts\\python scripts\\screenshots.py
  .venv\\Scripts\\python scripts\\screenshots.py <output-dir> --lang=en
"""
import os
# Цей скрипт РЕНДЕРИТЬ РЕАЛЬНЕ вікно (DWM/DirectWrite): offscreen тут заборонений
# каноном — бреше про шрифти й дає порожній кадр. Модулі tests/render_*_smoke
# роблять os.environ.setdefault("QT_QPA_PLATFORM", "offscreen") на імпорті, а ми
# один із них імпортуємо (FakeController). Тому ФІКСУЄМО платформу ДО будь-якого
# такого імпорту й до QApplication — інакше setdefault виграє, вікно рендериться
# offscreen, межі DWM порожні й grab падає з «cannot write empty image».
os.environ["QT_QPA_PLATFORM"] = "windows"
import ctypes
import ctypes.wintypes
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication

from whisper_core import profiles

# аргументи: позиційний — інша тека виводу (напр., preview/ для ітерацій дизайну);
# --states — додаткові кадри станів кнопок (disabled/focus) для Блоку 5 рубрики;
# --lang=uk|en — мова інтерфейсу й безпечних демонстраційних даних.
_POS = [a for a in sys.argv[1:] if not a.startswith("--")]
_FLAGS = {a for a in sys.argv[1:] if a.startswith("--")}
STATES = "--states" in _FLAGS
LANG = next((a.partition("=")[2] for a in _FLAGS if a.startswith("--lang=")), "uk")
if LANG not in {"uk", "en"}:
    sys.exit("--lang має бути uk або en.")
OUT = Path(_POS[0]).resolve() if _POS else ROOT / "docs" / "screenshots"

FAKE_TERMS = {
    "uk": """\
# Демо-словник для скриншотів (жодних приватних даних)
[terms]
Fable = ["фейбл", "файбл"]
GitHub = ["гітхаб", "гіт хаб"]
Cowork = ["коворк"]
Python = ["пайтон", "пітон"]
"ретранслятор" = ["ретрансльатор"]
""",
    "en": """\
# Demo dictionary for screenshots (no private data)
[terms]
Fable = ["fayble"]
GitHub = ["git hub"]
Cowork = ["co work"]
Python = ["pie thon"]
"dashboard" = ["dash board"]
""",
}

# частоти: нейромережа x4, дашборд x3, фронтенд x2 → кандидати вкладки «Словники»;
# ts/final/source — для вкладки «Історія» (час, текст, позначка «з файлу»)
FAKE_HISTORY = {
    "uk": [
        ("заливаю нову збірку нейромережа впала на другому кроці", "desktop"),
        ("нейромережа знову перевчилась подивись дашборд", "desktop"),
        ("додай на дашборд графік по фронтенд збірках", "file"),
        ("нейромережа готова фронтенд теж", "desktop"),
        ("дашборд оновив нейромережа в проді", "desktop"),
    ],
    "en": [
        ("uploading the new build the neural network failed on step two", "desktop"),
        ("the neural network finished training check the dashboard", "desktop"),
        ("add a frontend build chart to the dashboard", "file"),
        ("the neural network is ready and so is the frontend", "desktop"),
        ("dashboard updated the neural network is in production", "desktop"),
    ],
}


def make_sandbox() -> Path:
    """Тимчасовий profiles/-корінь із двома фейковими словниками."""
    tmp = Path(tempfile.mkdtemp(prefix="balachky-shots-"))
    proot = tmp / "profiles"
    default = proot / "default"
    default.mkdir(parents=True)
    (default / "terms.toml").write_text(FAKE_TERMS[LANG], encoding="utf-8")
    import time
    now = round(time.time())
    history = FAKE_HISTORY[LANG]
    (default / "history.jsonl").write_text(
        "\n".join(json.dumps(
            {"ts": now - (len(history) - i) * 5400,
             "raw": raw, "final": raw, "source": src}, ensure_ascii=False)
            for i, (raw, src) in enumerate(history)) + "\n",
        encoding="utf-8")
    (default / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    kolega = proot / "kolega"
    kolega.mkdir()
    (kolega / "terms.toml").write_text(
        '[terms]\nFable = ["фейбл"]\nGitHub = ["гітхаб"]\n', encoding="utf-8")
    (kolega / "profile.json").write_text('{"memory": true}', encoding="utf-8")
    (proot / "state.json").write_text('{"active": "default"}', encoding="utf-8")
    return tmp


class _FakeRecorder:
    """Заглушка мікрофона для скріншотів: сталий рівень для кадру смужки.
    Лінійна амплітуда → тіло смужки ~0.6, пікова риска ~0.78 (без кліпу)."""
    def take_meter(self):
        return 0.063, 0.22


class _FakeTray:
    """No-op частина DesktopApp-контракту для settings/closeEvent."""

    def sync_animations(self):
        pass

    def notify(self, _text):
        pass


from tests.render_nav_smoke import _NavController   # повний контракт 7 сторінок


class FakeController(_NavController):
    """Контракт DesktopApp для MainWindow: успадковує повний стаб 7 сторінок
    із render_nav_smoke + демо-надбудови для гарних кадрів."""

    def __init__(self, sandbox: Path):
        super().__init__(sandbox)

    def enqueue_file(self, path, **kw) -> int:
        self._jobs += 1
        return self._jobs

    def update_state(self):
        # демо-стан «є нова версія» — щоб на знімку було видно рядок оновлення,
        # золотий текст і кнопку «Завантажити» (жодної реальної мережі)
        return "1.0.0", "1.1.0", \
            "https://github.com/mykola-zhukovets/balachky/releases/latest", True


def grab_window(win, out_path: Path):
    """Системний скріншот, кроп по видимих межах вікна (без тіні DWM)."""
    from PIL import ImageGrab
    rect = ctypes.wintypes.RECT()
    DWMWA_EXTENDED_FRAME_BOUNDS = 9
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        int(win.winId()), DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect))
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    img.save(out_path, optimize=True)
    shown = out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path
    print(f"  {shown}  {img.size[0]}x{img.size[1]}  "
          f"{out_path.stat().st_size // 1024} КБ")


def main():
    if sys.platform != "win32":
        sys.exit("Тільки Windows (DWM/Mica).")
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)

    from fronts.desktop import i18n, theme
    from fronts.desktop.theme import QSS, load_fonts
    i18n.set_language(LANG)
    load_fonts()
    app.setStyleSheet(QSS)
    theme.apply_link_colors(app)   # як app.py на старті: роль Link = акцент теми
    # іконка вікна/таскбару за активною темою (як app.py): удень оригінал, уночі
    # моно-червоний силует — інакше золотий жук світився б у нічному титулбарі
    from fronts.desktop.main_window import app_icon
    app.setWindowIcon(app_icon(ROOT / "assets" / "balachky.ico"))

    sandbox = make_sandbox()
    # вкладка «Словники» читає profiles.list_profiles(ROOT) — переспрямовуємо
    # у пісочницю, щоб СПРАВЖНІ словники користувача не потрапили на знімок
    _orig_list = profiles.list_profiles
    profiles.list_profiles = lambda root=None: _orig_list(sandbox)

    ctrl = FakeController(sandbox)
    ctrl.cfg.backdrop = "auto"
    ctrl.cfg.language = LANG
    ctrl.cfg.ui_language = LANG

    # суцільний темний фон на весь екран: у крайні пікселі вікна (межа,
    # заокруглені кути) не просочиться вміст чужих вікон позаду
    matte = QWidget()
    matte.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    matte.setStyleSheet("background: #202020;")
    matte.setGeometry(QGuiApplication.primaryScreen().geometry())
    matte.show()

    from fronts.desktop.main_window import MainWindow, FileStatus
    win = MainWindow(ctrl)
    win.setWindowState(Qt.WindowNoState)     # QSettings міг відновити maximized
    # Клієнтська область ~FullHD: канон Миколи — «особистий перегляд на 1920».
    # 1856×1044 логічних + рамка/титул DWM (~кілька px з боків, ~32 зверху) дає
    # знімок близько 1872×1084 — верстку бачимо в тій щільності, що й користувач
    # на 1920×1080@100%. Було 1000×640 (знімок ~1707×999 після масштабу) — надто
    # тісно, кадри не відповідали канону 1920. Влазить у 1920×1080 з відступом.
    win.resize(1856, 1044)
    win.move(24, 16)
    win.setWindowFlag(Qt.WindowStaysOnTopHint, True)  # ніхто не перекриє знімок

    # --- Диктування: 3 фейк-картки (додаються ПІСЛЯ кадру порожнього стану) ---
    def populate_dictation():
        samples = {
            "uk": [
                ("залив реліз на гітхаб", "залив реліз на GitHub",
                 [("залив", 0.95), ("реліз", 0.9), ("на", 0.99), ("гітхаб", 0.82)]),
                ("запиши думку про новий дашборд поки не забув",
                 "запиши думку про новий дашборд поки не забув",
                 [("запиши", 0.93), ("думку", 0.9), ("про", 0.98), ("новий", 0.95),
                  ("дашборд", 0.42), ("поки", 0.96), ("не", 0.99), ("забув", 0.94)]),
                ("зустріч перенесли на п'ятницю о третій",
                 "зустріч перенесли на п'ятницю о третій",
                 [("зустріч", 0.94), ("перенесли", 0.9), ("на", 0.99),
                  ("п'ятницю", 0.87), ("о", 0.98), ("третій", 0.92)]),
            ],
            "en": [
                ("uploaded the release to git hub", "uploaded the release to GitHub",
                 [("uploaded", 0.95), ("the", 0.99), ("release", 0.9),
                  ("to", 0.99), ("git", 0.88), ("hub", 0.82)]),
                ("write down the dashboard idea before I forget",
                 "write down the dashboard idea before I forget",
                 [("write", 0.93), ("down", 0.9), ("the", 0.98), ("dashboard", 0.42),
                  ("idea", 0.95), ("before", 0.96), ("I", 0.99), ("forget", 0.94)]),
                ("the meeting moved to Friday at three",
                 "the meeting moved to Friday at three",
                 [("the", 0.99), ("meeting", 0.94), ("moved", 0.9), ("to", 0.99),
                  ("Friday", 0.87), ("at", 0.98), ("three", 0.92)]),
            ],
        }
        for raw, final, words in samples[LANG]:
            win.dictation.add_entry(raw, final, words)

    # --- Файли: 2 елементи черги (готовий + у процесі) ---
    # сегменти з таймкодами → на картці активне меню «Зберегти як…» (srt/vtt/docx)
    file_samples = {
        "uk": (
            [r"C:\Demo\голосове-від-колеги.ogg", r"C:\Demo\нарада-понеділок.m4a"],
            "Доброго ранку. Нагадую: о десятій - планірка по релізу, "
            "о другій - демо для замовника. Порядок денний скинув у чат.",
            [
                (0.0, 2.6, "Доброго ранку."),
                (2.6, 6.4, "Нагадую: о десятій - планірка по релізу,"),
                (6.4, 9.8, "о другій - демо для замовника."),
                (9.8, 12.5, "Порядок денний скинув у чат."),
            ],
        ),
        "en": (
            [r"C:\Demo\voice-note-from-a-colleague.ogg", r"C:\Demo\monday-meeting.m4a"],
            "Good morning. Reminder: release planning is at ten, "
            "and the customer demo is at two. The agenda is in the chat.",
            [
                (0.0, 2.6, "Good morning."),
                (2.6, 6.4, "Reminder: release planning is at ten,"),
                (6.4, 9.8, "and the customer demo is at two."),
                (9.8, 12.5, "The agenda is in the chat."),
            ],
        ),
    }
    file_names, file_text, fake_segments = file_samples[LANG]
    win.files.add_files(file_names)
    # feature/model-bottlenecks (під-хвиля 2): words із ймовірностями — картка файлу
    # тепер підсвічує непевні слова так само, як стрічка (тут «планірка» 0.44<0.5).
    fake_words = {
        "uk": [
            ("Доброго", 0.95), ("ранку", 0.93), ("Нагадую", 0.9), ("о", 0.98),
            ("десятій", 0.88), ("планірка", 0.44), ("по", 0.97), ("релізу", 0.83),
            ("о", 0.98), ("другій", 0.9), ("демо", 0.86), ("для", 0.98),
            ("замовника", 0.91), ("Порядок", 0.93), ("денний", 0.9),
            ("скинув", 0.89), ("у", 0.99), ("чат", 0.87),
        ],
        "en": [
            ("Good", 0.95), ("morning", 0.93), ("Reminder", 0.9),
            ("release", 0.83), ("planning", 0.44), ("is", 0.99), ("at", 0.98),
            ("ten", 0.88), ("customer", 0.91), ("demo", 0.86), ("two", 0.9),
            ("agenda", 0.93), ("chat", 0.87),
        ],
    }[LANG]
    ctrl.file_done.emit(1, file_text, "done:38", fake_segments, fake_words)
    # КОД стану, не перекладений текст: _on_status порівнює з FileStatus,
    # інакше busy-пілюля (спінер + німб) не потрапляє на канонічний кадр
    ctrl.file_status.emit(2, FileStatus.TRANSCRIBING)

    win.show()
    win.raise_()
    win.activateWindow()

    OUT.mkdir(parents=True, exist_ok=True)
    # 00 — порожній стан стрічки; далі populate() наповнює її фейк-картками.
    # Індекси сторінок: 0 Диктування · 1 Аудіофайли · 2 Історія · 3 Словники · 4 Налаштування
    def show_settings_system():
        # секція «Система» (оновлення + чекбокси) — ОКРЕМА вкладка, а не «низ
        # довгої сторінки»: налаштування давно розбито на вкладки QTabWidget, тож
        # старий скрол першої вкладки залишав кадр 07b дублем 07 (Розпізнавання).
        win.settings._tabs.setCurrentIndex(win.settings._tabs.count() - 1)

    seq = [("00-dictation-empty", 0, None),
           ("01-dictation", 0, populate_dictation),
           ("02-files", 1, None), ("03-meeting", 2, None),
           ("04-screen", 3, None), ("05-history", 4, None),
           ("06-vocab", 5, None), ("07-settings", 6, None),
           ("07b-settings-system", 6, show_settings_system),
           # REC-стан кнопок запису (остання — щоб не впливала на інші кадри)
           ("08-dictation-rec", 0, lambda: ctrl.rec_state.emit("recording"))]

    # --states: кадри станів контролів для Блоку 5 рубрики (normal/disabled/focus).
    # Ідуть НАПРИКІНЦІ — disabled/focus не мають забруднювати канонічні кадри вище.
    if STATES:
        def state_disabled():
            ctrl.rec_state.emit("idle")               # зняти REC-стан кадру 08
            win.dictation._formfill_btn.setEnabled(False)   # видимо «сіра» кнопка

        def state_focus():
            win.dictation._formfill_btn.setEnabled(True)
            # програмний setFocus + Tab → видиме фокус-кільце на наступному контролі
            win.dictation._rec_btn.setFocus(Qt.TabFocusReason)
            win.focusNextChild()

        seq += [("09-dictation-disabled", 0, state_disabled),
                ("10-dictation-focus", 0, state_focus)]

    def step(i=0):
        if i >= len(seq):
            app.quit()
            return
        name, row, prepare = seq[i]
        if prepare:
            prepare()
        win.set_page(row)
        def shot():
            grab_window(win, OUT / f"{name}.png")
            QTimer.singleShot(200, lambda: step(i + 1))
        QTimer.singleShot(600, shot)

    QTimer.singleShot(1200, step)     # даємо Mica/DWM час намалюватись
    code = app.exec()
    shutil.rmtree(sandbox, ignore_errors=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
