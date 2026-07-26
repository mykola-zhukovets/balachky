# Примітки до випуску · Release Notes — v1.2.3-beta

**Файл:** `BalachkySetup-1.2.3-beta-F19111EF.exe`<br>
**SHA-256:** `F19111EFC61FBA327148E1AC29AFB339E838556663FF73300290DCC6B5D7082F`<br>
**VirusTotal:** [0/65 виявлень](https://www.virustotal.com/gui/file/f19111efc61fba327148e1ac29afb339e838556663ff73300290dcc6b5d7082f/detection)

---

## Українська версія

Вітаємо у першому публічному бета-випуску програми “Балачки у Коростені” (`v1.2.3-beta`)!

### Про програму

“Балачки” — це персональний помічник для голосового введення тексту та обробки нарад. Голос, розшифровки й протоколи обробляються на Вашому комп'ютері та не завантажуються в хмару. Програма дає змогу надиктовувати текст у будь-які поля та застосунки, записувати й транскрибувати розмови із визначенням співрозмовників, створювати локальні ШІ-протоколи нарад (з коротким підсумком, рішеннями та завданнями), а також записувати екран. Інтернет потрібен лише для моделей, додаткових компонентів і оновлень, які Ви вирішите завантажити, та для перевірки оновлень, якщо Ви її дозволили.

### Як встановити

1. Завантажте інсталятор `BalachkySetup-1.2.3-beta-F19111EF.exe`.
2. Запустіть завантажений файл і дотримуйтесь підказок майстра встановлення.
3. **Зверніть увагу (попередження Windows SmartScreen):** Оскільки це новий бета-випуск і програма поки не має цифрового підпису, Windows може показати застереження під час запуску інсталятора. Це цілком очікувана поведінка для нових бета-програм. Щоб продовжити встановлення, натисніть **“Докладніше”** (More info) → **“Виконати все одно”** (Run anyway).

Перед запуском рекомендуємо звірити SHA-256. Перевірка VirusTotal 0/65 є додатковим сигналом, а не гарантією безпеки.

### Озвучення (“Прослухати”)

Інсталятор програми став компактнішим: рушій озвучення не входить до публічної збірки. Кнопка “Прослухати” присутня в інтерфейсі як анонс і чесно повідомляє про відсутність рушія. Сам рушій буде доступний для окремого завантаження згодом. Основні функції диктування та проведення нарад працюють у повному обсязі без нього.

### Відомі обмеження бети

- Інсталятор ще не має цифрового підпису, тому Windows може показати “Невідомий видавець”.
- Шифрування нарад вмикає користувач. Під час запису робочі файли можуть тимчасово лежати на диску відкритими; після завершення запису артефакти захищаються AES-256-GCM.
- Частину англійських формулювань буде додатково відшліфовано в наступних випусках.

### Як повідомити про проблему

Якщо Ви виявили помилку або маєте пропозицію щодо покращення програми:
- У застосунку: “Про програму” → “Повідомити про проблему”;
- Або створіть тему на сторінці [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues).

---

Дякуємо за використання програми та участь у випробуванні! Якщо “Балачки” стали Вам у пригоді, Ви можете підтримати розвиток проєкту кнопкою-сердечком “Підтримати автора” (банка Monobank, валютні рахунки або криптовалюта — на Ваш вибір).

---

## English version

Welcome to the first public beta release of Balachky (`v1.2.3-beta`)!

### About the application

Balachky is a personal voice typing and meeting assistant. Speech, transcripts, and minutes are processed on your computer and are not uploaded to a cloud service. It lets you dictate into any application, record and transcribe meetings with speaker identification, generate local AI minutes (summaries, decisions, and tasks), and record your screen. Internet access is used only for models, optional components, and updates you choose to download, plus update checks if you enable them.

### Installation instructions

1. Download the installer `BalachkySetup-1.2.3-beta-F19111EF.exe`.
2. Run the downloaded file and follow the installation wizard.
3. **Note on Windows SmartScreen:** Because this is a new public beta release without a digital signature yet, Windows SmartScreen may show a security warning. This is expected for new beta software. To proceed, click **“More info”** → **“Run anyway”**.

Verify the SHA-256 before running the file. The VirusTotal 0/65 result is an additional signal, not a security guarantee.

### Read-aloud (“Listen”)

The installer is now compact: the speech engine is not included in this public build. The “Listen” button is present in the interface as an announcement and honestly notifies that the engine is missing. The engine itself will be available as a separate download later. Core dictation and meeting transcription work completely without it.

### Known beta limitations

- The installer is not digitally signed yet, so Windows may show “Unknown publisher”.
- Meeting encryption is user-controlled. Working files may temporarily remain unencrypted on disk while recording; artifacts are protected with AES-256-GCM after recording ends.
- Some English UI wording will be polished in later releases.

### Reporting issues

If you encounter a bug or have a suggestion:
- In the app: “About” → “Report a problem”;
- Or open an issue on [GitHub Issues](https://github.com/mykola-zhukovets/balachky/issues).

---

Thank you for testing and using Balachky! If you find the app helpful, you can support development via **Settings → Support the author**.
