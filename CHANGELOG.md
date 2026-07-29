# Changelog

All notable changes to **Балачки у Коростені / Balachky** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed

- **Назва бренду за мовою.** Український інтерфейс зберігає назву **Балачки у Коростені**, англійський використовує коротку назву **Balachky**.
- **Language-specific brand name.** The Ukrainian interface keeps **Балачки у Коростені**; the English interface now uses **Balachky**.

---

## [1.2.3-beta] - 2026-07-25

**Інсталятор / Installer:** `BalachkySetup-1.2.3-beta-F19111EF.exe` (158,9 МБ / 158.9 MB; 166 573 976 байтів / bytes)
**SHA-256:** `F19111EFC61FBA327148E1AC29AFB339E838556663FF73300290DCC6B5D7082F`

### Added

**Українською:**

- **ШІ-протокол наради з коробки.** Створення підсумку, ключових рішень та переліку завдань із таймкодами безпосередньо з програми. Необхідна мовна модель завантажується за вибором користувача.
- **Центр “Моделі” в Налаштуваннях.** Усі моделі (розпізнавання мовлення, розрізнення голосів, ШІ-протокол) зібрано в одному місці із зазначенням стану й обсягу дискової памʼяті.
- **Вибір тла робочої зони.** Можливість обрати візуальне оформлення головного вікна: фірмовий маскот, суцільний колір або власне зображення.
- **Оновлений майстер першого запуску.** Зручне покрокове налаштування, яке перевіряє вже завантажені компоненти й допомагає підготувати програму до роботи.
- **Підтримка автора.** Окреме меню в Налаштуваннях із реквізитами (банківські картки, криптовалюта) та кнопками копіювання в один клік.
- **Зручне керування готовою нарадою.** Упорядковані кнопки дій, меню “Експортувати в …” та зрозумілі підтвердження (“Так/Ні”).
- **Компактні меню дій.** В Історії та Аудіофайлах довгий перелік кнопок замінено на зручні меню “…”, а небезпечні дії виділено червоним кольором.

**In English:**

- **Out-of-the-box AI meeting minutes.** Generate summaries, key decisions, and timestamped action items directly inside the app. The required model is downloaded at the user's choice.
- **“Models” center in Settings.** All models (speech recognition, voice separation, AI meeting minutes) gathered in one place with status and disk size details.
- **Custom workspace background.** Option to customize the main window background: signature mascot, solid color, or a custom image.
- **Updated first-run wizard.** Convenient step-by-step setup that detects pre-downloaded components and helps prepare the app for work.
- **Support the author section.** A dedicated menu in Settings with support options (bank cards, cryptocurrency) and one-click copy buttons.
- **Streamlined completed meeting actions.** Organized action buttons, an “Export to …” menu, and plain-language confirmation dialogs (“Yes/No”).
- **Compact action menus.** Replaced crowded button bars in History and Audio Files with clean “…” menus, with destructive actions clearly highlighted in red.

### Changed

**Українською:**

- **Звуки запису прибрано.** Звукові сигнали початку та завершення запису вимкнено для беззвучної роботи.
- **Оновлені умови ліцензії.** Ліцензію змінено з GPLv3 на PolyForm Noncommercial 1.0.0 (використання безкоштовне для всіх людей у некомерційних цілях; відкритий вихідний код).
- **Компактний інсталятор (165 МБ).** Рушій озвучення більше не входить у базовий комплект встановлення. Кнопка “Прослухати” залишається в інтерфейсі як анонс і чесно повідомляє про відсутність рушія; сам рушій буде доступний для окремого завантаження згодом.

**In English:**

- **Silent recording.** Start and stop sound signals have been removed for quiet operation.
- **Updated license terms.** License changed from GPLv3 to PolyForm Noncommercial 1.0.0 (free for non-commercial use by individuals, source code open for inspection).
- **Compact installer (165 MB).** The read-aloud engine is no longer included in the base installation package. The “Listen” button remains in the interface as an announcement and honestly reports that the engine is missing; the engine itself will be available as a separate download later.

### Fixed

**Українською:**

- **Збереження назви наради.** Виправлено кнопку перейменування наради (у попередніх складаннях вона зберігала стару назву).
- **Зрозумілі повідомлення про помилки.** Додано видимі сповіщення у разі скасування чи помилок обробки аудіо.
- **Українські одиниці вимірювання.** Показ розміру моделей і файлів переведено на український стандарт (“18,1 ГБ” замість “18.1 GB”).
- **Покращене читання таблиць.** Прибрано розрідження літер у заголовках таблиць.

