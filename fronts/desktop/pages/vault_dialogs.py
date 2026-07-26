"""Діалоги пароля сховища записів нарад (feature/meeting-encryption).

Діалоги ТОНКІ: перевірка нового пароля і лічильник спроб — чисті функції/класи
(тестуються без Qt), криптографія — методи контролера meeting_vault_*
(app.py → whisper_core.meeting.storage_crypto). Пароль ніде не логгується і не
зберігається; розшифрований ключ живе лише в пам'яті процесу.
"""
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QVBoxLayout,
)

from ..i18n import tr

MIN_PASSWORD_LEN = 8
MAX_UNLOCK_ATTEMPTS = 3


# i18n-ключі опису стану сховища для секції Налаштувань (стан → підпис).
VAULT_STATE_LABELS = {
    "none": "set_vault_state_none",
    "dpapi": "set_vault_state_dpapi",
    "password": "set_vault_state_password",
    "locked": "set_vault_state_locked",
    "keyfile": "set_vault_state_keyfile",
    "keyfile_locked": "set_vault_state_keyfile_locked",
    "twofactor": "set_vault_state_twofactor",
    "twofactor_locked": "set_vault_state_twofactor_locked",
    "lost": "meeting_error_key_lost",
}


def vault_controls_for_state(state: str) -> dict:
    """Які кнопки секції захисту доречні для стану сховища (чиста логіка, без Qt).

    set/set_keyfile/set_twofactor — лише коли секрету ще немає (none/dpapi).
    Парольне сховище (password/locked) дає change/remove/recovery і замкнене, бо
    діалоги питають чинний пароль; файл-ключ і двофактор реконфігуруються лише
    коли відкриті (їх спершу відмикають на вкладці «Записи»). lock — коли
    відкрито. «lost» не пропонує дій."""
    unset = state in ("none", "dpapi")
    has_password = state in ("password", "locked")
    opened = state in ("password", "keyfile", "twofactor")
    reconfigurable = has_password or opened
    return {
        "set": unset,
        "set_keyfile": unset,
        "set_twofactor": unset,
        "change": has_password,
        "remove": reconfigurable,
        "lock": opened,
        "recovery": reconfigurable,
    }


def validate_new_password(password: str, repeat: str) -> "str | None":
    """None — пароль придатний; інакше i18n-ключ помилки."""
    if len(password) < MIN_PASSWORD_LEN:
        return "vault_pw_too_short"
    if password != repeat:
        return "vault_pw_mismatch"
    return None


class UnlockAttempts:
    """Лічильник спроб розблокування: після третьої невдачі — відмова без
    підказок (діалог закривається, сховище лишається закритим)."""

    def __init__(self, limit: int = MAX_UNLOCK_ATTEMPTS):
        self._left = limit

    @property
    def exhausted(self) -> bool:
        return self._left <= 0

    def fail(self) -> bool:
        """Зареєструвати невдачу; True — можна пробувати ще."""
        self._left -= 1
        return self._left > 0


def _password_field() -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.Password)
    return field


def _error_label() -> QLabel:
    lbl = QLabel()
    lbl.setProperty("muted", True)
    lbl.setWordWrap(True)
    lbl.hide()
    return lbl


