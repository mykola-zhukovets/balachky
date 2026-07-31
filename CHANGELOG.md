# Changelog

All notable changes to **Балачки у Коростені / Balachky** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [1.2.4.1-beta] - 2026-07-31

**Інсталятор / Installer:** `BalachkySetup-1.2.4.1-beta-3C5F6144.exe` (161,8 МБ / 161.8 MB; 169 611 393 байтів / bytes)
**SHA-256:** `3C5F61447C22596CFED5B5ACD8C32D88A4B91BE1768B0DEFE340233437186145`

> Виправлення до 1.2.4-beta. Той випуск відкликано: у ньому не працювало відтворення відео всередині програми.
> A hotfix for 1.2.4-beta. That release was withdrawn: video playback inside the app did not work there.

### Fixed

**Українською:**

- **Відео знову грається у програмі.** У зібраній версії програвач не знаходив свої медіабібліотеки і показував помилку, хоча самі записи були цілі. Тепер записи екрана і нарад відтворюються як належить.
- **Чесні повідомлення програвача.** Замість “файл переміщено або пошкоджено” програма розрізняє два випадки: файл на місці, але не вдалося відтворити — і файлу справді немає.
- **Майстер першого запуску: вибір компонентів можна підтвердити.** Раніше на кроці додаткових можливостей була лише кнопка “Пропустити”, яка мовчки скидала позначені компоненти. З'явилася кнопка, що починає завантаження обраного.
- **Майстер бачить уже завантажені компоненти.** Розпізнавання співрозмовників більше не пропонується до завантаження, якщо воно вже є на комп'ютері.
- **Чесний лічильник кроків.** Майстер більше не обіцяє крок, якого не буде.
- **Підписи кнопок не обрізаються.** Рядки дій над записом екрана і над готовим аудіофайлом переносяться на новий рядок замість того, щоб різати текст. Додано автоматичну перевірку, яка ловить такі місця на різних ширинах вікна.

**In English:**

- **Video plays inside the app again.** In the packaged build the player could not find its media libraries and showed an error, even though the recordings themselves were intact. Screen and meeting recordings now play as they should.
- **Honest player messages.** Instead of “the file was moved or damaged”, the app now separates two cases: the file is there but playback failed, and the file is genuinely missing.
- **First-run wizard: your component choice can be confirmed.** The optional-components step only had a “Skip” button, which silently cleared the boxes you ticked. There is now a button that starts downloading what you selected.
- **The wizard sees components you already have.** Speaker recognition is no longer offered for download when it is already on the computer.
- **Honest step counter.** The wizard no longer promises a step that will not appear.
- **Button labels are no longer clipped.** Action rows above a screen recording and above a finished audio file wrap to a new line instead of cutting the text. An automatic check now catches such places at different window widths.

---
## [1.2.4-beta] - 2026-07-31 — ВІДКЛИКАНО / WITHDRAWN

> Цей випуск відкликано: у зібраній програмі не працювало відтворення відео. Використовуйте 1.2.4.1-beta.
> This release was withdrawn: video playback did not work in the packaged build. Use 1.2.4.1-beta instead.

**Інсталятор / Installer:** `BalachkySetup-1.2.4-beta-7307EA13.exe` (161,7 МБ / 161.7 MB; 169 593 245 байтів / bytes)
**SHA-256:** `7307EA13B0CAE2BCF7EE59417EE8A820F634299303242E99A60BE5F7FAC586F4`

### Added

**Українською:**

