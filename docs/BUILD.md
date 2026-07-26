# Збірка «Балачки у Коростені» — інсталер для Windows

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

## Передумови

- Windows 10/11 x64, Python 3.12.
- venv з залежностями застосунку (`requirements.txt`) + `pyinstaller`
  (dev-залежність):

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install pyinstaller
```

- Inno Setup 6 (для кроку 3):

```powershell
winget install -e --id JRSoftware.InnoSetup --silent --accept-package-agreements --accept-source-agreements
```

Компілятор після цього тут:
`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`
(або `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` при машинному встановленні).

## Крок 0 — звірити середовище з requirements

```powershell
.venv\Scripts\pip install -r requirements.txt
```

Крок обов'язковий перед кожною збіркою. 25.07 виявилось, що в `.venv` не був
установлений `psutil`, хоча він є і в `requirements.txt`, і в `hiddenimports`
спеки. PyInstaller не може спакувати пакет, якого немає в середовищі, — тобто
дистрибутив мовчки виходив без нього, і програма в релізі не міряла обсяг
оперативної памʼяті: на будь-якому залізі поводилась як на 8 ГБ і не тримала
розпізнавання й озвучення резидентно разом.

## Крок 1 — PyInstaller (onedir)

З кореня репозиторію:

```powershell
.venv\Scripts\pyinstaller balachky.spec --noconfirm
```

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

Сайдбар показує «версія 1.1.0 (abc1234)», де `abc1234` — короткий git-коміт
збірки. Той самий рядок іде в info.txt звіту про проблему й у шапку тест-журналу.

Механіка (аналогічна генерації `installer\version.iss`): крок PyInstaller у
`balachky.spec` виконує `git rev-parse --short HEAD` і **перезаписує**
`whisper_core\_buildinfo.py`, вбиваючи сталий `COMMIT`. У dev-режимі
репозиторна версія файлу має `COMMIT = None` і читає git у рантаймі (відкат на
`dev`, якщо git недоступний), тож збірка не потрібна, щоб побачити коміт локально.

Після PyInstaller-збірки `whisper_core\_buildinfo.py` лишається зі вшитим
комітом (робоче дерево «брудне» на цей файл). Щоб повернути dev-версію:

```powershell
git checkout -- whisper_core\_buildinfo.py
```

## Крок 3 — Inno Setup

Якщо змінювалася версія у `whisper_core\__init__.py` — перегенерувати
`installer\version.iss`:

```powershell
.venv\Scripts\python -c "from whisper_core import __version__ as v; print(f'#define AppVersion \"{v}\"')" | Set-Content installer\version.iss -Encoding utf8BOM
```

### Полегшена збірка (без озвучення)

Воркер озвучення тягне torch+CUDA — це 4.7 ГБ на диску проти ~150 МБ усього
іншого, а користуються озвученням не всі. Публічний інсталятор збираємо без
нього:

```powershell
$env:BALACHKY_SKIP_TTS = "1"
.venv\Scripts\pyinstaller balachky.spec --noconfirm
Remove-Item Env:\BALACHKY_SKIP_TTS
```

У такій збірці працює все, крім озвучення: `sidecar.engine_available()`
бачить відсутній `balachky-tts-worker.exe`, майстер перших кроків не
пропонує завантажувати голос, кнопки читання чесно кажуть, що рушія немає.
Повна збірка (з озвученням) — той самий рядок без змінної.

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
`BalachkySetup-<версія>-beta-<перші 8 символів суми>.exe`, кладе поруч
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

- **SmartScreen**: exe та інсталер не підписані — Windows покаже «невідомий
  видавець» / «Windows захистив ваш ПК» → «Докладніше → Виконати все одно».
  Ліки — сертифікат підпису коду (платно) або накопичення репутації.
- **Антивіруси**: PyInstaller-збірки інколи ловлять евристики. Канон
  (onedir + без UPX) мінімізує це; при скарзі — перевірити на VirusTotal і
  подати false-positive репорт вендору.
- **Оновлення**: `AppId` в `balachky.iss` сталий — нова версія ставиться
  поверх старої штатно. GUID НЕ ЗМІНЮВАТИ.
- Другий запуск exe не створює другий екземпляр — перший показує вікно
  (single-instance канал `balachky-single`).
