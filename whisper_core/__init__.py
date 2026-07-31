"""whisper_core — рушій Балачки.

Ядро НЕ імпортує ні PySide6, ні aiogram — тільки faster-whisper і stdlib.
Фронти (desktop, telegram) ходять до ядра, не навпаки.
"""

from .version import (
    DISPLAY_VERSION,
    PEP440_VERSION,
    RELEASE_CHANNEL,
    WINDOWS_FILE_VERSION,
)

__all__ = [
    "DISPLAY_VERSION",
    "PEP440_VERSION",
    "RELEASE_CHANNEL",
    "WINDOWS_FILE_VERSION",
    "__version__",
]

# Backwards-compatible public alias for human-readable/reporting consumers.
__version__ = DISPLAY_VERSION