- **Закладки просто під час наради.** Кнопка на сторінці наради або комбінація `Ctrl+Alt+B` з будь-якого місця миттєво позначає важливий момент — без вікон і без паузи в записі. У перегляді запису позначки показані списком, натискання перемотує на потрібне місце.
- **Зведення доріжок в окремий файл.** Виставте повзунками свій баланс (мікрофон гучніше, системний звук тихіше, зайві доріжки вимкнено) і збережіть окремий файл WAV саме з цим звучанням. Оригінальні доріжки лишаються незмінними.
- **Пошук у тексті наради.** `Ctrl+F` у вікні перегляду відкриває рядок пошуку над розшифровкою: збіги підсвічуються, поруч лічильник на кшталт “3 з 17”, `Enter` веде до наступного збігу з перемотуванням запису.
- **Кошик для нарад.** Видалена нарада спершу лежить у кошику 7 днів, а в повідомленні внизу екрана одразу є кнопка “Повернути”. Текст підтвердження чесно пояснює, що станеться з файлами далі.
- **Зрозумілі порожні сторінки.** Сторінка без вмісту тепер описує своє призначення й пропонує першу дію: почати запис, вибрати файли, увімкнути історію. Пошук окремо розрізняє “ще немає в чому шукати” і “за запитом нічого не знайдено”.
- **Завантаження моделі більше не блокує програму.** Вікно завантаження можна закрити — процес триває у фоні, а поступ видно карткою в бічній панелі. Якщо закрити програму посеред завантаження, наступний запуск продовжить з місця зупинки й перевірить цілісність файлів.
- **Точні цифри у вікні завантаження.** Видно завантажений і загальний обсяг, відсотки, поточну швидкість та розрахований час до кінця. Якщо сервер ще не повідомив розміру, показані лише фактично виміряні дані без вигаданих відсотків.
- **Слово підсвічується під час відтворення.** У розшифровці видно, яке саме слово звучить зараз. Рухоме підсвічування можна вимкнути, якщо воно заважає читати репліку цілком.
- **Текст наради поруч із відеозаписом.** Праворуч від відео йдуть репліки з іменами співрозмовників; натискання на репліку перемотує відео на цей момент. Панель можна згорнути або змінити її ширину.
- **Дії над записами екрана — просто на сторінці.** Над кожним відеозаписом є панель: перейменувати, показати в папці, зберегти як, скопіювати шлях, переглянути, вилучити. Кнопки видно постійно, без наведення миші.
- **Перегляд запису на весь екран.** Вікно перегляду відкривається на 85 % екрана, а подвійне натискання або клавіші `F` чи `F11` розгортають його повністю. `Esc` виходить з повного екрана, не закриваючи сам перегляд.
- **Усе про програму — на одній вкладці.** Призначення, версія, збірка, довідка, ліцензія та підтримка автора зібрані на вкладці “Про програму”. Журнал змін тепер вбудований у програму.
- **Налаштування впорядковано за групами.** Сім груп із чіткими відступами, помітна різниця між заголовком, назвою параметра й поясненням у всіх восьми темах оформлення. Довгі описи скорочено до одного речення, подробиці перенесено в підказки, а перемикачі знову показують типовий стан.
- **Головні кнопки названо за результатом.** “Обробити нараду” стало “Отримати текст наради”; Кнопка “Налаштування наради” отримала вигляд повноцінної; шість окремих параметрів запису екрана згорнуто в один компактний блок.
- **Інсталятор пояснює ліцензію і чесно питає про видалення.** Перед текстом ліцензії з’явилося коротке пояснення українською: некомерційне використання безкоштовне, зокрема для ЗСУ. Майстер видалення питає про налаштування й кеш і прямо каже, що Ваші записи, розшифровки та завантажені моделі лишаються на пристрої.
- **Складання дистрибутива не проковтує помилок.** Скрипт збірки перевіряє кожен крок окремо, вважає помилкою пропущений аудит готового пакета й вимагає, щоб підсумковий файл програми був свіжішим за час запуску збірки.
- **Тести, здатні впасти.** Додано перевірку проти тестів, які порівнюють текст сам із собою і тому мовчать навіть тоді, коли з програми зникає живий рядок.

**In English:**

