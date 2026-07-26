"""Тиха перевірка оновлень через GitHub Releases (без автозавантаження).

Ядро НЕ імпортує ні PySide6, ні мережевих бібліотек поза stdlib. Єдиний
зовнішній контакт застосунку в цьому модулі — GET на публічний GitHub API,
щоб дізнатися номер найновішої версії. Нічого про користувача не надсилається
(див. розділ «Приватність» у документації фічі).

Порівняння версій — packaging.version (PEP 440): «v0.3.0» == «0.3.0», тож
lstrip('v') не потрібен; невалідний тег не крашить (тиха відмова).
"""
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

from . import netlog   # доказова офлайновість: журнал вихідних з'єднань
from collections import namedtuple

from packaging.version import InvalidVersion, Version

log = logging.getLogger(__name__)

# Репозиторій релізів. 404 не доводить актуальність: репозиторій
# може бути приватним, видаленим або не мати latest release.
REPO = "mykola-zhukovets/balachky"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
# app.py очікує цей граничний socket timeout при коректному завершенні QThread.
SOCKET_TIMEOUT_SECONDS = 5

# Статуси результату перевірки.
UPDATE_AVAILABLE = "update_available"   # є новіша версія
UP_TO_DATE = "up_to_date"               # latest release є і він не новіший
NOT_MODIFIED = "not_modified"           # 304: з минулої перевірки нічого не змінилось
OFFLINE = "offline"                     # мережа/таймаут/rate-limit — тихо, нейтрально

# latest_version/url — None, коли статус не несе нової версії. etag — для наступного
# If-None-Match: умовний запит, що вертає 304, НЕ рахується проти ліміту 60/год;
# плюс троттлінг раз/тиждень тримає нас далеко від будь-якого ліміту.
# installer_url/sha256/notes — для автоматичної доставки:
# пряме посилання на asset-інсталятор, його очікуваний SHA-256 і «Що нового».
# None, коли реліз не містить встановлюваного інсталятора (лише сторінка релізу).
# Формат публікації описано у whisper_core.updater.
UpdateResult = namedtuple(
    "UpdateResult",
    "status latest_version url etag installer_url sha256 notes")


def _pick_installer(assets):
    """Знайти у списку assets релізу інсталятор (.exe) та його SHA-256.
    SHA беремо з поля `digest` самого asset («sha256:…»), яке GitHub рахує
    автоматично — без додаткового мережевого запиту. Повертає
    (installer_url|None, sha256|None); формат публікації — у whisper_core.updater."""
    if not isinstance(assets, list):
        return None, None
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").lower()
        dl = a.get("browser_download_url")
        if dl and name.endswith(".exe"):
            m = re.search(r"[0-9a-fA-F]{64}", a.get("digest") or "")
            return dl, (m.group(0).lower() if m else None)
    return None, None


def is_newer(latest: str, current: str) -> bool:
    """Чи latest строго новіший за current. Невалідний тег → False (не краш)."""
    if not latest:
        return False
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


def is_release_url(url: str | None) -> bool:
    """Чи веде кешоване посилання саме на Releases канонічного репозиторію."""
    if not url:
        return False
    try:
        parsed = urllib.parse.urlparse(url)
    except (TypeError, ValueError):
        return False
    prefix = f"/{REPO}/releases/"
    return (parsed.scheme == "https" and parsed.hostname == "github.com"
            and parsed.path.startswith(prefix))


def check_latest(current_version, etag=None, timeout=(3, SOCKET_TIMEOUT_SECONDS)) -> UpdateResult:
    """Запитати GitHub про найновіший реліз. Ніколи не кидає — усі помилки
    згортаються у статус OFFLINE. timeout — сумісний із requests
    кортеж (connect, read); urllib не вміє їх розділяти, тож беремо верхню межу
    як таймаут сокет-операції (офлайн усе одно валиться миттєво URLError'ом)."""
    sock_timeout = float(max(timeout)) if isinstance(timeout, (tuple, list)) else float(timeout)
    # User-Agent ОБОВʼЯЗКОВИЙ — без нього GitHub віддає 403.
    headers = {
        "User-Agent": f"Balachky-Korosten/{current_version}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = urllib.request.Request(API_URL, headers=headers)
    netlog.record_url(API_URL, kind=netlog.UPDATE, detail="check")
    try:
        with urllib.request.urlopen(req, timeout=sock_timeout) as resp:
            new_etag = resp.headers.get("ETag", etag)
            data = json.loads(resp.read().decode("utf-8"))
            # captive-portal/проксі може віддати 200 з валідним JSON, але не
            # об'єктом (масив/рядок/число) — data.get кинув би AttributeError
            # повз наш except і зламав би контракт «ніколи не кидає»
            if not isinstance(data, dict):
                return UpdateResult(OFFLINE, None, None, etag, None, None, None)
            tag = data.get("tag_name") or ""
            url = data.get("html_url") or ""
            status = UPDATE_AVAILABLE if is_newer(tag, current_version) else UP_TO_DATE
            # для показу знімаємо префікс «v» — щоб «нова версія: 1.1.0» збігалось
            # за стилем із «Версія 1.0.0»; порівняння версій робив уже сирий tag
            ver = tag[1:] if tag[:1] in ("v", "V") else tag
            installer_url, sha256 = _pick_installer(data.get("assets"))
            notes = (data.get("body") or "").strip() or None
            return UpdateResult(status, ver or None, url or None, new_etag,
                                installer_url, sha256, notes)
    except urllib.error.HTTPError as e:
        # HTTPError — підклас URLError, тож ловимо його ПЕРШИМ.
        if e.code == 304:                       # нічого не змінилось з минулого разу
            return UpdateResult(NOT_MODIFIED, None, None, etag, None, None, None)
        # 404/403/інші HTTP-відмови не підтверджують, що версія актуальна.
        log.debug("GitHub releases HTTP %s — тиха відмова", e.code)
        return UpdateResult(OFFLINE, None, None, etag, None, None, None)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        # URLError (офлайн/DNS), TimeoutError (сокет), OSError, JSONDecodeError.
        log.debug("Перевірка оновлень не вдалась: %s", e)
        return UpdateResult(OFFLINE, None, None, etag, None, None, None)
