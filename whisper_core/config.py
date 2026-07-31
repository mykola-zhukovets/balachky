"""Конфігурація. Дефолти в коді; config.toml (шлях — whisper_core.paths:
dev — корінь репо, frozen — %LOCALAPPDATA%\\Balachky) перекриє їх."""
import json
import logging
import os
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)


def _atomic_write_text(path: Path, text: str) -> None:
    """Записати текст поруч у temp, синхронізувати й атомарно підмінити."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass

# feature/audio-qol: дефолти Silero VAD (той самий фільтр, що застосовує
# faster-whisper при vad_filter=True). threshold=0.5 — дефолт faster-whisper;
# min_silence=500 — значення, захардкоджене раніше в engine.transcribe. Тримаємо
# тут як єдине джерело правди для конфігу, рушія та кнопки «Скинути до типових».
VAD_THRESHOLD_DEFAULT = 0.5
VAD_MIN_SILENCE_MS_DEFAULT = 500
# feature/audio-center: третій параметр Silero VAD — мінімальна тривалість
# мовлення (мс), коротші сплески відкидаються як не-мова (клацання клавіш).
# Дефолт faster-whisper/Silero = 250 мс.
VAD_MIN_SPEECH_MS_DEFAULT = 250
# feature/model-bottlenecks (під-хвиля 7, anti-repeat): no_repeat_ngram_size для
# faster-whisper. condition_on_previous_text=True без цього — відомий рецепт
# циклів-галюцинацій на довгих аудіо (диктування до 20 хв, 10-хв чанки наради).
# 3 (триграми) — обережний дефолт: 2 (біграми) різали б легітимні укр. повтори
# («так-так»). 0 = вимкнено (голий дефолт бібліотеки).
NO_REPEAT_NGRAM_DEFAULT = 3
MODEL_IDLE_UNLOAD_OPTIONS = (0, 300, 600, 1800, 3600, 7200, 14400)

# feature/audio-center: дефолти простого DSP (gate/AGC) — беремо з audiodsp,
# щоб UI, config і сам DSP мали одне джерело правди (як з VAD вище).
from .audiodsp import (  # noqa: E402
    NOISE_GATE_THRESHOLD_DB_DEFAULT, AGC_TARGET_DB_DEFAULT,
)


@dataclass
class Config:
    model_name: str = "large-v3"
    device: str = "cpu"
    compute_type: str = "int8"      # int8 — 2-4x швидше, якість для укр. практично та сама
    language: str = "uk"            # мова РОЗПІЗНАВАННЯ: код Whisper (uk/en/de…) або
                                    # "auto" — модель визначає сама. Нормалізація в
                                    # аргумент рушія — whisper_core.languages
                                    # (feature/multilang-asr, Т44)
    ui_language: str = "uk"         # мова ІНТЕРФЕЙСУ (uk | en); діє після перезапуску
    log_level: str = "INFO"          # routine-лог: INFO | DEBUG | WARNING
    sample_rate: int = 16000
    ptt_key: str = "ctrl+shift+space"  # push-to-talk: утримання комбінації (змінюється з UI)
    ptt_mode: str = "hold"          # hold (утримання) | toggle (перемикач) — Етап 5
                                    # | double_tap (feature/double-tap: подвійний
                                    # тап старт, одинарний — стоп; hands-free)
    double_tap_ms: int = 400        # feature/double-tap: вікно між двома тапами PTT
                                    # для старту (мс). Клампиться логікою у 200..600
    hotkey_backend: str = "native"  # feature/native-hotkeys: механізм глобальних
                                    # хоткеїв. "native" — RegisterHotKey/WinAPI
                                    # (без хука клавіатури); "legacy" — стара
                                    # бібліотека keyboard (миттєвий відкат без
                                    # ребілду, якщо native десь не спрацює)
    ptt_mouse_button: str = "none"  # feature/mouse-ptt: бічна кнопка миші як
                                    # ДОДАТКОВА кнопка запису — none|x1|x2 (opt-in)
    note_hotkey: str = ""           # feature/scratchpad-note: глобальна комбінація,
                                    # що відкриває плаваючу нотатку. Порожньо =
                                    # вимкнено (opt-in; пункт трею «Нотатка» є завжди)
    screen_protection: bool = False  # feature/mil-hardening: захист від захоплення екрана
    panic_lock_hotkey: str = ""      # feature/mil-hardening: хоткей миттєвого блокування
    beam_size: int = 5
    no_repeat_ngram_size: int = NO_REPEAT_NGRAM_DEFAULT  # feature/model-bottlenecks
                                    # (під-хвиля 7): захист від циклів-повторів на
                                    # довгих аудіо. 0 = вимкнено; 3 = обережний дефолт.
    highlight_uncertain_words: bool = True  # feature/model-bottlenecks (під-хвиля 2):
                                    # підсвічувати золотим слова, у яких модель менш
                                    # упевнена (probability<0.5), у стрічці диктування.
                                    # Вмикає word_timestamps (DTW-прохід) — трохи
                                    # повільніше на слабкому CPU, тож керовано.
    model_idle_unload_seconds: int = 600  # 10 хв без диктування/обробки →
                                    # звільнити STT/Gemma з RAM/VRAM; 0 = ніколи
    sounds: bool = True             # звукові сигнали запису
    animations: bool = True         # анімації інтерфейсу (fronts.desktop.motion)
    restore_clipboard: bool = True  # повертати попередній вміст буфера після автовставки
    paste_typing_fallback: bool = False  # feature/cascade-paste: у звичайні вікна
                                         # доставляти текст НАБОРОМ (посимвольно)
                                         # замість Ctrl+V — opt-in для полів, де
                                         # вставка заборонена; типово Ctrl+V як досі
    paste_preview: bool = False      # feature/paste-preview: показати картку
                                     # перед вставкою (переглянути/відредагувати),
                                     # замість негайної вставки; opt-in
    paste_confirm_on_window_change: bool = True  # feature/paste-safety: якщо активне
                                     # вікно змінилось від старту диктування — НЕ
                                     # вставляти наосліп, а лишити текст у буфері
                                     # й повідомити (військовий кейс); типово увімкнено
    paste_history_enabled: bool = True  # feature/paste-safety: тримати буфер
                                     # останніх вставок (трей-підменю «Останні
                                     # вставки»). Вимкнення чистить буфер — щоб
                                     # чутливі диктування не лишались на
                                     # розблокованій машині; типово увімкнено
    dictation_queue_enabled: bool = True  # feature/dictation-queue: можна диктувати
                                     # наступну фразу, не чекаючи розшифровки
                                     # попередньої; фрази обробляються по черзі, у
                                     # порядку запису. Типово увімкнено (запит Миколи №10)
    voice_punctuation: bool = False  # feature/voice-punctuation: слова-команди
                                     # («кома», «крапка»…) → розділові знаки; opt-in
                                     # (може псувати текст, де слово — зміст, не команда)
    voice_nav_enabled: bool = False  # feature/office-voice-nav: голосова навігація
                                     # полями зовнішніх документів («наступне поле» →
                                     # Tab, «комірка Б7» → перехід у Excel); opt-in.
                                     # Доки ВИМК — ці фрази лишаються звичайним текстом
    filler_cleanup: bool = False     # feature/filler-cleanup: історичний тумблер
                                     # (сумісність); тепер керується cleanup_level
    cleanup_level: str = ""          # feature/clean-mix: рівень автоочистки тексту
                                     # off/light/medium/strong; "" → похідне від
                                     # filler_cleanup (True→medium). ЕКСПЕРИМЕНТАЛЬНА
    autocorrect_enabled: bool = False  # feature/punctuation-plus: автокорекція
                                     # одруків (symspellpy + завантажуваний частотний
                                     # словник); opt-in, ЕКСПЕРИМЕНТАЛЬНА
    punctuator_enabled: bool = False  # feature/punctuation-plus: пунктуатор/ITN
                                     # (punctuators pcs_47lang, завантажувана ONNX-модель);
                                     # opt-in, ЕКСПЕРИМЕНТАЛЬНА
    phrase_memory_enabled: bool = False  # feature/bilingual-memory: білінгвальна
                                     # пам'ять фраз (phrases.toml профілю) — коли
                                     # увімкнено, затверджені пари «як чується →
                                     # як писати» підмішуються у словник термінів
                                     # і застосовуються ПІСЛЯ STT, ДО вставки.
                                     # Незалежний від preserve_speech (це терміни,
                                     # не стиль мовлення); opt-in.
    save_dictation_audio: bool = True  # feature/reverse-dictation: зберігати аудіо
                                     # кожного диктування у dictation_audio/ профілю,
                                     # щоб у Історії можна було ПЕРЕСЛУХАТИ своє
                                     # вимовляння перед виправленням. Гейт додатковий
                                     # до пам'яті профілю (memory_enabled); усе
                                     # локально. Вимкнути → лишається тільки текст.
    preserve_speech: bool = False    # feature/edit-pack: «Не виправляй мою мову» —
                                     # коли увімкнено, конвеєр _apply_text_enhancements
                                     # ОБХОДИТЬ автокорекцію одруків і пунктуатор, щоб
                                     # не чіпати суржик/діалект без згоди. Словники
                                     # профілю (виправлення імен/термінів) ЛИШАЮТЬСЯ.
    review_text_changes: bool = False  # feature/player-pack: «Огляд перед дією» —
                                     # для РУЧНОЇ розшифровки файлу показати diff
                                     # автоматичних змін (філери/автокорекція) з
                                     # вибором «Застосувати/Лишити як було»; opt-in.
                                     # Диктування (миттєва вставка/нотатка) ігнорує.
    check_updates: bool = False     # перевірка оновлень на GitHub (opt-in; офлайн за замовч.)
    auto_download_updates: bool = False  # feature/auto-update: тихо завантажувати
                                    # інсталятор нової версії у фоні (opt-in; за
                                    # замовч. ВИМК — гігабайти без згоди не качаємо)
    backdrop: str = "auto"          # auto — Mica-скло Win11 (якщо DWM дозволив) | off
    night_mode: bool = False        # feature/night-mode: нічний/червоний МОНО-режим
                                    # інтерфейсу для роботи з приладами нічного бачення
                                    # (мілітарі). Уся гама — темно-червона на чорному;
                                    # застосовується миттєво, без перезапуску.
    ui_color: str = "classic"       # feature/ui-color-picker: активна колірна тема
    workspace_bg: str = "mascot"    # mascot ("Жук") | solid ("Однотонне") | custom ("Своя картинка")
    workspace_custom_bg_path: "str | None" = None  # відносний шлях скопійованого файлу у user_dir()
    model_dir: "str | None" = None  # None → стандартний кеш HuggingFace
    input_device: "str | None" = None  # ІМʼЯ мікрофона (не індекс — той плаває
                                        # між перезапусками); None → системний за
                                        # замовчуванням. Резолвиться в індекс у Recorder.
    output_device: "str | None" = None  # feature/audio-center: ІМʼЯ пристрою ВИВОДУ
                                        # для відтворення тесту мікрофона; None →
                                        # системний за замовчуванням. Резолвиться в
                                        # індекс у recorder.play_audio, з відкатом на
                                        # системний, якщо пристрій зник.
    watch_enabled: bool = False         # feature/watch-folder: стежити за текою і
                                        # авто-розшифровувати нові аудіофайли в ній
    watch_dir: "str | None" = None      # feature/watch-folder: тека спостереження
                                        # (None → вимкнено, навіть якщо watch_enabled)
    meeting_dir: "str | None" = None    # feature/meeting-ui: тека записів нарад
                                        # (None → paths.meetings_dir(): локальна,
                                        # поза синхронізацією)
    meeting_encrypt: bool = False        # opt-in encryption for local meeting storage
    _config_corrupt: bool = field(default=False, init=False, repr=False)
    _config_recovered_from_backup: bool = field(
        default=False, init=False, repr=False)
    meeting_bookmark_hotkey: str = ""  # глобальна «Мітка» під час активної наради; opt-in
    meeting_ics_path: "str | None" = None  # feature/diary-calendar: файл календаря
                                        # (.ics) для авто-назви наради за часом; opt-in
    meeting_sources: str = "mic"        # feature/meeting-ui: стартовий пресет вкладки
                                        # «Нарада». "mic" (очна розмова — головний
                                        # сценарій) | "mic+sys" (онлайн-дзвінок)
    meeting_mic_devices: list[str] = field(default_factory=list)  # мультимік: 2..4 імен
    # Канонічний вибір фази запису. Порожній список означає сумісний fallback на
    # meeting_sources/input_device; непорожній містить microphone:<name> та
    # system:default і дозволяє будь-яку комбінацію мікрофонів + loopback.
    meeting_record_sources: list[str] = field(default_factory=list)
    meeting_export_segment_minutes: int = 10  # Whisper-ready WAV-блоки після stop
    operator_name: str = ""             # feature/evidence-plus: «хто зафіксував»
                                        # (вільний текст, не акаунт) — лягає у подію
                                        # created журналу цілісності; порожньо → не
                                        # пишемо (даних не вигадуємо)
    diarization_enabled: bool = False
    diarization_num_speakers: "int | None" = None  # None → автоматично
    diarization_model_dir: "str | None" = None
    voice_memory_enabled: bool = False  # feature/voice-memory (Т41): запам'ятовувати голоси співрозмовників
    protocol_ai_enabled: bool = True    # feature/protocol-activation: показувати
                                        # кнопки ШІ-протоколу/Q&A по нараді. Вимкнено
                                        # → Нарада працює повноцінно без ШІ (жодних
                                        # кнопок протоколу), рішення користувача.
    protocol_model: str = "fast"        # feature/ai-protocol: активна LLM для
                                        # протоколу наради. Пресет ("fast" Gemma 4
                                        # E4B ~5 ГБ | "quality" Gemma 4 12B QAT ~6.3 ГБ)
                                        # АБО id власної моделі з custom_models.
                                        # Модель — завантажуваний компонент, у білд
                                        # не вшита.
    # feature/tts-listen (пакет «Прослухати»): озвучення ВИМКНЕНО за замовчуванням
    # (мілітарі — звук з динаміка чути іншим). Активний голос — per-мова тексту
    # (scalar-поля замість dict заради scalar-серіалізатора config); хоткей
    # «Прослухати виділене» не призначений за замовчуванням.
    tts_enabled: bool = False
    tts_voice_uk: str = "styletts2_ua"
    tts_voice_en: str = "kokoro_en"
    tts_hotkey: str = ""
    tts_custom_voices: list[str] = field(default_factory=list)  # feature/tts-listen:
                                        # власні голоси (voice-pack) понад пресети —
                                        # кожен JSON-рядком (CustomVoice); переживає
                                        # перезапуск. §4.4 безпека при додаванні.
    custom_models: list[str] = field(default_factory=list)  # feature/llm-model-picker:
                                        # власні моделі ШІ понад пресети — кожна
                                        # JSON-рядком (id/label/kind/path/repo_id/
                                        # filename/розмір). Парситься у CustomModel;
                                        # переживає перезапуск.
    screen_recordings_dir: "str | None" = None  # незалежні відеозаписи; None → локальна папка
    screen_record_fps: int = 30
    screen_record_resolution: str = "native"
    screen_record_format: str = "webm"  # VP9/WebM (BSD-кодек); H.264/libx264 (GPL) прибрано
    screen_record_quality: str = "medium"
    screen_record_system_audio: bool = False
    meeting_screen_enabled: bool = False  # запис екрана — лише явний opt-in
    meeting_screen_fps: int = 12          # нарада, не гра: помірна частота кадрів
    meeting_screen_monitor: int = 1       # mss: фізичні монітори нумеруються з 1
    vad_threshold: float = VAD_THRESHOLD_DEFAULT  # feature/audio-qol: поріг мовлення
                                        # Silero VAD 0.1..0.9. Нижче → чутливіше до
                                        # тихої мови (але легше зловити шум). Діє з
                                        # наступної транскрипції (без перезапуску).
    vad_min_silence_ms: int = VAD_MIN_SILENCE_MS_DEFAULT  # feature/audio-qol:
                                        # мінімальна пауза (мс), після якої VAD
                                        # закриває сегмент. Діє з наступної транскрипції.
    vad_min_speech_ms: int = VAD_MIN_SPEECH_MS_DEFAULT  # feature/audio-center:
                                        # мінімальна тривалість мовлення (мс); коротші
                                        # сплески VAD відкидає. Діє з наступної транскрипції.
    noise_gate_enabled: bool = False    # feature/audio-center: RMS-шумовий гейт
                                        # перед Whisper (opt-in). Ріже тишу/шум між
                                        # словами; не спотворює мовний сигнал.
    noise_gate_threshold_db: float = NOISE_GATE_THRESHOLD_DB_DEFAULT  # поріг гейта (dBFS)
    agc_enabled: bool = False           # feature/audio-center: лінійний нормалізатор
                                        # гучності перед Whisper (opt-in) для тихих мікрофонів
    agc_target_db: float = AGC_TARGET_DB_DEFAULT  # цільовий RMS-рівень AGC (dBFS)
    auto_export_enabled: bool = False   # feature/auto-export: дописувати кожну
                                        # завершену розшифровку у файл-день теки
    auto_export_dir: "str | None" = None  # feature/auto-export: тека автозбереження
                                          # (None → вимкнено, навіть якщо enabled)
    auto_export_format: str = "md"      # feature/auto-export: "md" | "txt"
    # feature/obsidian-channel: канал доставки нарад у сховище Obsidian (.md-файл
    # у вибрану папку; Obsidian підхоплює його сам). Усе opt-in.
    obsidian_enabled: bool = False      # надсилати нараду до Obsidian після обробки
    obsidian_dir: "str | None" = None   # папка сховища (None → вимкнено, навіть якщо enabled)
    obsidian_filename_template: str = "{дата}-{назва}"  # шаблон імені; .md додається
    # feature/qol-pack: пакет зручностей диктування (усі opt-in / безпечні дефолти)
    undo_paste_key: str = ""            # глобальний хоткей «Скасувати останню
                                        # вставку»; "" → вимкнено (трей-пункт завжди є)
    insert_last_key: str = ""           # глобальний хоткей «Вставити останнє ще раз»;
                                        # "" → вимкнено (трей-пункт завжди є)
    command_edit_hotkey: str = ""       # feature/voice-edit-selection: глобальний
                                        # хоткей Command Mode (редагувати виділене
                                        # голосом); "" → вимкнено (трей-пункт завжди є)
    dictation_autostop_silence_s: int = 0   # автостоп диктування після N с тиші;
                                        # 0 → вимкнено (повзунок 1..30 у Налаштуваннях)
    dictation_max_duration_s: int = 1200    # ліміт тривалості диктування (с); 20 хв.
                                        # 0 → без ліміту. Попередження за ~30 с до кінця
    paste_confirm_sound: bool = True    # звук підтвердження вставки (увімкнено; «тихі години» глушать)
    quiet_hours_enabled: bool = False   # «тихі години»: у заданий проміжок — жодних звуків
    quiet_hours_start: str = "22:00"    # початок «тихих годин» (HH:MM)
    quiet_hours_end: str = "07:00"      # кінець «тихих годин» (HH:MM)
    pill_x: "int | None" = None         # feature/ux-center: збережена позиція
    pill_y: "int | None" = None         # плаваючого індикатора диктування (глобальні
                                        # координати лівого-верхнього кута). None → типова
                                        # позиція. При зникненні монітора — відкат до типової.
    player_resume_backstep_s: float = 1.5   # авто-відкат плеєра після паузи (с):
                                        # 0 / 0.5 / 1.5 / 3. 0 → без відкату
    recordings_dir: "str | None" = None  # feature/player-recordings: тека записів
                                        # диктофона (None → paths.recordings_dir():
                                        # локальна, поза синхронізацією)
    transcript_editing_enabled: bool = False  # feature/transcript-editing: кнопка
                                        # «Редагувати» на картках Файлів і Нарад
                                        # (правка тексту + запис у сховище). Пошук
                                        # по тексту доступний завжди; opt-in — лише
                                        # сама правка, бо вона перезаписує final.
    live_transcription: bool = False  # feature/live-transcription: живе прев'ю під
                                        # час запису; навантажує CPU, opt-in
    test_mode: bool = False           # Режим тестування: детальний дія-журнал для
                                        # живих тестів (DEBUG + покрокове логування
                                        # конвеєра); DEBUG не персистить — вимкнення
                                        # повертає рівень за log_level
    test_mode_include_text: bool = False  # включати тексти розшифровок у тест-журнал
                                        # (вимкнено за замовч.; приватність — журнал
                                        # тоді міститиме продиктований текст)
    test_mode_text_notice_shown: bool = False  # одноразове re-consent-нагадування
                                        # для старих профілів з include_text=True

    @classmethod
    def load(cls, config_path=None):
        c = cls()
        if config_path is None:
            config_path = paths.config_path()
        config_path = Path(config_path)
        if config_path.exists():
            try:
                data = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (UnicodeError, tomllib.TOMLDecodeError, OSError) as e:
                c._config_corrupt = True
                backup_path = Path(str(config_path) + ".bak")
                try:
                    data = tomllib.loads(
                        backup_path.read_text(encoding="utf-8"))
                except (UnicodeError, tomllib.TOMLDecodeError, OSError):
                    # Невідома політика захисту не може тихо перетворитися на
                    # meeting_encrypt=False. Без валідного бекапа беремо саме
                    # безпечний дефолт для цього одного security-поля.
                    data = {"meeting_encrypt": True}
                    log.warning(
                        "Не вдалося прочитати конфіг %s: %s — "
                        "шифрування нарад увімкнено як безпечний дефолт",
                        config_path, e)
                else:
                    c._config_recovered_from_backup = True
                    log.warning(
                        "Не вдалося прочитати конфіг %s: %s — "
                        "політику відновлено з %s",
                        config_path, e, backup_path)
            else:
                # В-05: обрив запису найчастіше лишає ВАЛІДНИЙ TOML — просто без
                # хвоста ключів (обрізання йде по межі рядка). Парсер мовчить, і
                # meeting_encrypt тихо падає в дефолт. save() пише цей ключ
                # ЗАВЖДИ, тож його відсутність поруч із наявним .bak = ознака
                # пошкодження, а не рукописного мінімального конфігу.
                backup_path = Path(str(config_path) + ".bak")
                if "meeting_encrypt" not in data and backup_path.exists():
                    c._config_corrupt = True
                    try:
                        backup = tomllib.loads(
                            backup_path.read_text(encoding="utf-8"))
                    except (UnicodeError, tomllib.TOMLDecodeError, OSError) as e:
                        log.warning(
                            "Конфіг %s неповний (немає політики шифрування), "
                            "бекап нечитний: %s — шифрування нарад увімкнено "
                            "як безпечний дефолт", config_path, e)
                        data["meeting_encrypt"] = True
                    else:
                        c._config_recovered_from_backup = True
                        log.warning(
                            "Конфіг %s неповний (немає політики шифрування) — "
                            "значення відновлено з %s", config_path, backup_path)
                        data = {**backup, **data}
                        data.setdefault("meeting_encrypt", True)
            # Міграція legacy-тумблера «Звуки запису» (24.07): раніше
            # sounds=False глушив УСІ звуки, включно зі вставкою. Сигнали
            # запису видалені назавжди, але явна воля користувача «тихо»
            # не має вмикати звук вставки — переносимо її ОДИН раз.
            if data.get("sounds") is False:
                data["paste_confirm_sound"] = False
                # погасити legacy-прапорець: generic-цикл нижче виставить
                # c.sounds=True, наступний save() запише sounds=true — і
                # міграція справді відбудеться ОДИН раз (рецензія-2: інакше вона
                # щозапуску мовчки ламала б явний UI-вибір користувача)
                data["sounds"] = True
            for key, val in data.items():
                if not hasattr(c, key):    # ігноруємо невідомі ключі
                    continue
                # Шляхові поля мають бути рядками: нестроковий тип (рукописна
                # правка TOML) далі кидає TypeError повз except (OSError,
                # ValueError) у споживачах — лишаємо дефолт (звірка №4 18.07)
                if key.endswith("_dir") and val is not None and not isinstance(val, str):
                    log.warning("Конфіг: %s має бути шляхом-рядком, отримано %r — ігнорую",
                                key, type(val).__name__)
                    continue
                setattr(c, key, val)
        try:
            c.meeting_screen_fps = min(15, max(1, int(c.meeting_screen_fps)))
        except (TypeError, ValueError):
            c.meeting_screen_fps = 12
        try:
            c.meeting_export_segment_minutes = min(
                60, max(1, int(c.meeting_export_segment_minutes)))
        except (TypeError, ValueError):
            c.meeting_export_segment_minutes = 10
        try:
            c.screen_record_fps = min(60, max(5, int(c.screen_record_fps)))
        except (TypeError, ValueError):
            c.screen_record_fps = 30
        try:
            idle_seconds = int(c.model_idle_unload_seconds)
            c.model_idle_unload_seconds = (
                idle_seconds if idle_seconds in MODEL_IDLE_UNLOAD_OPTIONS else 600)
        except (TypeError, ValueError):
            c.model_idle_unload_seconds = 600
        try:
            if c.diarization_num_speakers is not None:
                val = int(c.diarization_num_speakers)
                c.diarization_num_speakers = val if 2 <= val <= 10 else None
        except (TypeError, ValueError):
            c.diarization_num_speakers = None
        # тека моделей: env > config.toml > None (стандартний кеш HuggingFace)
        c.model_dir = os.environ.get("WHISPER_TYPER_MODELS") or c.model_dir or None
        if c.model_dir:
            try:
                from .models import resolve_cache_dir
                c.model_dir = resolve_cache_dir(c.model_dir)
            except (TypeError, ValueError, OSError) as e:
                log.warning("Невалідна тека моделей %r: %s — беру стандартний кеш",
                            c.model_dir, e)
                c.model_dir = None
        return c

    def save(self, config_path=None):
        """Записати config.toml (наш простий TOML).
        model_dir пишемо лише коли задана (порожня = стандартний кеш HuggingFace)."""
        if config_path is None:
            config_path = paths.config_path()
        if self.model_dir:
            from .models import resolve_cache_dir
            self.model_dir = resolve_cache_dir(self.model_dir)
        lines = ["# Балачки — конфігурація. Керується вікном Налаштувань; "
                 "коментарі не зберігаються. Довідка: config.example.toml"]
        keys = ["model_name", "device", "compute_type", "language",
                "ui_language", "log_level", "sample_rate", "ptt_key", "ptt_mode",
                "hotkey_backend",        # feature/native-hotkeys
                "ptt_mouse_button",      # feature/mouse-ptt
                "double_tap_ms",         # feature/double-tap
                "beam_size",
                "no_repeat_ngram_size",  # feature/model-bottlenecks: anti-repeat
                "highlight_uncertain_words",  # feature/model-bottlenecks: підсвітка
                "model_idle_unload_seconds",  # feature/model-idle-unload
                "sounds", "animations", "restore_clipboard",
                "paste_typing_fallback",  # feature/cascade-paste
                "paste_preview",         # feature/paste-preview
                "paste_confirm_on_window_change",  # feature/paste-safety
                "paste_history_enabled",           # feature/paste-safety
                "backdrop", "night_mode", "ui_color",   # feature/night-mode & feature/ui-color-picker
                "workspace_bg",  # feature/background-choice (Т76)
                "check_updates",
                "auto_download_updates",  # feature/auto-update
                "dictation_queue_enabled",  # feature/dictation-queue
                "voice_punctuation",     # feature/voice-punctuation
                "voice_nav_enabled",     # feature/office-voice-nav
                "filler_cleanup",        # feature/filler-cleanup
                "cleanup_level",         # feature/clean-mix: рівень автоочистки
                "autocorrect_enabled",   # feature/punctuation-plus
                "punctuator_enabled",    # feature/punctuation-plus
                "phrase_memory_enabled", # feature/bilingual-memory: пам'ять фраз
                "save_dictation_audio",  # feature/reverse-dictation: аудіо для «Переслухати»
                "preserve_speech",       # feature/edit-pack: не виправляй мою мову
                "review_text_changes",   # feature/player-pack: огляд перед дією
                "watch_enabled",         # feature/watch-folder
                "meeting_sources",       # feature/meeting-ui: пресет пишемо завжди
                "meeting_encrypt",       # feature/meeting-encryption: opt-in
                "meeting_mic_devices",   # feature/multi-mic: імена, не несталі індекси
                "meeting_record_sources", "meeting_export_segment_minutes",
                "operator_name",         # feature/evidence-plus: «хто зафіксував»
                "diarization_enabled",
                "protocol_ai_enabled",   # feature/protocol-activation: тумблер ШІ-протоколу
                "protocol_model",        # feature/ai-protocol: пресет LLM
                "tts_enabled", "tts_voice_uk", "tts_voice_en", "tts_hotkey",  # feature/tts-listen
                "tts_custom_voices",
                "meeting_screen_enabled", "meeting_screen_fps",
                "meeting_screen_monitor",
                "screen_record_fps", "screen_record_resolution", "screen_record_format",
                "screen_record_quality", "screen_record_system_audio",
                "vad_threshold", "vad_min_silence_ms",  # feature/audio-qol
                "vad_min_speech_ms",     # feature/audio-center
                "noise_gate_enabled", "noise_gate_threshold_db",  # feature/audio-center
                "agc_enabled", "agc_target_db",  # feature/audio-center
                "auto_export_enabled",   # feature/auto-export
                "auto_export_format",    # feature/auto-export
                "obsidian_enabled",      # feature/obsidian-channel
                "obsidian_filename_template",  # feature/obsidian-channel
                "undo_paste_key", "insert_last_key",          # feature/qol-pack
                "command_edit_hotkey",                        # feature/voice-edit-selection
                "dictation_autostop_silence_s",               # feature/qol-pack
                "dictation_max_duration_s",                   # feature/qol-pack
                "paste_confirm_sound",                        # feature/qol-pack
                "quiet_hours_enabled",                        # feature/qol-pack
                "quiet_hours_start", "quiet_hours_end",       # feature/qol-pack
                "transcript_editing_enabled",  # feature/transcript-editing
                "player_resume_backstep_s",    # авто-відкат плеєра після паузи
                "live_transcription",  # feature/live-transcription
                "screen_protection",   # feature/mil-hardening
                "test_mode", "test_mode_include_text",
                "test_mode_text_notice_shown"]  # Режим тестування
        if self.model_dir:
            keys.append("model_dir")
        if self.input_device:            # пишемо лише коли вибрано конкретний мікрофон
            keys.append("input_device")
        if self.output_device:           # feature/audio-center: лише коли обрано вивід
            keys.append("output_device")
        if self.watch_dir:               # feature/watch-folder: лише коли теку вибрано
            keys.append("watch_dir")
        if self.meeting_dir:             # feature/meeting-ui: лише коли теку задано
            keys.append("meeting_dir")
        if self.screen_recordings_dir:
            keys.append("screen_recordings_dir")
        if self.custom_models:           # feature/llm-model-picker: лише коли є власні
            keys.append("custom_models")
        if self.diarization_num_speakers is not None:
            keys.append("diarization_num_speakers")
        if self.diarization_model_dir:
            keys.append("diarization_model_dir")
        if self.auto_export_dir:         # feature/auto-export: лише коли теку вибрано
            keys.append("auto_export_dir")
        if self.obsidian_dir:            # feature/obsidian-channel: лише коли папку вибрано
            keys.append("obsidian_dir")
        if self.pill_x is not None and self.pill_y is not None:
            keys += ["pill_x", "pill_y"]   # feature/ux-center: лише коли позицію задано
        if self.meeting_bookmark_hotkey:
            keys.append("meeting_bookmark_hotkey")
        if self.meeting_ics_path:        # feature/diary-calendar: лише коли задано
            keys.append("meeting_ics_path")
        if self.note_hotkey:             # feature/scratchpad-note: лише коли задано
            keys.append("note_hotkey")
        if self.panic_lock_hotkey:        # feature/mil-hardening: лише коли задано
            keys.append("panic_lock_hotkey")
        if self.recordings_dir:          # feature/player-recordings: лише коли теку задано
            keys.append("recordings_dir")
        if self.workspace_custom_bg_path:  # feature/background-choice: лише коли задано
            keys.append("workspace_custom_bg_path")
        for key in keys:
            val = getattr(self, key)
            if isinstance(val, bool):
                val = "true" if val else "false"
            elif isinstance(val, str):
                # \ і " треба екранувати, інакше Windows-шлях зламає TOML
                val = '"' + val.replace("\\", "\\\\").replace('"', '\\"') + '"'
            elif isinstance(val, list):
                val = "[" + ", ".join(json.dumps(str(item), ensure_ascii=False) for item in val) + "]"
            lines.append(f"{key} = {val}")
        config_path = Path(config_path)
        text = "\n".join(lines) + "\n"
        try:
            if config_path.exists():
                previous = config_path.read_text(encoding="utf-8")
                try:
                    tomllib.loads(previous)
                except (UnicodeError, tomllib.TOMLDecodeError):
                    pass
                else:
                    _atomic_write_text(
                        Path(str(config_path) + ".bak"), previous)
            # Запобіжник: НІКОЛИ не писати невалідний TOML — битий конфіг на диску
            # запускає fail-closed скидання налаштувань у користувача (урок 24.07:
            # голе None у рядку ламало ВЕСЬ файл при кожному збереженні).
            tomllib.loads(text)
            _atomic_write_text(config_path, text)
            self._config_corrupt = False
            self._config_recovered_from_backup = False
        except OSError as e:
            log.error("Не вдалося зберегти %s: %s", config_path, e)


# --- feature/meeting-ui: пресети джерел вкладки «Нарада» ---
# Дзеркало whisper_core.meeting.session.PRESET_* (ядро наради Б1). Тримаємо їх і
# тут, щоб config/UI не залежали від імпорту модуля наради (той підвантажується
# лише при старті запису). Значення мусять збігатися з session.PRESET_*.
MEETING_PRESET_ONLYMIC = "onlymic"   # очна розмова: лише мікрофон (головний сценарій)
MEETING_PRESET_BOTH = "both"         # онлайн-дзвінок: мік + системний звук
MEETING_PRESET_MULTIMIC = "multimic" # очна нарада: окремий трек на кожен мікрофон
MEETING_MULTIMIC_MIN = 2
MEETING_MULTIMIC_MAX = 4
MEETING_MIC_SOURCE_PREFIX = "microphone:"
MEETING_SYSTEM_SOURCE = "system:default"


@dataclass(frozen=True)
class MeetingSourceSpec:
    """Один вибраний фізичний input або системний WASAPI loopback."""

    kind: str
    device_name: "str | None"


def meeting_microphone_token(name: "str | None") -> str:
    """Стабільний config-token мікрофона; порожнє ім’я = системний default."""
    return MEETING_MIC_SOURCE_PREFIX + (str(name).strip() if name else "")


def meeting_record_source_specs(cfg) -> list[MeetingSourceSpec]:
    """Канонічний впорядкований вибір запису зі зворотною сумісністю.

    Новий список має пріоритет. Старі конфіги без нього продовжують читати
    ``meeting_sources`` та ``meeting_mic_devices`` без міграції на диску.
    """
    specs = []
    seen = set()
    microphone_count = 0
    for raw in getattr(cfg, "meeting_record_sources", []) or []:
        token = str(raw).strip()
        if token == MEETING_SYSTEM_SOURCE:
            key = ("system", None)
        elif token.startswith(MEETING_MIC_SOURCE_PREFIX):
            name = token[len(MEETING_MIC_SOURCE_PREFIX):].strip() or None
            key = ("microphone", name)
        else:
            continue
        if (key[0] == "microphone"
                and microphone_count >= MEETING_MULTIMIC_MAX):
            continue
        if key not in seen:
            specs.append(MeetingSourceSpec(*key))
            seen.add(key)
            if key[0] == "microphone":
                microphone_count += 1
    if specs:
        return specs

    raw_preset = (getattr(cfg, "meeting_sources", "mic") or "mic").strip().lower()
    selected = meeting_mic_devices(cfg)
    if raw_preset == "multimic" and len(selected) >= MEETING_MULTIMIC_MIN:
        specs.extend(MeetingSourceSpec("microphone", name) for name in selected)
    else:
        specs.append(MeetingSourceSpec(
            "microphone", getattr(cfg, "input_device", None) or None))
    if raw_preset == "mic+sys":
        specs.append(MeetingSourceSpec("system", None))
    return specs


CLEANUP_LEVELS = ("off", "light", "medium", "strong")   # feature/clean-mix


def cleanup_level_for_cfg(cfg) -> str:
    """cfg → чинний рівень автоочистки тексту. Явний cleanup_level має пріоритет;
    інакше зворотна сумісність зі старим тумблером (filler_cleanup=True → medium)."""
    raw = getattr(cfg, "cleanup_level", "")
    if isinstance(raw, str) and raw.strip().lower() in CLEANUP_LEVELS:
        return raw.strip().lower()
    return "medium" if getattr(cfg, "filler_cleanup", False) else "off"


def protocol_custom_models(cfg):
    """Валідні власні моделі ШІ з конфігу (JSON-рядки → CustomModel), дедуп за id.
    Битий/небезпечний запис мовчки відкидається (from_json → None)."""
    from .protocol.model_manager import CustomModel
    out, seen = [], set()
    for raw in getattr(cfg, "custom_models", []) or []:
        cm = CustomModel.from_json(raw)
        if cm is None or cm.id in seen:
            continue
        seen.add(cm.id)
        out.append(cm)
    return out


def meeting_mic_devices(cfg) -> list[str]:
    """Валідний, дедуплікований вибір мікрофонів для мультиміку (2..4)."""
    result = []
    for name in getattr(cfg, "meeting_mic_devices", []) or []:
        name = str(name).strip()
        if name and name not in result:
            result.append(name)
        if len(result) == MEETING_MULTIMIC_MAX:
            break
    return result


def meeting_source_set(cfg) -> set:
    """Активні track-id з канонічного вибору; legacy пресети теж читаються."""
    if getattr(cfg, "meeting_record_sources", []) or []:
        specs = meeting_record_source_specs(cfg)
        mic_count = sum(spec.kind == "microphone" for spec in specs)
        tracks = set()
        mic_index = 0
        for spec in specs:
            if spec.kind == "system":
                tracks.add("sys")
            else:
                mic_index += 1
                tracks.add("mic" if mic_count == 1 else f"mic{mic_index}")
        return tracks or {"mic"}
    raw = (getattr(cfg, "meeting_sources", "mic") or "mic").strip().lower()
    if raw == "mic+sys":
        return {"mic", "sys"}
    devices = meeting_mic_devices(cfg)
    if raw == "multimic" and len(devices) >= MEETING_MULTIMIC_MIN:
        return {f"mic{i + 1}" for i in range(len(devices))}
    return {"mic"}


def meeting_preset_for_cfg(cfg) -> str:
    """cfg → стартовий пресет вкладки (для селектора)."""
    sources = meeting_source_set(cfg)
    if "sys" in sources:
        return MEETING_PRESET_BOTH
    if all(track.startswith("mic") and track != "mic" for track in sources):
        return MEETING_PRESET_MULTIMIC
    return MEETING_PRESET_ONLYMIC


def meeting_sources_for_preset(preset: str) -> str:
    """Пресет вкладки → рядок для cfg.meeting_sources."""
    if preset == MEETING_PRESET_BOTH:
        return "mic+sys"
    if preset == MEETING_PRESET_MULTIMIC:
        return "multimic"
    return "mic"
