"""Сторінка «Віддалений доступ»: налаштування Telegram-бота та голосові з інших пристроїв.

Забезпечує введення bot token (масковане поле, DPAPI), deep-link спарювання
приватного чату, відображення стану сервісу та стрічку отриманих розшифровок.
"""

import time

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
import qtawesome as qta

from whisper_core.history import read_recent

from .. import theme
from ..empty_state import EmptyState
from ..glass import FlowLayout, GlassButton
from ..i18n import tr
from . import page_header


class RemotePage(QWidget):
    """Сторінка керування віддаленим доступом та Telegram-ботом."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._pairing_url = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(32, 26, 32, 18)
        root.setSpacing(0)

        # Шапка сторінки
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addLayout(page_header(tr("nav_remote"), tr("set_telegram_body")), 1)
        root.addLayout(head)
        root.addSpacing(16)

        # Скрол-область для карток налаштувань та стрічки
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        content_widget = QWidget()
        content_lay = QVBoxLayout(content_widget)
        content_lay.setContentsMargins(0, 0, 16, 0)
        content_lay.setSpacing(16)

        # --- Картка 1: Підключення та стан бота ---
        self._setup_card = self._build_setup_card()
        content_lay.addWidget(self._setup_card)

        # --- Картка 2: Стрічка «Голосові з інших пристроїв» ---
        self._feed_card = self._build_feed_card()
        content_lay.addWidget(self._feed_card)

        content_lay.addStretch()
        scroll.setWidget(content_widget)
        root.addWidget(scroll, stretch=1)

        # Підключення до сигналів контролера
        _state_sig = getattr(self.controller, "telegram_state_changed", None)
        if _state_sig is not None:
            try:
                _state_sig.connect(self._on_state_changed)
            except Exception:
                pass

        _err_sig = getattr(self.controller, "telegram_error", None)
        if _err_sig is not None:
            try:
                _err_sig.connect(self._on_error)
            except Exception:
                pass

        # Початковий стан
        self.refresh()

    def _build_setup_card(self) -> QFrame:
        card = QFrame()
        card.setProperty("card", True)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(20, 18, 20, 20)
        card_lay.setSpacing(14)

        # Рядок 1: Eyebrow + Тумблер увімкнення
        top_row = QHBoxLayout()
        eyebrow = QLabel(tr("set_telegram_eyebrow"))
        eyebrow.setProperty("eyebrow", True)
        top_row.addWidget(eyebrow)
        top_row.addStretch()

        self.enable_toggle = QCheckBox(tr("set_telegram_enable"))
        self.enable_toggle.setObjectName("telegramEnableToggle")
        self.enable_toggle.setAccessibleName(tr("set_telegram_enable"))
        self.enable_toggle.toggled.connect(self._on_toggle_enabled)
        top_row.addWidget(self.enable_toggle)
        card_lay.addLayout(top_row)

        # Рядок 2: Статус сервісу
        status_row = QHBoxLayout()
        status_title = QLabel(f"{tr('set_telegram_status_label')}:")
        status_title.setProperty("muted", True)
        status_row.addWidget(status_title)

        self.status_lbl = QLabel(tr("set_telegram_status_not_configured"))
        self.status_lbl.setObjectName("telegramStatusLabel")
        self.status_lbl.setAccessibleName(tr("set_telegram_status_label"))
        self.status_lbl.setWordWrap(True)
        status_row.addWidget(self.status_lbl, 1)
        card_lay.addLayout(status_row)

        # Розділювач
        div = QFrame()
        div.setProperty("divider", True)
        card_lay.addWidget(div)

        # Рядок 3: Введення токена від BotFather
        token_label = QLabel(tr("set_telegram_token_label"))
        token_label.setProperty("section", True)
        card_lay.addWidget(token_label)

        token_row = QHBoxLayout()
        token_row.setSpacing(10)

        self.token_input = QLineEdit()
        self.token_input.setObjectName("telegramTokenInput")
        self.token_input.setEchoMode(QLineEdit.Password)
        self.token_input.setPlaceholderText(tr("set_telegram_token_placeholder"))
        self.token_input.setAccessibleName(tr("set_telegram_token_label"))
        self.token_input.textChanged.connect(self._on_token_text_changed)
        token_row.addWidget(self.token_input, 1)

        self.connect_btn = GlassButton(tr("set_telegram_connect"))
        self.connect_btn.setObjectName("telegramConnectButton")
        self.connect_btn.setAccessibleName(tr("set_telegram_connect"))
        self.connect_btn.setEnabled(False)
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        token_row.addWidget(self.connect_btn)
        card_lay.addLayout(token_row)

        token_hint = QLabel(tr("set_telegram_token_hint"))
        token_hint.setProperty("muted", True)
        token_hint.setWordWrap(True)
        card_lay.addWidget(token_hint)

        # Рядок 4: Дії спарювання та від'єднання
        actions_widget = QWidget()
        actions_row = FlowLayout(actions_widget, spacing=10)

        self.pair_btn = GlassButton(
            tr("set_telegram_pair_open"),
            icon=qta.icon("fa6s.paper-plane", color=theme.IDLE),
        )
        self.pair_btn.setObjectName("telegramPairButton")
        self.pair_btn.setAccessibleName(tr("set_telegram_pair_open"))
        self.pair_btn.setToolTip(tr("set_telegram_pair_open_hint"))
        self.pair_btn.clicked.connect(self._on_pair_clicked)
        actions_row.addWidget(self.pair_btn)

        self.botfather_btn = QPushButton(tr("set_telegram_botfather_open"))
        self.botfather_btn.setObjectName("telegramBotFatherButton")
        self.botfather_btn.setProperty("ghost", True)
        self.botfather_btn.setAccessibleName(tr("set_telegram_botfather_title"))
        self.botfather_btn.clicked.connect(self._on_botfather_clicked)
        actions_row.addWidget(self.botfather_btn)

        self.disconnect_btn = QPushButton(tr("set_telegram_disconnect"))
        self.disconnect_btn.setObjectName("telegramDisconnectButton")
        self.disconnect_btn.setProperty("ghost", True)
        self.disconnect_btn.setAccessibleName(tr("set_telegram_disconnect"))
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        actions_row.addWidget(self.disconnect_btn)

        card_lay.addWidget(actions_widget)

        # Підказка про термін дії посилання
        self.pair_expiry_lbl = QLabel("")
        self.pair_expiry_lbl.setProperty("muted", True)
        self.pair_expiry_lbl.setWordWrap(True)
        self.pair_expiry_lbl.hide()
        card_lay.addWidget(self.pair_expiry_lbl)

        # Блок приватності
        priv_box = QFrame()
        priv_box.setProperty("subcard", True)
        priv_lay = QVBoxLayout(priv_box)
        priv_lay.setContentsMargins(14, 12, 14, 12)
        priv_lay.setSpacing(6)

        priv_title = QLabel(tr("set_telegram_privacy"))
        priv_title.setProperty("muted", True)
        priv_title.setWordWrap(True)
        priv_lay.addWidget(priv_title)
        card_lay.addWidget(priv_box)

        return card

    def _build_feed_card(self) -> QFrame:
        card = QFrame()
        card.setProperty("card", True)
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(20, 18, 20, 20)
        card_lay.setSpacing(12)

        feed_title = QLabel(tr("remote_feed_title"))
        feed_title.setProperty("section", True)
        card_lay.addWidget(feed_title)

        self._feed_layout = QVBoxLayout()
        self._feed_layout.setSpacing(8)

        self._feed_empty = EmptyState(
            "fa6s.tower-broadcast",
            tr("common_empty_here"),
            tr("remote_feed_empty"),
        )
        self._feed_layout.addWidget(self._feed_empty)

        card_lay.addLayout(self._feed_layout)
        return card

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()

    def refresh(self):
        cfg = getattr(self.controller, "cfg", None)
        status_data = {}
        status_fn = getattr(self.controller, "telegram_status", None)
        if callable(status_fn):
            try:
                status_data = status_fn() or {}
            except Exception:
                status_data = {}

        enabled = bool(getattr(cfg, "telegram_enabled", False))
        self.enable_toggle.blockSignals(True)
        self.enable_toggle.setChecked(enabled)
        self.enable_toggle.blockSignals(False)

        self._render_status(status_data)
        self._refresh_feed()

    def _render_status(self, status_data: dict):
        state = status_data.get("state", "disabled")
        bot_name = status_data.get("bot_name", "")

        cfg = getattr(self.controller, "cfg", None)
        has_paired = bool(
            getattr(cfg, "telegram_user_id", 0) and getattr(cfg, "telegram_chat_id", 0)
        )

        if state == "active" or (has_paired and state not in ("token-invalid", "shutdown-failed", "bot-in-use")):
            name_display = f"@{bot_name}" if bot_name and not bot_name.startswith("@") else (bot_name or "бот")
            self.status_lbl.setText(tr("set_telegram_status_active", bot_name=name_display))
            self.status_lbl.setProperty("badge", "success")
            self.connect_btn.setText(tr("set_telegram_replace_token"))
            self.pair_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(True)
        elif state == "pairing":
            self.status_lbl.setText(tr("set_telegram_status_waiting_pair"))
            self.status_lbl.setProperty("badge", "warning")
            self.pair_btn.setEnabled(True)
            self.disconnect_btn.setEnabled(True)
        elif state == "checking":
            self.status_lbl.setText(tr("set_telegram_status_checking"))
            self.status_lbl.setProperty("badge", "warning")
        elif state == "offline":
            self.status_lbl.setText(tr("set_telegram_error_no_network"))
            self.status_lbl.setProperty("badge", "error")
        elif state == "token-invalid":
            self.status_lbl.setText(tr("set_telegram_error_token_invalid"))
            self.status_lbl.setProperty("badge", "error")
        elif state == "bot-in-use":
            self.status_lbl.setText(tr("set_telegram_error_bot_in_use"))
            self.status_lbl.setProperty("badge", "error")
        elif state == "webhook-active":
            self.status_lbl.setText(tr("set_telegram_error_webhook"))
            self.status_lbl.setProperty("badge", "error")
        elif state == "shutdown-failed":
            self.status_lbl.setText(tr("set_telegram_status_shutdown_unconfirmed"))
            self.status_lbl.setProperty("badge", "error")
        else:
            self.status_lbl.setText(
                tr("set_telegram_status_disabled") if getattr(cfg, "telegram_enabled", False) else tr("set_telegram_status_not_configured")
            )
            self.status_lbl.setProperty("badge", "")

        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)

    def _refresh_feed(self):
        # Очистити старі віджети крім empty
        while self._feed_layout.count() > 1:
            item = self._feed_layout.takeAt(1)
            if item.widget():
                item.widget().deleteLater()

        profile = getattr(self.controller, "profile", None)
        if not profile:
            self._feed_empty.show()
            return

        records = read_recent(profile, limit=20)
        remote_records = [
            (line, rec)
            for line, rec in records
            if rec.get("source") in ("remote", "telegram")
        ]

        if not remote_records:
            self._feed_empty.show()
            return

        self._feed_empty.hide()
        for line, rec in remote_records:
            text = (rec.get("final") or rec.get("raw") or "").strip()
            if not text:
                continue
            item_frame = QFrame()
            item_frame.setProperty("subcard", True)
            lay = QVBoxLayout(item_frame)
            lay.setContentsMargins(14, 10, 14, 10)
            lay.setSpacing(6)

            meta_row = QHBoxLayout()
            ts = rec.get("ts")
            time_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(ts)) if ts else "—"
            meta_lbl = QLabel(f"{time_str}  ·  {tr('hist_from_remote')}")
            meta_lbl.setProperty("muted", True)
            meta_row.addWidget(meta_lbl)
            meta_row.addStretch()

            copy_btn = QPushButton(tr("common_copy"))
            copy_btn.setProperty("ghost", True)
            copy_btn.setAccessibleName(tr("common_copy"))
            copy_btn.clicked.connect(lambda _=False, t=text: QApplication.clipboard().setText(t))
            meta_row.addWidget(copy_btn)
            lay.addLayout(meta_row)

            body_lbl = QLabel(text)
            body_lbl.setWordWrap(True)
            body_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(body_lbl)

            self._feed_layout.addWidget(item_frame)

    def _on_toggle_enabled(self, checked: bool):
        set_enabled_fn = getattr(self.controller, "telegram_set_enabled", None)
        if callable(set_enabled_fn):
            set_enabled_fn(checked)
        self.refresh()

    def _on_token_text_changed(self, text: str):
        token = text.strip()
        self.connect_btn.setEnabled(bool(token and ":" in token))

    def _on_connect_clicked(self):
        token = self.token_input.text().strip()
        if not token:
            return
        verify_fn = getattr(self.controller, "telegram_verify_token", None)
        if callable(verify_fn):
            verify_fn(token)
        self.token_input.clear()
        self.status_lbl.setText(tr("set_telegram_status_checking"))

    def _on_pair_clicked(self):
        pair_fn = getattr(self.controller, "telegram_begin_pairing", None)
        if not callable(pair_fn):
            return
        try:
            url = pair_fn()
            self._pairing_url = url
            if url:
                QDesktopServices.openUrl(QUrl(url))
                self.pair_expiry_lbl.setText(
                    f"{tr('set_telegram_pair_instruction')} {tr('set_telegram_pair_open_hint')}"
                )
                self.pair_expiry_lbl.show()
        except Exception as exc:
            QMessageBox.warning(self, tr("set_telegram_title"), str(exc))

    def _on_botfather_clicked(self):
        QDesktopServices.openUrl(QUrl("https://t.me/BotFather"))

    def _on_disconnect_clicked(self):
        res = QMessageBox.question(
            self,
            tr("set_telegram_disconnect_title"),
            tr("set_telegram_disconnect_body"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if res == QMessageBox.Yes:
            disconnect_fn = getattr(self.controller, "telegram_disconnect", None)
            if callable(disconnect_fn):
                disconnect_fn()
            self.refresh()

    def _on_state_changed(self, state_dict: dict):
        self._render_status(state_dict)
        self._refresh_feed()

    def _on_error(self, error_text: str):
        self.status_lbl.setText(f"{tr('set_telegram_status_error')}: {error_text}")
        self.status_lbl.setProperty("badge", "error")
        self.status_lbl.style().unpolish(self.status_lbl)
        self.status_lbl.style().polish(self.status_lbl)