- **Bookmark a moment while the meeting runs.** A button on the meeting page, or `Ctrl+Alt+B` from anywhere, marks the moment instantly — no dialog, no pause in the recording. Marks appear as a list in playback, and clicking one jumps straight to it.
- **Mix your track levels into one file.** Set the balance with the sliders — mic louder, system audio quieter, unused tracks off — and save a separate WAV that sounds exactly like that. The original tracks are left untouched.
- **Search inside a meeting transcript.** `Ctrl+F` in the playback window opens a search bar above the transcript. Matches are highlighted, a counter shows “3 of 17”, and `Enter` moves to the next match and scrubs the recording to it.
- **Deleted meetings go to a trash bin.** A deleted meeting sits in the bin for 7 days, and the notification at the bottom of the screen offers “Restore” right away. The delete confirmation explains plainly what happens to the files.
- **Empty pages that tell you what to do.** A page with nothing on it now explains what belongs there and offers the first step: start recording, pick files, turn on history. Search distinguishes “nothing to search yet” from “no results for this query”.
- **Model downloads no longer freeze the app.** Close the download window and the transfer keeps going in the background, with progress shown on a card in the side panel. Quit mid-download and the next launch resumes where it stopped, then verifies the files.
- **Real numbers in the download window.** You see downloaded and total size, percentage, current speed, and estimated time left. Until the server reports a size, only the measured figures are shown — no invented percentages.
- **The spoken word is highlighted as you listen.** The transcript follows the audio word by word. The moving highlight can be switched off if it gets in the way of reading a full remark.
- **Transcript alongside the video.** Remarks with speaker names run down the right side of the video, and clicking a remark jumps the video to that point. The panel can be collapsed or resized.
- **Screen recording actions right on the page.** Each recording has its own row of actions: rename, show in folder, save as, copy path, play, delete. The buttons stay visible instead of appearing on hover.
- **Full-screen playback.** The playback window opens at 85 % of the screen, and a double-click or `F` / `F11` expands it fully. `Esc` leaves full screen without closing playback.
- **One place for everything about the app.** Purpose, version, build, help, license, and support-the-author now live on a single “About” tab. The changelog ships inside the app.
- **Settings grouped and easier to scan.** Seven clearly spaced groups, with a visible difference between group heading, setting name, and explanation across all eight themes. Long descriptions are down to one sentence, the rest moved into tooltips, and toggles show their default state again.
- **Primary buttons named after the result.** “Process meeting” is now “Get the meeting text”, “Meeting settings” looks like a proper button, and six separate screen-recording options collapse into one compact block.
- **The installer explains the license and asks honestly about removal.** A short plain-language note precedes the license text: non-commercial use is free, including for the Armed Forces of Ukraine. The uninstaller asks about settings and cache, and states clearly that your recordings, transcripts, and downloaded models stay on the device.
- **The build script no longer swallows failures.** It checks the exit status of every step separately, treats a skipped audit of the finished package as a build failure, and requires the resulting program file to be newer than the moment the build started.
- **Tests that can actually fail.** Added a guard against tests that compare a string with itself and therefore stay green even when a live line disappears from the app.

### Fixed

**Українською:**

- **Майстер першого запуску більше не пропускає кроки.** Усунуто перехід з третього кроку одразу на сьомий. Крок озвучення показується завжди, з чесним поясненням, якщо в цій збірці рушія немає, а лічильник кроків обчислюється за фактичним складом збірки. Наприкінці з’явився підсумок: що вже готове, а що можна налаштувати пізніше.
- **Обсяг моделей на диску обчислювався подвійно.** Центр моделей показував 5,8 ГБ замість справжніх 1,6 ГБ, бо враховував і файли, і посилання на них. Розміри перераховано за фактичними байтами.
- **Кнопка завантаження голосу показує стан і результат.** Раніше натискання могло не дати жодної реакції. Тепер кнопка або неактивна з поясненням, або показує хід завантаження; помилки з фонових процесів більше не губляться.
- **Видно, з якого пристрою пише мікрофон.** Перед запуском наради показано назву активного пристрою, а якщо системний пристрій і пристрій зв’язку різні — застереження про можливу відсутність голосів співрозмовників. Банер про тишу зникає, щойно звук з’являється.
- **Повідомлення про помилки переписано людською мовою.** Перероблено 19 системних повідомлень. Тепер вони не починаються словом “Видалено” там, де нічого не втрачено; пояснюють, що аудіозапис цілий, навіть якщо постраждав журнал перевірки; розрізняють три різні причини відсутності тексту й називають доріжку, яка справді не запустилася.
- **Усунуто аварійне завершення під час збереження назви наради.** Програма більше не закривається під час натискання “Зберегти назву” на сторінці наради.
- **Кнопки над записом екрана більше не стрибають.** Панель дій не зміщується й не обрізається під час наведення миші та зміни розміру вікна.
- **Керування з клавіатури і контрастність.** У послідовність переходу клавішею `Tab` додано кнопки діалогу гарячих клавіш, вибір активного вікна й таблицю мережевого журналу. Повзунок плеєра отримав фокус, підтримує керування стрілками та клавішами `Home`/`End` і оголошується читачам екрана, а червоні кнопки вилучення — контраст 4,92:1 замість 3,87:1.
- **Верстка сторінки наради.** Позначки часу переносяться на новий рядок замість того, щоб розтягувати сторінку вбік; прибрано подвійний контур активних перемикачів і перекриття підказок сусідніми кнопками.
- **Мову інтерфейсу вивірено, і тепер вона перевіряється сама.** Прибрано новотвори та жаргон, усі підказки переведено на звертання “Ви”, ключові поняття названо однаково скрізь. У збірку вбудовано автоматичну перевірку текстів за внутрішнім словником, тож раз виправлене слово вже не повертається непоміченим.

