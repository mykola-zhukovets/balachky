# Збірка “Балачки у Коростені” — інсталер для Windows

Покроково відтворювана збірка: venv → PyInstaller (onedir) → Inno Setup
(per-user, без UAC). Результат — `BalachkySetup-<версія>.exe`, який звичайний
користувач ставить у два кліки без прав адміністратора.

## Канон рішення (не переглядати)

- **PyInstaller 6.x, onedir** — onefile ЗАБОРОНЕНИЙ (антивірусні
  фальспозитиви + повільний старт через розпакування).
- `--windowed`, **без UPX** (ще одна причина AV-тривог), **без CUDA**.
- Поверх — **Inno Setup per-user** (`PrivilegesRequired=lowest`, без UAC),
  сталий `AppId`-GUID (у `installer\balachky.iss`, не змінювати ніколи).
- **Модель Whisper НЕ вшивається** — докачується при першому запуску в кеш
  HuggingFace користувача (`%USERPROFILE%\.cache\huggingface\hub`).

## Два контури Python-оточення

Не змішуйте спільне dev/frozen-оточення зі складальним:

- **Спільний dev/frozen venv** (`.venv`) встановлює `requirements.txt` і,
  коли потрібні frozen-тести чи локальні перевірки спеки,
  `requirements-build.txt`. Важкого `llama-cpp-python` у цьому контурі немає:
  рядок у `requirements.txt` навмисно закоментований, щоб не зачіпати 70+
  worktree.
- **Складальний venv** (окрема тека поза робочим деревом
  репозиторію) — окреме середовище,
  з якого робиться реліз-білд. Після двох спільних файлів воно додатково
  встановлює `requirements-tts-build.txt`; саме там закріплено
  `llama-cpp-python==0.3.34` і CPU-індекс wheel.

`requirements-build.txt` в обох випадках лишається hash-lock рівно семи
інструментів PyInstaller. Не переносьте до нього
`llama-cpp-python`: це знову зробить важку runtime-залежність обов'язковою для
спільного dev-оточення.

## Передумови

- Windows 10/11 x64, Python 3.12.
- Спільний dev/frozen venv без важкої залежності:

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -r requirements-build.txt
```

- Для релізної збірки — окремий складальний venv:

```powershell
python -m venv <тека окремого складального середовища>
$buildPython = "<тека окремого складального середовища>\Scripts\python.exe"
& $buildPython -m pip install -r requirements.txt
& $buildPython -m pip install -r requirements-build.txt
& $buildPython -m pip install -r requirements-tts-build.txt
```

- Inno Setup 6 (для кроку 3):

```powershell
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
```

Компілятор після цього тут:
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`
(або `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` при машинному встановленні).

## Крок 0 — звірити середовище з requirements

Для спільного dev/frozen venv:

```powershell
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -r requirements-build.txt
```

Для релізного складального venv:

```powershell
$buildPython = "<тека окремого складального середовища>\Scripts\python.exe"
& $buildPython -m pip install -r requirements.txt
& $buildPython -m pip install -r requirements-build.txt
& $buildPython -m pip install -r requirements-tts-build.txt
```

Перед кожною релізною збіркою обов'язкові всі три команди другого блоку.
Не встановлюйте `requirements-tts-build.txt` у спільну `.venv`.
`requirements-build.txt` містить точні версії PyInstaller та його залежностей
і SHA-256 дозволеного wheel для кожного пакета; `pip` перевіряє ці хеші під час
встановлення. 25.07 виявилось, що в `.venv` не був
установлений `psutil`, хоча він є і в `requirements.txt`, і в `hiddenimports`
спеки. PyInstaller не може спакувати пакет, якого немає в середовищі, — тобто
дистрибутив мовчки виходив без нього, і програма в релізі не міряла обсяг
оперативної памʼяті: на будь-якому залізі поводилась як на 8 ГБ і не тримала
розпізнавання й озвучення резидентно разом.

## Крок 1 — PyInstaller (onedir)

З кореня репозиторію. Профіль складу задає
`BALACHKY_BUILD_PROFILE`; дозволені рівно два значення:

- `no-tts` — desktop, offline-діаризація і protocol-worker без
  `balachky-tts-worker.exe`. Це детермінований дефолт і профіль публічного
  інсталятора.
- `full` — той самий склад плюс TTS-worker і весь стек озвучення.

Публічна збірка (явний запис профілю рекомендований, хоча він збігається з
дефолтом):

```powershell
$env:BALACHKY_BUILD_PROFILE = "no-tts"
.venv\Scripts\pyinstaller balachky.spec --noconfirm
Remove-Item Env:\BALACHKY_BUILD_PROFILE
```

Повна збірка:

```powershell
$env:BALACHKY_BUILD_PROFILE = "full"
.venv\Scripts\pyinstaller balachky.spec --noconfirm
Remove-Item Env:\BALACHKY_BUILD_PROFILE
```

На старті spec друкує один рядок
`=== BALACHKY BUILD PROFILE: <profile> ===`. Невідоме чи порожнє явно задане
значення зупиняє збірку з переліком допустимих профілів. Якщо профілю бракує
обов'язкового модуля (наприклад, `torch` для `full` або `llama_cpp` для
`no-tts`), збірка падає до `Analysis` і називає профіль, компонент та модуль.

