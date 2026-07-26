"""Легкий власний DSP перед розпізнаванням: шумовий гейт і AGC.

Обидва — прості «немодельні» методи (RMS-гейт із гістерезисом і лінійний
нормалізатор гучності), а НЕ нейромережеве шумозаглушення. Дослідження 2026 року
(arXiv 2603.04710, 2512.17562) показали, що ML-enhancement перед Whisper
послідовно ПОГІРШУЄ WER через distribution mismatch. Гейт лише приглушує тихі
рамки (тишу/шум між словами), AGC лише масштабує амплітуду — сам мовний сигнал не
спотворюється, тож для розпізнавання це безпечно (див. RESEARCH §2). Обидва
опційні й вимкнені за замовчуванням. Без нових залежностей — тільки numpy.
"""
import numpy as np

# --- Дефолти: єдине джерело правди для config, UI та «Скинути до типових» ---
NOISE_GATE_THRESHOLD_DB_DEFAULT = -45.0   # рамки, тихіші за цей рівень, — тиша/шум
AGC_TARGET_DB_DEFAULT = -20.0             # цільовий RMS-рівень мовлення для AGC

# Внутрішні сталі гейта/AGC (не виносимо в UI — деталі реалізації)
_GATE_HYSTERESIS_DB = 6.0   # гейт закривається на 6 дБ НИЖЧЕ за поріг відкриття
_GATE_FRAME_MS = 20         # рамка аналізу рівня
_GATE_HOLD_MS = 80          # тримати відкритим ще стільки після падіння рівня
_GATE_FADE_MS = 5           # згладжування коефіцієнта (щоб не було клацань на межах)
_AGC_MAX_GAIN_DB = 20.0     # стеля підсилення (щоб не роздути тихий шум)
_AGC_LIMIT = 0.99           # жорсткий ліміт піків після нормалізації (анти-кліп)


def _rms(x) -> float:
    """RMS лінійної амплітуди; порожнє → 0. float64 для стабільності суми."""
    if len(x) == 0:
        return 0.0
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def _db(amp: float) -> float:
    """Лінійна амплітуда → dBFS; нуль/майже-нуль → -inf."""
    return float("-inf") if amp <= 1e-10 else 20.0 * float(np.log10(amp))


def noise_gate(audio, sample_rate: int,
               threshold_db: float = NOISE_GATE_THRESHOLD_DB_DEFAULT,
               *, hysteresis_db: float = _GATE_HYSTERESIS_DB,
               frame_ms: int = _GATE_FRAME_MS, hold_ms: int = _GATE_HOLD_MS,
               fade_ms: int = _GATE_FADE_MS):
    """RMS-гейт із гістерезисом: рамки, тихіші за поріг, приглушуються до нуля;
    гучніші — пропускаються без зміни. Гістерезис (закриття на 6 дБ нижче за
    відкриття) + утримання не дають гейту «тремтіти» на межі, а плавний перехід
    коефіцієнта прибирає клацання на стиках рамок.

    Чиста функція: повертає НОВИЙ масив float32 тієї ж довжини (вхід не мутує).
    Порожнє/None повертаємо як є.
    """
    if audio is None or len(audio) == 0:
        return audio
    a = np.asarray(audio, dtype=np.float32).flatten()
    frame = max(1, int(sample_rate * frame_ms / 1000))
    open_db = threshold_db
    close_db = threshold_db - hysteresis_db
    hold_frames = max(0, int(round(hold_ms / frame_ms)))
    gain = np.empty(len(a), dtype=np.float32)
    is_open = False
    hold = 0
    for start in range(0, len(a), frame):
        seg = a[start:start + frame]
        level = _db(_rms(seg))
        if is_open:
            if level < close_db:
                if hold > 0:            # ще тримаємо відкритим (хвіст слова)
                    hold -= 1
                else:
                    is_open = False
            else:
                hold = hold_frames      # рівень тримається — оновити утримання
        elif level >= open_db:
            is_open = True
            hold = hold_frames
        gain[start:start + len(seg)] = 1.0 if is_open else 0.0
    # згладити коефіцієнт: різкий 0→1 на межі рамки дав би клацання
    fade = max(1, int(sample_rate * fade_ms / 1000))
    if fade > 1:
        kernel = np.ones(fade, dtype=np.float32) / fade
        gain = np.convolve(gain, kernel, mode="same").astype(np.float32)
    return (a * gain).astype(np.float32)


def agc(audio, target_db: float = AGC_TARGET_DB_DEFAULT,
        *, max_gain_db: float = _AGC_MAX_GAIN_DB, limit: float = _AGC_LIMIT):
    """Простий лінійний нормалізатор гучності: масштабує ВЕСЬ буфер одним
    коефіцієнтом так, щоб RMS сягнув цільового рівня, з обмеженням підсилення
    (щоб не роздути тихий шум) і жорстким лімітом піків проти кліпінгу. Один
    коефіцієнт на весь запис — жодного «pumping», який Discord радить вимикати.

    Чиста функція: повертає НОВИЙ масив float32 (вхід не мутує). Тишу
    (RMS≈0) лишаємо без зміни — нормалізувати нічого.
    """
    if audio is None or len(audio) == 0:
        return audio
    a = np.asarray(audio, dtype=np.float32).flatten()
    rms = _rms(a)
    if rms <= 1e-10:                    # тиша — нічого нормалізувати
        return a.astype(np.float32)
    gain_db = min(target_db - _db(rms), max_gain_db)   # стеля підсилення
    out = a * float(10.0 ** (gain_db / 20.0))
    peak = float(np.max(np.abs(out)))
    if peak > limit:                    # анти-кліп: підрізати під ліміт
        out = out * (limit / peak)
    return out.astype(np.float32)