**In English:**

- **The first-run wizard no longer skips steps.** It could jump from step three to step seven; that is fixed. The read-aloud step always appears, with an honest note when the build has no engine, and the step counter reflects what is actually in the build. The wizard now ends with a summary of what is ready and what can wait.
- **Model disk usage was counted twice.** The model center reported 5.8 GB instead of the real 1.6 GB because it counted both the files and the links pointing at them. Sizes are now measured in actual bytes.
- **The voice download button shows state and outcome.** It used to do nothing at all in some cases. Now it is either disabled with an explanation or showing download progress, and errors raised in background work are no longer lost.
- **You can see which device the mic is recording from.** The active device name appears before you start a meeting, with a warning when the system device and the communications device differ and other people’s voices may be missing. The silence banner clears as soon as sound arrives.
- **Error messages rewritten in plain language.** Nineteen system messages were reworked. They no longer open with “Deleted” where nothing was lost, they confirm the audio is intact when only the verification log was damaged, they separate the three reasons a transcript can be missing, and they name the track that actually failed to start.
- **Fixed a crash when saving a meeting title.** The app no longer quits when you press “Save title” on the meeting page.
- **Screen recording buttons stop jumping.** The action row no longer shifts or gets clipped on hover or when the window is resized.
- **Keyboard access and contrast.** `Tab` order now includes the shortcut dialog buttons, the active-window picker, and the network log table. The player slider takes focus, responds to arrows and `Home`/`End`, and is announced to screen readers; red delete buttons went from 3.87:1 to 4.92:1 contrast.
- **Meeting page layout.** Timestamp chips wrap to a new line instead of stretching the page sideways, active toggles lost their doubled outline, and tooltips no longer sit under neighboring buttons.
- **Interface wording reviewed, and now checked automatically.** Coined words and jargon are gone, every prompt uses the formal address, and each concept has one name throughout. The build now runs an automatic check of interface text against an internal dictionary, so a word that was fixed once does not quietly come back.

### Security

**Українською:**

- **Запис наради захищено від збоїв диска й відключення пристроїв.** Програма більше не пише звук “у нікуди”: якщо кадри стійко не зберігаються (бракує прав, збій USB, втручання антивірусу), Ви бачите попередження завчасно, а в разі переповнення диска — негайно. Записані частини наради примусово скидаються на диск на межі кожного сегмента, тож раптове знеструмлення не забирає вже записане. Від’єднання мікрофона під час запису супроводжується повідомленням про перехід на системний.
- **Завершення наради переживає збій запису на диск.** Якщо диск відповідає помилкою на останньому кроці, файли однаково закриваються коректно, і запис не лишається в стані “триває” назавжди.
- **Ім’я облікового запису Windows не потрапляє у звіти.** Шляхи виду `C:\Users\…` у службових журналах і в архіві команди “Повідомити про проблему” замінюються нейтральною позначкою. Перед збиранням архіву відкривається вікно з переліком того, що саме в нього увійде.
- **Закрите сховище й службові файли.** Повідомлення про закрите сховище пояснює варіанти доступу й нагадує про код відновлення, якщо пароль втрачено. Тимчасові файли та службові журнали ізольовано й виключено з публікації.

