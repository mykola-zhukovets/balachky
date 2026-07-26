"""Desktop-фронт (PySide6): system tray + push-to-talk + вставка.

Правило потоків: callback-и keyboard-хука й аудіо НЕ торкаються Qt/моделі —
лише Signal.emit() / list.append(). Уся зміна UI — в GUI-потоці через слоти.
"""
