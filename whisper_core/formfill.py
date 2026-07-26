"""Заповнення шаблонів голосом (feature/voice-form-fill).

Спрощений офлайн-аналог Dragon «fill-in-the-blanks», БЕЗ LLM. Чиста логіка без
Qt і без глобального стану — UI лишається тонким:

  parse_fields(template)        — «[поле]» у порядку появи, дублікати згорнуто
  FormSession(template)         — курсор по полях + значення + підстановка
  match_nav_command(text, lang) — сказана фраза → "next"/"prev"/None
  list_templates()/load_template — сховище шаблонів (whisper_core.paths)

Шаблон — звичайний текст із плейсхолдерами у квадратних дужках: «[посада]».
Одне й те саме ім'я поля = одна позиція курсора; значення підставляється в УСІ
його входження. Незаповнене поле лишається як «[поле]», щоб видно, чого бракує.

Навігаційні команди розпізнаються ЛИШЕ при точному збігу нормалізованої фрази
(як тригери сніпетів) — щоб «наступне поле» всередині диктанту не перемикало
курсор випадково. ASR-граматики НЕ будуємо: команда приходить готовим текстом
з існуючого механізму розпізнавання.
"""
import re
import shutil

from whisper_core import paths

#: ім'я поля — будь-що між [ ], крім дужок і переносу рядка; внутрішні пробіли
#: імені зрізаються при нормалізації
_FIELD_RE = re.compile(r"\[([^\[\]\n]+?)\]")

#: розширення файлів шаблонів
_TEMPLATE_EXT = (".txt", ".md")

#: точні фрази навігації за мовою (нормалізовані: нижній регістр, без кінцевої
#: пунктуації). Множина синонімів — на випадок різного розпізнавання.
_NAV_COMMANDS = {
    "uk": {
        "наступне поле": "next", "далі": "next", "наступне": "next",
        "попереднє поле": "prev", "назад": "prev", "попереднє": "prev",
    },
    "en": {
        "next field": "next", "next": "next",
        "previous field": "prev", "previous": "prev", "back": "prev",
    },
}

_TRAILING = " \t.!?,"


def _normalize(text: str) -> str:
    """Канонічна форма фрази: нижній регістр, стиснуті пробіли, без кінцевої
    пунктуації (як normalize_trigger у сніпетах)."""
    s = re.sub(r"\s+", " ", text.strip().lower())
    return s.rstrip(_TRAILING)


def parse_fields(template: str) -> list:
    """Імена полів «[поле]» у порядку першої появи; дублікати згорнуто.
    Порожні дужки «[]»/«[ ]» ігноруються."""
    seen = {}
    for m in _FIELD_RE.finditer(template or ""):
        name = m.group(1).strip()
        if name and name not in seen:
            seen[name] = True
    return list(seen)


def iter_segments(template: str) -> list:
    """Розбити шаблон на послідовність сегментів для рендера UI (підсвітка).
    Кожен елемент — кортеж ("text", рядок) або ("field", ім'я_поля). Порожні
    дужки лишаються звичайним текстом. Конкатенація other-частин відновлює
    оригінал (парсинг живе тут, а не в UI)."""
    out = []
    pos = 0
    for m in _FIELD_RE.finditer(template or ""):
        name = m.group(1).strip()
        if not name:
            continue  # «[]» → лишити у наступному text-сегменті
        if m.start() > pos:
            out.append(("text", template[pos:m.start()]))
        out.append(("field", name))
        pos = m.end()
    if pos < len(template or ""):
        out.append(("text", template[pos:]))
    return out


def match_nav_command(text: str, language: str = "uk"):
    """Сказана фраза → "next"/"prev"/None. Тільки точний збіг нормалізованої
    фрази з відомою командою (часткові — ні, безпека)."""
    table = _NAV_COMMANDS.get(language)
    if not table:
        return None
    return table.get(_normalize(text))


class FormSession:
    """Стан заповнення одного шаблону: курсор по унікальних полях + значення.

    Не тримає Qt і не пише файли — UI сам вирішує, коли показати render() і що
    робити зі скопійованим результатом."""

    def __init__(self, template: str):
        self.template = template or ""
        self.fields = parse_fields(self.template)
        self.values = {f: "" for f in self.fields}
        self.index = 0

    # --- курсор ---------------------------------------------------------
    @property
    def current_field(self):
        """Ім'я поточного поля або None (шаблон без полів)."""
        if not self.fields:
            return None
        return self.fields[self.index]

    def next_field(self):
        """Наступне поле; на останньому лишається (clamp, без wrap)."""
        if self.fields:
            self.index = min(self.index + 1, len(self.fields) - 1)
        return self.current_field

    def prev_field(self):
        """Попереднє поле; на першому лишається (clamp)."""
        if self.fields:
            self.index = max(self.index - 1, 0)
        return self.current_field

    def go_to(self, name: str):
        """Поставити курсор на конкретне поле за іменем (ігнорує невідоме)."""
        if name in self.fields:
            self.index = self.fields.index(name)
        return self.current_field

    # --- значення -------------------------------------------------------
    def set_value(self, text: str):
        """Замінити значення ПОТОЧНОГО поля."""
        f = self.current_field
        if f is not None:
            self.values[f] = text.strip()

    def append_value(self, text: str):
        """Дописати до значення поточного поля (для диктування частинами)."""
        f = self.current_field
        if f is None:
            return
        chunk = text.strip()
        if not chunk:
            return
        cur = self.values[f]
        self.values[f] = f"{cur} {chunk}".strip() if cur else chunk

    def clear_current(self):
        """Очистити значення поточного поля."""
        f = self.current_field
        if f is not None:
            self.values[f] = ""

    def value_of(self, name: str) -> str:
        return self.values.get(name, "")

    @property
    def is_complete(self) -> bool:
        """Усі поля заповнені (шаблон без полів вважаємо готовим)."""
        return all(self.values[f] for f in self.fields)

    # --- підстановка ----------------------------------------------------
    def render(self) -> str:
        """Текст із підставленими значеннями. Заповнене поле → значення;
        незаповнене лишається як «[поле]». Однакові імена підставляються скрізь."""
        def repl(m):
            name = m.group(1).strip()
            val = self.values.get(name, "")
            return val if val else m.group(0)
        return _FIELD_RE.sub(repl, self.template)


# --- сховище шаблонів ---------------------------------------------------
def _seed_from_bundle(target):
    """Скопіювати приклади шаблонів зі збірки у writable-теку, якщо порожньо
    (аналог сідування словників). У dev bundled_templates_dir()==None → no-op."""
    bundle = paths.bundled_templates_dir()
    if bundle is None:
        return
    for src in bundle.iterdir():
        if src.suffix.lower() in _TEMPLATE_EXT:
            dst = target / src.name
            if not dst.exists():
                try:
                    shutil.copyfile(src, dst)
                except OSError:
                    pass


def list_templates() -> list:
    """Файли шаблонів (.txt/.md) у templates_dir, відсортовані за іменем.
    Порожню теку сідуємо прикладами зі збірки (frozen)."""
    d = paths.templates_dir()
    have = [p for p in d.iterdir() if p.suffix.lower() in _TEMPLATE_EXT] \
        if d.exists() else []
    if not have:
        _seed_from_bundle(d)
        have = [p for p in d.iterdir() if p.suffix.lower() in _TEMPLATE_EXT] \
            if d.exists() else []
    return sorted(have, key=lambda p: p.name.lower())


def load_template(path) -> str:
    """Прочитати текст шаблону. Помилка читання → порожній рядок."""
    from pathlib import Path
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