**In English:**

- **Meeting recording protected against disk failures and unplugged devices.** The app no longer writes audio into a void: if frames consistently fail to save (missing permissions, a USB glitch, antivirus interference), you get a warning early — and immediately when the disk is full. Recorded segments are flushed to disk at each segment boundary, so a sudden power loss does not take what was already captured. Unplugging the mic mid-recording now tells you the app switched to the system device.
- **Finishing a meeting survives a failed disk write.** If the disk returns an error on the final step, the files are still closed properly and the recording does not stay stuck in the “in progress” state.
- **Your Windows account name stays out of reports.** Paths like `C:\Users\…` in service logs and in the archive built by “Report a problem” are replaced with a neutral placeholder. Before the archive is assembled, a window lists exactly what will go into it.
- **Locked storage and service files.** The locked-storage message explains the ways back in and points to the recovery code if the password is lost. Temporary files and service logs are isolated and kept out of anything published.
---

## [1.2.3-beta] - 2026-07-25

**Інсталятор / Installer:** `BalachkySetup-1.2.3-beta-F19111EF.exe` (158,9 МБ / 158.9 MB; 166 573 976 байтів / bytes)
**SHA-256:** `F19111EFC61FBA327148E1AC29AFB339E838556663FF73300290DCC6B5D7082F`

### Added

**Українською:**

- **ШІ-протокол наради з коробки.** Створення підсумку, ключових рішень та переліку завдань із таймкодами безпосередньо з програми. Необхідна мовна модель завантажується за вибором користувача.
- **Центр “Моделі” в Налаштуваннях.** Усі моделі (розпізнавання мовлення, розрізнення голосів, ШІ-протокол) зібрано в одному місці із зазначенням стану й обсягу дискової пам’яті.
- **Вибір тла робочої зони.** Можливість обрати візуальне оформлення головного вікна: фірмовий маскот, суцільний колір або власне зображення.
- **Оновлений майстер першого запуску.** Зручне покрокове налаштування, яке перевіряє вже завантажені компоненти й допомагає підготувати програму до роботи.
- **Підтримка автора.** Окреме меню в Налаштуваннях із реквізитами (банківські картки, криптовалюта) та кнопками копіювання в один клік.
- **Зручне керування готовою нарадою.** Упорядковані кнопки дій, меню “Експортувати в …” та зрозумілі підтвердження (“Так/Ні”).
- **Компактні меню дій.** В Історії та Аудіофайлах довгий перелік кнопок замінено на зручні меню “…”, а небезпечні дії виділено червоним кольором.

**In English:**

- **Out-of-the-box AI meeting minutes.** Generate summaries, key decisions, and timestamped action items directly inside the app. The required model is downloaded at the user’s choice.
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
> **Зверніть увагу:** створення ШІ-протоколу наради на комп’ютері без окремої відеокарти виконується процесором і може тривати десятки хвилин (залежно від процесора й довжини наради). З відеокартою NVIDIA обробка відбувається значно швидше.

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
> **Note:** generating AI meeting minutes on a computer without a dedicated graphics card runs on the CPU and may take dozens of minutes (depending on CPU performance and meeting length). With an NVIDIA graphics card, processing is significantly faster.

---

## [1.2.2] - 2026-07-20

### Added

**Українською:**

- **Автовивантаження моделі з пам’яті відеокарти.** Коли диктування довго не використовується, програма сама звільняє модель із VRAM; наступний запис підвантажує її назад. Менше зайнятої відеопам’яті у простої.
- **Черга диктувань.** Кілька відрізків мовлення підряд шикуються в чергу й опрацьовуються по порядку, нічого не губиться.
- **Аудіоплеєр із хвилею.** Прослуховування запису прямо з картки розшифровки з візуалізацією звукової хвилі.

**In English:**

- **Model unload from GPU memory.** When dictation is idle for a while, the app releases the model from VRAM by itself; the next recording loads it back. Less GPU memory held while idle.
- **Dictation queue.** Several speech segments in a row line up in a queue and are processed in order — nothing is dropped.
- **Audio player with waveform.** Play back a recording straight from the transcription card, with a sound-wave visualization.

