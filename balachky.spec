# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller-спека «Балачки у Коростені» → dist/Balachky/ (onedir).

Канон: onedir (onefile заборонений — антивіруси + повільний старт),
windowed, БЕЗ UPX, БЕЗ CUDA. Модель Whisper НЕ вшивається — докачується
при першому запуску в кеш HuggingFace користувача.

Збірка:  .venv\\Scripts\\pyinstaller balachky.spec
"""
import re
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
)
from PyInstaller.utils.win32.versioninfo import (
    VSVersionInfo,
    FixedFileInfo,
    StringFileInfo,
    StringTable,
    StringStruct,
    VarFileInfo,
    VarStruct,
)

# Версію тягнемо парсингом whisper_core/__init__.py (без import — ядро при
# завантаженні тягне faster-whisper; SPECPATH — тека цієї спеки).
_init_src = (Path(SPECPATH) / "whisper_core" / "__init__.py").read_text("utf-8")
_ver = re.search(r'__version__\s*=\s*"([^"]+)"', _init_src).group(1)
_vtuple = tuple(int(x) for x in (_ver.split(".") + ["0", "0", "0", "0"])[:4])

# Вбиваємо коміт збірки у whisper_core/_buildinfo.py, щоб сайдбар, звіт про
# проблему й шапка тест-журналу показували «версія X.Y.Z (abc1234)» без git у
# frozen-exe. Аналог генерації installer\version.iss (див. docs/BUILD.md). Файл
# ГЕНЕРУЄТЬСЯ тут; репозиторна версія лишає COMMIT=None (у dev читаємо git).
import subprocess as _sp
_commit = "dev"
try:
    _out = _sp.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(SPECPATH),
                   capture_output=True, text=True, timeout=5)
    _commit = (_out.stdout or "").strip() or "dev"
except Exception:
    pass
(Path(SPECPATH) / "whisper_core" / "_buildinfo.py").write_text(
    '"""ЗГЕНЕРОВАНО balachky.spec під час збірки — НЕ редагувати вручну."""\n'
    f'COMMIT = "{_commit}"\n\n\n'
    'def build_commit() -> str:\n'
    '    return COMMIT\n\n\n'
    'def build_version(version: str) -> str:\n'
    '    return f"{version} ({COMMIT})"\n',
    encoding="utf-8")

datas = [
    ("assets", "assets"),                      # іконка (paths.assets_dir())
                                               # + feature/tts-listen: демо-WAV
                                               # «Почути приклад» у assets/tts-samples
                                               # (§7.6; генеруються на білді)
    ("config.example.toml", "."),              # довідка з коментарями
    ("terms.toml", "."),                       # приклад словника — сід
                                               # першого профілю (paths.bundled_terms_example)
    ("templates", "templates"),                # приклади шаблонів для заповнення
                                               # голосом (paths.bundled_templates_dir)
    ("THIRD-PARTY-NOTICES.txt", "."),          # ліцензійні зобовʼязання GPL/LGPL/CC-BY
    ("licenses", "licenses"),                  # повні тексти сторонніх ліцензій
    ("README.uk.md", "."),                     # довідка укр. (paths.bundled_doc)
    ("README.md", "."),                        # довідка англ. (paths.bundled_doc)
    ("README.en.md", "."),                     # сумісність зі старими посиланнями
    ("scripts/verify.py", "scripts"),          # feature/evidence-plus: незалежний
                                               # перевіряч у доказовому пакеті
                                               # (evidence.verifier_source сягає sys._MEIPASS)
]
binaries = []
hiddenimports = [
    "_portaudiowpatch", "mss", "mss.windows",
    "cryptography.hazmat.primitives.ciphers.aead",
    # feature/tts-listen (§6.4): словник вимови — відмінкові форми української через
    # pymorphy3 (лінивий import у lexicon.generate_forms; модульний граф не бачить).
    "pymorphy3", "pymorphy3_dicts_uk", "dawg2_python",
    # feature/ed25519-journal: підпис журналу доказовості (лінивий import у signing.py)
    "cryptography.hazmat.primitives.asymmetric.ed25519",
    "cryptography.hazmat.primitives.serialization",
    # Обсяг оперативної памʼяті машини (heavy_models._default_total_ram). Import
    # усередині try/except у тілі функції — модульний граф його не бачив, і в
    # збірку psutil не потрапляв: перевірено по Analysis-00.toc і dist. Наслідок
    # був невидимий на dev-машині й дорогий у релізі — без psutil захист працює
    # fail-closed, тобто програма на будь-якому залізі вважає памʼять малою і не
    # тримає розпізнавання й озвучення резидентно разом. Знайдено 25.07.
    "psutil",
]
# українські дані pymorphy3 (dawg-словники) — data-файли пакета
datas += collect_data_files("pymorphy3_dicts_uk")

# ctranslate2: рушій faster-whisper, нативні DLL (ctranslate2.dll, libiomp5md…)
binaries += collect_dynamic_libs("ctranslate2")
datas += collect_data_files("ctranslate2")
binaries += collect_dynamic_libs("cryptography")
datas += collect_data_files("cryptography")

# PyAudioWPatch (режим «Нарада», WASAPI loopback): нативна частина —
# _portaudiowpatch*.pyd із влінкованою PortAudio (самодостатня). Це TOP-LEVEL
# модуль, а не файл усередині теки пакета, тож collect_dynamic_libs("pyaudiowpatch")
# його НЕ бачить (дає 0); беремо .pyd явно за шляхом. hiddenimport вище страхує
# з боку модульного графа [A].
import importlib.util as _ilu

_paw_spec = _ilu.find_spec("_portaudiowpatch")
if _paw_spec is not None and _paw_spec.origin:
    binaries.append((_paw_spec.origin, "."))

# PyAV: H.264/MP4 для штатного запису екрана наради. Забираємо лише DLL,
# що вже постачаються wheel-ом (зокрема av.libs), без зовнішніх залежностей.
binaries += collect_dynamic_libs("av")
datas += collect_data_files("av")
# faster_whisper: assets (silero VAD onnx тощо)
datas += collect_data_files("faster_whisper")

# sherpa-onnx: ОПЦІЙНА offline-діаризація мовців (Slice 3). Обгортаємо find_spec,
# щоб збірка з venv БЕЗ sherpa не була помилкою (capability-safe: така збірка
# стартує й ховає фічу). Коли пакет є — збираємо розширення + нативні DLL (імпорти
# ліниві), реєструємо рантайм-хук порядку DLL і перевіряємо, що ключові файли й
# версія onnxruntime (pinned 1.27) справді потрапили у дистрибутив.
import importlib.util as _sherpa_ilu

_sherpa_runtime_hooks = []
if _sherpa_ilu.find_spec("sherpa_onnx") is not None:
    hiddenimports += ["sherpa_onnx"]
    # collect_dynamic_libs за замовчуванням шукає лише *.dll/*.dylib/lib*.so —
    # розширення-модуль _sherpa_onnx*.pyd (лежить у sherpa_onnx/lib) без явного
    # патерну не потрапляє у frozen, і діаризація в інсталяторі мертва.
    _sherpa_bins = collect_dynamic_libs(
        "sherpa_onnx",
        search_patterns=["*.dll", "*.dylib", "lib*.so", "*.pyd"])
    _sherpa_data = collect_data_files("sherpa_onnx")
    binaries += _sherpa_bins
    datas += _sherpa_data
    _collected = {Path(src).name.lower() for src, _dst in _sherpa_bins}
    _collected |= {Path(src).name.lower() for src, _dst in _sherpa_data}
    _required_prefixes = ("_sherpa_onnx",)          # .pyd з суфіксом ABI
    _required_files = ("onnxruntime.dll", "sherpa-onnx-c-api.dll",
                       "sherpa-onnx-cxx-api.dll")
    _missing = [f for f in _required_files if f not in _collected]
    if not any(n.startswith(_required_prefixes) and n.endswith(".pyd")
               for n in _collected):
        _missing.append("_sherpa_onnx*.pyd")
    if _missing:
        raise SystemExit(
            "balachky.spec: у збірку sherpa_onnx не потрапили файли: "
            + ", ".join(_missing))
    try:
        import onnxruntime as _ort
        _mm = ".".join(_ort.__version__.split(".")[:2])
        if _mm != "1.27":
            raise SystemExit(
                f"balachky.spec: onnxruntime {_ort.__version__} != pinned 1.27 "
                "— онови пін і перезапусти frozen-тести.")
    except ImportError:
        raise SystemExit("balachky.spec: sherpa_onnx є, а onnxruntime відсутній")
    _rth = Path(SPECPATH) / "packaging" / "pyi_rth_sherpa_onnx.py"
    if _rth.is_file():
        _sherpa_runtime_hooks = [str(_rth)]
# qtawesome: шрифти іконок (fa6s…)
datas += collect_data_files("qtawesome")

# feature/screen-studio: незалежний режим Запис екрана. Модулі імпортуються
# ЛІНИВО (from whisper_core.screen.recorder import … всередині методу UI),
# тож модульний граф їх не бачить — кладемо явно, інакше режим падає на інсталяції.
hiddenimports += ["whisper_core.screen", "whisper_core.screen.recorder",
                  "whisper_core.screen.win32"]

# feature/player-recordings: QMediaPlayer вантажить бекенд ліниво, тому одного
# import PySide6.QtMultimedia у модульному графі недостатньо. Кладемо DLL
# multimedia-плагінів саме в шлях, який Qt шукає у frozen onedir-збірці.
hiddenimports += ["PySide6.QtMultimedia"]

# VC++ runtime (MSVCP140/VCRUNTIME140/VCRUNTIME140_1): ctranslate2.dll і ffmpeg-
# DLL (av) — це MSVC-збірки й лінкуються на MSVC-рантайм. На чистій Windows VM
# без встановленого VC++ Redistributable Windows не знайде рантайм і завантаження
# ctranslate2.dll падає ('DLL load failed'). Кладемо копії з PySide6 у КОРІНЬ
# _internal явно — поруч із ctranslate2.dll, де Windows їх шукає. Не покладаємось
# на випадковий dep-walk. Самодостатньо, без окремого кроку інсталятора.
import PySide6 as _pyside6

_pyside6_dir = Path(_pyside6.__file__).parent
_multimedia_plugins_dir = _pyside6_dir / "plugins" / "multimedia"
if _multimedia_plugins_dir.is_dir():
    binaries += [
        (str(plugin), "PySide6/plugins/multimedia")
        for plugin in _multimedia_plugins_dir.glob("*.dll")
    ]
for _rt_dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
    _rt_src = _pyside6_dir / _rt_dll
    if _rt_src.is_file():
        binaries.append((str(_rt_src), "."))

a = Analysis(
    ["run_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=_sherpa_runtime_hooks,
    excludes=[
        "matplotlib", "tkinter", "_tkinter",       # не використовуються
        "pytest", "unittest",                      # тести
        "aiogram", "fronts.telegram",              # telegram-фронт не для desktop-збірки
        "IPython", "jedi",
        # GPL-пакети GUI-автоматизації: у коді не імпортуються, але могли б лишитись
        # у venv транзитивно від PyAutoGUI. Явно виключаємо, щоб GPL-код НІКОЛИ не
        # потрапив у дистрибутив під PolyForm NC (ввід — власний ctypes wininput).
        "pyautogui", "pymsgbox", "mouseinfo",
        # Російські словники pymorphy3: транзитивна залежність самого pymorphy3,
        # яку програма НЕ використовує (розбір кличеться лише з lang="uk"). У
        # попередній збірці вони лежали в дистрибутиві — 16 МБ даних, яких ніхто
        # не читає. Перевірено живим прогоном: український розбір працює без них.
        "pymorphy3_dicts_ru",
        # feature/tts-listen (§12.1): torch/transformers і TTS-специфіка живуть ЛИШЕ
        # у balachky-tts-worker.exe. З GUI-Analysis їх виключаємо (легкий exe, без
        # ризику AV, швидший старт). ctranslate2 НЕ виключати — чинний STT імпортує
        # його напряму (whisper_core/engine.py), виключення зламало б розпізнавання.
        "torch", "torchaudio", "transformers", "vocos", "ipa_uk",
        "ukrainian_word_stress", "tts_uk", "styletts2_inference",
        # AI-протокол: llama-cpp-python живе ЛИШЕ у balachky-protocol-worker.exe
        # (окремий console-процес). З GUI-Analysis виключаємо — GUI його не імпортує,
        # а нативні llama.dll/ggml*.dll не мають дублюватись у головному exe.
        "llama_cpp",
    ],
    noarchive=False,
)

# ASIO-збірка PortAudio містить пропрієтарний Steinberg ASIO SDK — не поширюємо;
# sounddevice за замовчуванням вантажить MIT-версію libportaudio64bit.dll
a.datas = [d for d in a.datas if "portaudio64bit-asio" not in d[0].lower()]
a.binaries = [b for b in a.binaries if "portaudio64bit-asio" not in b[0].lower()]

# Російські словники pymorphy3 викидаємо ПІСЛЯ Analysis, а не через excludes:
# excludes блокує лише імпорт модуля, а сам pymorphy3 приносить дані обох мов
# через свій hook — після першої спроби 25.07 у dist усе одно лежали 16 МБ
# pymorphy3_dicts_ru. Програма їх не читає (розбір лише lang="uk"), тому фільтруємо
# і дані, і чистий Python. Перевірку робить tests/test_build_excludes_ru_dicts.py.
_RU_DICTS = "pymorphy3_dicts_ru"
a.datas = [entry for entry in a.datas if _RU_DICTS not in entry[0].replace("\\", "/")]
a.pure = [entry for entry in a.pure if not entry[0].startswith(_RU_DICTS)]

pyz = PYZ(a.pure)

# Ресурс версії .exe: заповнює властивості файлу й підвищує репутацію
# непідписаного PyInstaller-exe у SmartScreen/AV-евристиках (за політики noupx).
_version_res = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_vtuple,
        prodvers=_vtuple,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "Mykola Zhukovets"),
                StringStruct("FileDescription", "Балачки у Коростені"),
                StringStruct("FileVersion", _ver),
                StringStruct("InternalName", "Balachky"),
                StringStruct("OriginalFilename", "Balachky.exe"),
                StringStruct("ProductName", "Балачки у Коростені"),
                StringStruct("ProductVersion", _ver),
                StringStruct("LegalCopyright", "© Mykola Zhukovets"),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # onedir!
    name="Balachky",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                    # noupx — канон (AV-фальспозитиви)
    console=False,                # windowed; для діагностики зібрати True
    icon="assets\\balachky.ico",
    version=_version_res,         # метадані версії/продукту у властивостях файлу
)

# feature/tts-listen (§12.1): окремий balachky-tts-worker.exe з ВЛАСНИМ Analysis
# (torch/torchaudio/vocos + StyleTTS2-гілка transformers/ipa_uk/ukrainian_word_stress
# та RAD-TTS-гілка tts_uk). Guarded find_spec("torch"): збірка з venv БЕЗ torch НЕ
# помилка (як sherpa) — тоді воркер-EXE не пакується, і TTS у такому білді просто
# недоступний (чесна деградація; sidecar піднімає error). Обидва EXE — в ОДНОМУ
# COLLECT → наявний installer wildcard (installer/balachky.iss recursesubdirs)
# підхопить воркер поруч із Balachky.exe без правки .iss.
import importlib.util as _torch_ilu

_worker_targets = [exe, a.binaries, a.datas]
# BALACHKY_SKIP_TTS=1 — свідомо полегшена збірка: воркер озвучення тягне
# torch+CUDA (4.7 ГБ на диску проти ~150 МБ усього іншого), а користуються
# озвученням не всі. Без нього застосунок працює повністю, крім озвучення:
# sidecar.engine_available() бачить відсутній exe, майстер не пропонує
# качати голос, кнопки читання чесно кажуть, що рушія немає.
import os as _os
_skip_tts = _os.environ.get("BALACHKY_SKIP_TTS") == "1"
if not _skip_tts and _torch_ilu.find_spec("torch") is not None:
    _tts_hidden = [
        "whisper_core.tts.worker", "whisper_core.tts.engines",
        "whisper_core.tts.engines.styletts2", "whisper_core.tts.engines.radtts",
        "torch", "torchaudio", "numpy",
    ]
    for _opt in ("transformers", "ipa_uk", "ukrainian_word_stress",
                 "ukrainian_accentor",
                 # six — extern-залежність torch.package-архіву accentor-lite.pt:
                 # PackageImporter тягне його в рантаймі, тож modulegraph не бачить
                 # → "No module named 'six'" у styletts2.load. (Єдиний відсутній
                 # extern архіву; решта — torch/numpy/stdlib — уже в білді.)
                 "six",
                 "styletts2_inference", "vocos", "tts_uk", "numba", "librosa",
                 "scipy"):
        if _torch_ilu.find_spec(_opt) is not None:
            _tts_hidden.append(_opt)
    _worker_bins = list(binaries)
    _worker_datas = list(datas)
    for _pkg in ("torch", "torchaudio"):
        try:
            _worker_bins += collect_dynamic_libs(_pkg)
            _worker_datas += collect_data_files(_pkg)
        except Exception:
            pass
    # data-файли G2P-стека StyleTTS2: модель акцентора (ukrainian_accentor/
    # accentor-lite.pt) і trie наголосів (ukrainian_word_stress/data/stress.trie).
    # hiddenimports тягне лише .py — ці .pt/.trie треба збирати ЯВНО, інакше
    # styletts2.load() падає у Stressifier ("accentor-lite.pt: No such file").
    # (Раніше дефект ховав import-lock deadlock, який не давав дійти до load.)
    for _pkg in ("ukrainian_word_stress", "ukrainian_accentor"):
        if _torch_ilu.find_spec(_pkg) is not None:
            try:
                _worker_datas += collect_data_files(_pkg)
            except Exception:
                pass
    # styletts2_inference: PLBert(AlbertModel) — transformers auto_docstring на
    # інстанціюванні класу робить inspect.getsource(PLBert) → відкриває
    # styletts2_inference/models.py. Frozen пакує лише .pyc, тож бандлимо .py-
    # ДЖЕРЕЛА як дані (include_py_files), інакше styletts2.load падає
    # "No such file: models.py".
    if _torch_ilu.find_spec("styletts2_inference") is not None:
        try:
            _worker_datas += collect_data_files(
                "styletts2_inference", include_py_files=True)
        except Exception:
            pass
    a_worker = Analysis(
        ["run_tts_worker.py"],
        pathex=["."],
        binaries=_worker_bins,
        datas=_worker_datas,
        hiddenimports=_tts_hidden,
        hookspath=[],
        runtime_hooks=_sherpa_runtime_hooks,
        excludes=["matplotlib", "tkinter", "_tkinter", "pytest",
                  "aiogram", "fronts", "PySide6"],   # воркер БЕЗ Qt/GUI
        noarchive=False,
    )
    pyz_worker = PYZ(a_worker.pure)
    exe_worker = EXE(
        pyz_worker, a_worker.scripts, [],
        exclude_binaries=True, name="balachky-tts-worker",
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False,
        # console=True: IPC воркера — на stdin/stdout. windowed (console=False) дав би
        # sys.stdin/stdout=None → IPC не піднявся б (ревізія Sol §12.1). Приховане
        # вікно консолі забезпечує CREATE_NO_WINDOW у parent при спавні (sidecar).
        console=True,
    )
    _worker_targets = [exe, exe_worker, a.binaries, a.datas,
                       a_worker.binaries, a_worker.datas]

# AI-протокол наради (feature/protocol-activation): окремий balachky-protocol-worker.exe
# з ВЛАСНИМ Analysis (llama-cpp-python + нативні llama.dll/ggml*.dll, ізольовані від
# GUI-exe). console=True — IPC воркера по stdin/stdout (windowed дав би stdin/stdout=None,
# §12.1). Guarded find_spec("llama_cpp"): збірка з venv БЕЗ llama НЕ помилка (як sherpa/
# torch) — тоді воркер-EXE не пакується, і AI-протокол у такому білді просто недоступний
# (чесна деградація; sidecar піднімає error, кнопки неактивні з підказкою). VC++ рантайм
# (msvcp140/vcruntime140) уже кладеться в _internal для ctranslate2 — llama.dll (MSVC-
# складання) користується тими самими копіями в спільному onedir.
import importlib.util as _llama_ilu

if _llama_ilu.find_spec("llama_cpp") is not None:
    _pw_hidden = ["llama_cpp"]
    _pw_bins = list(binaries) + collect_dynamic_libs("llama_cpp")
    _pw_datas = list(datas) + collect_data_files("llama_cpp")
    _pw_names = {Path(_s).name.lower() for _s, _d in _pw_bins}
    _pw_missing = []
    if "llama.dll" not in _pw_names:
        _pw_missing.append("llama.dll")
    if not any(n.startswith("ggml") and n.endswith(".dll") for n in _pw_names):
        _pw_missing.append("ggml*.dll")
    if _pw_missing:
        raise SystemExit(
            "balachky.spec: у збірку llama_cpp не потрапили нативні DLL: "
            + ", ".join(_pw_missing))
    a_pworker = Analysis(
        ["run_protocol_worker.py"],
        pathex=["."],
        binaries=_pw_bins,
        datas=_pw_datas,
        hiddenimports=_pw_hidden,
        hookspath=[],
        runtime_hooks=_sherpa_runtime_hooks,
        excludes=["matplotlib", "tkinter", "_tkinter", "pytest", "aiogram",
                  "fronts", "PySide6",                # воркер БЕЗ Qt/GUI
                  "torch", "torchaudio", "transformers", "styletts2_inference",
                  "tts_uk", "vocos"],                  # і без TTS-стека
        noarchive=False,
    )
    pyz_pworker = PYZ(a_pworker.pure)
    exe_pworker = EXE(
        pyz_pworker, a_pworker.scripts, [],
        exclude_binaries=True, name="balachky-protocol-worker",
        debug=False, bootloader_ignore_signals=False, strip=False,
        upx=False,
        console=True,                                  # IPC на stdin/stdout (§12.1)
    )
    _worker_targets += [exe_pworker, a_pworker.binaries, a_pworker.datas]

coll = COLLECT(
    *_worker_targets,
    strip=False,
    upx=False,
    name="Balachky",
)