**In English:**

- **Meeting title saving.** Fixed the meeting rename button, which retained the previous title in earlier builds.
- **Clear error messaging.** Added visible status messages when processing is canceled or encounters an error.
- **Localized measurement units.** Converted model and file size labels to Ukrainian standard formatting in Ukrainian localization (“18,1 ГБ”).
- **Improved table readability.** Removed wide letter spacing in table headers for better legibility.

### Component sizes / Таблиця розмірів

**Українською:**

| Компонент | Варіант | Розмір |
| --- | --- | --- |
| Інсталятор | публічна збірка | 165 МБ |
| Розпізнавання мовлення | Найлегша (small) | ~0,5 ГБ |
| | Середня (medium) | ~1,5 ГБ |
| | Швидка (large-v3-turbo, за умовчанням) | ~1,6 ГБ |
| | Найточніша (large-v3) | ~3 ГБ |
| Протокол наради (ШІ) | Швидка (Gemma 4 E4B) | ~4,98 ГБ |
| | Якісніша (Gemma 4 12B) | ~6,72 ГБ |
| Розрізнення голосів | sherpa-onnx | ~34,3 МБ |
| Рушій озвучення | немає в цій збірці | окреме завантаження згодом |

> [!NOTE]
> **Зверніть увагу:** створення ШІ-протоколу наради на комп'ютері без окремої відеокарти виконується процесором і може тривати десятки хвилин (залежно від процесора й довжини наради). З відеокартою NVIDIA обробка відбувається значно швидше.

**In English:**

| Component | Variant | Size |
| --- | --- | --- |
| Installer | public build | 165 MB |
| Speech recognition | Lightest (small) | ~0.5 GB |
| | Medium (medium) | ~1.5 GB |
| | Fast (large-v3-turbo, default) | ~1.6 GB |
| | Most accurate (large-v3) | ~3 GB |
| Meeting protocol (AI) | Fast (Gemma 4 E4B) | ~4.98 GB |
| | Higher quality (Gemma 4 12B) | ~6.72 GB |
| Voice separation | sherpa-onnx | ~34.3 MB |
| Read-aloud engine | not in this build | separate download later |

> [!NOTE]
> **Please note:** generating AI meeting minutes on a computer without a dedicated graphics card runs on the CPU and may take dozens of minutes (depending on CPU performance and meeting length). With an NVIDIA graphics card, processing is significantly faster.

---

## 1.2.2 - 2026-07-20

Внутрішня бета-збірка; публічного релізу цієї версії не було. / Internal beta build; this version was never publicly released.

### Added

**Українською:**

- **Автовивантаження моделі з памʼяті відеокарти.** Коли диктування довго не використовується, програма сама звільняє модель із VRAM; наступний запис підвантажує її назад. Менше зайнятої відеопамʼяті у простої.
- **Черга диктувань.** Кілька відрізків мовлення підряд шикуються в чергу й опрацьовуються по порядку, нічого не губиться.
- **Аудіоплеєр із хвилею.** Прослуховування запису прямо з картки розшифровки з візуалізацією звукової хвилі.

**In English:**

- **Model unload from GPU memory.** When dictation is idle for a while, the app releases the model from VRAM by itself; the next recording loads it back. Less GPU memory held while idle.
- **Dictation queue.** Several speech segments in a row line up in a queue and are processed in order — nothing is dropped.
- **Audio player with waveform.** Play back a recording straight from the transcription card, with a sound-wave visualization.

### Changed

**Українською:**

- **Стійкіший конвеєр «Наради».** Розділено фази запису й обробки — запис не залежить від опрацювання, менше збоїв на довгих сесіях.
- **Оновлений візуальний дизайн** (ревізія інтерфейсу) — узгоджена сітка, картки, заставка.
- **Тексти інтерфейсу простою мовою** — жаргон замінено на людські формулювання (українська локалізація).
- **Чесний підпис точності й памʼяті.** Рядок точності/VRAM більше не обіцяє зайвого: підпис int8 та завантаження моделі відображають реальний стан.

**In English:**

- **More robust «Meeting» pipeline.** Recording and processing phases are separated — recording no longer depends on processing, fewer failures on long sessions.
- **Refreshed visual design** (UI revision) — consistent grid, cards, splash screen.
- **Plain-language interface texts** — jargon replaced with human wording (Ukrainian localization).
- **Honest accuracy and memory labels.** The accuracy/VRAM line no longer overpromises: the int8 label and model loading reflect the real state.

