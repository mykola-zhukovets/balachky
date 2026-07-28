"""Пакет «Прослухати» (TTS): синтез мовлення в окремому sidecar-процесі.

Дзеркало whisper_core.protocol: torch/transformers ізольовані у воркері-підпроцесі
(краш нативного шару не валить GUI; ~2.5 ГБ RAM віддаються ОС kill-ом процесу).
Головний GUI-процес НЕ імпортує torch. UI заходить сюди лише через колбеки/сигнали.

Модуль-межа: whisper_core.tts.* — БЕЗ Qt.

IPC — ПОТОКОВИЙ по реченнях (не «повний WAV наприкінці»), зі скасуванням і однією
явною політикою одночасності (один активний запит на sidecar). Контракт повідомлень
описано у whisper_core.tts.worker.
"""
from __future__ import annotations

# --- IPC: типи повідомлень (контракт sidecar↔worker) ---
# Керівні (parent → worker):
MSG_PING = "ping"
MSG_LOAD_VOICE = "load_voice"
MSG_SYNTHESIZE = "synthesize"
MSG_CANCEL = "cancel"
MSG_SHUTDOWN = "shutdown"

# Відповіді (worker → parent):
MSG_PONG = "pong"
MSG_VOICE_LOADED = "voice_loaded"
MSG_ACCEPTED = "accepted"           # synthesize прийнято активним воркером
MSG_BUSY = "busy"                   # уже є активний запит (parent вирішує політику)
MSG_PROGRESS = "progress"           # {sentence:i,total:n}
MSG_CHUNK_READY = "chunk_ready"     # речення i готове (плеєр грає одразу)
MSG_RESULT = "result"               # синтез завершено повністю
MSG_CANCELLED = "cancelled"
MSG_ERROR = "error"

# Обмеження першого чанку заради TTFS < 0.5 c: якщо перше «речення» довге (суцільний
# абзац без крапки), воркер ріже перший чанк по м'якій межі (кома/двокрапка/N слів).
# Умова: cap у межах 8-12 слів; фіксуємо 10.
FIRST_CHUNK_MAX_WORDS = 10

# Дедлайн кооперативного cancel: воркер зупиняється між реченнями; якщо застряг у
# нативному forward довше — батько робить hard-kill і спавнить новий процес.
CANCEL_COOPERATIVE_DEADLINE_S = 1.5

# Змінна оточення, що змушує worker узяти фейковий рушій замість torch-моделі.
# Використовується тестами (реальний IPC-шлях без важкої моделі) і як
# страхувальник, коли рушій недоступний (torch не встановлено).
ENV_FAKE_BACKEND = "BALACHKY_TTS_FAKE"

# Позначка, яку повертає фейковий рушій замість реального аудіо. Оркестрація ловить
# її, щоб НЕ видати заглушку за справжній синтез (урок судді протоколу: без рушія
# показуй «завантажте голос», а не тишу за успіх).
FAKE_ENGINE_MARKER = "[fake-tts]"

# Префікс temp-теки plaintext-аудіо (батько-власник; §8.9). У імені — лише
# випадковий суфікс, НІКОЛИ вміст/назва наради.
PLAINTEXT_TEMP_PREFIX = "balachky-tts-plain-"

__all__ = [
    "MSG_PING", "MSG_LOAD_VOICE", "MSG_SYNTHESIZE", "MSG_CANCEL", "MSG_SHUTDOWN",
    "MSG_PONG", "MSG_VOICE_LOADED", "MSG_ACCEPTED", "MSG_BUSY", "MSG_PROGRESS",
    "MSG_CHUNK_READY", "MSG_RESULT", "MSG_CANCELLED", "MSG_ERROR",
    "FIRST_CHUNK_MAX_WORDS", "CANCEL_COOPERATIVE_DEADLINE_S",
    "ENV_FAKE_BACKEND", "FAKE_ENGINE_MARKER", "PLAINTEXT_TEMP_PREFIX",
]