### Changed

**Українською:**

- **Стійкіший конвеєр “Наради”.** Розділено фази запису й обробки — запис не залежить від опрацювання, менше збоїв на довгих сесіях.
- **Оновлений візуальний дизайн** (ревізія інтерфейсу) — узгоджена сітка, картки, заставка.
- **Тексти інтерфейсу простою мовою** — жаргон замінено на людські формулювання (українська локалізація).
- **Чесний підпис точності й пам’яті.** Рядок точності/VRAM більше не обіцяє зайвого: підпис int8 та завантаження моделі відображають реальний стан.

**In English:**

- **More robust “Meeting” pipeline.** Recording and processing phases are separated — recording no longer depends on processing, fewer failures on long sessions.
- **Refreshed visual design** (UI revision) — consistent grid, cards, splash screen.
- **Plain-language interface texts** — jargon replaced with human wording (Ukrainian localization).
- **Honest accuracy and memory labels.** The accuracy/VRAM line no longer overpromises: the int8 label and model loading reflect the real state.

### Fixed

**Українською:**

- **Виправлено рідкісний збій заставки на повільному старті.** На холодному запуску з великою моделлю (коли завантаження триває довше за появу лінії прогресу) застосунок міг упасти на переході від заставки до головного вікна (`QVariantAnimation already deleted`). Анімацію лінії прогресу тепер коректно звільнено.

**In English:**

- **Fixed a rare splash-screen crash on slow startup.** On a cold start with a large model (when loading outlasts the progress-line appearance) the app could crash on the transition from splash to the main window (`QVariantAnimation already deleted`). The progress-line animation reference is now released correctly.

---

## [1.0.0] - 2026-07-12

Перший офіційний публічний випуск. / First official public release.

### Added

**Українською:**

