"""Ядро режиму «Нарада»: захоплення звуку, сесії на диску, пост-обробка.

Цей пакет — БЕЗ Qt і БЕЗ мережі (правило меж зі спеки). Тут живе єдине джерело
правди про формат сесії на диску: константи схеми, статуси, назви доріжок,
нативний формат потоків. І `capture`, і `session`, і (сусідній білдер) `postprocess`
імпортують їх ЗВІДСИ, а не дублюють кожен у себе.
"""

#: версія формату meeting.json — щоб майбутні міграції не ламали старі сесії
SCHEMA = 2

#: нативний формат обох WASAPI-потоків (жива проба: мік і loopback — 48 кГц/2 кан.)
NATIVE_RATE = 48000
NATIVE_CHANNELS = 2

#: довжина сегмента сирого запису на диск (~30-60 с [A]); константа, не конфіг
DEFAULT_SEGMENT_SECONDS = 45

# Довжина окремого Whisper-ready WAV-файла. Сирі 45-секундні шматки лишаються
# crash-safe внутрішнім форматом; після stop вони потоково групуються по 10 хв.
DEFAULT_EXPORT_SEGMENT_SECONDS = 10 * 60

#: назви доріжок = назви підтек сесії
TRACK_MIC = "mic"
TRACK_SYS = "sys"
TRACKS = (TRACK_MIC, TRACK_SYS)

# коди статусу сесії — ПОРІВНЮЄМО в коді, показуємо через tr (фронт)
STATUS_RECORDING = "recording"
STATUS_STOPPED = "stopped"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"
STATUS_CORRUPTED = "corrupted"

# пресети джерел (задають, скільки доріжок писати)
PRESET_ONLYMIC = "onlymic"   # очна розмова: лише мікрофон (головний сценарій №1)
PRESET_BOTH = "both"         # онлайн-дзвінок: мік + системний звук
PRESET_MULTIMIC = "multimic" # очна нарада: окремий трек на кожен мікрофон

__all__ = [
    "SCHEMA", "NATIVE_RATE", "NATIVE_CHANNELS", "DEFAULT_SEGMENT_SECONDS",
    "DEFAULT_EXPORT_SEGMENT_SECONDS",
    "TRACK_MIC", "TRACK_SYS", "TRACKS",
    "STATUS_RECORDING", "STATUS_STOPPED", "STATUS_PROCESSING",
    "STATUS_DONE", "STATUS_ERROR", "STATUS_INTERRUPTED", "STATUS_CORRUPTED",
    "PRESET_ONLYMIC", "PRESET_BOTH", "PRESET_MULTIMIC",
]