class VaultUnlockDialog(QDialog):
    """Запит пароля перед зашифрованим вмістом (історія/плеєр/експорт).
    exec() == Accepted → сховище розблоковано цією сесією."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._attempts = UnlockAttempts()
        self.setWindowTitle(tr("vault_unlock_title"))
        self.setModal(True)
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        prompt = QLabel(tr("vault_unlock_prompt"))
        prompt.setProperty("strong", True)
        prompt.setWordWrap(True)
        lay.addWidget(prompt)

        self._password = _password_field()
        self._password.returnPressed.connect(self._submit)
        lay.addWidget(self._password)

        self._error = _error_label()
        lay.addWidget(self._error)

        forgot = QPushButton(tr("vault_unlock_forgot"))
        forgot.setProperty("link", True)
        forgot.setFlat(True)
        forgot.clicked.connect(self._open_recovery)
        lay.addWidget(forgot)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        unlock = QPushButton(tr("vault_unlock_button"))
        unlock.setProperty("accent", True)
        unlock.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(unlock)
        lay.addLayout(btns)

    def _submit(self):
        if self.controller.meeting_vault_unlock(self._password.text()):
            self.accept()
            return
        self._password.clear()
        if self._attempts.fail():
            self._error.setText(tr("vault_pw_wrong"))
            self._error.show()
            return
        # 3-тя невдача: відмова без підказок — закриваємо діалог
        self._error.setText(tr("vault_unlock_failed"))
        self.reject()

    def _open_recovery(self):
        """«Забув пароль»: вхід кодом відновлення розблоковує це саме сховище."""
        if VaultRecoveryUnlockDialog(self.controller, self).exec() == QDialog.Accepted:
            self.accept()


class VaultPasswordDialog(QDialog):
    """Задати / змінити / зняти пароль сховища (mode: set | change | remove).
    exec() == Accepted → операція виконана контролером."""

    _TITLES = {"set": "vault_pw_title_set", "change": "vault_pw_title_change",
               "remove": "vault_pw_title_remove"}

    def __init__(self, controller, mode: str, parent=None):
        if mode not in self._TITLES:
            raise ValueError(f"невідомий режим діалогу пароля: {mode!r}")
        super().__init__(parent)
        self.controller = controller
        self.mode = mode
        self.setWindowTitle(tr(self._TITLES[mode]))
        self.setModal(True)
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        self._current = self._new = self._repeat = None
        if mode in ("change", "remove"):
            current_lbl = QLabel(tr("vault_pw_current_label"))
            current_lbl.setProperty("formlabel", True)
            current_lbl.setWordWrap(True)
            lay.addWidget(current_lbl)
            self._current = _password_field()
            lay.addWidget(self._current)
        if mode in ("set", "change"):
            new_lbl = QLabel(tr("vault_pw_new_label"))
            new_lbl.setProperty("formlabel", True)
            new_lbl.setWordWrap(True)
            lay.addWidget(new_lbl)
            self._new = _password_field()
            lay.addWidget(self._new)
            repeat_lbl = QLabel(tr("vault_pw_repeat_label"))
            repeat_lbl.setProperty("formlabel", True)
            repeat_lbl.setWordWrap(True)
            lay.addWidget(repeat_lbl)
            self._repeat = _password_field()
            lay.addWidget(self._repeat)
            warning = QLabel(tr("vault_pw_warning"))
            warning.setProperty("muted", True)
            warning.setWordWrap(True)
            lay.addWidget(warning)

        self._error = _error_label()
        lay.addWidget(self._error)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr(self._TITLES[mode]))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _show_error(self, key: str):
        self._error.setText(tr(key))
        self._error.show()

    def _submit(self):
        current = self._current.text() if self._current is not None else None
        if self.mode == "remove":
            error = self.controller.meeting_vault_remove_password(current)
        else:
            new = self._new.text()
            error = validate_new_password(new, self._repeat.text())
            if error is None:
                error = self.controller.meeting_vault_set_password(
                    new, current=current)
        if error is not None:
            self._show_error(error)
            return
        # Перше задання пароля щойно створило код відновлення → показати раз.
        pop = getattr(self.controller, "meeting_vault_pop_recovery_code", None)
        code = pop() if pop is not None else None
        if code:
            VaultRecoveryCodeDialog(code, self).exec()
        self.accept()


def _mono_font() -> QFont:
    font = QFont()
    font.setStyleHint(QFont.Monospace)
    font.setFamily("Consolas")
    font.setPointSize(14)
    return font


class VaultRecoveryCodeDialog(QDialog):
    """Одноразовий показ коду відновлення: моно-шрифт, «Копіювати», попередження.
    Закривається лише свідомо («Я зберіг код») — код більше не покажеться."""

    def __init__(self, code: str, parent=None):
        super().__init__(parent)
        self.code = code
        self.setWindowTitle(tr("vault_recovery_title"))
        self.setModal(True)
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(12)

        intro = QLabel(tr("vault_recovery_intro"))
        intro.setProperty("strong", True)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        code_lbl = QLabel(code)
        code_lbl.setFont(_mono_font())
        code_lbl.setTextInteractionFlags(code_lbl.textInteractionFlags()
                                         | code_lbl.textInteractionFlags().TextSelectableByMouse)
        code_lbl.setWordWrap(True)
        code_lbl.setProperty("code", True)
        lay.addWidget(code_lbl)

        warning = QLabel(tr("vault_recovery_warning"))
        warning.setProperty("muted", True)
        warning.setWordWrap(True)
        lay.addWidget(warning)

        self._copied = _error_label()
        lay.addWidget(self._copied)

        btns = QHBoxLayout()
        copy = QPushButton(tr("common_copy"))
        copy.clicked.connect(self._copy)
        done = QPushButton(tr("vault_recovery_done"))
        done.setProperty("accent", True)
        done.clicked.connect(self.accept)
        btns.addWidget(copy)
        btns.addStretch()
        btns.addWidget(done)
        lay.addLayout(btns)

    def _copy(self):
        clip = QApplication.clipboard()
        if clip is not None:
            clip.setText(self.code)
        self._copied.setText(tr("vault_recovery_copied"))
        self._copied.show()


class VaultRecoveryUnlockDialog(QDialog):
    """Вхід кодом відновлення. exec() == Accepted → сховище розблоковано, і
    користувачу одразу пропонується задати новий пароль (старий код лишається)."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(tr("vault_recovery_unlock_title"))
        self.setModal(True)
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        prompt = QLabel(tr("vault_recovery_unlock_prompt"))
        prompt.setProperty("strong", True)
        prompt.setWordWrap(True)
        lay.addWidget(prompt)

        self._code = QLineEdit()
        self._code.setFont(_mono_font())
        self._code.returnPressed.connect(self._submit)
        lay.addWidget(self._code)

        self._error = _error_label()
        lay.addWidget(self._error)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("vault_recovery_unlock_button"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _submit(self):
        if not self.controller.meeting_vault_unlock_with_recovery(self._code.text()):
            self._error.setText(tr("vault_recovery_wrong"))
            self._error.show()
            return
        # Розблоковано: пропонуємо задати новий пароль (не обов'язково).
        VaultPasswordDialog(self.controller, "set", self).exec()
        self.accept()


class VaultRegenerateRecoveryDialog(QDialog):
    """Створити новий код відновлення (з запитом чинного пароля). Успіх →
    показ нового коду один раз; старий код одразу перестає діяти."""

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle(tr("set_vault_pw_recovery"))
        self.setModal(True)
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        current_lbl = QLabel(tr("vault_pw_current_label"))
        current_lbl.setProperty("formlabel", True)
        current_lbl.setWordWrap(True)
        lay.addWidget(current_lbl)
        self._current = _password_field()
        self._current.returnPressed.connect(self._submit)
        lay.addWidget(self._current)

        self._error = _error_label()
        lay.addWidget(self._error)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("set_vault_pw_recovery"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _submit(self):
        error, code = self.controller.meeting_vault_regenerate_recovery(
            self._current.text())
        if error is not None:
            self._error.setText(tr(error))
            self._error.show()
            return
        VaultRecoveryCodeDialog(code, self).exec()
        self.accept()


# ------------------------------------------------------------------- файл-ключ
def _pick_keyfile(parent) -> "str | None":
    """Діалог вибору наявного файла-ключа (порожній вибір → None)."""
    path, _ = QFileDialog.getOpenFileName(parent, tr("vault_keyfile_pick"))
    return path or None


def _save_keyfile(parent) -> "str | None":
    """Діалог збереження нового файла-ключа (порожній вибір → None)."""
    path, _ = QFileDialog.getSaveFileName(
        parent, tr("vault_keyfile_create_title"), tr("vault_keyfile_default_name"))
    return path or None


class _KeyfilePickerRow:
    """Спільна логіка «Вибрати файл-ключ…» + підпис вибраного (тонкий міксин)."""

    def _build_keyfile_row(self, layout):
        self._keyfile_path = None
        pick = QPushButton(tr("vault_keyfile_pick"))
        pick.clicked.connect(self._on_pick_keyfile)
        layout.addWidget(pick)
        self._keyfile_lbl = QLabel()
        self._keyfile_lbl.setProperty("muted", True)
        self._keyfile_lbl.setWordWrap(True)
        self._keyfile_lbl.hide()
        layout.addWidget(self._keyfile_lbl)

    def _on_pick_keyfile(self):
        path = _pick_keyfile(self)
        if not path:
            return
        self._keyfile_path = path
        from pathlib import Path
        self._keyfile_lbl.setText(tr("vault_selected_file", name=Path(path).name))
        self._keyfile_lbl.show()


class VaultProtectKeyfileDialog(QDialog, _KeyfilePickerRow):
    """Перевести сховище під захист файлом-ключем (two_factor=False) або
    «пароль+файл» (two_factor=True). exec() == Accepted → захист увімкнено."""

    def __init__(self, controller, two_factor: bool, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.two_factor = two_factor
        self.setWindowTitle(tr("vault_protect_twofactor_title" if two_factor
                               else "vault_protect_keyfile_title"))
        self.setModal(True)
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        prompt = QLabel(tr("vault_protect_keyfile_prompt"))
        prompt.setProperty("strong", True)
        prompt.setWordWrap(True)
        lay.addWidget(prompt)

        self._new = self._repeat = None
        if two_factor:
            new_lbl = QLabel(tr("vault_pw_new_label"))
            new_lbl.setProperty("formlabel", True)
            new_lbl.setWordWrap(True)
            lay.addWidget(new_lbl)
            self._new = _password_field()
            lay.addWidget(self._new)
            repeat_lbl = QLabel(tr("vault_pw_repeat_label"))
            repeat_lbl.setProperty("formlabel", True)
            repeat_lbl.setWordWrap(True)
            lay.addWidget(repeat_lbl)
            self._repeat = _password_field()
            lay.addWidget(self._repeat)

        self._build_keyfile_row(lay)

        warning = QLabel(tr("vault_keyfile_warning"))
        warning.setProperty("muted", True)
        warning.setWordWrap(True)
        lay.addWidget(warning)

        self._error = _error_label()
        lay.addWidget(self._error)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        submit = QPushButton(tr("common_ok"))
        submit.setProperty("accent", True)
        submit.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(submit)
        lay.addLayout(btns)

    def _show_error(self, key: str):
        self._error.setText(tr(key))
        self._error.show()

    def _submit(self):
        password = None
        if self.two_factor:
            password = self._new.text()
            error = validate_new_password(password, self._repeat.text())
            if error is not None:
                self._show_error(error)
                return
        if not self._keyfile_path:
            self._show_error("vault_keyfile_none_picked")
            return
        error = self.controller.meeting_vault_set_keyfile(
            self._keyfile_path, password)
        if error is not None:
            self._show_error(error)
            return
        pop = getattr(self.controller, "meeting_vault_pop_recovery_code", None)
        code = pop() if pop is not None else None
        if code:
            VaultRecoveryCodeDialog(code, self).exec()
        self.accept()


class VaultKeyfileUnlockDialog(QDialog, _KeyfilePickerRow):
    """Відкрити сховище файлом-ключем (і паролем у двофакторному режимі).
    exec() == Accepted → сховище розблоковано цією сесією."""

    def __init__(self, controller, two_factor: bool, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.two_factor = two_factor
        self._attempts = UnlockAttempts()
        self.setWindowTitle(tr("vault_keyfile_unlock_title"))
        self.setModal(True)
        self.setMinimumWidth(460)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(10)

        prompt = QLabel(tr("vault_keyfile_unlock_prompt_2fa" if two_factor
                           else "vault_keyfile_unlock_prompt"))
        prompt.setProperty("strong", True)
        prompt.setWordWrap(True)
        lay.addWidget(prompt)

        self._password = None
        if two_factor:
            self._password = _password_field()
            self._password.returnPressed.connect(self._submit)
            lay.addWidget(self._password)

        self._build_keyfile_row(lay)

        self._error = _error_label()
        lay.addWidget(self._error)

        forgot = QPushButton(tr("vault_unlock_forgot"))
        forgot.setProperty("link", True)
        forgot.setFlat(True)
        forgot.clicked.connect(self._open_recovery)
        lay.addWidget(forgot)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel = QPushButton(tr("common_cancel"))
        cancel.clicked.connect(self.reject)
        unlock = QPushButton(tr("vault_unlock_button"))
        unlock.setProperty("accent", True)
        unlock.clicked.connect(self._submit)
        btns.addWidget(cancel)
        btns.addWidget(unlock)
        lay.addLayout(btns)

    def _submit(self):
        if not self._keyfile_path:
            self._error.setText(tr("vault_keyfile_none_picked"))
            self._error.show()
            return
        password = self._password.text() if self._password is not None else None
        if self.controller.meeting_vault_unlock_with_keyfile(
                self._keyfile_path, password):
            self.accept()
            return
        if self._password is not None:
            self._password.clear()
        if self._attempts.fail():
            self._error.setText(tr("vault_keyfile_bad"))
            self._error.show()
            return
        self._error.setText(tr("vault_unlock_failed"))
        self.reject()

    def _open_recovery(self):
        """«Забув пароль»: код відновлення відкриває будь-яке захищене сховище."""
        if VaultRecoveryUnlockDialog(self.controller, self).exec() == QDialog.Accepted:
            self.accept()
