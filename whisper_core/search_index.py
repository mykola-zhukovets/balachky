"""Глобальний пошук: єдиний індекс по всіх джерелах тексту застосунку.

Ядро — БЕЗ Qt і БЕЗ мережі (як history/recordings/meeting.session). Індексує три
джерела, усі ЛОКАЛЬНІ:

  * диктування — рядки history.jsonl кожного словника (source="desktop");
  * розшифровки аудіофайлів — ті самі history.jsonl (source="file"): черга
    «Файли» дописує результат туди ж;
  * наради — transcript.json кожної сесії у сховищі нарад (кожна репліка несе
    таймкод `start`), з назвою/датою з meeting.json.

Приватність (канон): індекс будується В ПАМʼЯТІ з локальних файлів під час
пошуку; нічого нікуди не відправляється і на диск поза вже наявними файлами не
пишеться. Тому окремого файлу-кешу немає — свіжий скан на кожен показ сторінки
надійніший за кеш (жодної інвалідизації), а обсяг тексту дрібний.

Матчинг — лінійний скан із ранжуванням (без важких залежностей). Кожен документ
дає «сіно» = текст + кілька форматів своєї дати, тож запит словом і запит датою
(«17.07», «17.07.2026», «2026-07-17») працюють однаково — як уже робить фільтр на
вкладці «Історія». Ранжування: більше збігів термів у ТЕКСТІ — вище; тай-брейк —
новіші першими.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

# Види джерел (порівнюються в коді; показ — через локалізацію у фронті).
KIND_DICTATION = "dictation"
KIND_FILE = "file"
KIND_MEETING = "meeting"

# Скільки символів контексту показувати навколо збігу у фрагменті.
_SNIPPET_RADIUS = 48


@dataclass
class SearchDoc:
    """Одна одиниця пошуку. ref — усе, що потрібно фронту, аби відкрити джерело:
    для наради це session_id; для історії — сам JSON-рядок (точне співпадіння для
    навігації/фільтра, як у видаленні історії)."""
    kind: str
    ref: str
    title: str            # назва наради/дата; для історії — ""
    date: float           # epoch-секунди (ts запису або created наради)
    text: str
    profile: str = ""     # словник історії; для нарад — ""
    timecode: "float | None" = None   # секунда репліки в нараді; інакше None


@dataclass
class SearchResult:
    kind: str
    ref: str
    title: str
    date: float
    snippet: str
    profile: str = ""
    timecode: "float | None" = None
    score: float = 0.0


def _date_haystack(date: float) -> str:
    """Кілька форматів дати документа одним рядком — щоб запит датою у будь-якому
    з поширених виглядів («17.07.2026», «2026-07-17», «17.07») знаходив документ."""
    if not date:
        return ""
    lt = time.localtime(date)
    return f"{time.strftime('%d.%m.%Y', lt)} {time.strftime('%Y-%m-%d', lt)}"


def _snippet(text: str, terms: list) -> str:
    """Фрагмент-контекст навколо першого збігу будь-якого терма. Немає збігу в
    тексті (матч був лише по даті) → початок тексту. Обрізані краї позначаємо «…»."""
    low = text.lower()
    pos = -1
    for term in terms:
        i = low.find(term)
        if i != -1 and (pos == -1 or i < pos):
            pos = i
    if pos == -1:
        pos = 0
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(text), pos + _SNIPPET_RADIUS)
    frag = text[start:end].strip()
    if start > 0:
        frag = "… " + frag
    if end < len(text):
        frag = frag + " …"
    return frag


class SearchIndex:
    """Незмінний список документів + лінійний пошук по ньому."""

    def __init__(self, docs: list):
        self.docs = list(docs)

    @classmethod
    def build(cls, *, history_paths=(), meetings_root=None) -> "SearchIndex":
        """Зібрати індекс із локальних джерел.

        history_paths — ітерабельне (Profile | Path | str): читаємо history.jsonl,
        кожен запис → документ (диктування чи файл — за полем source).
        meetings_root — тека сховища нарад: кожна підтека з meeting.json +
        transcript.json → документи-репліки з таймкодами.
        Будь-яке відсутнє/битнє джерело просто пропускаємо (пошук некритичний)."""
        docs: list = []
        for src in history_paths or ():
            docs.extend(_history_docs(src))
        if meetings_root is not None:
            docs.extend(_meeting_docs(meetings_root))
        return cls(docs)

    def search(self, query: str, *, limit: "int | None" = 50) -> list:
        """Документи, що містять УСІ терми запиту (у тексті або в даті), новіші/
        релевантніші першими. Порожній запит → []."""
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        results = []
        for doc in self.docs:
            text_low = doc.text.lower()
            haystack = text_low + " " + _date_haystack(doc.date)
            if not all(term in haystack for term in terms):
                continue
            score = sum(text_low.count(term) for term in terms)
            results.append(SearchResult(
                kind=doc.kind, ref=doc.ref, title=doc.title, date=doc.date,
                snippet=_snippet(doc.text, terms), profile=doc.profile,
                timecode=doc.timecode, score=score))
        # Релевантність (більше збігів) спадно; тай-брейк — новіші першими.
        results.sort(key=lambda r: (r.score, r.date), reverse=True)
        return results[:limit] if limit is not None else results


def _history_docs(source) -> list:
    """Документи з одного history.jsonl. source може мати .history_path (Profile)
    або бути Path/str. Ім'я словника беремо з .name, якщо є."""
    path = Path(getattr(source, "history_path", source))
    profile = str(getattr(source, "name", "") or "")
    out: list = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = (rec.get("final") or rec.get("raw") or "").strip()
        if not text:
            continue
        kind = KIND_FILE if rec.get("source") == "file" else KIND_DICTATION
        out.append(SearchDoc(kind=kind, ref=line, title="",
                             date=float(rec.get("ts") or 0), text=text,
                             profile=profile))
    return out


