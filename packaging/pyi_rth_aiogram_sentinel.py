"""Рантайм-хук PyInstaller: мінімальний stub `sentinel` для aiogram.

`aiogram.types.base` використовує:
    from unittest.mock import sentinel
для оголошення `UNSET = sentinel.UNSET`.
При цьому весь пакет `unittest` виключений з дистрибутиву Балачок (`_COMMON_EXCLUDES`)
і заборонений правилами безпеки/розміру (`FORBIDDEN_MODULES` у test_distribution_audit.py).

Цей хук підставляє ізольований фіктивний об'єкт `unittest.mock.sentinel` у `sys.modules`,
якщо `unittest` не знайдено, що дозволяє `aiogram` завантажитися без помилки
ModuleNotFoundError і без включення повного фреймворку `unittest` у збірку.
"""
import sys
from types import ModuleType

if "unittest" not in sys.modules:
    class _SentinelObject:
        def __init__(self, name: str) -> None:
            self.name = name

        def __repr__(self) -> str:
            return f"sentinel.{self.name}"

    class _Sentinel:
        def __getattr__(self, name: str) -> _SentinelObject:
            if name.startswith("_"):
                raise AttributeError(name)
            obj = _SentinelObject(name)
            setattr(self, name, obj)
            return obj

    _u = ModuleType("unittest")
    _m = ModuleType("unittest.mock")
    _m.sentinel = _Sentinel()
    _u.mock = _m

    sys.modules["unittest"] = _u
    sys.modules["unittest.mock"] = _m
