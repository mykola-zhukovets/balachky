"""Чисто ФАЙЛОВА детекція стану моделі — БЕЗ мережі, без snapshot_download.

Ядро мусить уміти сказати «пінований знімок є / є інша ревізія / нема нічого»
самими Path.exists/glob, щоб desktop-фронт міг показати відновлення замість
краху. Тут живе спільна логіка (repo_for / revision_for / model_present), яку
раніше тримав fronts/desktop/onboarding.py; онбординг тепер реекспортує її
звідси, а ядро (engine.py) імпортує без залежності від PySide6.

ПРИВАТНІСТЬ: жоден виклик у цьому модулі не ходить у мережу — тільки читання
локальної файлової системи (кеш-формат HuggingFace).
"""
import os
import shutil
import tempfile
from pathlib import Path

# фолбек, якщо faster_whisper недоступний на момент виклику (не має статись)
_FALLBACK_REPOS = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
}

# стани, які повертає resolve_model_state
PINNED_OK = "pinned_ok"                       # пінований знімок на місці
OTHER_REVISION_PRESENT = "other_revision"     # є повний знімок, але інший коміт
ABSENT = "absent"                             # жодного повного знімка


def repo_for(model_name: str) -> str:
    """Repo — саме той, що шукатиме WhisperModel (мапа faster_whisper).

    Невідоме ім'я (власна модель за HF-id або локальна тека) → повертаємо його ж:
    для HF-id це і є репозиторій; для локального шляху детекція просто не знайде
    кеш-теку (модель уже лежить на диску й вантажиться напряму). Головне — не
    падати KeyError на recovery-шляху для не-пресетної моделі."""
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS[model_name]
    except Exception:
        return _FALLBACK_REPOS.get(model_name, model_name)


def revision_for(model_name: str):
    """Пінований коміт репо моделі (supply-chain) — той самий, що вантажить рушій.
    Джерело істини — whisper_core.engine.MODEL_REVISIONS (щоб докачка і рушій
    завжди були на одному хеші). None → немапована модель, качаємо/вантажимо main."""
    from whisper_core.engine import MODEL_REVISIONS
    return MODEL_REVISIONS.get(model_name)


def _snapshot_complete(snap: Path) -> bool:
    """Чи знімок ПОВНИЙ: ваги + config.json + tokenizer.json + vocabulary.*.
    Скасована на півдорозі докачка лишає частину файлів → повним не вважаємо.

    Перевіряємо НАЯВНІСТЬ ІМЕН через листинг теки (os.listdir), НЕ заходячи за
    символьні лінки HF-кешу (snapshots/<rev>/файл → ../../blobs/<hash>): якщо тека
    моделі лежить за reparse point / mount point, .exists()/.stat() (що йдуть за
    лінком) кидають OSError (WinError 448 «untrusted mount point»), а читання
    записів теки — ні. Так детекція не роняє застосунок і бачить наявний знімок."""
    try:
        names = set(os.listdir(snap))
    except OSError:
        return False
    weights = bool(names & {"model.bin", "model.safetensors"})
    rest = {"config.json", "tokenizer.json"} <= names
    vocab = any(n.startswith("vocabulary.") for n in names)
    return weights and rest and vocab


def model_snapshot_usable(model_dir: str, repo_id: str, revision) -> bool:
    """Чи цільовий знімок повний і потрібні файли реально читаються.

    На відміну від :func:`model_present`, ця перевірка навмисно відкриває по
    одному байту кожного потрібного файла. Так Engine відрізняє відсутню,
    неповну або недоступну через symlink/mount модель від CUDA/DLL/driver
    помилки: останню не можна маскувати діалогом відновлення моделі.
    """
    if not revision:
        # Немаповані моделі (наприклад, small у smoke-конфігу) вантажаться як
        # main. Якщо в кеші є повний локальний snapshot, перевіряємо саме його,
        # щоб їхні CUDA/DLL помилки теж не маскувались як відсутня модель.
        revision = local_snapshot_revision(model_dir, repo_id)
        if not revision:
            return False
    snap = (Path(model_dir) / ("models--" + repo_id.replace("/", "--"))
            / "snapshots" / revision)
    try:
        names = set(os.listdir(snap))
        weights = next((n for n in ("model.bin", "model.safetensors")
                        if n in names), None)
        vocab = [n for n in names if n.startswith("vocabulary.")]
        required = ([weights, "config.json", "tokenizer.json"] + vocab
                    if weights and vocab else [])
        if not required or not {"config.json", "tokenizer.json"} <= names:
            return False
        for name in required:
            with open(snap / name, "rb") as fh:
                if not fh.read(1):
                    return False
    except OSError:
        return False
    return True