def _meeting_docs(meetings_root) -> list:
    """Документи з усіх сесій нарад: кожна репліка transcript.json → документ із
    таймкодом. Немає transcript.json → падаємо на transcript.txt одним блоком (без
    таймкоду). Назва/дата — з meeting.json."""
    root = Path(meetings_root)
    out: list = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        meta = _load_meeting_meta(d)
        if meta is None:
            continue
        session_id = d.name
        title = meta.get("title") or ""
        created = float(meta.get("created") or 0)
        utterances = _load_utterances(d)
        if utterances:
            for u in utterances:
                text = (u.get("text") or "").strip()
                if not text:
                    continue
                out.append(SearchDoc(
                    kind=KIND_MEETING, ref=session_id, title=title, date=created,
                    text=text, timecode=_as_float(u.get("start"))))
        else:
            text = _read_transcript_txt(d)
            if text:
                out.append(SearchDoc(kind=KIND_MEETING, ref=session_id,
                                     title=title, date=created, text=text))
    return out


def _load_meeting_meta(session_dir: Path) -> "dict | None":
    # Замкнене/втрачене сховище (VaultPasswordRequired/VaultKeyLost) не має валити
    # весь індекс — просто пропускаємо цю нараду (решта лишається шукабельною).
    from whisper_core.meeting.storage_crypto import VaultKeyLost, VaultPasswordRequired
    try:
        from whisper_core.meeting.session import read_artifact
        return json.loads(read_artifact(session_dir, "meeting.json").decode("utf-8"))
    except (OSError, ValueError, VaultPasswordRequired, VaultKeyLost):
        return None


def _load_utterances(session_dir: Path) -> list:
    from whisper_core.meeting.storage_crypto import VaultKeyLost, VaultPasswordRequired
    try:
        from whisper_core.meeting.session import read_artifact
        data = json.loads(read_artifact(session_dir, "transcript.json").decode("utf-8"))
    except (OSError, ValueError, VaultPasswordRequired, VaultKeyLost):
        return []
    return data if isinstance(data, list) else []


def _read_transcript_txt(session_dir: Path) -> str:
    from whisper_core.meeting.storage_crypto import VaultKeyLost, VaultPasswordRequired
    try:
        from whisper_core.meeting.session import read_artifact
        return read_artifact(session_dir, "transcript.txt").decode("utf-8").strip()
    except (OSError, VaultPasswordRequired, VaultKeyLost):
        return ""


def _as_float(value) -> "float | None":
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
