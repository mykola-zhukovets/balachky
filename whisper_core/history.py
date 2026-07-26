"""Пам'ять: журнал транскрипцій (джерело для learn.py).

Шлях — параметр (на Етапі 3 — history.jsonl активного профілю).
Запис некритичний: помилка логу не має зривати основний потік.
"""
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()


@contextmanager
def history_lock(path: Path):
    """Один lock для append і rewrite history.jsonl, між потоками й процесами."""
    path = Path(path)
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    with thread_lock:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.seek(0)
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
                unlock = lambda: msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                unlock()


# Публічний lock для всіх операцій над history.jsonl; alias лишає сумісність.
_history_lock = history_lock


def _atomic_rewrite(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log_history(history_path, raw: str, final: str, *, source: str = "desktop",
                enabled: bool = True, audio: str | None = None):
    """Дописати один рядок JSON. enabled=False (вимкнена пам'ять профілю) → нічого не пише.

    ``audio`` — ім'я файлу збереженого аудіо цього диктування (у теці
    dictation_audio/ профілю) для «Переслухати» у зворотному диктуванні; None
    (не збережено / файлові джерела) → поле не пишемо, картка лишає кнопку
    неактивною.

    Повертає записаний dict (з ts) — щоб UI-картка знала свій ts для точкового
    видалення; None, якщо запис не зроблено (пам'ять вимкнена або помилка вводу)."""
    if not enabled:
        return None
    try:
        # id — стабільна ідентичність запису для точкового виправлення
        # (whisper_core.self_learning / update_final_by_id): не залежить від тексту,
        # тож дублікати з однаковим final не переплутуються.
        rec = {"id": uuid.uuid4().hex, "ts": round(time.time()), "raw": raw,
               "final": final, "source": source}
        if audio:
            rec["audio"] = audio
        path = Path(history_path)
        with history_lock(path):
            with path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        return rec
    except Exception:
        return None


def delete_line(history_path, line: str) -> None:
    """Прибрати ОДИН рядок історії (перше точне співпадіння) — перезаписати файл.
    Файл змінили/рядка вже нема → тихий no-op (не критично для UI)."""
    path = Path(history_path)
    try:
        with history_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            lines.remove(line)
            _atomic_rewrite(path, "\n".join(lines) + ("\n" if lines else ""))
    except (OSError, ValueError):
        pass


def update_final(history_path, old_final: str, new_final: str,
                 *, source: str | None = None) -> bool:
    """Оновити поле final НАЙНОВІШОГО запису, де final == old_final (і, якщо
    задано source, — той самий source). raw НЕ чіпаємо (сирий текст лишається
    оригіналом; редагується лише final — feature/transcript-editing).

    Найновіші записи — в кінці файлу (append), тож ідемо з кінця. Повертає True,
    якщо запис знайдено й переписано; False — файлу нема / збігу нема / помилка
    вводу (некритично для UI: правка все одно лишається в пам'яті картки).

    Read-цілого-файлу→write під тим самим history_lock, що append/rewrite: інакше
    конкурентний писар (фонове диктування/файл/зворотне) у вікно між read і write
    губиться. Запис — атомарний tmp+os.replace (як update_record/update_final_by_id)."""
    path = Path(history_path)
    try:
        with history_lock(path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return False
            for i in range(len(lines) - 1, -1, -1):
                line = lines[i]
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("final") != old_final:
                    continue
                if source is not None and rec.get("source") != source:
                    continue
                rec["final"] = new_final
                lines[i] = json.dumps(rec, ensure_ascii=False)
                _atomic_rewrite(path, "\n".join(lines) + ("\n" if lines else ""))
                return True
    except OSError:
        return False
    return False


def update_record(history_path, ts, *, final: str | None = None,
                  mark_edited: bool = False) -> bool:
    """Зворотне диктування: оновити запис із заданим ``ts`` — за потреби переписати
    ``final`` (raw ЗАВЖДИ лишається дослівним оригіналом, verbatim-принцип) і/або
    позначити його ``edited`` (виправлено вручну/голосом).

    Матчимо саме за ``ts`` (точно той запис, що на картці), а не за текстом —
    щоб дублікати з однаковим текстом не переплутувались. Найновіші записи в
    кінці файлу, тож ідемо з кінця (при рідкій колізії ts у межах секунди
    виграє найновіший — та сама семантика, що update_final). Повертає True,
    якщо запис знайдено; False — файлу нема / ts не знайдено / помилка вводу
    (некритично для UI: правка все одно лишається в пам'яті картки)."""
    path = Path(history_path)
    try:
        with history_lock(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            for i in range(len(lines) - 1, -1, -1):
                line = lines[i]
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts") != ts:
                    continue
                changed = False
                if final is not None and rec.get("final") != final:
                    rec["final"] = final
                    changed = True
                if mark_edited and not rec.get("edited"):
                    rec["edited"] = True
                    changed = True
                if changed:
                    lines[i] = json.dumps(rec, ensure_ascii=False)
                    _atomic_rewrite(path, "\n".join(lines) + ("\n" if lines else ""))
                return True
    except OSError:
        return False
    return False


def update_final_by_id(history_path, rec_id, new_final: str, *,
                       fallback=None) -> bool:
    """Виправлення користувача: оновити final + позначку edited ТОЧНО того запису,
    чия ідентичність передана. На відміну від update_final («найновіший з таким
    final») — НІКОЛИ не зачепить інший запис з однаковим текстом.

    rec_id — стабільний id запису (log_history виставляє його новим записам). Для
    старих записів без id — guarded fallback (ts, raw, final, source): оновлюємо
    ЛИШЕ якщо збіг РІВНО один (інакше безпечно нічого не робимо, щоб не переписати
    чужий однаковий запис). raw ніколи не чіпаємо (verbatim). Повертає True, якщо
    оновлено; False — файлу нема / збігу нема / неоднозначний fallback / помилка."""
    path = Path(history_path)
    try:
        with history_lock(path):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return False
            target = None
            if rec_id:
                for i in range(len(lines) - 1, -1, -1):
                    rec = _parse_line(lines[i])
                    if rec is not None and rec.get("id") == rec_id:
                        target = i
                        break
            elif fallback is not None:
                ts, raw, old_final, source = fallback
                hits = []
                for i, line in enumerate(lines):
                    rec = _parse_line(line)
                    if (rec is not None and rec.get("ts") == ts
                            and rec.get("raw") == raw and rec.get("final") == old_final
                            and rec.get("source") == source):
                        hits.append(i)
                if len(hits) == 1:          # неоднозначно → безпечний no-op
                    target = hits[0]
            if target is None:
                return False
            rec = _parse_line(lines[target])
            rec["final"] = new_final
            rec["edited"] = True
            lines[target] = json.dumps(rec, ensure_ascii=False)
            _atomic_rewrite(path, "\n".join(lines) + ("\n" if lines else ""))
            return True
    except OSError:
        return False


def _parse_line(line: str):
    if not line.strip():
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def read_recent(source, limit: int | None = None) -> list:
    """Прочитати історію: найновіші записи першими.

    source — Profile (має .history_path), Path або str зі шляхом до history.jsonl.
    Повертає список кортежів (рядок-json, dict): сам рядок потрібен для точкового
    видалення запису в UI (точне співпадіння). Биті/порожні рядки пропускаємо.
    limit=None → усі записи; інакше — стільки найновіших.
    """
    path = Path(getattr(source, "history_path", source))
    records = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append((line, json.loads(line)))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    records.reverse()               # найновіші першими
    return records[:limit] if limit is not None else records
