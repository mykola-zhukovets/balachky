"""Чиста логіка «пакета зручностей» диктування (feature/qol-pack).

Тут — лише детермінований стан і функції без побічних ефектів (жодного Qt,
жодного вводу): їх легко покрити unit-тестами. UI/ввід (Backspace, звук, трей,
таймери) живуть у fronts.desktop і кличуть цю логіку.

Складові:
  • UndoBuffer         — пам'ять останньої вставки (скасувати / вставити ще раз);
  • parse_hhmm         — «22:00» → хвилини від опівночі;
  • in_quiet_hours     — чи діють «тихі години» зараз (з переходом через північ);
  • AutostopMonitor    — автостоп диктування після N секунд тиші;
  • duration_status    — ліміт тривалості диктування (ok / warn / stop).
"""


# --- 1–2. Буфер вставки: «Скасувати останню» / «Вставити ще раз» ------------
class UndoBuffer:
    """Пам'ять останньої вставленої розшифровки.

    record(text)   — запам'ятати щойно вставлений текст (і скільки символів
                     можна скасувати);
    consume_undo() — повернути к-сть Backspace для скасування і ЗАБУТИ її
                     (скасування одноразове: повторний хоткей не з'їсть зайвого,
                     навіть якщо натиснути двічі);
    last_text      — текст для повторної вставки (лишається й після скасування,
                     щоб «Вставити ще раз» працювало).
    """

    def __init__(self):
        self._last_text = None      # остання вставка (для повторної вставки)
        self._undo_len = 0          # скільки символів ще можна скасувати (0 = нічого)

    def record(self, text):
        """Запам'ятати вставлений текст. Порожній/None — нема чого скасовувати
        й нема що повторювати."""
        text = text or ""
        self._last_text = text or None
        self._undo_len = len(text)

    def has_undo(self) -> bool:
        """Чи є що скасувати (вставка ще не скасована)."""
        return self._undo_len > 0

    def consume_undo(self) -> int:
        """К-сть Backspace для скасування; далі скасування вважаємо використаним
        (щоб повторне натискання не видалило зайвого)."""
        n = self._undo_len
        self._undo_len = 0
        return n

    @property
    def last_text(self):
        """Текст останньої вставки для повторної вставки (або None)."""
        return self._last_text

    def has_text(self) -> bool:
        return bool(self._last_text)


# --- 1b. Буфер останніх вставок: «повторити вставку» / перегляд останніх N ----
class PasteHistory:
    """Кільцевий буфер останніх N ФІНАЛЬНИХ вставок цієї сесії.

    Це НЕ історія розшифровок (та живе у файлі профілю) — тут саме тексти, які
    реально пішли у вставку (можливо, відредаговані у картці перегляду). Тримаємо
    в пам'яті сесії, щоб дати «повторити останню вставку» і перегляд останніх N із
    можливістю вставити будь-яку. Найновіша — остання в _items.

    record(text)  — додати вставку (порожні/None ігноруємо; старі за межею
                    ємності витісняються);
    recent()      — список останніх вставок, НАЙНОВІША ПЕРШОЮ (для меню/списку);
    last          — текст останньої вставки (для «повторити останню») або None.
    """

    def __init__(self, capacity: int = 10):
        self._cap = max(1, int(capacity))
        self._items = []            # найстаріша перша, найновіша остання

    def record(self, text) -> None:
        text = text or ""
        if not text:
            return
        self._items.append(text)
        if len(self._items) > self._cap:
            del self._items[0]      # витіснити найстарішу

    def recent(self):
        """Останні вставки, найновіша першою (зручно для списку/меню)."""
        return list(reversed(self._items))

    @property
    def last(self):
        return self._items[-1] if self._items else None

    def has_items(self) -> bool:
        return bool(self._items)

    def clear(self) -> None:
        """Забути всі збережені вставки (тумблер історії вимкнено — не лишати
        чутливі тексти в пам'яті сесії / трей-підменю)."""
        self._items.clear()

    def __len__(self):
        return len(self._items)