### Fixed

**Українською:**

- **Виправлено рідкісний збій заставки на повільному старті.** На холодному запуску з великою моделлю (коли завантаження триває довше за появу лінії прогресу) застосунок міг упасти на переході від заставки до головного вікна (`QVariantAnimation already deleted`). Анімацію лінії прогресу тепер коректно звільнено.

**In English:**

- **Fixed a rare splash-screen crash on slow startup.** On a cold start with a large model (when loading outlasts the progress-line appearance) the app could crash on the transition from splash to the main window (`QVariantAnimation already deleted`). The progress-line animation reference is now released correctly.

---

## 1.0.0 - 2026-07-12

Внутрішня бета-збірка; публічного релізу цієї версії не було. Перелічені нижче можливості вперше стали публічно доступні у складі [1.2.3-beta]. / Internal beta build; this version was never publicly released. The features listed below first became publicly available as part of [1.2.3-beta].

### Added

**Українською:**

- **Диктування голосом.** Затиснув клавішу (типово F23), сказав, відпустив — текст з'являється там, де ти друкуєш: у листі, чаті, документі. Підтримка комбінацій клавіш (наприклад `ctrl+shift+x`).
- **Розшифровка аудіофайлів.** Перетягни голосове з Telegram, запис із диктофона чи наради — отримай текст. Черга файлів, спільний рушій із диктуванням.
- **Експорт результатів.** Збереження як звичайний текст, субтитри `.srt` / `.vtt` (за нормами субтитрування BBC) або документ Word `.docx`.
- **Словник складних слів із самонавчанням.** Програма запам'ятовує складні слова й імена (зокрема англійські назви всередині української мови). Кілька наборів слів із перемиканням; програма сама помічає слова, які ти часто повторюєш, і пропонує додати їх. Виправлення слів прямо з картки розшифровки; підсвічування невпевнених слів.
- **Профілі пам'яті.** Кілька людей чи контекстів на одному комп'ютері — у кожного свій словник та історія.
- **Історія розшифровок** з окремою сторінкою та швидким копіюванням.
- **Значок у треї** зі станом (мікрофон: сірий — очікування, червоний — запис, золотий — обробка), швидкі дії та підменю «Останні розшифровки».
- **Вибір мікрофона** (список пристроїв WASAPI) та жива смужка рівня сигналу під час запису.
- **Тиха перевірка оновлень** через GitHub Releases (без телеметрії, з можливістю вимкнути; програма ніколи не завантажує оновлення сама, лише відкриває сторінку).
- **Майстер першого запуску** з завантаженням файлу розпізнавання голосу (~3 ГБ), після чого все працює офлайн.
- **Двомовний інтерфейс** — українська та англійська.
- **Прискорення на відеокарті NVIDIA.** Опційна докачка CUDA-рушія — розпізнавання швидше на сумісних відеокартах; без GPU все працює як раніше, на процесорі.
- **Режим «Нарада».** Записує мікрофон і системний звук одночасно та веде стенограму розмови.
- **Каскадна вставка тексту.** Автовставка пробує кілька способів по черзі, а якщо буфер обміну недоступний — набирає текст посимвольно.
- **Профілі під застосунок.** Свої налаштування (мова, словник) вмикаються автоматично залежно від того, у якому вікні диктуєш.
- **Перевірка мікрофона.** Тестовий запис і налаштування чутливості визначення голосу (VAD) прямо в налаштуваннях.
- **Тека спостереження.** Вказана тека сама розшифровує нові аудіофайли, щойно вони туди потрапляють.
- **Статистика диктування.** Скільки наговорено слів і часу — за день, тиждень, усього.
- **Керування словниками.** Створення, перейменування, видалення наборів слів і масовий імпорт списком.
- **Повернення буфера обміну.** Після автовставки в буфері знову те, що там було до цього.
- **Голосова пунктуація.** Слова-команди («крапка», «кома»…) перетворюються на розділові знаки — за бажанням, вимкнено за замовчуванням.
- **Скляний фон Mica.** На Windows 11 вікна можуть мати напівпрозорий фон системи.
- **Видалення моделей.** Звільнити місце на диску, прибравши завантажену модель розпізнавання просто з налаштувань.
- **Автоекспорт розшифровок.** Кожна розшифровка сама зберігається у вибраному форматі та теці, без ручного експорту.
- **Сніпети.** Короткі голосові команди-скорочення, які підставляють заготовлений текст.
- **Чистка слів-паразитів.** Розпізнавання прибирає вокалізації-хезитації («ем», «гм») і миттєві повтори слів; змістовні слова («типу», «ну») лишає без змін.
- **Бічна кнопка миші.** Диктування можна запускати кнопкою на боці миші, не лише клавішею.
- **Повністю офлайн.** Голос і текст нікуди не відправляються.

