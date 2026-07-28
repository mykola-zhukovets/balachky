# Data and privacy · Дані та приватність

This document describes `v1.2.3-beta`. Network behaviour may differ in later releases.

Цей документ описує `v1.2.3-beta`. Мережева поведінка наступних випусків може відрізнятися.

[English](#english) · [Українська](#українська)

## English

### The short version

- Recording, speech recognition, transcription, dictionaries, history, and meeting processing run on your computer.
- Balachky does not send your voice, audio files, recordings, transcripts, dictionaries, or AI minutes drafts to a server for processing.
- The app does not require an account and does not include advertising or analytics telemetry.
- Internet access is used for a connection check when the setup wizard opens, downloads you choose, and update checks or downloads you enable.

Once the models and optional components you need are available, the main work modes can run without an internet connection.

### Where Balachky stores its files

The program is normally installed here:

```text
%LOCALAPPDATA%\Programs\Balachky
```

Your settings and working data are stored separately:

```text
%LOCALAPPDATA%\Balachky
```

This folder may contain:

- `config.toml` — settings;
- `context_profiles.toml` — app profiles;
- `profiles\` — dictionaries, learning data, and dictation history;
- `templates\` — voice-fill templates;
- `meetings\` — meeting recordings and transcripts;
- `recordings\` — recordings made with the recorder;
- `screen\` — screen recordings;
- `corpus\` — local audio-and-text examples used for recognition learning;
- `components\` — optional punctuation, autocorrection, and AI minutes components;
- `diarization\` — optional speaker-separation models;
- `updates\` — downloaded update installers;
- `tts-engine\` and `tts-voices\` — optional read-aloud files, if installed;
- `network_log.jsonl` — the network actions recorded by Balachky.

Meeting, recorder, and screen-recording folders can be changed in Settings. Files in a custom folder are not moved back automatically and may remain there after uninstalling the app.

### Where recognition models are stored

Recognition models selected during setup or later in Settings are normally stored here:

```text
%LOCALAPPDATA%\Balachky\models
```

Balachky can also use compatible models from the Hugging Face cache:

```text
%USERPROFILE%\.cache\huggingface\hub
```

If you selected a custom model folder, the files remain in that folder.

### When `v1.2.3-beta` uses the internet

| Action | What happens |
|---|---|
| Setup wizard | When the setup wizard opens, the app makes a TCP connection to `1.1.1.1:53` to check whether a network connection is available. It does not send voice or transcripts. |
| Recognition models | Files are downloaded when you choose a model during setup or later in Settings. |
| Optional components | Files are downloaded when you choose features such as punctuation, autocorrection, speaker separation, local AI minutes, or supported read-aloud components. |
| Update check | A manual check contacts the GitHub Releases API. Automatic weekly checks occur only if you enable them. |
| Update download | The installer is downloaded when you accept an update, or in the background only if you separately enable background downloads. The app checks the published SHA-256 before offering to run it. |
| Help, support, or report links | A page opens in your browser only after you select the link. Balachky does not post a report or upload a log automatically. |

The services contacted for downloads can see the usual connection details, including your IP address. Update requests also identify the current Balachky version in the request header. Voice, recordings, transcripts, history, and dictionaries are not included in those requests.

### About the network log

The in-app network log records network actions that Balachky explicitly marks, including the server and action type. It does not contain the contents of your voice or text.

This log is not a complete operating-system traffic record. In particular, do not treat an empty Balachky log as proof that no connection occurred. Use Windows Resource Monitor or a tool such as Wireshark if you need an independent record.

### Removing your data

1. Uninstall Balachky from Windows Settings.
2. Delete the data folder if you no longer need its contents:

   ```text
   %LOCALAPPDATA%\Balachky
   ```

3. Delete any compatible models you no longer need from:

   ```text
   %USERPROFILE%\.cache\huggingface\hub
   ```

4. Check any custom folders you selected for models, meetings, recordings, or screen recordings.
5. If needed, remove the small settings key:

   ```text
   HKEY_CURRENT_USER\Software\Balachky
   ```

Deleting these folders also deletes the dictionaries, history, recordings, transcripts, and models stored inside them. Check their contents first.

---

## Українська

### Коротко

- Запис, розпізнавання голосу, розшифровка, словники, історія й обробка нарад відбуваються на Вашому комп’ютері.
- Балачки не надсилають Ваш голос, аудіофайли, записи, розшифровки, словники чи чернетки ШІ-протоколів на сервер для обробки.
- Програма не потребує облікового запису й не містить рекламної або аналітичної телеметрії.
- Інтернет використовується для перевірки з’єднання, коли відкривається майстер налаштування, обраних Вами завантажень і перевірок чи завантажень оновлень, які Ви дозволили.

Коли потрібні моделі й додаткові компоненти вже є, основні режими можуть працювати без інтернету.

### Де Балачки зберігають файли

Програма зазвичай встановлена тут:

```text
%LOCALAPPDATA%\Programs\Balachky
```

Налаштування й робочі дані зберігаються окремо:

```text
%LOCALAPPDATA%\Balachky
```

У цій папці можуть бути:

- `config.toml` — налаштування;
- `context_profiles.toml` — профілі програм;
- `profiles\` — словники, дані навчання й історія диктувань;
- `templates\` — шаблони для заповнення голосом;
- `meetings\` — записи й розшифровки нарад;
- `recordings\` — записи диктофона;
- `screen\` — записи екрана;
- `corpus\` — локальні пари аудіо й тексту для навчання розпізнавання;
- `components\` — додаткові компоненти пунктуації, автокорекції та ШІ-протоколу;
- `diarization\` — додаткові моделі розрізнення голосів;
- `updates\` — завантажені інсталятори оновлень;
- `tts-engine\` і `tts-voices\` — додаткові файли озвучення, якщо їх установлено;
- `network_log.jsonl` — мережеві дії, які записали Балачки.

Папки нарад, диктофона й записів екрана можна змінити в налаштуваннях. Файли у власній папці не переносяться назад автоматично й можуть залишитися там після видалення програми.

### Де лежать моделі розпізнавання

Моделі розпізнавання, обрані під час налаштування або пізніше в Налаштуваннях, зазвичай зберігаються тут:

```text
%LOCALAPPDATA%\Balachky\models
```

Балачки також можуть використовувати сумісні моделі з кешу Hugging Face:

```text
%USERPROFILE%\.cache\huggingface\hub
```

Якщо Ви обрали власну папку моделей, файли залишаються в ній.

### Коли `v1.2.3-beta` використовує інтернет

| Дія | Що відбувається |
|---|---|
| Майстер налаштування | Коли відкривається майстер налаштування, програма створює TCP-з’єднання з `1.1.1.1:53`, щоб перевірити доступність мережі. Голос і розшифровки не надсилаються. |
| Моделі розпізнавання | Файли завантажуються, коли Ви обираєте модель під час налаштування або пізніше. |
| Додаткові компоненти | Файли завантажуються, коли Ви обираєте пунктуацію, автокорекцію, розрізнення голосів, локальний ШІ-протокол або підтримувані компоненти озвучення. |
| Перевірка оновлень | Ручна перевірка звертається до GitHub Releases. Щотижнева автоматична перевірка працює лише після того, як Ви її ввімкнете. |
| Завантаження оновлення | Інсталятор завантажується після Вашої згоди або у фоні, лише якщо Ви окремо дозволили фонове завантаження. Перед запуском програма звіряє опублікований SHA-256. |
| Посилання на довідку, підтримку чи звіт | Сторінка відкривається у браузері тільки після Вашого натискання. Балачки не публікують звіт і не завантажують журнал автоматично. |

Сервіси завантаження бачать звичайні технічні дані з’єднання, зокрема IP-адресу. Запит оновлень також містить поточну версію Балачок у технічному заголовку. Голос, записи, розшифровки, історія й словники до цих запитів не додаються.

### Про журнал мережі

Журнал у програмі записує мережеві дії, які Балачки явно позначають, зокрема адресу сервера й тип дії. Вміст Вашого голосу чи тексту туди не потрапляє.

Це не повний системний запис трафіку. Зокрема, порожній журнал Балачок сам по собі не доводить, що з’єднань не було. Для незалежної перевірки використовуйте “Монітор ресурсів” Windows або програму на кшталт Wireshark.

### Як видалити свої дані

1. Видаліть Балачки через Параметри Windows.
2. Якщо дані більше не потрібні, видаліть папку:

   ```text
   %LOCALAPPDATA%\Balachky
   ```

3. Видаліть непотрібні сумісні моделі з:

   ```text
   %USERPROFILE%\.cache\huggingface\hub
   ```

4. Перевірте власні папки, які Ви обрали для моделей, нарад, диктофона чи записів екрана.
5. За потреби видаліть невеликий ключ налаштувань:

   ```text
   HKEY_CURRENT_USER\Software\Balachky
   ```

Разом із цими папками зникнуть словники, історія, записи, розшифровки й моделі, що лежать усередині. Спершу перевірте їхній вміст.
