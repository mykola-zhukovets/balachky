"""Батьківська сторона TTS-sidecar: керує підпроцесом-воркером і говорить JSON-ом.

Дзеркало whisper_core.protocol.sidecar, але з ПОТОКОВИМ протоколом (§3.2): один
активний synthesize, стрімінг chunk_ready по реченнях, кооперативний cancel із
hard-kill на застряглому. Політика одночасності — PARENT-only (воркер лише повертає
`busy`): latest-wins (playback/прев'ю/хоткей) і reject-busy (експорт).

БЕЗ Qt. Реєстр живих sidecar-ів для idle-lifecycle (any_speaking/idle_transition/
shutdown_all) — координатор HeavyModelCoordinator ухвалює рішення, реєстр — механізм."""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
import weakref
from contextlib import contextmanager

from . import (CANCEL_COOPERATIVE_DEADLINE_S, MSG_ACCEPTED, MSG_BUSY,
               MSG_CANCEL, MSG_CANCELLED, MSG_CHUNK_READY, MSG_ERROR,
               MSG_LOAD_VOICE, MSG_PING, MSG_PONG, MSG_PROGRESS, MSG_RESULT,
               MSG_SHUTDOWN, MSG_SYNTHESIZE, MSG_VOICE_LOADED)


class TtsSidecarError(RuntimeError):
    """Sidecar недоступний, упав або повернув помилку синтезу."""


class TtsSidecarTimeout(TtsSidecarError):
    """Відповідь не надійшла у відведений час."""


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
        except Exception:                    # noqa: BLE001
            pass


def any_speaking() -> bool:
    with _registry_lock:
        return any(sc.running for sc in tuple(_running_sidecars))


@contextmanager
def idle_transition():
    """Бар'єр: TTS не може стартувати між idle-check і unload (той самий lock-order,
    що whisper_core.protocol.sidecar — registry ЗОВНІ, engine ВСЕРЕДИНІ)."""
    with _registry_lock:
        yield


def shutdown_all() -> None:
    with _registry_lock:
        sidecars = tuple(_running_sidecars)
    for sc in sidecars:
        sc.shutdown()


def default_worker_command() -> list:
    """Команда запуску воркера.

    Перевіряє:
      1. Завантажений рушій у %LOCALAPPDATA%\\Balachky\\tts-engine\\balachky-tts-worker.exe
      2. Поруч із GUI-exe у frozen-режимі (balachky-tts-worker.exe)
      3. Модуль розробки `python -m whisper_core.tts.worker`
    """
    from whisper_core import paths
    user_exe = paths.tts_engine_exe_path()
    if user_exe.exists():
        return [str(user_exe)]

    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        worker_exe = os.path.join(exe_dir, "balachky-tts-worker.exe")
        return [worker_exe]

    return [sys.executable, "-m", "whisper_core.tts.worker"]


def engine_available() -> bool:
    """Чи є в цій програмі доступний або завантажений рушій озвучення."""
    from whisper_core import paths
    if paths.tts_engine_exe_path().exists():
        return True
    if getattr(sys, "frozen", False):
        return os.path.exists(default_worker_command()[0])
    return True



def _spawn_kwargs() -> dict:
    kwargs = {}
    if os.name == "nt":
        flags = 0
        flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = flags
    else:
        kwargs["preexec_fn"] = lambda: os.nice(10)   # noqa: PLW1509
    return kwargs


