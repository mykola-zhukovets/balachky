"""AI-протокол наради: локальна LLM у sidecar-підпроцесі.

Найбільша фіча Балачок за аудитом: із діаризованого транскрипту наради
(`transcript.json` — сегменти з таймкодами й мітками мовців) робимо структурований
протокол українською: Підсумок / Рішення / Задачі (таблиця Хто-Що-Термін-таймкод) /
Розділи наради. 100% офлайн, opt-in.

Архітектура (за вердиктами дослідження 17.07.2026 і патерном Meetily):
  - движок — **Gemma 4 E4B GGUF Q4_K_M** через llama-cpp-python;
  - llama-cpp-python крутиться в ОКРЕМОМУ підпроцесі (`whisper_core.protocol.worker`),
    а не в GUI-процесі: краш CUDA/нативного шару не валить UI (як пунктуатор — опційна
    залежність з graceful degradation);
  - IPC — лінійний JSON по stdin/stdout (generate/response/ping/pong/shutdown);
  - модель — завантажуваний компонент (~3-4 ГБ), у білд НЕ вшивається (як діаризація).

Модуль-межа: whisper_core.* — БЕЗ Qt. UI (кнопка «Створити протокол», прогрес,
скасування) живе у fronts/desktop; сюди воно приходить лише через колбеки.
"""
from __future__ import annotations

# --- IPC: типи повідомлень (контракт sidecar↔worker) ---
MSG_PING = "ping"
MSG_PONG = "pong"
MSG_GENERATE = "generate"
MSG_RESPONSE = "response"
MSG_ERROR = "error"
MSG_SHUTDOWN = "shutdown"

# Дефолти генерації (низька температура — секретар не фантазує).
DEFAULT_N_CTX = 8192
DEFAULT_MAX_TOKENS = 2048
DEFAULT_TEMPERATURE = 0.2
# Скільки шарів моделі класти на відеокарту. 0 — рахувати на процесорі (наш
# дефолт: постачаємо CPU-складання llama-cpp, працює на будь-якому залізі).
# -1 — максимум шарів на GPU за наявності CUDA-складання (опційне прискорення,
# з чесною деградацією на CPU). Прокидається через IPC, керується в Розширеному.
DEFAULT_N_GPU_LAYERS = 0

# Змінна оточення, що змушує worker узяти фейковий бекенд замість llama-cpp-python.
# Використовується тестами (реальний IPC-шлях без важкої моделі) і як
# страхувальник, коли пакет недоступний.
ENV_FAKE_BACKEND = "BALACHKY_PROTOCOL_FAKE"

# Позначка, яку повертає FakeBackend.generate замість реального тексту. Оркестрація
# ловить її, щоб НЕ видати заглушку за відповідь/протокол (урок судді: без бекенда
# показуй «встановіть компонент», а не порожнє/фейкове).
FAKE_BACKEND_MARKER = "[fake-backend]"

__all__ = [
    "MSG_PING", "MSG_PONG", "MSG_GENERATE", "MSG_RESPONSE", "MSG_ERROR",
    "MSG_SHUTDOWN", "DEFAULT_N_CTX", "DEFAULT_MAX_TOKENS", "DEFAULT_TEMPERATURE",
    "DEFAULT_N_GPU_LAYERS", "ENV_FAKE_BACKEND", "FAKE_BACKEND_MARKER",
]