def model_present(model_dir: str, repo_id: str, revision=None) -> bool:
    """Чи є в теці ПОВНИЙ знімок ПІНОВАНОЇ ревізії моделі (кеш-формат HuggingFace):
    models--org--name/snapshots/<revision>/ із вагами, config.json, tokenizer.json
    і vocabulary.*. Перевіряємо саме пінований коміт, бо рушій вантажить саме його
    (local_files_only) — інша ревізія в кеші не врятує.
    revision=None (немапована модель) → підійде будь-який повний знімок."""
    snapshots = (Path(model_dir) / ("models--" + repo_id.replace("/", "--"))
                 / "snapshots")
    try:
        if not snapshots.is_dir():
            return False
        snaps = [snapshots / revision] if revision else list(snapshots.iterdir())
    except OSError:
        return False
    return any(_snapshot_complete(snap) for snap in snaps)


def local_snapshot_revision(model_dir: str, repo_id: str):
    """Sha будь-якого ПОВНОГО локального знімка моделі (для офлайн-старту наявної
    ревізії, коли пінованої нема). revision=None+local_files_only може не знайти
    знімок без refs/main, тож рушію треба передати фактичний коміт. None — нема."""
    snapshots = (Path(model_dir) / ("models--" + repo_id.replace("/", "--"))
                 / "snapshots")
    try:
        if not snapshots.is_dir():
            return None
        for snap in snapshots.iterdir():
            if _snapshot_complete(snap):
                return snap.name
    except OSError:
        return None
    return None


def _snapshot_all_real(snap: Path) -> bool:
    """Чи знімок ПОВНИЙ і всі потрібні файли — РЕАЛЬНІ (не символьні лінки):
    ваги (model.bin/safetensors) + config.json + tokenizer.json + vocabulary.*.
    Підтверджує успіх дереференсу: рушій (ctranslate2) відкриє реальні файли без
    ходіння крізь reparse point (WinError 448)."""
    try:
        names = set(os.listdir(snap))
    except OSError:
        return False

    def _real(name: str) -> bool:
        try:
            return not os.path.islink(snap / name)
        except OSError:
            return False

    weights = next((n for n in ("model.bin", "model.safetensors")
                    if n in names), None)
    if not weights or not _real(weights):
        return False
    if not ({"config.json", "tokenizer.json"} <= names):
        return False
    if not (_real("config.json") and _real("tokenizer.json")):
        return False
    vocab = [n for n in names if n.startswith("vocabulary.")]
    return bool(vocab) and all(_real(n) for n in vocab)


def model_all_real(model_dir, repo_id, revision=None) -> bool:
    """Чи є в теці повний знімок моделі, де всі потрібні файли РЕАЛЬНІ
    (не символьні лінки HF-кешу). revision=None (немапована модель) → будь-який
    повний локальний знімок. Для вибору між кандидатами: real-file знімок
    frozen exe відкриє одразу, symlink-знімок потребує дереференсу
    (див. dereference_snapshot / WinError 448)."""
    if not revision:
        revision = local_snapshot_revision(model_dir, repo_id)
        if not revision:
            return False
    snap = (Path(model_dir) / ("models--" + repo_id.replace("/", "--"))
            / "snapshots" / revision)
    return _snapshot_all_real(snap)


