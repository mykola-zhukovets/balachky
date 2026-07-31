"""Оркестрація AI-протоколу: звʼязує модель-менеджер + sidecar + генерацію в одну
точку входу для UI. Сюди ж зведено обробку помилок (E5): краш сайдкара / брак моделі
/ скасування дають зрозумілі повідомлення й лог у balachky.log.

Межа модуля: БЕЗ Qt. UI (кнопка/прогрес/скасування) тримає екземпляр ProtocolGenerator,
кличе run() у власному потоці й cancel() при потребі.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import (DEFAULT_N_CTX, DEFAULT_N_GPU_LAYERS, DEFAULT_TEMPERATURE,
               FAKE_BACKEND_MARKER)
from . import command_edit as _ce
from . import generate as _gen
from . import model_manager as _mm
from . import qa as _qa
from . import rewrite as _rw
from .sidecar import Sidecar, SidecarError
# save_protocol пише через ядро сесії — top-level, щоб гарантовано не впасти на
# plaintext-фолбек (збереження шифрування запечатаних сесій).
from whisper_core.meeting.session import write_artifact

_log = logging.getLogger("balachky.protocol")

PROTOCOL_FILENAME = "protocol.md"

# Показуємо, коли бекенд LLM недоступний (llama-cpp-python не встановлено або
# вихід — заглушка FakeBackend): краще чесна помилка, ніж сміття замість протоколу.
BACKEND_UNAVAILABLE_MSG = (
    "Модель мовної генерації недоступна. Встановіть компонент мовної моделі "
    "(llama-cpp-python) і завантажте модель протоколу, щоб створювати протоколи.")


def backend_available() -> bool:
    """Чи встановлено бекенд генерації llama-cpp-python (опційна залежність).
    UI перевіряє це ПЕРЕД генерацією, щоб не видавати заглушку за протокол."""
    from .worker import llama_available
    return llama_available()


class ProtocolError(RuntimeError):
    """Загальна помилка генерації протоколу (зрозуміле повідомлення для UI)."""


class ProtocolModelMissing(ProtocolError):
    """Модель пресета ще не завантажена."""


class ProtocolCancelled(ProtocolError):
    """Користувач скасував генерацію."""


def _models_root(model_root=None) -> Path:
    """Корінь теки моделей: тести передають власний, продакшн → paths."""
    if model_root is not None:
        return Path(model_root)
    from .. import paths
    return paths.protocol_models_dir()


def model_available(active_id: str, model_root=None, custom_models=None) -> bool:
    """Чи готова АКТИВНА модель (пресет або власна). Невідомий id → False (без
    тихого фолбеку на інший пресет)."""
    resolved = _mm.resolve(active_id, _models_root(model_root), custom_models or [])
    return bool(resolved is not None and resolved.available())


def _resolve_ready_path(active_id, model_root, custom_models, missing_exc):
    """Активна модель → шлях до файлу; якщо її немає/недоступна — чесна помилка
    (НЕ підміняємо іншою моделлю нишком). Спільне для всіх трьох генераторів."""
    resolved = _mm.resolve(active_id, _models_root(model_root), custom_models or [])
    if resolved is None or not resolved.integrity_available():
        raise missing_exc("Модель мовної генерації ще не завантажена")
    return str(resolved.model_path)


def save_protocol(session_dir, markdown: str) -> Path:
    """Атомарно зберегти protocol.md поруч із записом наради. Повертає шлях.

    Пишемо ЛИШЕ через session.write_artifact: у запечатаній сесії воно кладе
    protocol.md.enc і ніколи не лишає plaintext-sidecar. Модуль session — ядро,
    що завжди наявне, тож окремий tempfile-фолбек лише ризикував би відкритим
    текстом і тут не потрібен."""
    session_dir = Path(session_dir)
    write_artifact(session_dir, PROTOCOL_FILENAME, markdown.encode("utf-8"))
    return session_dir / PROTOCOL_FILENAME


class ProtocolGenerator:
    """Один прогін генерації протоколу. Тримає sidecar на час run(); cancel()
    вбиває його між кроками (single-pass переривається killом процесу).

    worker_command/env — інжекція для тестів (фейк-воркер без llama)."""

    def __init__(self, preset_id: str, *, model_root=None, custom_models=None,
                 n_ctx: int = DEFAULT_N_CTX, n_gpu_layers: int = DEFAULT_N_GPU_LAYERS,
                 temperature: float = DEFAULT_TEMPERATURE, generate_timeout: float = 900.0,
                 worker_command=None, env=None):
        self._preset_id = str(preset_id or "")   # активний id (пресет або власний)
        self._model_root = model_root
        self._custom_models = custom_models
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._temperature = temperature
        self._timeout = generate_timeout
        self._worker_command = worker_command
        self._env = env
        self._sidecar = None
        self._cancelled = False

    def available(self) -> bool:
        return model_available(self._preset_id, self._model_root, self._custom_models)

    def cancel(self) -> None:
        """Скасувати: підняти прапорець і вбити сайдкар (перерве активний generate)."""
        self._cancelled = True
        sc = self._sidecar
        if sc is not None:
            try:
                sc.shutdown(timeout=2.0)
            except Exception:                # noqa: BLE001 — cancel має бути безпечним
                pass

    def run(self, utterances, *, me_label: str = "Я",
            others_label: str = "Співрозмовники", speaker_names=None,
            max_tokens: int = 2048) -> str:
        """Згенерувати Markdown-протокол. Кидає ProtocolModelMissing / ProtocolCancelled
        / ProtocolError (усе логуючи). Порожній транскрипт → порожній рядок."""
        model_path = _resolve_ready_path(
            self._preset_id, self._model_root, self._custom_models, ProtocolModelMissing)
        self._sidecar = Sidecar(command=self._worker_command, env=self._env)

        def generate_fn(prompt, *, max_tokens=max_tokens, n_ctx=None):
            if self._cancelled:
                raise ProtocolCancelled("Генерацію протоколу скасовано")
            return self._sidecar.generate(
                prompt, model_path=model_path,
                n_ctx=n_ctx if n_ctx is not None else self._n_ctx,
                max_tokens=max_tokens, temperature=self._temperature,
                n_gpu_layers=self._n_gpu_layers, timeout=self._timeout)

        try:
            self._sidecar.start()
            result = _gen.generate_protocol(
                utterances, generate_fn, me_label=me_label,
                others_label=others_label, speaker_names=speaker_names,
                max_tokens=max_tokens)
        except ProtocolCancelled:
            raise
        except _gen.ProtocolContextOverflow as exc:
            # Детермінований overflow — чесна конкретна помилка, НЕ «спробуйте ще раз».
            _log.error("Транскрипт не влазить у контекст навіть частинами: %s", exc)
            raise ProtocolError(
                "Нарада надто велика для обробки навіть частинами. "
                "Спробуйте розбити запис на коротші частини.") from exc
        except SidecarError as exc:
            if self._cancelled:
                raise ProtocolCancelled("Генерацію протоколу скасовано") from exc
            _log.exception("Сайдкар LLM урвався під час генерації протоколу")
            raise ProtocolError(
                "Помічник із протоколом несподівано зупинився. Спробуйте ще раз."
            ) from exc
        except Exception as exc:             # noqa: BLE001
            _log.exception("Помилка генерації протоколу наради")
            raise ProtocolError(f"Не вдалося створити протокол: {exc}") from exc
        finally:
            sc, self._sidecar = self._sidecar, None
            if sc is not None:
                try:
                    sc.shutdown()
                except Exception:            # noqa: BLE001
                    pass
        if self._cancelled:
            raise ProtocolCancelled("Генерацію протоколу скасовано")
        # Гейт «тихої заглушки»: якщо бекенд недоступний (немає llama-cpp-python),
        # worker бере FakeBackend, чий generate повертає рядок-заглушку. Він НЕ
        # схожий на протокол — відхиляємо, щоб не зберегти сміття як успіх.
        if result.strip() and not _gen.is_valid_protocol(result):
            _log.error("Вихід LLM не є протоколом — бекенд недоступний "
                       "або повернув заглушку")
            raise ProtocolError(BACKEND_UNAVAILABLE_MSG)
        return result


# --- Q&A по нараді -----------------------------------------------------------

class QAError(RuntimeError):
    """Загальна помилка Q&A по нараді (зрозуміле повідомлення для UI)."""


class QAModelMissing(QAError):
    """Модель пресета ще не завантажена."""


class QACancelled(QAError):
    """Користувач скасував відповідь."""


class QAGenerator:
    """Одна відповідь на питання по нараді. Тримає sidecar на час run();
    cancel() вбиває його між кроками. Той самий бекенд/модель, що AI-протокол —
    інший лише промт (whisper_core.protocol.qa). worker_command/env — інжекція
    для тестів (фейк-воркер без llama)."""

    def __init__(self, preset_id: str, *, model_root=None, custom_models=None,
                 n_ctx: int = DEFAULT_N_CTX, n_gpu_layers: int = DEFAULT_N_GPU_LAYERS,
                 temperature: float = DEFAULT_TEMPERATURE, generate_timeout: float = 900.0,
                 worker_command=None, env=None):
        self._preset_id = str(preset_id or "")   # активний id (пресет або власний)
        self._model_root = model_root
        self._custom_models = custom_models
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._temperature = temperature
        self._timeout = generate_timeout
        self._worker_command = worker_command
        self._env = env
        self._sidecar = None
        self._cancelled = False

    def available(self) -> bool:
        return model_available(self._preset_id, self._model_root, self._custom_models)

    def cancel(self) -> None:
        """Скасувати: підняти прапорець і вбити сайдкар (перерве активний generate)."""
        self._cancelled = True
        sc = self._sidecar
        if sc is not None:
            try:
                sc.shutdown(timeout=2.0)
            except Exception:                # noqa: BLE001 — cancel має бути безпечним
                pass

    def run(self, question, utterances, *, me_label: str = "Я",
            others_label: str = "Співрозмовники", speaker_names=None,
            max_tokens: int = 1024) -> str:
        """Відповісти на питання по нараді. Кидає QAModelMissing / QACancelled /
        QAError (усе логуючи). Порожнє питання/транскрипт → порожній рядок."""
        model_path = _resolve_ready_path(
            self._preset_id, self._model_root, self._custom_models, QAModelMissing)
        self._sidecar = Sidecar(command=self._worker_command, env=self._env)

        def generate_fn(prompt, *, max_tokens=max_tokens, n_ctx=None):
            if self._cancelled:
                raise QACancelled("Відповідь скасовано")
            return self._sidecar.generate(
                prompt, model_path=model_path,
                n_ctx=n_ctx if n_ctx is not None else self._n_ctx,
                max_tokens=max_tokens, temperature=self._temperature,
                n_gpu_layers=self._n_gpu_layers, timeout=self._timeout)

        try:
            self._sidecar.start()
            result = _qa.answer_question(
                question, utterances, generate_fn, me_label=me_label,
                others_label=others_label, speaker_names=speaker_names,
                max_tokens=max_tokens)
        except QACancelled:
            raise
        except SidecarError as exc:
            if self._cancelled:
                raise QACancelled("Відповідь скасовано") from exc
            _log.exception("Сайдкар LLM урвався під час відповіді на питання")
            raise QAError(
                "Помічник несподівано зупинився. Спробуйте ще раз."
            ) from exc
        except Exception as exc:             # noqa: BLE001
            _log.exception("Помилка відповіді на питання по нараді")
            raise QAError(f"Не вдалося отримати відповідь: {exc}") from exc
        finally:
            sc, self._sidecar = self._sidecar, None
            if sc is not None:
                try:
                    sc.shutdown()
                except Exception:            # noqa: BLE001
                    pass
        if self._cancelled:
            raise QACancelled("Відповідь скасовано")
        # Гейт «тихої заглушки»: без llama-cpp-python worker бере FakeBackend, чий
        # generate повертає рядок-позначку (FAKE_BACKEND_MARKER). Відповідь вільна,
        # тож is_valid_protocol тут не годиться — ловимо позначку і порожнечу
        # напряму, щоб НЕ показати заглушку/пустку за успіх (урок рецензента).
        if FAKE_BACKEND_MARKER in result:
            _log.error("Вихід LLM — заглушка FakeBackend: бекенд недоступний")
            raise QAError(BACKEND_UNAVAILABLE_MSG)
        # Порожнє питання/транскрипт дають "" легітимно (UI не викликає з таким);
        # порожня відповідь на РЕАЛЬНЕ питання — збій, не показуємо як успіх.
        if question and str(question).strip() and utterances and not result.strip():
            raise QAError("Порожня відповідь від моделі. Спробуйте ще раз.")
        return result


# --- Переформатування надиктованого тексту (feature/output-formats) ----------

class RewriteError(RuntimeError):
    """Загальна помилка AI-переформатування (зрозуміле повідомлення для UI)."""


class RewriteModelMissing(RewriteError):
    """Модель пресета ще не завантажена."""


class RewriteCancelled(RewriteError):
    """Користувач скасував переформатування."""


class RewriteGenerator:
    """Один прогін AI-переформатування надиктованого тексту. Той самий бекенд і
    модель, що AI-протокол/Q&A — інший лише промт (whisper_core.protocol.rewrite).
    Тримає sidecar на час run(); cancel() вбиває його. worker_command/env —
    інжекція для тестів (фейк-воркер без llama)."""

    def __init__(self, preset_id: str, *, model_root=None, custom_models=None,
                 n_ctx: int = DEFAULT_N_CTX, n_gpu_layers: int = DEFAULT_N_GPU_LAYERS,
                 temperature: float = DEFAULT_TEMPERATURE, generate_timeout: float = 900.0,
                 worker_command=None, env=None):
        self._preset_id = str(preset_id or "")   # активний id (пресет або власний)
        self._model_root = model_root
        self._custom_models = custom_models
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._temperature = temperature
        self._timeout = generate_timeout
        self._worker_command = worker_command
        self._env = env
        self._sidecar = None
        self._cancelled = False

    def available(self) -> bool:
        return model_available(self._preset_id, self._model_root, self._custom_models)

    def cancel(self) -> None:
        """Скасувати: підняти прапорець і вбити сайдкар (перерве активний generate)."""
        self._cancelled = True
        sc = self._sidecar
        if sc is not None:
            try:
                sc.shutdown(timeout=2.0)
            except Exception:                # noqa: BLE001 — cancel має бути безпечним
                pass

    def run(self, text: str, template_id: str, *, custom_prompt: "str | None" = None,
            max_tokens: int = 1024) -> str:
        """Переписати ``text`` за шаблоном/власним промтом. Кидає
        RewriteModelMissing / RewriteCancelled / RewriteError (усе логуючи).
        Порожній текст → порожній рядок."""
        text = (text or "").strip()
        if not text:
            return ""
        model_path = _resolve_ready_path(
            self._preset_id, self._model_root, self._custom_models, RewriteModelMissing)
        self._sidecar = Sidecar(command=self._worker_command, env=self._env)

        def generate_fn(prompt, *, max_tokens=max_tokens, n_ctx=None):
            if self._cancelled:
                raise RewriteCancelled("Переформатування скасовано")
            return self._sidecar.generate(
                prompt, model_path=model_path,
                n_ctx=n_ctx if n_ctx is not None else self._n_ctx,
                max_tokens=max_tokens, temperature=self._temperature,
                n_gpu_layers=self._n_gpu_layers, timeout=self._timeout)

        try:
            self._sidecar.start()
            result = _rw.rewrite_text(
                text, template_id, generate_fn,
                custom_prompt=custom_prompt, max_tokens=max_tokens)
        except RewriteCancelled:
            raise
        except _rw.UnknownTemplate as exc:
            raise RewriteError(f"Невідомий шаблон переформатування: {exc}") from exc
        except SidecarError as exc:
            if self._cancelled:
                raise RewriteCancelled("Переформатування скасовано") from exc
            _log.exception("Сайдкар LLM урвався під час переформатування тексту")
            raise RewriteError(
                "Помічник несподівано зупинився. Спробуйте ще раз."
            ) from exc
        except Exception as exc:             # noqa: BLE001
            _log.exception("Помилка AI-переформатування тексту")
            raise RewriteError(f"Не вдалося переформатувати: {exc}") from exc
        finally:
            sc, self._sidecar = self._sidecar, None
            if sc is not None:
                try:
                    sc.shutdown()
                except Exception:            # noqa: BLE001
                    pass
        if self._cancelled:
            raise RewriteCancelled("Переформатування скасовано")
        # Гейт «тихої заглушки» (урок рецензента): без llama-cpp-python worker бере
        # FakeBackend, чий generate повертає FAKE_BACKEND_MARKER. Вихід вільний,
        # тож is_valid_protocol тут не годиться — ловимо позначку і порожнечу
        # напряму, щоб НЕ показати заглушку/пустку за успіх.
        if FAKE_BACKEND_MARKER in result:
            _log.error("Вихід LLM — заглушка FakeBackend: бекенд недоступний")
            raise RewriteError(BACKEND_UNAVAILABLE_MSG)
        if not result.strip():
            raise RewriteError("Порожній результат від моделі. Спробуйте ще раз.")
        return result


# --- Command Mode: голосове редагування виділеного (feature/voice-edit-selection) ---

class CommandEditError(RuntimeError):
    """Загальна помилка голосового редагування виділеного (зрозуміле для UI)."""


class CommandEditModelMissing(CommandEditError):
    """Модель пресета ще не завантажена."""


class CommandEditCancelled(CommandEditError):
    """Користувач скасував редагування."""


class CommandEditGenerator:
    """Один прогін голосового редагування виділеного тексту: виділений фрагмент +
    голосова команда → переписаний фрагмент. Той самий бекенд/модель, що
    AI-протокол/Q&A — інший лише промт (whisper_core.protocol.command_edit).
    Тримає sidecar на час run(); cancel() вбиває його. worker_command/env —
    інжекція для тестів (фейк-воркер без llama)."""

    def __init__(self, preset_id: str, *, model_root=None, custom_models=None,
                 n_ctx: int = DEFAULT_N_CTX, n_gpu_layers: int = DEFAULT_N_GPU_LAYERS,
                 temperature: float = DEFAULT_TEMPERATURE, generate_timeout: float = 900.0,
                 worker_command=None, env=None):
        self._preset_id = str(preset_id or "")   # активний id (пресет або власний)
        self._model_root = model_root
        self._custom_models = custom_models
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._temperature = temperature
        self._timeout = generate_timeout
        self._worker_command = worker_command
        self._env = env
        self._sidecar = None
        self._cancelled = False

    def available(self) -> bool:
        return model_available(self._preset_id, self._model_root, self._custom_models)

    def cancel(self) -> None:
        """Скасувати: підняти прапорець і вбити сайдкар (перерве активний generate)."""
        self._cancelled = True
        sc = self._sidecar
        if sc is not None:
            try:
                sc.shutdown(timeout=2.0)
            except Exception:                # noqa: BLE001 — cancel має бути безпечним
                pass

    def run(self, selected_text: str, command: str, *, max_tokens: int = 1024) -> str:
        """Переписати ``selected_text`` за голосовою командою ``command``. Кидає
        CommandEditModelMissing / CommandEditCancelled / CommandEditError (усе
        логуючи). Порожній текст або порожня команда → порожній рядок."""
        selected_text = (selected_text or "").strip()
        command = (command or "").strip()
        if not selected_text or not command:
            return ""
        if self._cancelled:
            raise CommandEditCancelled("Редагування скасовано")
        model_path = _resolve_ready_path(
            self._preset_id, self._model_root, self._custom_models,
            CommandEditModelMissing)
        self._sidecar = Sidecar(command=self._worker_command, env=self._env)

        def generate_fn(prompt, *, max_tokens=max_tokens, n_ctx=None):
            if self._cancelled:
                raise CommandEditCancelled("Редагування скасовано")
            return self._sidecar.generate(
                prompt, model_path=model_path,
                n_ctx=n_ctx if n_ctx is not None else self._n_ctx,
                max_tokens=max_tokens, temperature=self._temperature,
                n_gpu_layers=self._n_gpu_layers, timeout=self._timeout)

        try:
            self._sidecar.start()
            result = _ce.apply_command(
                selected_text, command, generate_fn, max_tokens=max_tokens)
        except CommandEditCancelled:
            raise
        except SidecarError as exc:
            if self._cancelled:
                raise CommandEditCancelled("Редагування скасовано") from exc
            _log.exception("Сайдкар LLM урвався під час голосового редагування")
            raise CommandEditError(
                "Помічник несподівано зупинився. Спробуйте ще раз."
            ) from exc
        except Exception as exc:             # noqa: BLE001
            _log.exception("Помилка голосового редагування виділеного")
            raise CommandEditError(f"Не вдалося відредагувати: {exc}") from exc
        finally:
            sc, self._sidecar = self._sidecar, None
            if sc is not None:
                try:
                    sc.shutdown()
                except Exception:            # noqa: BLE001
                    pass
        if self._cancelled:
            raise CommandEditCancelled("Редагування скасовано")
        # Гейт «тихої заглушки» (урок рецензента): без llama-cpp-python worker бере
        # FakeBackend, чий generate повертає FAKE_BACKEND_MARKER. Вихід вільний,
        # тож is_valid_protocol тут не годиться — ловимо позначку і порожнечу
        # напряму, щоб НЕ заміняти виділення заглушкою/пусткою.
        if FAKE_BACKEND_MARKER in result:
            _log.error("Вихід LLM — заглушка FakeBackend: бекенд недоступний")
            raise CommandEditError(BACKEND_UNAVAILABLE_MSG)
        if not result.strip():
            raise CommandEditError("Порожній результат від моделі. Спробуйте ще раз.")
        return result