class TtsSidecar:
    """Володіє одним підпроцесом-воркером TTS. Один активний synthesize за раз."""

    def __init__(self, command=None, *, env=None):
        self._command = list(command) if command else default_worker_command()
        self._env = env
        self._proc = None
        self._reader = None
        self._q = queue.Queue()
        self._start_lock = threading.Lock()
        self._closed = False

    # --- життєвий цикл ---
    def start(self) -> None:
        with _registry_lock:
            if self._closed:
                raise TtsSidecarError("Sidecar TTS уже завершено")
            if self._proc is not None:
                return
            full_env = dict(os.environ)
            # frozen: жодного прихованого завантаження під час synth (§12.1)
            full_env.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
            full_env.setdefault("HF_HUB_OFFLINE", "1")
            full_env.setdefault("TRANSFORMERS_OFFLINE", "1")
            full_env.setdefault("PYTHONUTF8", "1")
            full_env.setdefault("PYTHONIOENCODING", "utf-8")
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
                raise TtsSidecarError(f"Не вдалося запустити sidecar TTS: {exc}") from exc
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
                    pass
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(None)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _send(self, payload: dict) -> None:
        if not self.running:
            raise TtsSidecarError("Sidecar TTS не запущено або він уже завершився")
        try:
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise TtsSidecarError(f"Sidecar TTS урвав зв'язок: {exc}") from exc

    def _await(self, match, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TtsSidecarTimeout("Sidecar TTS не відповів вчасно")
            try:
                msg = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TtsSidecarTimeout("Sidecar TTS не відповів вчасно")
            if msg is None:
                rc = self._proc.poll() if self._proc else None
                raise TtsSidecarError(f"Sidecar TTS завершився несподівано (код {rc})")
            if match(msg):
                return msg

    def ping(self, timeout: float = 5.0) -> bool:
        if self._proc is None:
            self.start()
        self._send({"type": MSG_PING})
        msg = self._await(lambda m: m.get("type") in (MSG_PONG, MSG_ERROR), timeout)
        return msg.get("type") == MSG_PONG

    def load_voice(self, voice_id: str, *, engine: str, manifest_path: str,
                   voice_rev="", timeout: float = 120.0) -> dict:
        """Завантажити голос за ВАЛІДОВАНИМ абсолютним локальним manifest_path
        (§3.2). Повний unload попереднього на боці воркера (§7.7)."""
        req_id = uuid.uuid4().hex
        if self._proc is None:
            self.start()
        self._send({"type": MSG_LOAD_VOICE, "id": req_id, "voice_id": voice_id,
                    "engine": engine, "manifest_path": manifest_path,
                    "voice_rev": voice_rev})
        msg = self._await(
            lambda m: m.get("id") == req_id
            and m.get("type") in (MSG_VOICE_LOADED, MSG_ERROR), timeout)
        if msg.get("type") == MSG_ERROR:
            raise TtsSidecarError(msg.get("message") or "Не вдалося завантажити голос")
        return msg

    def synthesize_stream(self, *, text: str, voice_id: str, wav_dir: str,
                          source_start_cp: int = 0, lexicon_snapshot=None,
                          lexicon_rev="", voice_rev="", want_timings: bool = False,
                          engine: str = "", on_event=None, chunk_timeout: float = 120.0):
        """Запустити synthesize й проганяти події (accepted/progress/chunk_ready/
        result/error/cancelled) через `on_event`. Повертає id запиту.

        Один активний запит: `busy` → TtsSidecarError (parent-політика вирішує вище,
        це страхувальник від гонки)."""
        req_id = uuid.uuid4().hex
        if self._proc is None:
            self.start()
        self._send({"type": MSG_SYNTHESIZE, "id": req_id, "text": text,
                    "voice_id": voice_id, "source_start_cp": source_start_cp,
                    "lexicon_snapshot": lexicon_snapshot or [],
                    "lexicon_rev": lexicon_rev, "voice_rev": voice_rev,
                    "want_timings": want_timings, "engine": engine,
                    "wav_dir": wav_dir})
        first = self._await(
            lambda m: m.get("id") == req_id
            and m.get("type") in (MSG_ACCEPTED, MSG_BUSY, MSG_ERROR), chunk_timeout)
        if first.get("type") == MSG_BUSY:
            raise TtsSidecarError("Воркер зайнятий іншим синтезом")
        if first.get("type") == MSG_ERROR:
            raise TtsSidecarError(first.get("message") or "Помилка синтезу")
        if on_event:
            on_event(first)
        # тягнемо події до result/cancelled/error
        while True:
            msg = self._await(
                lambda m: m.get("id") == req_id and m.get("type") in (
                    MSG_PROGRESS, MSG_CHUNK_READY, MSG_RESULT, MSG_CANCELLED,
                    MSG_ERROR), chunk_timeout)
            if on_event:
                on_event(msg)
            if msg.get("type") in (MSG_RESULT, MSG_CANCELLED):
                return req_id
            if msg.get("type") == MSG_ERROR:
                raise TtsSidecarError(msg.get("message") or "Помилка синтезу")

    def cancel(self, req_id: str, *, deadline: float = CANCEL_COOPERATIVE_DEADLINE_S) -> bool:
        """Кооперативний cancel: шлемо cancel{id}, чекаємо `cancelled` до дедлайну;
        якщо застряг у нативному forward — hard-kill (батько спавнить новий процес
        наступним запитом). Повертає True, якщо cancelled прийшов кооперативно."""
        if not self.running:
            return True
        try:
            self._send({"type": MSG_CANCEL, "id": req_id})
        except TtsSidecarError:
            return True
        try:
            self._await(lambda m: m.get("id") == req_id
                        and m.get("type") == MSG_CANCELLED, deadline)
            return True
        except (TtsSidecarTimeout, TtsSidecarError):
            self.hard_kill()                    # застряг → hard-kill
            return False

    def hard_kill(self) -> None:
        """Негайно вбити процес (застряглий нативний forward, §3.2). Наступний
        запит підніме свіжий процес."""
        proc = self._proc
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        self._teardown()

    def _teardown(self) -> None:
        if self._proc is None:
            return
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

    def shutdown(self, timeout: float = 5.0) -> None:
        self._closed = True
        if self._proc is None:
            return
        try:
            if self.running:
                try:
                    self._send({"type": MSG_SHUTDOWN})
                except TtsSidecarError:
                    pass
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    logging.getLogger(__name__).warning(
                        "tts-sidecar не завершився навіть після kill() — можливий зомбі")
        finally:
            self._teardown()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.shutdown()
