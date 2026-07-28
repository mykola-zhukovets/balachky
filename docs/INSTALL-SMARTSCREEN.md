# Windows warning during installation · Попередження Windows під час встановлення

**English** · [Українська](#українська)

## Why Windows shows this warning

The Balachky v1.2.3-beta installer is not digitally signed. Windows SmartScreen may therefore show “Windows protected your PC” and identify the publisher as unknown.

This warning means that Windows cannot verify a trusted publisher signature for this file. It does not, by itself, prove that the installer is harmful or safe.

## Before you continue

Only use the installer from the [official Balachky v1.2.3-beta release](https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.3-beta):

`BalachkySetup-1.2.3-beta-F19111EF.exe`

Do not run a copy received through a messenger, file-sharing site, or another unofficial source.

If you want to verify the download, compare its SHA-256 with the value published for this exact release:

`F19111EFC61FBA327148E1AC29AFB339E838556663FF73300290DCC6B5D7082F`

### Check SHA-256 in PowerShell

1. Open the folder containing the installer in File Explorer.
2. Right-click an empty area in the folder and choose “Open in Terminal”, or open PowerShell separately.
3. Run the command below with the actual path to your downloaded file:

```powershell
Get-FileHash "C:\path\to\BalachkySetup-1.2.3-beta-F19111EF.exe" -Algorithm SHA256
```

4. Compare the complete result with the SHA-256 above.

If the values differ, do not run the file. Delete it and download the installer again from the official release page.

A matching SHA-256 means that the downloaded bytes match the file identified by the published hash. A hash does not verify the author on its own and is not a guarantee that a file is safe.

The release verification record dated 26 July 2026 reports [0 detections from 65 VirusTotal engines](https://www.virustotal.com/gui/file/f19111efc61fba327148e1ac29afb339e838556663ff73300290dcc6b5d7082f/detection) for this exact file. This is additional information, not a safety guarantee.

## Run the installer

Continue only if you intended to install Balachky and are satisfied that you downloaded the expected file.

1. In the SmartScreen window, select “More info”.
2. Check that the app name is the installer you downloaded.
3. Select “Run anyway” to continue.

If the file name, source, or SHA-256 is not what you expected, close the warning and do not run the file.

For ordinary bugs or installation problems, use [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues). Report security problems privately through **Security → Report a vulnerability** in the repository.

---

<a id="українська"></a>

## Українська

### Чому Windows показує це попередження

Інсталятор Балачок v1.2.3-beta не має цифрового підпису. Тому Windows SmartScreen може показати повідомлення “Windows захистила Ваш ПК” і вказати невідомого видавця.

Це попередження означає, що Windows не може перевірити довірений підпис видавця для цього файла. Саме по собі воно не доводить, що інсталятор шкідливий або безпечний.

### Перед продовженням

Використовуйте інсталятор лише з [офіційної сторінки випуску Балачок v1.2.3-beta](https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.3-beta):

`BalachkySetup-1.2.3-beta-F19111EF.exe`

Не запускайте копію, отриману через месенджер, файловий обмінник або інше неофіційне джерело.

Щоб перевірити завантаження, порівняйте SHA-256 файла зі значенням, опублікованим саме для цього випуску:

`F19111EFC61FBA327148E1AC29AFB339E838556663FF73300290DCC6B5D7082F`

### Як перевірити SHA-256 у PowerShell

1. Відкрийте в Провіднику папку із завантаженим інсталятором.
2. Клацніть правою кнопкою миші у вільному місці та оберіть “Відкрити в Терміналі” або окремо відкрийте PowerShell.
3. Виконайте команду нижче, підставивши справжній шлях до завантаженого файла:

```powershell
Get-FileHash "C:\шлях\до\BalachkySetup-1.2.3-beta-F19111EF.exe" -Algorithm SHA256
```

4. Порівняйте повний результат зі значенням SHA-256 вище.

Якщо значення відрізняються, не запускайте файл. Видаліть його й завантажте інсталятор знову з офіційної сторінки випуску.

Збіг SHA-256 означає, що завантажені байти відповідають файлу, для якого опубліковано цей хеш. Сам по собі хеш не підтверджує автора й не гарантує безпечності файла.

У записі перевірки випуску від 26 липня 2026 року для цього точного файла зазначено [0 виявлень серед 65 рушіїв VirusTotal](https://www.virustotal.com/gui/file/f19111efc61fba327148e1ac29afb339e838556663ff73300290dcc6b5d7082f/detection). Це додаткова інформація, а не гарантія безпеки.

### Як запустити інсталятор

Продовжуйте лише тоді, коли Ви справді хотіли встановити Балачки й переконалися, що завантажили очікуваний файл.

1. У вікні SmartScreen оберіть “Докладніше”.
2. Перевірте, що вказано назву завантаженого Вами інсталятора.
3. Оберіть “Виконати попри все”, щоб продовжити.

Якщо назва файла, джерело або SHA-256 не відповідають очікуваним, закрийте попередження й не запускайте файл.

Про звичайні помилки або проблеми зі встановленням повідомляйте в [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues). Проблеми безпеки надсилайте приватно через **Security → Report a vulnerability** у репозиторії.