def dereference_snapshot(model_dir, repo_id, revision) -> bool:
    """САМО-ЛІКУВАННЯ: замінити символьні лінки у знімку моделі РЕАЛЬНИМИ копіями
    файлів. Заморожений (без підпису) exe на деяких системах не має права ходити
    за лінками HF-кешу (snapshots/<rev>/model.bin → ../../blobs/<hash>): Windows
    кидає WinError 448 «untrusted mount point» на traversal, і ctranslate2 падає
    «Unable to open file 'model.bin'». Тоді детекція бачить модель НАЯВНОЮ, але
    вона не вантажиться — користувача крутить у циклі докачки замість «просто
    працює». Дереференс лагодить це БЕЗ мережі.

    Для КОЖНОГО запису-лінка в знімку: читаємо ціль через os.path.realpath
    (канонічний шлях до blob — БЕЗ ходіння крізь reparse point самого лінка),
    копіюємо байти у тимчасовий файл у ТІЙ САМІЙ теці (shutil.copyfile) й атомарно
    (os.replace) підміняємо лінк реальним файлом.

    Ідемпотентна: не-лінки пропускає. Чіпає ЛИШЕ файли всередині цього знімка —
    нічого не видаляє поза ним, blob-джерело лишається на місці (модель у теці
    іншого застосунку не ушкоджується). Якщо realpath/копіювання кидає OSError
    (traversal усе ще заблоковано 448 / нема доступу / диск повний) — дереференс
    неможливий: повертаємо False, і звичайне відновлення-докачка йде далі.

    True → знімок має ВСІ потрібні файли (model.bin/config.json/tokenizer.json/
    vocabulary.*) реальними; False → інакше (немапована ревізія, збій, неповний)."""
    if not revision:
        return False            # без пінованого коміта не знаємо, який знімок
    snap = (Path(model_dir) / ("models--" + repo_id.replace("/", "--"))
            / "snapshots" / revision)
    try:
        entries = os.listdir(snap)
    except OSError:
        return False
    for name in entries:
        path = snap / name
        try:
            if not os.path.islink(path):
                continue                # уже реальний файл — ідемпотентність
        except OSError:
            return False
        tmp = None
        try:
            real = os.path.realpath(path)          # канонічний шлях до blob
            fd, tmp = tempfile.mkstemp(dir=str(snap), prefix=".deref-")
            os.close(fd)
            shutil.copyfile(real, tmp)             # читаємо реальний blob напряму
            os.replace(tmp, path)                  # атомарна підміна лінка файлом
        except OSError:
            if tmp is not None:                    # прибрати недокопію
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return False
    return _snapshot_all_real(snap)


def _canonical_cache_root(model_dir) -> str:
    """Звести будь-який вибраний рівень HF-кешу до кореня ``hub``.

    Діалог вибору теки дозволяє вказати HF_HOME, ``hub``, каталог конкретної
    моделі, ``snapshots`` або каталог ревізії. Для detector/download_root усі ці
    варіанти мають означати один і той самий hub cache root.
    """
    expanded = os.path.expandvars(os.path.expanduser(os.fspath(model_dir)))
    path = Path(os.path.abspath(expanded))
    name = path.name.casefold()
    parent_name = path.parent.name.casefold()

    if parent_name == "snapshots" and path.parent.parent.name.casefold().startswith("models--"):
        path = path.parent.parent.parent       # snapshots/<revision> → hub
    elif name == "snapshots" and path.parent.name.casefold().startswith("models--"):
        path = path.parent.parent              # models--.../snapshots → hub
    elif name.startswith("models--"):
        path = path.parent                     # models--org--repo → hub
    elif name != "hub":
        hub = path / "hub"
        try:
            if hub.is_dir():
                path = hub                    # HF_HOME/cache root → hub
        except OSError:
            pass
    return os.path.normpath(str(path))


def resolve_cache_dir(model_dir):
    """Канонічний hub cache root для detector/download/save.

    ``cfg.model_dir=None`` → стандартний кеш HuggingFace. Явний шлях може
    бути будь-яким рівнем структури HF-кешу; повертається завжди корінь ``hub``.
    """
    if model_dir:
        return _canonical_cache_root(model_dir)
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
        return _canonical_cache_root(HF_HUB_CACHE)
    except Exception:
        hf_home = (os.environ.get("HF_HOME")
                   or (Path.home() / ".cache" / "huggingface"))
        return _canonical_cache_root(Path(hf_home) / "hub")


class ModelState:
    """Результат resolve_model_state: state ∈ {PINNED_OK, OTHER_REVISION_PRESENT,
    ABSENT}; revision — пінований sha (PINNED_OK) або фактичний локальний sha
    (OTHER_REVISION_PRESENT) чи None (ABSENT)."""
    __slots__ = ("state", "revision")

    def __init__(self, state, revision):
        self.state = state
        self.revision = revision