- **Диктування голосом.** Затиснув клавішу (типово F23), сказав, відпустив — текст з’являється там, де ти друкуєш: у листі, чаті, документі. Підтримка комбінацій клавіш (наприклад `ctrl+shift+x`).
- **Розшифровка аудіофайлів.** Перетягни голосове з Telegram, запис із диктофона чи наради — отримай текст. Черга файлів, спільний рушій із диктуванням.
- **Експорт результатів.** Збереження як звичайний текст, субтитри `.srt` / `.vtt` (за нормами субтитрування BBC) або документ Word `.docx`.
- **Словник складних слів із самонавчанням.** Програма запам’ятовує складні слова й імена (зокрема англійські назви всередині української мови). Кілька наборів слів із перемиканням; програма сама помічає слова, які ти часто повторюєш, і пропонує додати їх. Виправлення слів прямо з картки розшифровки; підсвічування невпевнених слів.
- **Профілі пам’яті.** Кілька людей чи контекстів на одному комп’ютері — у кожного свій словник та історія.
- **Історія розшифровок** з окремою сторінкою та швидким копіюванням.
- **Значок у треї** зі станом (мікрофон: сірий — очікування, червоний — запис, золотий — обробка), швидкі дії та підменю “Останні розшифровки”.
- **Вибір мікрофона** (список пристроїв WASAPI) та жива смужка рівня сигналу під час запису.
- **Тиха перевірка оновлень** через GitHub Releases (без телеметрії, з можливістю вимкнути; програма ніколи не завантажує оновлення сама, лише відкриває сторінку).
- **Майстер першого запуску** з завантаженням файлу розпізнавання голосу (~3 ГБ), після чого все працює офлайн.
- **Двомовний інтерфейс** — українська та англійська.
- **Прискорення на відеокарті NVIDIA.** Опційна докачка CUDA-рушія — розпізнавання швидше на сумісних відеокартах; без GPU все працює як раніше, на процесорі.
- **Режим “Нарада”.** Записує мікрофон і системний звук одночасно та веде стенограму розмови.
- **Каскадна вставка тексту.** Автовставка пробує кілька способів по черзі, а якщо буфер обміну недоступний — набирає текст посимвольно.
- **Профілі під застосунок.** Свої налаштування (мова, словник) вмикаються автоматично залежно від того, у якому вікні диктуєш.
- **Перевірка мікрофона.** Тестовий запис і налаштування чутливості визначення голосу (VAD) прямо в налаштуваннях.
- **Тека спостереження.** Вказана тека сама розшифровує нові аудіофайли, щойно вони туди потрапляють.
- **Статистика диктування.** Скільки наговорено слів і часу — за день, тиждень, усього.
- **Керування словниками.** Створення, перейменування, видалення наборів слів і масовий імпорт списком.
- **Повернення буфера обміну.** Після автовставки в буфері знову те, що там було до цього.
- **Голосова пунктуація.** Слова-команди (“крапка”, “кома”…) перетворюються на розділові знаки — за бажанням, вимкнено за замовчуванням.
- **Скляний фон Mica.** На Windows 11 вікна можуть мати напівпрозорий фон системи.
- **Видалення моделей.** Звільнити місце на диску, прибравши завантажену модель розпізнавання просто з налаштувань.
- **Автоекспорт розшифровок.** Кожна розшифровка сама зберігається у вибраному форматі та теці, без ручного експорту.
- **Сніпети.** Короткі голосові команди-скорочення, які підставляють заготовлений текст.
- **Чистка слів-паразитів.** Розпізнавання прибирає вокалізації-хезитації (“ем”, “гм”) і миттєві повтори слів; змістовні слова (“типу”, “ну”) лишає без змін.
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
- **Cascading paste.** Auto-paste tries a few methods in turn, falling back to typing the text character by character if the clipboard isn’t available.
- **Per-app profiles.** Settings (language, vocabulary) switch automatically depending on which window you’re dictating into.
- **Microphone test.** A test recording and voice-detection (VAD) sensitivity control, right in settings.
- **Watch folder.** A chosen folder transcribes new audio files on its own as soon as they land there.
- **Dictation stats.** Words and time dictated — per day, per week, in total.
- **Vocabulary management.** Create, rename, and delete word sets, plus bulk-import a whole list at once.
- **Clipboard restore.** After auto-paste, the clipboard gets back whatever was in it before.
- **Voice punctuation.** Command words ("period", "comma"...) turn into punctuation marks — opt-in, off by default.
- **Mica glass background.** On Windows 11, windows can pick up the system’s translucent background.
- **Model deletion.** Free up disk space by removing a downloaded recognition model right from settings.
- **Auto-export transcriptions.** Every transcription saves itself in the chosen format and folder, no manual export needed.
- **Snippets.** Short voice shortcuts that insert a prepared piece of text.
- **Filler-word cleanup.** Recognition removes filler vocalizations ("um", "mhm") and immediate word repeats from the text on its own; meaningful words like "like" are left untouched.
- **Mouse side button.** Dictation can also be started with a side button on the mouse, not just a key.
- **Fully offline.** Voice and text never leave your computer.

### Security & Privacy

- **Приватність за замовчуванням.** Усе відбувається на комп’ютері користувача; голос і текст не йдуть на чужі сервери чи в інтернет. / Privacy by default — everything runs on the user’s machine; voice and text never reach external servers or the internet.
- **Перевірка оновлень без телеметрії** та з можливістю повного вимкнення. / Update check carries no telemetry and can be fully disabled.

### License

Безкоштовно для людей, код відкритий для перевірки — ліцензія PolyForm Noncommercial 1.0.0 (`LICENSE`) з додатковими дозволами (`COMMERCIAL-LICENSE.md`). Copyright © 2026 Микола Жуковець. Сторонні компоненти — у `THIRD-PARTY-NOTICES.txt`. /
Free for people, source open for inspection — PolyForm Noncommercial 1.0.0 license (`LICENSE`) with additional permissions (`COMMERCIAL-LICENSE.md`). Copyright © 2026 Mykola Zhukovets. Third-party components are listed in `THIRD-PARTY-NOTICES.txt`.

[Unreleased]: https://github.com/mykola-zhukovets/balachky/compare/v1.2.3-beta...HEAD
[1.2.3-beta]: https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.3-beta
[1.2.2]: https://github.com/mykola-zhukovets/balachky/releases/tag/v1.2.2
[1.0.0]: https://github.com/mykola-zhukovets/balachky/releases/tag/v1.0.0
