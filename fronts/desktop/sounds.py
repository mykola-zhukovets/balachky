"""Звукові сигнали: короткі гудки через winsound (stdlib, лише Windows).

Beep блокує потік — граємо в daemon-потоці. Будь-яка помилка (не-Windows,
нема аудіопристрою) ковтається мовчки: звук — не критична функція.
"""
import threading

_TONES = {
    "paste": (1300, 45),   # feature/qol-pack: підтвердження вставки (короткий, вищий)
}


def play(kind: str) -> None:
    if kind not in _TONES:
        return

    def _beep(freq=_TONES[kind][0], ms=_TONES[kind][1]):
        try:
            import winsound
            winsound.Beep(freq, ms)
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True).start()
