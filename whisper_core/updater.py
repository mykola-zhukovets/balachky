"""Доставка оновлення: завантаження інсталятора релізу з перевіркою SHA-256.

Ядро БЕЗ Qt і без сторонніх мереж-бібліотек (лише stdlib urllib). Якщо
UI-шар хоче прогрес — передає колбек `progress(downloaded, total)`.

────────────────────────────────────────────────────────────────────────
ФОРМАТ RELEASE-МЕТАДАНИХ (що публікувати при кожному релізі)
────────────────────────────────────────────────────────────────────────
Канал оновлень — GitHub Releases (`/repos/<repo>/releases/latest`). Наявний
`whisper_core.updates.check_latest` вже читає `tag_name` (версія) та
`html_url` (сторінка релізу). Для АВТОМАТИЧНОЇ доставки реліз має додатково
містити інсталятор як asset і його SHA-256. Публікуючи реліз, зроби так:

1. Тег релізу — `v<X.Y.Z>` (PEP 440).
2. Тіло релізу (release body) — примітки «Що нового» звичайним текстом /
   Markdown. Показуються користувачу як є.
3. Assets:
   • Інсталятор Windows `.exe` (per-user Inno Setup), напр.
     `Balachky-Setup-<X.Y.Z>.exe`. Береться єдиний asset, чия назва
     закінчується на `.exe`.
   • SHA-256 цього інсталятора — БУДЬ-ЯКИМ із двох способів (updater
     перевіряє їх у цьому порядку):
       (a) поле `digest` самого asset у відповіді GitHub API, формат
           `sha256:<64-hex>` — GitHub рахує його автоматично для кожного
           завантаженого файлу; окремих дій не потрібно; АБО
       (b) окремий asset-«сусід» на ім'я `<installer>.sha256`, у якому
           лежить 64-символьний hex-digest (можна у форматі `sha256sum`:
           `<hex> *<installer>`).
   Якщо жоден спосіб не дає валідного 64-hex, оновлення вважається
   «не встановлюваним автоматично»: кнопка завантаження ховається, але
   користувач і далі може відкрити сторінку релізу вручну.

Тобто ЄДИНЕ, що треба додатково зробити при релізі поверх звичайного
випуску, — прикріпити інсталятор `.exe` як asset. SHA-256 GitHub дасть сам
(спосіб «a»); спосіб «b» — резерв, якщо digest відсутній.
"""
import hashlib
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path

from whisper_core import paths
from whisper_core import netlog   # доказова офлайновість: журнал вихідних з'єднань

log = logging.getLogger(__name__)

_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
# Читаємо великими шматками — інсталятор може бути кількасот МБ.
_CHUNK = 256 * 1024


class UpdateError(Exception):
    """База для помилок доставки оновлення."""


class InsecureURLError(UpdateError):
    """URL не https:// — завантаження заборонене."""


class ChecksumError(UpdateError):
    """SHA-256 завантаженого файлу не збігся з очікуваним."""


class DownloadError(UpdateError):
    """Мережева/файлова помилка під час завантаження."""


def installers_dir() -> Path:
    """%LOCALAPPDATA%\\Balachky\\updates — куди кладемо інсталятори.
    Створюється ідемпотентно."""
    d = paths.user_dir() / "updates"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # помилка спливе там, де реально пишемо файл
    return d


def normalize_sha256(value: str | None) -> "str | None":
    """Витягти канонічний 64-hex (нижній регістр) із рядка `digest`/sha-файлу.
    Приймає «sha256:ABC…», «abc… *file», просто hex. None/сміття → None."""
    if not value:
        return None
    m = _HEX64.search(value)
    return m.group(0).lower() if m else None


