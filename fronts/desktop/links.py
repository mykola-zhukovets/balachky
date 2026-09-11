"""Єдине джерело правди для зовнішніх посилань автора.

Ці адреси показуються в кількох місцях інтерфейсу (майстер першого запуску,
Налаштування → «Про автора», хаб «Про програму»). Тримаємо їх в ОДНОМУ модулі,
щоб не дублювати URL рядками по кодовій базі — правку робимо тут раз.
"""

# Публічний репозиторій програми.
GITHUB_URL = "https://github.com/mykola-zhukovets/balachky"
ISSUE_URL = GITHUB_URL + "/issues/new"

# Сторінка автора в X (Twitter). Адресу підтвердив власник 25.07.
X_URL = "https://x.com/zukovec20653"

# «Підтримати автора»: варіанти допомоги (гривня, долари, євро, крипта)
SUPPORT_MONO_UAH = "https://send.monobank.ua/jar/21rfey7KTz"
SUPPORT_PRIVAT_USD = "https://www.privat24.ua/send/4h4jh"
SUPPORT_PRIVAT_EUR = "https://www.privat24.ua/send/4h5jr"
SUPPORT_USDT_TRC20 = "TTsc47PDTe2rUkeXcZGTQwR6driykkP2s8"
SUPPORT_BTC = "bc1q8wqskryef3ey09jxhv9epdv7kpxnzg8vcf40hy"
SUPPORT_ETH = "0x6A9FeF1CB66C20D31f770a970F790aFC85243A57"

# Зворотна сумісність: дефолтне посилання для mono-банки
SUPPORT_URL = SUPPORT_MONO_UAH


def reveal_in_explorer(path) -> bool:
    """Відкрити Провідник Windows із виділенням файлу або теку в системному файловому менеджері.

    Безпечно перехоплює винятки (неіснуючий шлях, OSError) та логує їх з anonymize_path.
    Повертає True, якщо запуск виконано, і False при помилці.
    """
    import logging
    import os
    import subprocess
    import sys
    from pathlib import Path
    from whisper_core.paths import anonymize_path

    try:
        p = Path(path).resolve()
        if not p.exists():
            return False

        if sys.platform.startswith("win"):
            if p.is_file():
                try:
                    subprocess.Popen(["explorer", "/select,", str(p)])
                    return True
                except Exception:
                    pass
            os.startfile(str(p))
            return True
        else:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices
            target = p.parent if p.is_file() else p
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
    except Exception as e:
        logging.exception("Не вдалося відкрити у провіднику %s: %s",
                          anonymize_path(str(path)), anonymize_path(str(e)))
        return False

