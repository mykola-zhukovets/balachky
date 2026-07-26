"""Шляхи застосунку: єдине місце, де вирішується «де ми живемо».

Два режими:
  dev (звичайний запуск з репо)  — усе як раніше: config.toml, profiles/,
      assets/ лежать у корені репозиторію.
  frozen (PyInstaller-збірка)    — код і дані інсталяції лежать поруч з exe
      (тека може бути read-only: Program Files або {localappdata}\\Programs),
      тому все, що ПИШЕТЬСЯ (config.toml, profiles/), іде в
      %LOCALAPPDATA%\\Balachky.

Правило для решти коду: жодного Path(__file__).parents[N] — тільки функції
цього модуля.
"""
import os
import sys
from pathlib import Path

#: True, коли працюємо як PyInstaller-збірка (Balachky.exe)
FROZEN: bool = bool(getattr(sys, "frozen", False))

if FROZEN:
    #: тека інсталяції — де лежить Balachky.exe (може бути read-only!)
    APP_ROOT = Path(sys.executable).resolve().parent
    #: тека з data-файлами PyInstaller (onedir → _internal поруч з exe)
    _DATA_DIR = Path(getattr(sys, "_MEIPASS", APP_ROOT))
    #: користувацькі дані — ЗАВЖДИ writable, переживають перевстановлення
    USER_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Balachky"
else:
    #: корінь репозиторію (paths.py → whisper_core → корінь)
    APP_ROOT = Path(__file__).resolve().parents[1]
    _DATA_DIR = APP_ROOT
    #: у dev-режимі користувацькі дані живуть у корені репо, як і раніше
    USER_DIR = APP_ROOT


def safe_under(root, target) -> bool:
    """``target`` ФІЗИЧНО в межах ``root`` (сам ``root`` теж рахується «у межах»).

    Обидва шляхи резолвляться (``resolve`` знімає "..", символьні лінки, регістр
    тому), далі перевіряється ``target.is_relative_to(root)``. Захист від
    path-traversal: інструменти, що будують шлях із зовнішнього рядка (MCP-сервер
    під недовіреним контекстом — промт-інʼєкція), звіряють результат ПЕРЕД
    читанням/записом і відмовляють, якщо він вислизнув за межі даних застосунку.
    Спільний для CLI та MCP — корінь фіксимо в одному місці."""
    try:
        r = Path(root).resolve()
        t = Path(target).resolve()
    except (OSError, ValueError):
        return False
    return t == r or t.is_relative_to(r)


def user_dir() -> Path:
    """Тека користувацьких даних; створюється за потреби (ідемпотентно)."""
    try:
        USER_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл
    return USER_DIR


def config_path() -> Path:
    """config.toml: dev — корінь репо; frozen — %LOCALAPPDATA%\\Balachky."""
    return user_dir() / "config.toml"


def context_profiles_path() -> Path:
    """context_profiles.toml (feature/context-profiles): поруч із config.toml.
    Окремий файл — бо серіалізатор config.py скалярний і не пише [[profile]]."""
    return user_dir() / "context_profiles.toml"


def snippets_path() -> Path:
    """snippets.toml — колишні глобальні голосові шаблони. Фіча злита в «Макроси»
    (per-profile); цей шлях лишився ЛИШЕ як джерело одноразової міграції
    (whisper_core.macros.migrate_snippets) та для читання старих архівів
    налаштувань. dev — корінь репо; frozen — USER_DIR."""
    return user_dir() / "snippets.toml"


def templates_dir() -> Path:
    """Тека шаблонів для заповнення голосом (.txt/.md): dev — <репо>/templates;
    frozen — %LOCALAPPDATA%\\Balachky\\templates. Writable, переживає
    перевстановлення. Створюється ідемпотентно.

    feature/voice-form-fill."""
    d = user_dir() / "templates"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл шаблону
    return d


def bundled_templates_dir() -> "Path | None":
    """Приклади шаблонів зі збірки — сід для templates_dir() у frozen-режимі.
    У dev повертає None: там сідом слугують готові файли <репо>/templates.

    feature/voice-form-fill."""
    if not FROZEN:
        return None
    d = _DATA_DIR / "templates"
    return d if d.exists() else None


def profiles_root() -> Path:
    """База профілів: тека, що МІСТИТЬ profiles/ (whisper_core.profiles
    сам додає /profiles). dev — корінь репо; frozen — USER_DIR."""
    return user_dir()


def meetings_dir() -> Path:
    """Дефолтне сховище нарад: user_dir()/"meetings" (frozen —
    %LOCALAPPDATA%\\Balachky\\meetings; dev — <репо>/meetings). ЛОКАЛЬНЕ й ПОЗА
    синхронізованими теками (безпекова вимога). Створюється ідемпотентно.

    feature/meeting-ui."""
    d = user_dir() / "meetings"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл сесії
    return d


def recordings_dir() -> Path:
    """Дефолтне сховище записів диктофона: user_dir()/"recordings" (frozen —
    %LOCALAPPDATA%\\Balachky\\recordings; dev — <репо>/recordings). ЛОКАЛЬНЕ й
    поза синхронізованими теками. Створюється ідемпотентно.

    feature/player-recordings."""
    d = user_dir() / "recordings"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл запису
    return d


