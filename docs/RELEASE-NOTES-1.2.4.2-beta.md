# Примітки до випуску · Release Notes — v1.2.4.2-beta

**Файл / File:** `BalachkySetup-1.2.4.2-beta-20862C01.exe`<br>
**SHA-256:** `20862C0159ED6806A234085C27B3BC108A601161C63C730767F8D644FBBC43B6`<br>
**Розмір / Size:** 161,8 МБ / 161.8 MB (169 618 367 байтів / bytes)<br>
**VirusTotal:** [сторінка файла за SHA-256 / file page by SHA-256](https://www.virustotal.com/gui/file/20862c0159ed6806a234085c27b3bc108a601161c63c730767f8d644fbbc43b6/detection)

---

## Українська версія

Виправлений бета-випуск програми “Балачки у Коростені” (`v1.2.4.2-beta`).

### Що виправлено в 1.2.4.2

Підписи кнопок більше не обрізаються — ні у вузькому вікні, ні за збільшеного системного шрифту. Це стосується бокового меню, сторінок Диктування, Нарада, Словники, Аудіофайли, Запис екрана, усіх вкладок Налаштувань і майстра першого запуску. Версія програми знову видна в боковому меню. У збірку вбудовано автоматичну перевірку, яка обходить усі сторінки на найвужчому вікні зі збільшеним шрифтом, щоб таке більше не проходило непоміченим.

Усе, що виправляла 1.2.4.1 (відтворення відео всередині програми та зауваження до майстра першого запуску), лишається на місці.

### What is fixed in 1.2.4.2

Button labels are no longer clipped — neither in a narrow window nor with a larger system font. This covers the side menu, the Dictation, Meeting, Dictionaries, Audio files and Screen recording pages, every Settings tab, and the first-run wizard. The app version is shown in the side menu again. The build now sweeps every page at the narrowest window size with an enlarged font, so this cannot slip through unnoticed again.

Everything fixed in 1.2.4.1 (video playback inside the app and the first-run wizard findings) remains in place.

### Про програму

“Балачки” — це персональний помічник для голосового введення тексту та обробки нарад. Голос, розшифровки й протоколи обробляються на Вашому комп’ютері та не завантажуються в хмару. Програма дає змогу надиктовувати текст у будь-які поля та застосунки, записувати й транскрибувати розмови із визначенням співрозмовників, створювати локальні ШІ-протоколи нарад (з коротким підсумком, рішеннями та завданнями), а також записувати екран. Інтернет потрібен лише для моделей, додаткових компонентів і оновлень, які Ви вирішите завантажити, та для перевірки оновлень, якщо Ви її дозволили.

### Що нового

- **Закладки просто під час наради.** Кнопка на сторінці наради або `Ctrl+Alt+B` з будь-якого місця позначає важливий момент миттєво — без вікон і без паузи в записі. У перегляді запису позначки показані списком, натискання перемотує на потрібне місце.
- **Зведення доріжок в окремий файл.** Виставте повзунками свій баланс (мікрофон гучніше, системний звук тихіше, зайві доріжки вимкнено) і збережіть окремий файл WAV саме з цим звучанням. Оригінальні доріжки лишаються незмінними.
- **Пошук у тексті наради.** `Ctrl+F` у вікні перегляду відкриває рядок пошуку над розшифровкою: збіги підсвічуються, поруч лічильник на кшталт “3 з 17”, `Enter` веде до наступного збігу з перемотуванням запису.
- **Кошик для нарад.** Видалена нарада спершу лежить у кошику 7 днів, а в повідомленні внизу екрана одразу є кнопка “Повернути”. Текст підтвердження пояснює, що станеться з файлами далі.
- **Ім’я облікового запису Windows не потрапляє у звіти.** Шляхи виду `C:\Users\…` у службових журналах і в архіві команди “Повідомити про проблему” замінюються нейтральною позначкою. Перед збиранням архіву відкривається вікно з переліком того, що саме в нього увійде.

Крім цього: завантаження моделі більше не блокує програму й продовжується після перезапуску, слово підсвічується під час відтворення, текст наради йде поруч із відеозаписом, порожні сторінки пояснюють своє призначення, налаштування впорядковано за групами, 19 системних повідомлень переписано людською мовою. Повний перелік змін — у журналі змін, який тепер вбудований у програму, і у файлі `CHANGELOG.md`.

### Як встановити

1. Завантажте інсталятор `BalachkySetup-1.2.4.2-beta-20862C01.exe` з розділу Releases.
2. Запустіть завантажений файл і дотримуйтесь підказок майстра встановлення.
3. **Зверніть увагу (попередження Windows SmartScreen):** Оскільки це бета-випуск і програма поки не має цифрового підпису, Windows може показати застереження під час запуску інсталятора. Це цілком очікувана поведінка для нових бета-програм. Щоб продовжити встановлення, натисніть **“Докладніше”** (More info) → **“Виконати все одно”** (Run anyway).

Перед запуском рекомендуємо звірити SHA-256. Якщо звіту VirusTotal для цього файла ще немає, його можна надіслати на перевірку самостійно; результат сканування є додатковим сигналом, а не гарантією безпеки.

Попередню версію вилучати не потрібно: встановлення поверх зберігає Ваші записи, розшифровки, налаштування й завантажені моделі.

### Озвучення (“Прослухати”)

Інсталятор програми лишається компактним: рушій озвучення не входить до публічної збірки. Кнопка “Прослухати” присутня в інтерфейсі як анонс і повідомляє про відсутність рушія. Сам рушій буде доступний для окремого завантаження згодом. Основні функції диктування та проведення нарад працюють у повному обсязі без нього.

### Відомі обмеження бети

- Інсталятор ще не має цифрового підпису, тому Windows може показати “Невідомий видавець”.
- Шифрування нарад вмикає користувач. Під час запису робочі файли можуть тимчасово лежати на диску відкритими; після завершення запису артефакти захищаються AES-256-GCM.
- Нарада з кошика зникає остаточно через 7 днів. Якщо запис потрібен, поверніть його раніше.
- Частину англійських формулювань буде додатково відшліфовано в наступних випусках.

### Як повідомити про проблему

Якщо Ви виявили помилку або маєте пропозицію щодо покращення програми:
- У застосунку: “Про програму” → “Повідомити про проблему”;
- Або створіть тему на сторінці [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues).

---

Дякуємо за використання програми та участь у випробуванні! Якщо “Балачки” стали Вам у пригоді, Ви можете підтримати розвиток проєкту кнопкою-сердечком “Підтримати автора” (банка Monobank, валютні рахунки або криптовалюта — на Ваш вибір).

---

## English version

The fixed beta release of Balachky (`v1.2.4.2-beta`).

### About the application

Balachky is a personal voice typing and meeting assistant. Speech, transcripts, and minutes are processed on your computer and are not uploaded to a cloud service. It lets you dictate into any application, record and transcribe meetings with speaker identification, generate local AI minutes (summaries, decisions, and tasks), and record your screen. Internet access is used only for models, optional components, and updates you choose to download, plus update checks if you enable them.

### What’s new

- **Bookmark a moment while the meeting runs.** A button on the meeting page, or `Ctrl+Alt+B` from anywhere, marks the moment instantly — no dialog, no pause in the recording. Marks appear as a list in playback, and clicking one jumps straight to it.
- **Mix your track levels into one file.** Set the balance with the sliders — mic louder, system audio quieter, unused tracks off — and save a separate WAV that sounds exactly like that. The original tracks are left untouched.
- **Search inside a meeting transcript.** `Ctrl+F` in the playback window opens a search bar above the transcript. Matches are highlighted, a counter shows “3 of 17”, and `Enter` moves to the next match and scrubs the recording to it.
- **Deleted meetings go to a trash bin.** A deleted meeting sits in the bin for 7 days, and the notification at the bottom of the screen offers “Restore” right away. The delete confirmation explains plainly what happens to the files.
- **Your Windows account name stays out of reports.** Paths like `C:\Users\…` in service logs and in the archive built by “Report a problem” are replaced with a neutral placeholder. Before the archive is assembled, a window lists exactly what will go into it.

Also in this release: model downloads keep running in the background and resume after a restart, the transcript follows the audio word by word, remarks run alongside the video, empty pages explain what belongs there, settings are grouped and easier to scan, and 19 system messages were rewritten in plain language. The full list is in the changelog, which now ships inside the app, and in `CHANGELOG.md`.

### Installation instructions

1. Download the installer `BalachkySetup-1.2.4.2-beta-20862C01.exe` from the Releases page.
2. Run the downloaded file and follow the installation wizard.
3. **Note on Windows SmartScreen:** Because this is a beta release without a digital signature yet, Windows SmartScreen may show a security warning. This is expected for new beta software. To proceed, click **“More info”** → **“Run anyway”**.

Verify the SHA-256 before running the file. If no VirusTotal report exists for this file yet, you can submit it yourself; a scan result is an additional signal, not a security guarantee.

There is no need to uninstall the previous version. Installing over it keeps your recordings, transcripts, settings, and downloaded models.

### Read-aloud (“Listen”)

The installer stays compact: the speech engine is not included in this public build. The “Listen” button is present in the interface as an announcement and notifies you that the engine is missing. The engine itself will be available as a separate download later. Core dictation and meeting transcription work completely without it.

### Known beta limitations

- The installer is not digitally signed yet, so Windows may show “Unknown publisher”.
- Meeting encryption is user-controlled. Working files may temporarily remain unencrypted on disk while recording; artifacts are protected with AES-256-GCM after recording ends.
- A meeting in the trash bin is gone for good after 7 days. Restore it before then if you need it.
- Some English UI wording will be polished in later releases.

### Reporting issues

If you encounter a bug or have a suggestion:
- In the app: “About” → “Report a problem”;
- Or open an issue on [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues).

---

Thank you for testing and using Balachky! If you find the app helpful, you can support development via **Settings → Support the author**.
