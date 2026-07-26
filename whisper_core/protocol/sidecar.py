"""Батьківська сторона sidecar-а: керує підпроцесом LLM і говорить з ним JSON-ом.

Патерн Meetily: окремий підпроцес зі ЗНИЖЕНИМ пріоритетом, лінійний JSON по
stdin/stdout, health-check ping/pong, таймаути. Краш нативного шару llama-cpp-python
не валить GUI — тут ми ловимо RC процесу й піднімаємо зрозумілу помилку.

БЕЗ Qt. UI ганяє generate() у власному потоці й показує прогрес/скасування сам.

Читання з пайпа на Windows не селектиться, тож окремий потік-читач зливає рядки
stdout у чергу, а виклики чекають на потрібну відповідь із таймаутом.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import uuid
import weakref
from contextlib import contextmanager

from . import (DEFAULT_MAX_TOKENS, DEFAULT_N_CTX, DEFAULT_N_GPU_LAYERS,
               DEFAULT_TEMPERATURE, MSG_ERROR, MSG_GENERATE, MSG_PING, MSG_PONG,
               MSG_RESPONSE, MSG_SHUTDOWN)


class SidecarError(RuntimeError):
    """Сайдкар недоступний, упав або повернув помилку генерації."""


class SidecarTimeout(SidecarError):
    """Відповідь не надійшла у відведений час."""


# Реєстр потрібен desktop idle-lifecycle: він не має знати про конкретні
# Protocol/Q&A/Rewrite діалоги, але мусить бачити кожен живий Gemma worker.
_registry_lock = threading.RLock()
_running_sidecars = weakref.WeakSet()
_activity_listeners = set()


def add_activity_listener(callback) -> None:
    with _registry_lock:
        _activity_listeners.add(callback)


def remove_activity_listener(callback) -> None:
    with _registry_lock:
        _activity_listeners.discard(callback)


def _notify_activity() -> None:
    with _registry_lock:
        listeners = tuple(_activity_listeners)
    for callback in listeners:
        try:
            callback()
        except Exception:
            pass


def any_running() -> bool:
    with _registry_lock:
        return any(sidecar.running for sidecar in tuple(_running_sidecars))


@contextmanager
def idle_transition():
    """Бар'єр: Gemma не може стартувати між idle-check і unload."""
    with _registry_lock:
        yield


def shutdown_all() -> None:
    """Закрити всі зареєстровані sidecar-и (лише коли caller уже звірив busy)."""
    with _registry_lock:
        sidecars = tuple(_running_sidecars)
    for sidecar in sidecars:
        sidecar.shutdown()


def default_worker_command() -> list:
    """Команда запуску воркера. frozen → окремий balachky-protocol-worker.exe
    (console=True, IPC по stdin/stdout); dev — `python -m whisper_core.protocol.worker`.

    Головний Balachky.exe для воркера непридатний: він windowed (console=False), тож
    sys.stdin/stdout=None і IPC не піднявся б (той самий урок, що з TTS-воркером,
    §12.1). Тому пакуємо окремий console-exe (run_protocol_worker.py у balachky.spec)."""
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        worker_exe = os.path.join(exe_dir, "balachky-protocol-worker.exe")
        return [worker_exe]
    return [sys.executable, "-m", "whisper_core.protocol.worker"]


def _spawn_kwargs() -> dict:
    """Прапорці запуску: знижений пріоритет + без вікна консолі (Windows);
    м'який nice (POSIX)."""
    kwargs = {}
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = flags
    else:
        kwargs["preexec_fn"] = lambda: os.nice(10)   # noqa: PLW1509
    return kwargs