Застарілий `BALACHKY_SKIP_TTS` тимчасово підтримується:
`1` мапиться на `no-tts`, `0` — на `full`, і spec друкує попередження.
Суперечливе одночасне задання старої та нової змінних зупиняє збірку.

Результат: `dist\Balachky\Balachky.exe` + тека `_internal\` (~390 МБ:
PySide6, ctranslate2, onnxruntime, av, numpy…). Точка входу — `run_app.py`
(еквівалент `python -m fronts.desktop`).

Спека вже містить усе потрібне: datas (assets\, config.example.toml,
terms.toml як сід словника), collect для нативних DLL ctranslate2, assets
faster_whisper (VAD onnx), шрифти qtawesome; excludes (matplotlib, tkinter,
aiogram, тести).

## Крок 2 — димова перевірка exe

```powershell
dist\Balachky\Balachky.exe
```

Це трей-застосунок: вікна одразу нема, значок біля годинника. Процес має
жити стабільно (`Get-Process Balachky`). Перший запуск качає модель
(за замовчуванням large-v3, ~3 ГБ) — для швидкої перевірки можна заздалегідь
покласти `%LOCALAPPDATA%\Balachky\config.toml` з `model_name = "small"`.

Якщо крашиться мовчки: тимчасово перезібрати з `console=True` у
`balachky.spec` — traceback з'явиться в консолі.

## Номер збірки (git-коміт у сайдбарі й звіті)

Сайдбар показує “версія 1.1.0 (abc1234)”, де `abc1234` — короткий git-коміт
збірки. Той самий рядок іде в info.txt звіту про проблему й у шапку тест-журналу.

Механіка (аналогічна генерації `installer\version.iss`): крок PyInstaller у
`balachky.spec` виконує `git rev-parse --short HEAD` і **перезаписує**
`whisper_core\_buildinfo.py`, вбиваючи сталий `COMMIT`. У dev-режимі
репозиторна версія файлу має `COMMIT = None` і читає git у рантаймі (відкат на
`dev`, якщо git недоступний), тож збірка не потрібна, щоб побачити коміт локально.

Після PyInstaller-збірки `whisper_core\_buildinfo.py` лишається зі вшитим
комітом (робоче дерево “брудне” на цей файл). Щоб повернути dev-версію:

```powershell
git checkout -- whisper_core\_buildinfo.py
```

## Крок 3 — Inno Setup

Якщо змінювалася версія у `whisper_core\version.py` — перегенерувати
`installer\version.iss`:

```powershell
.venv\Scripts\python -c 'from whisper_core import DISPLAY_VERSION as d, WINDOWS_FILE_VERSION as w; q=chr(34); print("#define AppVersion "+q+d+q); print("#define WindowsFileVersion "+q+".".join(map(str,w))+q)' | Set-Content installer\version.iss -Encoding utf8BOM
```

Компіляція:

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\balachky.iss
```

Результат: `installer\Output\BalachkySetup-<версія>.exe` (~96 МБ, LZMA2).

### Обов'язковий крок: контрольна сума в назві

```powershell
pwsh -File scripts\finalize_installer.ps1
```

Скрипт рахує SHA-256, перейменовує файл у публікаційну назву
`BalachkySetup-<display-версія>-<перші 8 символів суми>.exe`, кладе поруч
`<назва>.sha256` і друкує готовий рядок для опису релізу.

Крок обов'язковий: описи релізів і README називають саме таку назву, а Inno
Setup сам суми в назву не додає. Доти це робилося руками — і саме такий ручний
крок перед публікацією колись забудуть.

## Що і куди ставиться / пишеться

| Що | Де |
| --- | --- |
| Програма (exe + _internal) | `%LOCALAPPDATA%\Programs\Balachky` |
| Конфіг `config.toml` | `%LOCALAPPDATA%\Balachky` |
| Профілі/словники/пам'ять `profiles\` | `%LOCALAPPDATA%\Balachky` |
| Модель Whisper | `%USERPROFILE%\.cache\huggingface\hub` |
| Геометрія вікна | реєстр (QSettings `Balachky\Balachky`) |

Єдине джерело правди про шляхи — `whisper_core\paths.py`: у frozen-режимі все, що пишеться, іде в
`%LOCALAPPDATA%\Balachky` (тека інсталяції може бути read-only); у dev-режимі
все лишається в корені репо, як раніше. Користувацькі дані переживають
оновлення та видалення програми (інсталер їх свідомо не чіпає).

## Розміри (1.1.0, орієнтовно)

- `dist\Balachky\` — ~390 МБ
- `BalachkySetup-1.1.0-<SHA>.exe` — ~114 МБ

## Застереження

- **SmartScreen**: exe та інсталер не підписані — Windows покаже “невідомий
  видавець” / “Windows захистив ваш ПК” → “Докладніше → Виконати все одно”.
  Ліки — сертифікат підпису коду (платно) або накопичення репутації.
- **Антивіруси**: PyInstaller-збірки інколи ловлять евристики. Канон
  (onedir + без UPX) мінімізує це; при скарзі — перевірити на VirusTotal і
  подати false-positive репорт вендору.
- **Оновлення**: `AppId` в `balachky.iss` сталий — нова версія ставиться
  поверх старої штатно. GUID НЕ ЗМІНЮВАТИ.
- Другий запуск exe не створює другий екземпляр — перший показує вікно
  (single-instance канал `balachky-single`).