**In English:**

- **Voice dictation.** Hold a key (F23 by default), speak, release — the text appears wherever you type: email, chat, document. Key-combo hotkeys supported (e.g. `ctrl+shift+x`).
- **Audio file transcription.** Drop a Telegram voice message, dictaphone or meeting recording and get text back. File queue, shared engine with dictation.
- **Export.** Save as plain text, `.srt` / `.vtt` subtitles (BBC subtitle norms), or a Word `.docx` document.
- **Self-learning vocabulary.** The app remembers hard words and names (including English terms inside Ukrainian text). Multiple switchable word sets; it notices words you repeat often and offers to add them. Fix words straight from the transcription card; uncertain-word highlighting.
- **Memory profiles.** Several people or contexts on one computer — each with its own dictionary and history.
- **Transcription history** with a dedicated page and one-click copy.
- **Tray icon** with live state (microphone: grey idle / red recording / gold busy), quick actions and a "Recent transcriptions" submenu.
- **Microphone selection** (WASAPI device list) and a live level meter during recording.
- **Silent update check** via GitHub Releases (no telemetry, opt-out; the app never self-downloads, it only opens the download page).
- **First-run onboarding wizard** that downloads the speech-recognition model (~3 GB), after which everything runs offline.
- **Bilingual UI** — Ukrainian and English.
- **NVIDIA GPU acceleration.** Optional CUDA engine download for faster recognition on compatible graphics cards; everything still runs on CPU without one.
- **"Meeting" mode.** Records the microphone and system audio together and keeps a transcript of the conversation.
- **Cascading paste.** Auto-paste tries a few methods in turn, falling back to typing the text character by character if the clipboard isn't available.
- **Per-app profiles.** Settings (language, vocabulary) switch automatically depending on which window you're dictating into.
- **Microphone test.** A test recording and voice-detection (VAD) sensitivity control, right in settings.
- **Watch folder.** A chosen folder transcribes new audio files on its own as soon as they land there.
- **Dictation stats.** Words and time dictated — per day, per week, in total.
- **Vocabulary management.** Create, rename, and delete word sets, plus bulk-import a whole list at once.
- **Clipboard restore.** After auto-paste, the clipboard gets back whatever was in it before.
- **Voice punctuation.** Command words ("period", "comma"...) turn into punctuation marks — opt-in, off by default.
- **Mica glass background.** On Windows 11, windows can pick up the system's translucent background.
- **Model deletion.** Free up disk space by removing a downloaded recognition model right from settings.
- **Auto-export transcriptions.** Every transcription saves itself in the chosen format and folder, no manual export needed.
- **Snippets.** Short voice shortcuts that insert a prepared piece of text.
- **Filler-word cleanup.** Recognition removes filler vocalizations ("um", "mhm") and immediate word repeats from the text on its own; meaningful words like "like" are left untouched.
- **Mouse side button.** Dictation can also be started with a side button on the mouse, not just a key.
- **Fully offline.** Voice and text never leave your computer.

### Security & Privacy

- **Приватність за замовчуванням.** Усе відбувається на комп'ютері користувача; голос і текст не йдуть на чужі сервери чи в інтернет. / Privacy by default — everything runs on the user's machine; voice and text never reach external servers or the internet.
- **Перевірка оновлень без телеметрії** та з можливістю повного вимкнення. / Update check carries no telemetry and can be fully disabled.

### License

Безкоштовно для людей, код відкритий для перевірки — ліцензія PolyForm Noncommercial 1.0.0 (`LICENSE`) з додатковими дозволами (`COMMERCIAL-LICENSE.md`). Copyright © 2026 Микола Жуковець. Сторонні компоненти — у `THIRD-PARTY-NOTICES.txt`. /
Free for people, source open for inspection — PolyForm Noncommercial 1.0.0 license (`LICENSE`) with additional permissions (`COMMERCIAL-LICENSE.md`). Copyright © 2026 Mykola Zhukovets. Third-party components are listed in `THIRD-PARTY-NOTICES.txt`.

[Unreleased]: https://github.com/mykola-zhukovets/balachky/compare/v1.2.3-beta...HEAD
[1.2.3-beta]: https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.3-beta
