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
_ENCRYPTED_SUFFIX = ".enc"
_ENCRYPTION_CONTEXT = "balachky-dictation-history-v1"


@contextmanager
def history_lock(path: Path, timeout: float | None = None):
    """Один lock для append/rewrite, між потоками й процесами.

    ``timeout`` обмежує очікування в секундах; ``None`` зберігає попередню
    поведінку без обмеження.
    """
    path = Path(path)
    if timeout is not None and timeout < 0:
        raise ValueError("lock timeout must be non-negative")
    deadline = None if timeout is None else time.monotonic() + timeout
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
    if deadline is None:
        acquired = thread_lock.acquire()
    else:
        acquired = thread_lock.acquire(timeout=max(0.0, deadline - time.monotonic()))
    if not acquired:
        raise TimeoutError(f"Timed out waiting for lock: {path}")
    try:
        lock_path = path.with_name(path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+b") as lock_file:
            if os.fstat(lock_file.fileno()).st_size == 0:
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
                    except OSError as exc:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"Timed out waiting for lock: {lock_path}") from exc
                        delay = 0.01
                        if deadline is not None:
                            delay = min(delay, max(0.0, deadline - time.monotonic()))
                        time.sleep(delay)
                unlock = lambda: msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                if deadline is None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                else:
                    while True:
                        try:
                            fcntl.flock(
                                lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            break
                        except OSError as exc:
                            if time.monotonic() >= deadline:
                                raise TimeoutError(
                                    f"Timed out waiting for lock: {lock_path}") from exc
                            time.sleep(min(
                                0.01, max(0.0, deadline - time.monotonic())))
                unlock = lambda: fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                unlock()
    finally:
        thread_lock.release()


# Публічний lock для всіх операцій над history.jsonl; alias лишає сумісність.
_history_lock = history_lock


def _atomic_rewrite(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def encrypted_path(history_path) -> Path:
    """Шлях активної зашифрованої копії поруч із сумісним history.jsonl."""
    path = Path(history_path)
    return path.with_name(path.name + _ENCRYPTED_SUFFIX)


def is_encrypted(history_path) -> bool:
    """Чи є активною зашифрована копія історії."""
    return encrypted_path(history_path).exists()


def _read_text(path: Path) -> str:
    encrypted = encrypted_path(path)
    if encrypted.exists():
        from whisper_core.meeting.storage_crypto import (
            decrypt_to_memory, ensure_dek)
        plain = decrypt_to_memory(
            encrypted, ensure_dek(path.parent), context=_ENCRYPTION_CONTEXT)
        return plain.decode("utf-8")
    return path.read_text(encoding="utf-8")


def _rewrite(path: Path, text: str, *, encrypted: bool) -> None:
    encrypted_file = encrypted_path(path)
    if encrypted:
        from whisper_core.meeting.storage_crypto import encrypt_bytes, ensure_dek
        encrypt_bytes(
            text.encode("utf-8"), encrypted_file, ensure_dek(path.parent),
            context=_ENCRYPTION_CONTEXT)
        path.unlink(missing_ok=True)
    else:
        _atomic_rewrite(path, text)
        encrypted_file.unlink(missing_ok=True)


def set_encryption(history_path, enabled: bool) -> None:
    """Перевести одну історію між відкритим JSONL і AES-GCM-контейнером.

    Спершу атомарно створюється нова копія, лише потім прибирається стара.
    Якщо після аварії співіснують обидві, зашифрована копія є канонічною.
    Відсутню/порожню ще не створену історію не матеріалізуємо.
    """
    path = Path(history_path)
    encrypted = encrypted_path(path)
    with history_lock(path):
        if enabled:
            if encrypted.exists():
                _read_text(path)  # перевірити ключ і цілісність до cleanup
                path.unlink(missing_ok=True)
            elif path.exists():
                _rewrite(path, path.read_text(encoding="utf-8"), encrypted=True)
        elif encrypted.exists():
            _rewrite(path, _read_text(path), encrypted=False)


def log_history(history_path, raw: str, final: str, *, source: str = "desktop",
                enabled: bool = True, audio: str | None = None,
                encrypt: bool = False):
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
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            if encrypt or encrypted_path(path).exists():
                try:
                    previous = _read_text(path)
                except FileNotFoundError:
                    previous = ""
                _rewrite(path, previous + line, encrypted=True)
            else:
                with path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(line)
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
            lines = _read_text(path).splitlines()
            lines.remove(line)
            _rewrite(
                path, "\n".join(lines) + ("\n" if lines else ""),
                encrypted=encrypted_path(path).exists())
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
                lines = _read_text(path).splitlines()
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
                _rewrite(
                    path, "\n".join(lines) + ("\n" if lines else ""),
                    encrypted=encrypted_path(path).exists())
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
            lines = _read_text(path).splitlines()
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
                    _rewrite(
                        path, "\n".join(lines) + ("\n" if lines else ""),
                        encrypted=encrypted_path(path).exists())
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
                lines = _read_text(path).splitlines()
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
            _rewrite(
                path, "\n".join(lines) + ("\n" if lines else ""),
                encrypted=encrypted_path(path).exists())
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
        for line in _read_text(path).splitlines():
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
