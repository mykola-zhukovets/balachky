"""Shared reset for process-global state that must not leak between tests."""
import os

# Тестові/offscreen процеси НЕ повинні займати робочий канал single-instance
# "balachky-single": 31.07 unittest-гейт тримав QLocalServer живим офскрін, і
# власник, запустивши run_app.py, отримав хибне «вже запущені». Суфікс
# унікальний на процес; читає fronts.desktop.app._instance_channel_name().
# setdefault — модуль можна імпортувати кілька разів (discover імпортує кожен
# test-файл), суфікс має лишатись тим самим у межах одного процесу.
os.environ.setdefault("BALACHKY_INSTANCE_SUFFIX", f"-test-{os.getpid()}")


def reset_process_caches() -> None:
    """Restore mutable process state to the defaults of a fresh app process."""
    from fronts.desktop import i18n, theme
    from whisper_core import autocorrect, updater
    from whisper_core.meeting import storage_crypto
    from whisper_core.protocol import model_manager
    from whisper_core.tts import voices

    for module in (model_manager, voices, updater, autocorrect):
        with module._INTEGRITY_CACHE_LOCK:
            module._INTEGRITY_CACHE.clear()
    storage_crypto._PASSWORD_CACHE.clear()

    if i18n.current_language() != "uk":
        i18n.set_language("uk")
    if theme.current_ui_color() != "classic":
        theme.set_ui_color("classic")
