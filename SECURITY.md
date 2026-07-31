# Безпека та звітність про вразливості · Security & Vulnerability Reporting

Цей документ описує політику безпеки проєкту **“Балачки у Коростені” (Balachky)** та порядок приватної звітності про знайдені вразливості.

This document describes the security policy for **Balachky** and the private disclosure process for security vulnerabilities.

---

## Українською

### Як повідомити про вразливість
“Балачки” працюють із приватними аудіозаписами, стенограмами нарад та шифрованим сховищем (AES-256-GCM), тому безпека даних є пріоритетом.

Якщо Ви виявили потенційну вразливість у програмі чи її компонентах, **будь ласка, повідомте про неї приватно**:
1. Перейдіть на сторінку репозиторію на GitHub.
2. Відкрийте вкладку **Security → Report a vulnerability**.
3. Заповніть деталі вразливості (кроки відтворення, потенційний вплив).

**Будь ласка, НЕ створюйте публічні Issues та НЕ обговорюйте знайдені вразливості у відкритому доступі**, поки вони не будуть опрацьовані.

### Чого очікувати (строки та процес)
- **Первинна відповідь:** проєкт веде одна людина, тож жодних строків не обіцяємо — відповідь надійде, щойно автор зможе.
- **Оцінка та виправлення:** після підтвердження вразливості автор за можливості оцінить ступінь ризику та опрацює виправлення в майбутніх оновленнях. Це не є гарантійним зобов'язанням: програма надається “як є” (див. `LICENSE`).
- **Публічне розкриття:** Після випуску патча деталі оприлюднюються через GitHub Security Advisory за погодженням з автором звіту.

### Що НЕ вважається вразливістю безпеки
Оскільки “Балачки” є офлайновим десктопним застосунком, що працює на комп'ютері користувача, до вразливостей **НЕ належать**:
- Доступ користувача (або процесів із його правами) до власних файлів, даних чи шифрованих сховищ на його власному пристрої (локальна модель загроз передбачає операції в межах прав поточного користувача Windows).
- Фізичний доступ сторонніх осіб до розблокованого пристрою користувача.
- Збої у роботі чи помилки інтерфейсу, які не призводять до витоку даних чи виконання довільного коду.
- Попередження SmartScreen через відсутність цифрового підпису бета-інсталятора (про це прямо зазначено в документації [docs/INSTALL-SMARTSCREEN.md](docs/INSTALL-SMARTSCREEN.md)).

---

## English

### Reporting a Vulnerability
Balachky handles private meeting recordings, dictation transcripts, and encrypted local storage (AES-256-GCM). Data security is a core design choice.

If you find a security vulnerability, **please report it privately**:
1. Go to the repository main page on GitHub.
2. Open the **Security → Report a vulnerability** tab.
3. Provide details, reproduction steps, and potential impact.

**Please do NOT file public Issues or disclose vulnerabilities publicly** until they are resolved.

### Response Timelines & Process
- **Initial Acknowledgment:** the project is maintained by one person, so no response time is promised — you will hear back as soon as the author can.
- **Triage & Patching:** once confirmed, the author will assess severity and work on a fix in future updates where possible. This is not a warranty commitment: the software is provided “as is” (see `LICENSE`).
- **Disclosure:** After a release containing the fix is published, advisory details are disclosed in coordination with the reporter.

### Out of Scope (What is NOT a Vulnerability)
As Balachky is a local offline desktop application, the following are **NOT** considered security vulnerabilities:
- A local user (or processes running with user privileges) accessing their own files, application state, or decrypted data on their own machine.
- Physical access to an unlocked host machine.
- General application crashes or UI bugs that do not lead to data leaks or arbitrary code execution.
- Unsigned installer SmartScreen warnings (this status is explicitly documented in [docs/INSTALL-SMARTSCREEN.md](docs/INSTALL-SMARTSCREEN.md)).