def corpus_dir() -> Path:
    """Локальне сховище корпусу точності розпізнавання: user_dir()/"corpus"
    (frozen — %LOCALAPPDATA%\\Balachky\\corpus; dev — <репо>/corpus). Пари
    «аудіо-кліп + розпізнаний + виправлений текст» (manifest.jsonl + WAV).
    ЛОКАЛЬНЕ, нікуди не відправляється (канон приватності). Створюється
    ідемпотентно. feature/accuracy-corpus."""
    d = user_dir() / "corpus"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл кліпу
    return d


def screen_recordings_dir() -> Path:
    """Локальна папка незалежних відеозаписів."""
    d = user_dir() / "screen"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def diarization_models_dir() -> Path:
    """Локальна тека моделей діаризації (ніколи не всередині інсталяції)."""
    return user_dir() / "diarization"


def components_dir() -> Path:
    """Тека завантажуваних компонентів постобробки тексту (частотний словник
    автокорекції, ONNX-модель пунктуатора). Як і моделі діаризації — локальна,
    поза інсталяцією, переживає перевстановлення. feature/punctuation-plus."""
    return user_dir() / "components"


def autocorrect_dict_path() -> Path:
    """Файл частотного словника автокорекції (symspellpy: «слово частота» на
    рядок). Завантажуваний компонент — у білд не вшивається. feature/punctuation-plus."""
    return components_dir() / "uk_freq.txt"


def punctuator_model_dir() -> Path:
    """Тека ONNX-моделі пунктуатора (punctuators pcs_47lang). Завантажуваний
    компонент — у білд не вшивається. feature/punctuation-plus."""
    return components_dir() / "punctuator"


def protocol_models_dir() -> Path:
    """База GGUF-моделей локальної LLM для AI-протоколу наради. Як діаризація й
    пунктуатор — локальна, поза інсталяцією, переживає перевстановлення.
    Кожен пресет (fast/quality) — у власній підтеці. feature/ai-protocol."""
    return components_dir() / "llm"


def protocol_model_dir(preset_id: str) -> Path:
    """Тека GGUF-моделі конкретного пресета (fast|quality). Файл усередині —
    model.gguf. Завантажуваний компонент ~3-8 ГБ, у білд не вшивається."""
    from .protocol import model_manager  # ліниво: уникаємо циклічного імпорту
    return protocol_models_dir() / model_manager.safe_preset_id(preset_id)


def tts_voices_dir() -> Path:
    """База завантажуваних голосів TTS: user_dir()/"tts-voices" (frozen —
    %LOCALAPPDATA%\\Balachky\\tts-voices; dev — <репо>/tts-voices). Кожен голос —
    у власній підтеці tts-voices/<voice_id>/. Локальна, поза інсталяцією, переживає
    перевстановлення. feature/tts-listen (пакет «Прослухати»)."""
    d = user_dir() / "tts-voices"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # нехай помилка спливе там, де реально пишуть файл голосу
    return d


def tts_engine_dir() -> Path:
    """Тека завантаженого рушія TTS: user_dir()/"tts-engine"
    (%LOCALAPPDATA%\\Balachky\\tts-engine у frozen-режимі)."""
    d = user_dir() / "tts-engine"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def tts_engine_exe_path() -> Path:
    """Шлях до виконуваного файла завантаженого рушія TTS."""
    return tts_engine_dir() / "balachky-tts-worker.exe"



def asset_root() -> Path:
    """Корінь, звідки резолвляться бандл-ресурси (assets/, шрифти тощо) —
    для коду, якому потрібен САМЕ корінь, а не вже готова assets_dir().

    На відміну від APP_ROOT/_DATA_DIR (застигають при першому імпорті
    модуля), рахує sys.frozen/sys._MEIPASS ЖИВЦЕМ при кожному виклику —
    тестам зручно мокати sys напряму. dev — корінь репозиторію;
    frozen — тека PyInstaller-даних (onedir → _internal поруч з exe).

    Правило з шапки модуля лишається чинним: жодного
    Path(__file__).parents[N] у fronts/* — тільки цей резолвер."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS",
                             Path(sys.executable).resolve().parent))
    return APP_ROOT


def assets_dir() -> Path:
    """Статичні ресурси (іконка тощо): dev — <репо>/assets;
    frozen — _internal/assets (пакується через datas у .spec)."""
    return _DATA_DIR / "assets"


def bundled_doc(name: str) -> "Path | None":
    """Документ зі збірки/репо за іменем файлу (README.uk.md, README.md,
    THIRD-PARTY-NOTICES.txt): dev — корінь репо; frozen — _internal
    (пакується через datas у .spec). None, якщо файлу немає."""
    p = _DATA_DIR / name
    return p if p.exists() else None


def bundled_terms_example() -> "Path | None":
    """Приклад словника terms.toml зі збірки — сід для першого профілю
    у frozen-режимі. У dev повертає None: там сідом слугує кореневий
    terms.toml через звичайну міграцію profiles.ensure_migrated."""
    if not FROZEN:
        return None
    p = _DATA_DIR / "terms.toml"
    return p if p.exists() else None
