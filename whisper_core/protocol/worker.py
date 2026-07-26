"""Підпроцес-воркер LLM: `python -m whisper_core.protocol.worker`.

Читає лінійний JSON зі stdin, пише лінійний JSON у stdout (по одному повідомленню
на рядок). Уся важка робота (llama-cpp-python) ізольована тут — краш нативного шару
вбиває лише цей процес, а не GUI Балачок.

Контракт повідомлень (див. whisper_core.protocol.__init__):
  ← {"type":"ping"}                              → {"type":"pong"}
  ← {"type":"generate","id":..,"prompt":..,      → {"type":"response","id":..,"text":..}
       "model_path":..,"n_ctx":..,"max_tokens":..,"temperature":..}
                                                  або {"type":"error","id":..,"message":..}
  ← {"type":"shutdown"}                          → процес завершується

ВАЖЛИВО: у stdout йде ЛИШЕ наш JSON. Логи llama-cpp-python (verbose) підуть у stderr
й потраплять у balachky.log, не ламаючи парсинг відповідей.

Бекенд llama-cpp-python — ОПЦІЙНИЙ. Якщо пакета немає (або виставлено ENV_FAKE_BACKEND),
береться фейковий бекенд: IPC-шлях лишається робочим (тести, graceful degradation),
але generate повертає позначку, що модель недоступна.
"""
from __future__ import annotations

import json
import os
import sys

from . import (DEFAULT_MAX_TOKENS, DEFAULT_N_CTX, DEFAULT_N_GPU_LAYERS,
               DEFAULT_TEMPERATURE, ENV_FAKE_BACKEND, FAKE_BACKEND_MARKER,
               MSG_ERROR, MSG_GENERATE, MSG_PING, MSG_PONG, MSG_RESPONSE,
               MSG_SHUTDOWN)


def llama_available() -> bool:
    """Чи встановлено llama-cpp-python (опційна залежність)."""
    import importlib.util
    try:
        return importlib.util.find_spec("llama_cpp") is not None
    except (ImportError, ValueError):
        return False


# Модулі, які ПРОГРІВАЄМО ОДНОПОТОКОВО у main() ДО циклу читання stdin — той самий
# клас ризику, що у TTS-воркері (фікс a1751d3): у frozen Windows перший `import
# llama_cpp` тягне нативні llama.dll/ggml*.dll через DLL-loader-lock; якщо це
# відбувається на головному потоці, поки батько вже пише в наш stdin-пайп, можливий
# import-lock deadlock. Прогрів робить важкий імпорт single-threaded ДО будь-якого
# IPC. Tolerant: без пакета (білд без llama) — тихий пропуск, як і раніше.
_WARMUP_MODULES = ("numpy", "llama_cpp")


def _warm_engine_imports(modules=_WARMUP_MODULES, import_module=None, log=None):
    """Прогріти важкі імпорти рушія LLM ОДНОПОТОКОВО (до циклу IPC). Повертає список
    успішно прогрітих модулів (для юніт-тесту й таймінгу в stderr). Будь-яка помилка
    імпорту = тихий пропуск (деградація як раніше — generate потім чесно поверне
    помилку через IPC)."""
    import importlib
    import time
    imp = import_module or importlib.import_module
    warmed = []
    for name in modules:
        t0 = time.monotonic()
        try:
            imp(name)
        except Exception as exc:                  # noqa: BLE001 — тихо деградуємо
            if log is not None:
                log(f"[llm-warmup] skip {name}: {type(exc).__name__}: {exc}")
            continue
        warmed.append(name)
        if log is not None:
            log(f"[llm-warmup] {name} {time.monotonic() - t0:.2f}s")
    return warmed


class FakeBackend:
    """Бекенд без llama: для тестів IPC і чесної деградації без пакета/моделі.

    generate повертає детерміноване позначення — реальний текст протоколу дає лише
    справжня модель. Тести генерації (E3) інжектять власну generate-функцію, тож
    цей текст їм не потрібен."""

    def generate(self, prompt: str, *, n_ctx: int, max_tokens: int,
                 temperature: float, model_path: str,
                 n_gpu_layers: int = DEFAULT_N_GPU_LAYERS) -> str:
        return f"{FAKE_BACKEND_MARKER} модель мовної генерації недоступна"


