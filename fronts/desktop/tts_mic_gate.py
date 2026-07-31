"""Синхронний БАР'ЄР автоглушіння TTS перед стартом мікрофона (§9.1, блокер 3).

Озвучка з динаміка = петля в мікрофон + акустичний витік (мілітарі). `rec_state`
емітиться пізно/не завжди, тож пауза по ньому запізнюється. Рішення — СИНХРОННИЙ
`before_microphone_start(reason)`, викликаний ПЕРШИМ у КОЖНІЙ з 6 точок старту
мікрофона, ДО recorder/live-STT start; викликач ОБОВʼЯЗКОВО перевіряє повернений bool
(`if not ...: return`) — це справжній бар'єр, а не косметика (рецензія, критично).

Порядок: зупинити playback (QMediaPlayer лише в GUI-потоці) → cancel synth + завершити
TTS-sidecar + звільнити TTS-lease (координатор) → ДОЧЕКАТИСЯ підтвердженого Stopped/
Paused (confirm_stopped); якщо не підтверджено за timeout → hard-kill TTS і повторна
перевірка. Повертає True лише коли playback ГАРАНТОВАНО зупинений; інакше False —
запис НЕ стартує (краще не писати, ніж писати з витоком озвучки в мікрофон).

Чисте ядро без прямих залежностей на app: уся взаємодія — через передані колбеки."""
from __future__ import annotations


def before_microphone_start(reason, *, stop_playback, coordinator,
                            confirm_stopped=None, hard_kill=None, on_step=None) -> bool:
    """Заглушити/скасувати TTS ДО відкриття мікрофона. Повертає True лише коли
    playback підтверджено зупинений; False → викликач МУСИТЬ перервати старт запису."""
    def _step(name):
        if on_step is not None:
            try:
                on_step(name)
            except Exception:                    # noqa: BLE001
                pass

    _step(f"gate:{reason}")
    # 1. Зупинити відтворення (QMediaPlayer — лише GUI-потік; викликач це гарантує).
    try:
        if stop_playback is not None:
            stop_playback()
    except Exception:                            # noqa: BLE001
        pass
    _step("playback_stopped")
    # 2. Скасувати активний synth + завершити TTS-sidecar + звільнити TTS-lease.
    try:
        if coordinator is not None:
            coordinator.yield_to_microphone()
    except Exception:                            # noqa: BLE001
        pass
    _step("tts_yielded")
    # 3. Дочекатися підтвердженого Stopped/Paused. Без confirm_stopped — безпечний
    #    дефолт True (після yield+shutdown TTS уже завершено; тести/прості точки).
    if confirm_stopped is None:
        _step("confirmed")
        return True
    try:
        ok = bool(confirm_stopped())
    except Exception:                            # noqa: BLE001
        ok = False
    if not ok:
        # не підтвердилось за timeout → hard-kill TTS (примусово мовчить) і перевірка
        try:
            if hard_kill is not None:
                hard_kill()
        except Exception:                        # noqa: BLE001
            pass
        try:
            ok = bool(confirm_stopped())
        except Exception:                        # noqa: BLE001
            ok = False
    _step("confirmed" if ok else "not_confirmed")
    return ok
