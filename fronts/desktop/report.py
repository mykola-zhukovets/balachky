"""Звіт про проблему: пакує лог, безпечний конфіг і коротке info у zip.

Логіка збирання архіву — поза Qt (DPI та інше передаються через extra_info),
щоб покривати юніт-тестом. Кнопка в Налаштуваннях — лише тонка обгортка.

ПРИВАТНІСТЬ: у звіт іде тільки whitelist полів моделі й аудіо. Шляхи робочих
папок, назви пристроїв та інші приватні поля свідомо НЕ включаються. Перед
пакуванням файли логу проганяються через crash.sanitize_log_bytes — ім'я
облікового запису Windows у будь-яких шляхах усередині логу замінюється на
"<користувач>". Кнопка в Налаштуваннях перед викликом показує користувачу
діалог із чесним описом вмісту архіву (див. pages/settings._report_problem).
"""
import logging
import platform
import zipfile
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Поля конфігурації, безпечні для звіту: лише модель і аудіо.
_SAFE_CONFIG_FIELDS = (
    "model_name", "device", "compute_type", "language", "ui_language",
    "beam_size", "sample_rate", "log_level",
    "vad_threshold", "vad_min_silence_ms", "vad_min_speech_ms",
    "noise_gate_enabled", "noise_gate_threshold_db",
    "agc_enabled", "agc_target_db",
)


def safe_config_dump(cfg) -> str:
    """Текст лише з whitelist-полів (модель/аудіо). None cfg → порожньо."""
    if cfg is None:
        return ""
    lines = [f"{name} = {getattr(cfg, name)!r}"
             for name in _SAFE_CONFIG_FIELDS if hasattr(cfg, name)]
    return "\n".join(lines)


def _total_memory_mb():
    """Загальна фізична пам'ять (МБ) через WinAPI; None, якщо недоступно."""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalMemoryStatusEx.argtypes = (ctypes.POINTER(_MemStatus),)
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys // (1024 * 1024))
    except Exception:
        pass
    return None


def build_number() -> str:
    """«1.0.0 (abc1234)» — версія + короткий коміт збірки. М'який відкат на саму
    версію, якщо _buildinfo недоступний (не має валити збірку звіту)."""
    from whisper_core import DISPLAY_VERSION
    try:
        from whisper_core._buildinfo import build_version
        return build_version(DISPLAY_VERSION)
    except Exception:
        return DISPLAY_VERSION


def loaded_components() -> str:
    """Список ключових завантажених компонентів (модулі важких залежностей, що
    вже в sys.modules) — допомагає діагностувати, чи докачано опційні фічі.
    Лише імена, без версій і шляхів (приватність)."""
    import sys
    watched = ("faster_whisper", "ctranslate2", "onnxruntime", "symspellpy",
               "sherpa_onnx", "av", "PySide6", "torch", "numpy")
    present = [name for name in watched if name in sys.modules]
    return "\n".join(present)


def build_info_text(*, app_version, extra=None) -> str:
    """Коротке info.txt: версія+коміт, ОС, Python, пам'ять + передані пари (DPI)."""
    lines = [
        f"Balachky {build_number()}",
        f"OS: {platform.platform()}",
        f"Python: {platform.python_version()}",
    ]
    mem = _total_memory_mb()
    if mem is not None:
        lines.append(f"RAM: {mem} MB")
    for key, value in (extra or {}).items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def build_report_zip(dest_dir, *, app_version, cfg=None, log_dir=None,
                     extra_info=None, now=None) -> Path:
    """Зібрати zip-звіт у dest_dir і повернути шлях до нього.

    Кладе info.txt (версія+коміт), config-safe.txt (whitelist), components.txt
    (завантажені компоненти) і logs/*.log (основний та ротовані бекапи). Чиста
    функція: жодного Qt, час і DPI приходять ззовні.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    zip_path = dest_dir / f"balachky-звіт-{stamp}.zip"

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("info.txt", build_info_text(
            app_version=app_version, extra=extra_info))
        zf.writestr("config-safe.txt", safe_config_dump(cfg))
        zf.writestr("components.txt", loaded_components())
        if log_dir is not None:
            from .crash import sanitize_log_bytes
            log_dir = Path(log_dir)
            for name in sorted(p.name for p in log_dir.glob("balachky*.log")):
                try:
                    raw = (log_dir / name).read_bytes()
                    zf.writestr(f"logs/{name}", sanitize_log_bytes(raw))
                except OSError:
                    pass
    return zip_path
