"""Глобальний push-to-talk хук. Callback у keyboard-потоці — ЛИШЕ emit сигнал.

Клавіша може бути одиночною ("f23") або комбінацією ("ctrl+shift+space") —
не в усіх є вільна окрема клавіша. Для комбінації: старт, коли затиснуто все
разом (add_hotkey зі suppress — глушить основну клавішу ЛИШЕ поки ціле комбо
затиснуте, тож звичайний Space поза комбо друкується нормально); стоп (у режимі
утримання) — відпускання БУДЬ-ЯКОГО компонента комбінації (Ctrl, Shift чи
основної). Останнє — через загальний хук із нормалізацією імені: on_release_key
на окремий компонент ловив би лише один бік модифікатора (це й був баг «клавіша
відпущена, а запис триває»).
"""
import logging

from PySide6.QtCore import QObject, Signal

_DEFAULT_KEY = "ctrl+shift+space"

# ліві/праві варіанти модифікаторів → канонічне ім'я (keyboard шле «left ctrl»
# для лівого і «ctrl»/«right ctrl» для правого — зводимо до одного)
_MOD_ALIASES = {
    "left ctrl": "ctrl", "right ctrl": "ctrl",
    "left shift": "shift", "right shift": "shift",
    "left alt": "alt", "right alt": "alt", "alt gr": "alt",
    "left windows": "windows", "right windows": "windows",
    "left cmd": "windows", "right cmd": "windows",
}


def normalize_name(name: str) -> str:
    """Ім'я клавіші з keyboard-події → канонічне (ліві/праві модифікатори зведені,
    решта — нижній регістр). Порожнє ім'я → "" (безпечно для зіставлення)."""
    n = (name or "").lower()
    return _MOD_ALIASES.get(n, n)


def combo_signature(key: str) -> frozenset:
    """Комбінація → множина канонічних компонентів (порядок/регістр/сторона
    модифікатора не важать): «shift+ctrl+space» == «ctrl+shift+space».
    Порожній ключ → порожня множина. feature/qol-pack."""
    return frozenset(normalize_name(p.strip())
                     for p in (key or "").split("+") if p.strip())


def combos_equal(a: str, b: str) -> bool:
    """Чи дві комбінації — одна й та сама клавіша (канонічно). Порожні не
    конфліктують ні з чим. feature/qol-pack: валідація конфліктів хоткеїв."""
    sa, sb = combo_signature(a), combo_signature(b)
    return bool(sa) and sa == sb


def pretty(key: str) -> str:
    """«ctrl+shift+space» → «Ctrl + Shift + Space» для показу користувачу."""
    labels = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
              "windows": "Win", "space": "Space"}
    parts = [p.strip() for p in (key or "").split("+") if p.strip()]
    return " + ".join(labels.get(p.lower(), p.upper()) for p in parts)


class Hotkey(QObject):
    pressed = Signal()
    released = Signal()

    def __init__(self, key: str):
        super().__init__()
        self._key = key
        self._combo_parts = set()   # нормалізовані компоненти комбінації
        self._active = False        # комбінація зараз повністю затиснута
        # пошкоджений/невідомий ключ із config.toml (keyboard кидає ValueError)
        # НЕ має валити старт застосунку — тихий відкат на дефолт-комбінацію
        if not self._hook(key) and key != _DEFAULT_KEY:
            self._key = _DEFAULT_KEY
            self._hook(_DEFAULT_KEY)

    def _hook(self, key: str) -> bool:
        """Повісити хуки на клавішу/комбінацію. True — успіх, False — невідома
        клавіша (keyboard.ValueError) чи інша помилка реєстрації."""
        # keyboard лениво: legacy-бекенд; native-режим цю бібліотеку не вантажить
        import keyboard
        try:
            if "+" in key:
                # комбінація: add_hotkey стріляє (і suppress'ить), коли всі клавіші
                # затиснуті разом; стоп — відпускання будь-якого компонента (нижче)
                self._combo_parts = {normalize_name(p) for p in key.split("+")}
                self._active = False
                keyboard.add_hotkey(key, self._on_combo_press, suppress=True)
                # загальний хук ловить «up» кожного компонента (і лівий, і правий
                # модифікатор) — надійне утримання без «завислого» запису
                keyboard.hook(self._on_combo_event)
            else:
                # suppress=True: клавіша не «проходить» далі в активне вікно
                keyboard.on_press_key(key, lambda e: self.pressed.emit(), suppress=True)
                keyboard.on_release_key(key, lambda e: self.released.emit(), suppress=True)
            return True
        except Exception:
            logging.exception("Не вдалося повісити хук на клавішу «%s»", key)
            return False

    def _on_combo_press(self):
        # add_hotkey може повторно стрельнути на авто-повтор основної клавіші —
        # гейт _active дає один pressed на одне фізичне затискання комбінації
        if self._active:
            return
        self._active = True
        self.pressed.emit()

    def _on_combo_event(self, ev):
        # стоп на відпускання будь-якого компонента комбінації (Ctrl/Shift/основна)
        if ev.event_type != "up" or not self._active:
            return
        if normalize_name(ev.name) in self._combo_parts:
            self._active = False
            self.released.emit()

    def rebind(self, key: str) -> bool:
        """Перепризначити клавішу запису на льоту (з вкладки Налаштування).
        Якщо новий ключ не зачепився — повертаємо попередній робочий, а не
        лишаємо застосунок зовсім без хука. True = новий ключ реально діє."""
        import keyboard
        keyboard.unhook_all()   # ми — єдиний користувач хуків у процесі
        self._active = False
        if self._hook(key):
            self._key = key
            return True
        self._hook(self._key)
        return False
