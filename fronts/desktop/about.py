"""Інформаційний хаб «Про програму» — відкривається кліком по шапці сайдбара.

Зводить в одне місце те, що нетех-користувач шукає під «Про програму»: версію
й збірку, посилання на репозиторій, довідку, ліцензії третіх сторін, кнопку
звіту про проблему й подяки. Аудиторія очікує, що програма працює офлайн, тож
кожне зовнішнє посилання явно позначене як вихід в інтернет; відкриваються лише
https-адреси у системному браузері (setOpenExternalLinks).

Дії (довідка / звіт / ліцензії) делегуються назад через колбеки — логіка вже
живе в Налаштуваннях (pages/settings.py), тут її не дублюємо.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)

from whisper_core import __version__
from whisper_core._buildinfo import build_commit
from .i18n import tr
from .links import GITHUB_URL, X_URL   # єдине джерело правди для лінків автора


def _open_support_menu(btn):
    """Меню способів підтримки автора — те саме, що в Налаштуваннях (імпорт
    відкладений: сторінка Налаштувань важка й тягне за собою півпрограми)."""
    from .pages.settings import show_support_menu
    show_support_menu(btn)


class AboutDialog(QDialog):
    """Модальний хаб «Про програму». on_help / on_report / on_licenses —
    колбеки на вже існуючі дії Налаштувань (None → відповідна кнопка ховається)."""

    def __init__(self, parent=None, *, on_help=None, on_report=None,
                 on_licenses=None):
        super().__init__(parent)
        self.setWindowTitle(tr("about_title"))
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel(tr("about_title"))
        title.setProperty("strong", True)
        lay.addWidget(title)

        lead = QLabel(tr("set_about_lead"))
        lead.setWordWrap(True)
        lay.addWidget(lead)

        version = QLabel(tr("about_version_line", ver=__version__,
                            build=build_commit()))
        version.setProperty("muted", True)
        version.setWordWrap(True)
        lay.addWidget(version)

        # явна позначка, що посилання нижче ведуть в інтернет (офлайн-аудиторія)
        net_note = QLabel(tr("about_net_note"))
        net_note.setProperty("muted", True)
        net_note.setWordWrap(True)
        lay.addWidget(net_note)

        github = QLabel('<a href="{url}">{text}</a>'.format(
            url=GITHUB_URL, text=tr("about_github")))
        github.setOpenExternalLinks(True)   # лише https, системний браузер
        github.setWordWrap(True)
        github.setToolTip(tr("net_link_hint"))
        github.setAccessibleName(tr("about_github"))
        lay.addWidget(github)

        # X (Twitter) автора. Пошту тут не показуємо — рішення власника.
        x_link = QLabel('<a href="{url}">{text}</a>'.format(
            url=X_URL, text=tr("about_x")))
        x_link.setObjectName("aboutXLink")
        x_link.setOpenExternalLinks(True)   # лише https, системний браузер
        x_link.setWordWrap(True)
        x_link.setToolTip(tr("net_link_hint"))
        x_link.setAccessibleName(tr("about_x"))
        lay.addWidget(x_link)

        # кнопки дій — делегують у вже існуючу логіку Налаштувань
        row = QHBoxLayout()
        row.setSpacing(8)
        # підтримати автора — той самий механізм меню, що в Налаштуваннях
        support_btn = QPushButton(tr("about_support_link"))
        support_btn.setObjectName("aboutSupportBtn")
        support_btn.setAccessibleName(tr("about_support_link"))
        support_btn.setToolTip(tr("about_support_more_hint"))
        support_btn.clicked.connect(lambda: _open_support_menu(support_btn))
        row.addWidget(support_btn)
        if on_help is not None:
            help_btn = QPushButton(tr("set_help_label"))
            help_btn.setAccessibleName(tr("set_help_label"))
            help_btn.clicked.connect(lambda: on_help())
            row.addWidget(help_btn)
        if on_licenses is not None:
            lic_btn = QPushButton(tr("set_third_party_btn"))
            lic_btn.setAccessibleName(tr("set_third_party_btn"))
            lic_btn.clicked.connect(lambda: on_licenses())
            row.addWidget(lic_btn)
        if on_report is not None:
            rep_btn = QPushButton(tr("set_report_problem"))
            rep_btn.setAccessibleName(tr("set_report_problem"))
            rep_btn.clicked.connect(lambda: on_report())
            row.addWidget(rep_btn)
        row.addStretch()
        lay.addLayout(row)

        thanks = QLabel(tr("about_thanks"))
        thanks.setProperty("muted", True)
        thanks.setWordWrap(True)
        lay.addWidget(thanks)

        close = QPushButton(tr("common_close"))
        close.setAccessibleName(tr("common_close"))
        close.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(close)
        lay.addLayout(close_row)