class Sidecar:
    """Володіє одним підпроцесом-воркером LLM.

    Один запит «у польоті» за раз (IPC лінійний): _lock серіалізує write→read.
    Використання: with Sidecar() as s: s.generate(prompt, model_path=...).
    """

    def __init__(self, command=None, *, env=None):
        self._command = list(command) if command else default_worker_command()
        self._env = env
        self._proc = None
        self._reader = None
        self._q = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False

    # --- життєвий цикл ---
    def start(self) -> None:
        with _registry_lock:
            if self._closed:
                raise SidecarError("Сайдкар LLM уже завершено")
            if self._proc is not None:
                return
            full_env = dict(os.environ)
            if self._env:
                full_env.update(self._env)
            try:
                self._proc = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", bufsize=1,
                    env=full_env, **_spawn_kwargs())
            except OSError as exc:
                raise SidecarError(f"Не вдалося запустити сайдкар LLM: {exc}") from exc
            _running_sidecars.add(self)
            self._reader = threading.Thread(target=self._read_loop, daemon=True)
            self._reader.start()
        _notify_activity()

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._q.put(json.loads(line))
                except ValueError:
                    pass                    # чужий рядок у stdout — ігноруємо
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(None)               # сигнал «потік stdout закрився»

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, payload: dict) -> None:
        if not self.running:
            raise SidecarError("Сайдкар LLM не запущено або він уже завершився")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise SidecarError(f"Сайдкар LLM урвав зв'язок: {exc}") from exc

    def _await(self, match, timeout: float) -> dict:
        """Чекати повідомлення, для якого match(msg) істинне. None у черзі =
        stdout закрився (процес упав) → SidecarError."""
        import time
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SidecarTimeout("Сайдкар LLM не відповів вчасно")
            try:
                msg = self._q.get(timeout=remaining)
            except queue.Empty:
                raise SidecarTimeout("Сайдкар LLM не відповів вчасно")
            if msg is None:
                rc = self._proc.poll() if self._proc else None
                raise SidecarError(f"Сайдкар LLM завершився несподівано (код {rc})")
            if match(msg):
                return msg

    # --- запити ---
    def ping(self, timeout: float = 5.0) -> bool:
        """Health-check. True — воркер живий і відповів pong."""
        with self._lock:
            if self._proc is None:
                self.start()
            self._send({"type": MSG_PING})
            msg = self._await(lambda m: m.get("type") in (MSG_PONG, MSG_ERROR), timeout)
            return msg.get("type") == MSG_PONG

    def generate(self, prompt: str, *, model_path: str, n_ctx: int = DEFAULT_N_CTX,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 temperature: float = DEFAULT_TEMPERATURE,
                 n_gpu_layers: int = DEFAULT_N_GPU_LAYERS,
                 timeout: float = 600.0) -> str:
        """Згенерувати текст. Піднімає SidecarError/SidecarTimeout при збої."""
        req_id = uuid.uuid4().hex
        with self._lock:
            if self._proc is None:
                self.start()
            self._send({"type": MSG_GENERATE, "id": req_id, "prompt": prompt,
                        "model_path": model_path, "n_ctx": n_ctx,
                        "max_tokens": max_tokens, "temperature": temperature,
                        "n_gpu_layers": n_gpu_layers})
            msg = self._await(
                lambda m: (m.get("type") in (MSG_RESPONSE, MSG_ERROR)
                           and m.get("id") == req_id), timeout)
            if msg.get("type") == MSG_ERROR:
                raise SidecarError(msg.get("message") or "Помилка генерації в сайдкарі")
            return msg.get("text", "")

    def shutdown(self, timeout: float = 5.0) -> None:
        """М'яко зупинити воркер (shutdown → wait → kill як остання інстанція)."""
        self._closed = True
        if self._proc is None:
            return
        try:
            if self.running:
                try:
                    self._send({"type": MSG_SHUTDOWN})
                except SidecarError:
                    pass
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # kill() надіслано; довше не блокуємо і не пробиваємо
                    # виняток наскрізь. finally однаково прибере дескриптори
                    # й реєстр — інакше shutdown_all/idle-unload впав би.
                    logging.getLogger(__name__).warning(
                        "sidecar-процес не завершився навіть після kill() — можливий зомбі-процес")
        finally:
            for stream in (self._proc.stdin, self._proc.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
            self._proc = None
            self._reader = None
            with _registry_lock:
                _running_sidecars.discard(self)
            _notify_activity()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