def resolve_model_state(cfg) -> ModelState:
    """Суто ФАЙЛОВА (без мережі) детекція: чи готовий рушій стартувати офлайн.
    PINNED_OK → пінований знімок на місці; OTHER_REVISION_PRESENT → є інший
    повний знімок (revision несе його sha); ABSENT → нічого повного нема."""
    repo = repo_for(cfg.model_name)
    pinned = revision_for(cfg.model_name)
    cache_dir = resolve_cache_dir(cfg.model_dir)
    if model_present(cache_dir, repo, pinned):
        return ModelState(PINNED_OK, pinned)
    other = local_snapshot_revision(cache_dir, repo)
    if other is not None:
        return ModelState(OTHER_REVISION_PRESENT, other)
    return ModelState(ABSENT, None)


# --- feature/delete-model: безпечне видалення теки моделі з кешу ---
def known_repos() -> set:
    """repo_id усіх моделей, якими КЕРУЄ застосунок (ключі MODEL_REVISIONS).
    Видаляти дозволяємо лише ці теки — чужі моделі в спільному кеші не чіпаємо."""
    from whisper_core.engine import MODEL_REVISIONS
    return {repo_for(name) for name in MODEL_REVISIONS}


def _dir_size(path) -> int:
    """Сума розмірів РЕАЛЬНИХ файлів у теці (БЕЗ ходіння за symlink-ами кешу, щоб
    не рахувати blob двічі: snapshots/<rev>/model.bin → ../../blobs/<hash>).
    os.walk не спускається у symlink-теки (followlinks=False за замовчуванням)."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            fp = os.path.join(root, name)
            if os.path.islink(fp):
                continue                       # symlink-и не рахуємо (двійник blob)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def model_snapshot_size(model_dir, repo_id) -> int:
    """Фактичний розмір теки моделі repo_id на диску у байтах (0 — теки нема).
    Використовується UI для «звільнити N» ще ДО підтвердження видалення."""
    repo_dir = (Path(resolve_cache_dir(model_dir))
                / ("models--" + repo_id.replace("/", "--")))
    return _dir_size(repo_dir)


def _within(path: str, root: str) -> bool:
    """Чи канонічний `path` лежить усередині канонічного `root`
    (регістронезалежно; різні диски / несумісні шляхи → False)."""
    path = os.path.normcase(path)
    root = os.path.normcase(root)
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:                          # різні диски / mix abs+rel
        return False


def delete_model(model_dir, model_name) -> int:
    """Видалити з кешу HuggingFace усю теку моделі `model_name` і повернути
    к-сть звільнених байтів. Видаляємо кеш-одиницю `models--org--repo` цілком
    (snapshots + blobs + refs), тож звільняється РЕАЛЬНЕ місце, а не лише
    symlink-и знімка.

    БЕЗПЕКА (два незалежні бар'єри перед будь-яким видаленням):
      * репо мусить бути ВІДОМИМ застосунку (known_repos) — чужі моделі, що
        лежать у тому ж спільному кеші, не чіпаємо;
      * канонічний шлях теки (os.path.realpath знімає symlink/junction) мусить
        лишитись ПІД канонічним коренем кешу; якщо `models--org--repo` — лінк
        назовні, відмовляємо і НІЧОГО не видаляємо.

    ValueError — коли бар'єр не пройдено або теки нема (нічого не видалено);
    OSError — коли саме видалення не вдалося (напр. файл моделі залочений
    memory-map активного рушія — тоді UI показує зрозумілу помилку)."""
    repo_id = repo_for(model_name)
    if repo_id not in known_repos():
        raise ValueError(f"Невідома модель: {model_name!r}")
    root = resolve_cache_dir(model_dir)
    repo_dir = os.path.join(root, "models--" + repo_id.replace("/", "--"))
    real_root = os.path.realpath(root)
    real_repo = os.path.realpath(repo_dir)      # знімає symlink/junction
    if not _within(real_repo, real_root):
        raise ValueError(
            f"Тека моделі {repo_id} веде за межі кешу {root} — видалення скасовано")
    if not os.path.isdir(real_repo):
        raise ValueError(f"Теки моделі {repo_id} нема в {root}")
    freed = _dir_size(real_repo)
    shutil.rmtree(real_repo)
    return freed
