"""Синглтон DownloadManager: одне фонове завантаження моделі AI-протоколу
одночасно, НЕЗАЛЕЖНЕ від будь-якого відкритого діалогу.

Причина: модальний ProtocolModelDownloadDialog блокував усю програму на весь
час качання 4.6 ГБ моделі: людина не могла ні диктувати, ні переглядати
наради, поки чекала.

Дизайн: сам процес завантаження лишається у ProtocolModelDownloadWorker
(protocol_ui.py, той самий QThread, що якісно тягне GGUF і рахує прогрес).
DownloadManager лише ВОЛОДІЄ активним воркером незалежно від будь-якого вікна:
відкриті сторінки (нарада, редагування команд, Налаштування → ModelsHub)
підписуються на його сигнали й реактивно оновлюються, а сам діалог поступу
стає «переглядачем» стану менеджера — його можна закрити (не скасовуючи
докачку) чи знову відкрити.

Дозволено РІВНО одне завантаження одночасно (§3.2 спеки): друга спроба для
ТІЄЇ Ж моделі приєднується (already_this), спроба для ІНШОЇ моделі відхиляється
(busy_other) — виклик вирішує, що показати користувачу (конфлікт-діалог).
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, Signal

STARTED = "started"
ALREADY_THIS = "already_this"
BUSY_OTHER = "busy_other"


def key_for(target_dir) -> str:
    """Канонічний ключ моделі — абсолютний шлях до її теки."""
    return os.path.abspath(os.fspath(target_dir))


class DownloadManager(QObject):
    # (key, done, total) — done/total у байтах, ще без дроселя (кожен віджет
    # дроселить малювання сам, як ProtocolModelDownloadDialog._on_progress).
    progress = Signal(str, object, object)
    started = Signal(str, str)                  # (key, label)
    finished_ok = Signal(str)                   # (key,)
    failed = Signal(str, str)                   # (key, message)
    cancelled = Signal(str)                     # (key,)

    _instance = None

    @classmethod
    def instance(cls) -> "DownloadManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        super().__init__()
        self._worker = None
        self._key = None
        self._label = ""
        self._done = 0
        self._total = 0
        self._started_at = None
        self._explicit_cancel = False    # True лише для user-ініційованого cancel_download()

    # ------------------------------------------------------------- запити стану
    def is_busy(self) -> bool:
        return self._worker is not None

    def active_key(self):
        return self._key

    def active_label(self) -> str:
        return self._label

    def is_downloading(self, target_dir) -> bool:
        return self._worker is not None and self._key == key_for(target_dir)

    def progress_for(self, target_dir):
        """(done, total) для цієї моделі, якщо саме вона качається зараз — інакше None."""
        if not self.is_downloading(target_dir):
            return None
        return self._done, self._total

    def elapsed_seconds(self) -> "float | None":
        if self._started_at is None or not self.is_busy():
            return None
        return time.monotonic() - self._started_at

    # --------------------------------------------------------------- керування
    def start_download(self, target_dir, *, preset_id=None, custom=None,
                        force=False, label="") -> str:
        """Почати (чи приєднатися до) фонового завантаження.

        Повертає STARTED (нове завантаження почалось), ALREADY_THIS (ця сама
        модель уже качається — прогрес продовжує йти, дублікат НЕ створено) або
        BUSY_OTHER (качається ІНША модель — виклик має спитати користувача:
        зачекати чи скасувати поточне)."""
        key = key_for(target_dir)
        if self._worker is not None:
            return ALREADY_THIS if self._key == key else BUSY_OTHER

        from .pages.protocol_ui import ProtocolModelDownloadWorker
        self._key = key
        self._label = label
        self._done = 0
        self._total = 0
        self._started_at = time.monotonic()
        self._explicit_cancel = False
        worker = ProtocolModelDownloadWorker(
            target_dir, preset_id=preset_id, custom=custom, force=force)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_finished_ok)
        worker.failed.connect(self._on_failed)
        worker.cancelled.connect(self._on_cancelled)
        self._worker = worker
        worker.start()
        self.started.emit(key, label)
        return STARTED

    def cancel_download(self, target_dir=None) -> None:
        """Явне скасування (кнопка «Скасувати»): прибирає частковий файл —
        на відміну від закриття програми (drain_workers чіпляє той самий
        worker.cancel(), але БЕЗ прапорця explicit → .part лишається, §4 спеки)."""
        if self._worker is None:
            return
        if target_dir is not None and self._key != key_for(target_dir):
            return
        self._explicit_cancel = True
        self._worker.cancel()

    # ---------------------------------------------------------------- сигнали
    def _on_progress(self, done, total):
        self._done, self._total = done, total
        self.progress.emit(self._key, done, total)

    def _detach(self):
        worker = self._worker
        self._worker = None
        if worker is None:
            return None
        try:
            worker.progress.disconnect()
            worker.finished_ok.disconnect()
            worker.failed.disconnect()
            worker.cancelled.disconnect()
        except (RuntimeError, TypeError):
            pass
        from .onboarding import _reap_worker
        _reap_worker(worker)
        return worker

    def _on_finished_ok(self):
        key = self._key
        self._detach()
        self._key = None
        self._explicit_cancel = False
        self.finished_ok.emit(key)

    def _on_failed(self, msg):
        key = self._key
        self._detach()
        self._key = None
        self._explicit_cancel = False
        self.failed.emit(key, msg)

    def _on_cancelled(self):
        key = self._key
        target_dir = key   # key == абсолютний шлях до теки моделі
        explicit = self._explicit_cancel
        self._detach()
        self._key = None
        self._explicit_cancel = False
        if explicit and target_dir is not None:
            from whisper_core.protocol import model_manager as mm
            mm.discard_partial_download(target_dir)
        self.cancelled.emit(key)