def sha256_of(path, chunk: int = _CHUNK) -> str:
    """Порахувати SHA-256 файлу потоково (не тримаючи його в пам'яті)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def local_installer_path(url: str, dest_dir=None) -> Path:
    """Куди ляже інсталятор із цього URL (за іменем файлу в URL)."""
    name = os.path.basename(urllib.parse.urlparse(url).path) or "installer.exe"
    base = Path(dest_dir) if dest_dir else installers_dir()
    return base / name


def is_downloaded(url: str, expected_sha256: str, dest_dir=None) -> bool:
    """Чи вже лежить валідний (за SHA) інсталятор — щоб не качати вдруге."""
    want = normalize_sha256(expected_sha256)
    if not want:
        return False
    final = local_installer_path(url, dest_dir)
    try:
        return final.exists() and sha256_of(final) == want
    except OSError:
        return False


def installer_ready(url: str, dest_dir=None) -> "Path | None":
    """Готовий (завантажений і вже SHA-перевірений) інсталятор для цього URL,
    або None. Дешево — без повторного хешування: фінальний файл зʼявляється лише
    через os.replace ПІСЛЯ успішної перевірки SHA-256, тож сама його наявність
    гарантує цілість (на відміну від `is_downloaded`, який перехешовує)."""
    final = local_installer_path(url, dest_dir)
    return final if final.exists() else None


def _require_https(url: str) -> None:
    try:
        scheme = urllib.parse.urlparse(url).scheme
    except (TypeError, ValueError):
        scheme = ""
    if scheme.lower() != "https":
        raise InsecureURLError(f"Небезпечний URL оновлення (не https): {url!r}")


def download_installer(url: str, expected_sha256: str, *, progress=None,
                       dest_dir=None, resume: bool = True,
                       chunk: int = _CHUNK, timeout: float = 30.0,
                       context=None, should_cancel=None) -> Path:
    """Завантажити інсталятор із `url`, перевірити SHA-256 і атомарно
    покласти у теку оновлень. Повертає шлях до готового файлу.

    • HTTPS-only: інакше InsecureURLError (нічого не завантажується).
    • resume: якщо лишився `<файл>.part`, докачуємо через HTTP Range
      (сервер без підтримки діапазонів → чистий рестарт з нуля).
    • progress(downloaded, total|None): викликається під час читання.
    • SHA-256 не збігся → файл видаляється, ChecksumError (ніякого
      «оновлення» з невалідним інсталятором на диск не потрапляє).
    • Атомарність: пишемо у `.part`, і лише після валідації os.replace
      на фінальне ім'я (частковий файл ніколи не виглядає як готовий).
    """
    _require_https(url)
    want = normalize_sha256(expected_sha256)
    if not want:
        raise ChecksumError("Немає валідного очікуваного SHA-256 — відмова")

    final = local_installer_path(url, dest_dir)
    part = final.with_name(final.name + ".part")
    final.parent.mkdir(parents=True, exist_ok=True)

    have = part.stat().st_size if (resume and part.exists()) else 0
    headers = {"User-Agent": "Balachky-Updater"}
    if have:
        headers["Range"] = f"bytes={have}-"

    req = urllib.request.Request(url, headers=headers)
    netlog.record_url(url, kind=netlog.UPDATE, detail="installer")
    try:
        # context=None → штатна перевірка сертифікатів системи (HTTPS-безпека).
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            # 206 → сервер докачує з offset; 200 (навіть якщо ми просили Range)
            # → діапазони не підтримуються, починаємо з нуля.
            if have and getattr(resp, "status", resp.getcode()) != 206:
                have = 0
            mode = "r+b" if have else "wb"
            remaining = resp.headers.get("Content-Length")
            total = (have + int(remaining)) if remaining is not None else None
            with open(part, mode) as f:
                if have:
                    f.seek(have)
                    f.truncate()
                done = have
                if progress:
                    progress(done, total)
                while True:
                    if should_cancel and should_cancel():
                        # кооперативне скасування (напр., вихід із застосунку):
                        # `.part` лишаємо на диску — наступний запуск докачає
                        raise DownloadError("Завантаження скасовано")
                    block = resp.read(chunk)
                    if not block:
                        break
                    f.write(block)
                    done += len(block)
                    if progress:
                        progress(done, total)
    except InsecureURLError:
        raise
    except (OSError, ValueError) as e:
        # Обірвана мережа/диск: `.part` лишаємо — наступний виклик докачає.
        raise DownloadError(f"Не вдалося завантажити оновлення: {e}") from e

    # Розрив може завершитися «чистим» EOF раніше за Content-Length — тоді файл
    # неповний. Лишаємо `.part` (не чіпаємо), щоб наступний виклик докачав Range.
    if total is not None and done < total:
        raise DownloadError(
            f"Завантаження неповне: {done} з {total} байтів — докачаємо пізніше")

    got = sha256_of(part)
    if got != want:
        try:
            part.unlink()
        except OSError:
            pass
        raise ChecksumError(
            f"SHA-256 не збігся: очікували {want}, отримали {got}")

    os.replace(part, final)  # атомарний «реліз» готового файлу
    log.info("Оновлення завантажено й перевірено: %s", final)
    return final