# --- 7. «Тихі години» -------------------------------------------------------
def parse_hhmm(value) -> "int | None":
    """«ГГ:ХХ» → хвилини від опівночі (0..1439). Невалідне → None.

    Приймає лише 24-годинний «HH:MM». Порожнє, сміття чи 25:99 → None
    (викликач тоді трактує «тихі години» як вимкнені)."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h = int(parts[0])
        m = int(parts[1])
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def in_quiet_hours(now_min: int, start_min: int, end_min: int) -> bool:
    """Чи потрапляє момент now_min у діапазон «тихих годин» [start, end).

    Початок включно, кінець виключно. Порожній діапазон (start == end) →
    False (нема тиші). Діапазон через північ (start > end, напр. 22:00→07:00)
    обробляється: now у ньому, коли now >= start АБО now < end."""
    now_min %= 1440
    start_min %= 1440
    end_min %= 1440
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    # діапазон через північ
    return now_min >= start_min or now_min < end_min


def sounds_muted_now(cfg, now_min: int) -> bool:
    """Чи мовчать зараз усі звуки через «тихі години» (getattr-захищено для
    старих мок-конфігів). Викликається перед кожним звуком."""
    if not getattr(cfg, "quiet_hours_enabled", False):
        return False
    start = parse_hhmm(getattr(cfg, "quiet_hours_start", ""))
    end = parse_hhmm(getattr(cfg, "quiet_hours_end", ""))
    if start is None or end is None:
        return False
    return in_quiet_hours(now_min, start, end)


# --- 5. Автостоп диктування після N секунд тиші -----------------------------
# Поріг «тиші» за лінійною RMS-амплітудою (0..1). ~-46 dBFS: нижче — фонова
# тиша/шум мовчазної кімнати, вище — активне мовлення. VAD у рушії однаково
# зріже кінцеву тишу; це лише тригер завершення запису.
AUTOSTOP_RMS_THRESHOLD = 0.005


class AutostopMonitor:
    """Стежить за безперервною тишею й каже, коли пора зупинити запис.

    update(rms, now) викликається періодично (GUI-таймер). Поки rms вище порогу
    — лічильник тиші скидається. Коли тиша тримається безперервно >= silence_s —
    повертає True (один раз; далі викликач зупиняє запис і робить reset).
    silence_s <= 0 → автостоп вимкнено (update завжди False)."""

    def __init__(self, silence_s: float, threshold: float = AUTOSTOP_RMS_THRESHOLD):
        self.silence_s = float(silence_s)
        self.threshold = float(threshold)
        self._silent_since = None

    def reset(self):
        """Новий запис / щойно був звук — забути накопичену тишу."""
        self._silent_since = None

    def update(self, rms: float, now: float) -> bool:
        """→ True, коли безперервна тиша досягла порога silence_s."""
        if self.silence_s <= 0:
            return False
        if rms >= self.threshold:
            self._silent_since = None
            return False
        if self._silent_since is None:
            self._silent_since = now
            return False
        return (now - self._silent_since) >= self.silence_s


# --- 6. Ліміт максимальної тривалості диктування ----------------------------
def duration_status(elapsed_s: float, limit_s: float,
                    warn_before_s: float = 30.0) -> str:
    """Стан за тривалістю запису: 'ok' | 'warn' | 'stop'.

    limit_s <= 0 → ліміт вимкнено (завжди 'ok'). 'stop' коли досягнуто ліміту;
    'warn' у вікні warn_before_s перед лімітом (щоб попередити один раз, поки
    ще пишемо). Інакше 'ok'."""
    if limit_s <= 0:
        return "ok"
    if elapsed_s >= limit_s:
        return "stop"
    if elapsed_s >= limit_s - max(0.0, warn_before_s):
        return "warn"
    return "ok"
