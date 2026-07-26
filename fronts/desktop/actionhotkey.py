"""Глобальні хоткеї простих дій (feature/qol-pack).

Скасувати останню вставку / Вставити останнє ще раз — кожен опційний
(порожній рядок = вимкнено; трей-пункти діють завжди). Реєстрація через
keyboard.add_hotkey; callback стріляє у keyboard-потоці, тож він має ЛИШЕ
викликати переданий обробник, який маршалить роботу в GUI-потік (сигнал).

Окремо від PTT-хука (fronts.desktop.hotkey). PTT rebind робить keyboard.unhook_all
(«ми — єдиний користувач»), тому після нього контролер кличе reapply(), щоб
повернути наші хоткеї. Знімаємо лише СВОЇ хендли (remove_hotkey), не unhook_all.
"""
import logging


class ActionHotkeys:
    def __init__(self, on_undo, on_insert):
        self._on_undo = on_undo
        self._on_insert = on_insert
        self._undo_key = ""
        self._insert_key = ""
        self._handles = []          # хендли keyboard.add_hotkey (для точкового зняття)

    def apply(self, undo_key: str, insert_key: str) -> None:
        """Оновити комбінації й перереєструвати (порожні — просто не вішаються)."""
        self._undo_key = undo_key or ""
        self._insert_key = insert_key or ""
        self.reapply()

    def reapply(self) -> None:
        """Перевісити хоткеї (після PTT rebind, що робить unhook_all)."""
        import keyboard   # лениво: legacy-бекенд; native цю бібліотеку не вантажить
        self._clear()
        for key, cb in ((self._undo_key, self._on_undo),
                        (self._insert_key, self._on_insert)):
            if not key:
                continue
            try:
                self._handles.append(keyboard.add_hotkey(key, cb))
            except Exception:
                logging.exception("Не вдалося повісити хоткей дії «%s»", key)

    def _clear(self) -> None:
        import keyboard
        for h in self._handles:
            try:
                keyboard.remove_hotkey(h)
            except (KeyError, ValueError):
                pass                # уже знято (напр. unhook_all під час rebind)
            except Exception:
                logging.exception("Не вдалося зняти хоткей дії")
        self._handles = []