class LlamaBackend:
    """Живий бекенд llama-cpp-python. Модель вантажиться ліниво й кешується за
    (шлях, n_ctx, n_gpu_layers): один процес обслуговує кілька generate без
    перезавантаження.

    Виклик іде через ``create_chat_completion`` — штатний chat-шлях instruction-
    tuned Gemma, а не голий ``create_completion``. Це принципово: chat-шаблон
    Gemma 4 (вшитий у GGUF) за замовчуванням ВИМИКАЄ «міркування» (enable_thinking
    default false → у turn моделі підставляється порожній канал думки). Голий
    текстовий промт цього не робить — модель зривається в багатотисячний ланцюг
    англійського reasoning ДО чистовика (на CPU — десятки хвилин). Тобто chat-шлях
    — і коректніший формат, і приборкання thinking одночасно (живий тест 24.07)."""

    def __init__(self):
        self._model = None
        self._key = None

    def _ensure(self, model_path: str, n_ctx: int, n_gpu_layers: int):
        key = (model_path, n_ctx, n_gpu_layers)
        if self._model is not None and self._key == key:
            return self._model
        if not model_path or not os.path.isfile(model_path):
            raise FileNotFoundError(f"Файл моделі не знайдено: {model_path!r}")
        from llama_cpp import Llama
        # n_gpu_layers: 0 — на процесорі (наш дефолт, CPU-складання працює всюди);
        # -1 — максимум шарів на GPU за наявності CUDA-складання (llama.cpp сам
        # відкочується на CPU, якщо GPU/CUDA нема). verbose=False — нативні логи в
        # stderr, не в stdout (stdout — JSON-канал IPC).
        self._model = Llama(model_path=model_path, n_ctx=n_ctx,
                            n_gpu_layers=n_gpu_layers, verbose=False)
        self._key = key
        return self._model

    def generate(self, prompt: str, *, n_ctx: int, max_tokens: int,
                 temperature: float, model_path: str,
                 n_gpu_layers: int = DEFAULT_N_GPU_LAYERS) -> str:
        model = self._ensure(model_path, n_ctx, n_gpu_layers)
        # Один user-turn: llama-cpp-python сам застосовує вшитий chat-шаблон GGUF
        # (thinking off за замовчуванням). content може бути None при tool-call —
        # тут інструментів нема, тож завжди текст; про всяк випадок нормалізуємо.
        out = model.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens, temperature=temperature)
        return out["choices"][0]["message"].get("content") or ""


def _make_backend():
    if os.environ.get(ENV_FAKE_BACKEND) or not llama_available():
        return FakeBackend()
    return LlamaBackend()


def _write(stream, payload: dict) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


def _handle(msg: dict, backend) -> "dict | None":
    """Обробити одне повідомлення. None → сигнал завершення (shutdown)."""
    kind = msg.get("type")
    if kind == MSG_PING:
        return {"type": MSG_PONG}
    if kind == MSG_SHUTDOWN:
        return None
    if kind == MSG_GENERATE:
        req_id = msg.get("id")
        try:
            text = backend.generate(
                msg.get("prompt", ""),
                n_ctx=int(msg.get("n_ctx", DEFAULT_N_CTX)),
                max_tokens=int(msg.get("max_tokens", DEFAULT_MAX_TOKENS)),
                temperature=float(msg.get("temperature", DEFAULT_TEMPERATURE)),
                model_path=msg.get("model_path", ""),
                n_gpu_layers=int(msg.get("n_gpu_layers", DEFAULT_N_GPU_LAYERS)))
            return {"type": MSG_RESPONSE, "id": req_id, "text": text}
        except Exception as exc:            # noqa: BLE001 — будь-яка помилка йде назад рядком
            return {"type": MSG_ERROR, "id": req_id, "message": str(exc)}
    return {"type": MSG_ERROR, "id": msg.get("id"),
            "message": f"Невідомий тип повідомлення: {kind!r}"}


def main() -> int:
    # Windows-консоль/пайп за замовч. не UTF-8: кирилиця у відповіді впала б із
    # UnicodeEncodeError і вбила процес. Форсуємо UTF-8 на обидва боки IPC.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    # Прогріти важкі імпорти рушія ОДНОПОТОКОВО ДО циклу IPC (фікс класу import-lock
    # deadlock у frozen Windows — див. _warm_engine_imports). Нативні друки рушія
    # на імпорті глушимо в stderr, щоб stdout лишався чистим JSON-каналом.
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        _warm_engine_imports(log=lambda m: print(m, file=sys.stderr, flush=True))
    backend = _make_backend()
    stdout = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _write(stdout, {"type": MSG_ERROR, "message": "Пошкоджений JSON у запиті"})
            continue
        reply = _handle(msg, backend)
        if reply is None:
            break
        _write(stdout, reply)
    return 0


def selftest() -> int:
    """Self-check для frozen-gate: прогін ping+generate через IPC-обробник із
    FakeBackend (реальний шлях розбору повідомлень) + import-probe нативного
    llama_cpp. Друкує рядок SELFTEST і повертає 0 (ок) / 1 (збій)."""
    os.environ.setdefault(ENV_FAKE_BACKEND, "1")
    backend = _make_backend()
    ok = True
    detail = []
    pong = _handle({"type": MSG_PING}, backend)
    ok = ok and pong == {"type": MSG_PONG}
    detail.append("ping=" + ("ok" if pong == {"type": MSG_PONG} else "FAIL"))
    resp = _handle({"type": MSG_GENERATE, "id": "st", "prompt": "x",
                    "model_path": ""}, backend)
    got = isinstance(resp, dict) and resp.get("type") == MSG_RESPONSE
    ok = ok and got
    detail.append("generate=" + ("ok" if got else "FAIL"))
    detail.append("llama_cpp=" + ("present" if llama_available() else "absent"))
    print("SELFTEST " + ("PASS" if ok else "FAIL") + " " + " ".join(detail),
          flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
