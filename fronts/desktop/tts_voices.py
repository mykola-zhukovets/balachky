"""Менеджер голосів TTS (§7.3, Хвиля 3): вкладка «Голоси» — картки за патерном
model_manager, ЗГРУПОВАНІ ЗА МОВОЮ, рекомендований — першим.

Картка: назва, рушій, мови, розмір, ліцензія (клік → сторінка), «Завантажити»
(consent+SHA), «Почути зразок» (демо-WAV §7.6; заглушка з чесним текстом, доки WAV
не згенеровано на білді), «Видалити», радіо «активний для цієї мови». OpenRAIL-M-
голоси несуть позначку «згенеровано ШІ» (§7.6). «Інша модель…» → voice-pack (§4.4).

Колбеки звʼязує app. Панель самодостатня для visual_gate (без мережі/воркерів)."""
from __future__ import annotations

from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDialog, QFrame,
                               QHBoxLayout, QLabel, QRadioButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .glass import GlassButton
from .i18n import tr, human_size
from whisper_core.tts import voices as _v

_human_size = human_size



class VoiceCard(QFrame):
    def __init__(self, preset, *, available=False, active=False, lang="uk",
                 on_download=None, on_sample=None, on_delete=None, on_activate=None,
                 group=None, parent=None):
        super().__init__(parent)
        self.setProperty("card", True)
        self._preset = preset
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        title = QLabel(tr(preset.label_key))
        title.setProperty("card_title", True)
        lay.addWidget(title)

        langs = ", ".join(preset.languages)
        meta = QLabel(f"{preset.engine_kind} · {tr('tts_voice_lang', langs=langs)} · "
                      f"{_human_size(preset.approx_size_bytes)} · {preset.license_name}")
        meta.setWordWrap(True)
        lay.addWidget(meta)

        # OpenRAIL-M → обовʼязкова позначка «згенеровано ШІ» (§7.6)
        if "openrail" in (preset.license_name or "").lower():
            note = QLabel(tr("tts_voice_ai_note"))
            note.setProperty("card_note", True)
            lay.addWidget(note)

        # RAD-TTS: пофонемний мапінг приблизний (best-effort) → чесна нота (суд хвилі 3)
        if preset.engine_kind == "radtts":
            approx = QLabel(tr("tts_voice_approx_karaoke"))
            approx.setProperty("card_note", True)
            approx.setWordWrap(True)
            lay.addWidget(approx)

        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(8)

        if not available:
            dl = GlassButton(tr("tts_voice_download"))
            dl.setToolTip(tr("hint_tts_voice_download"))
            dl.clicked.connect(lambda: on_download and on_download(preset.id))
            row_l.addWidget(dl)
        else:
            self._radio = QRadioButton(tr("tts_voice_active"))
            self._radio.setChecked(active)
            self._radio.toggled.connect(
                lambda on: on and on_activate and on_activate(preset.id, lang))
            if group is not None:
                group.addButton(self._radio)
            row_l.addWidget(self._radio)
            dele = GlassButton(tr("tts_voice_delete"))
            dele.clicked.connect(lambda: on_delete and on_delete(preset.id))
            row_l.addWidget(dele)

        # «Почути зразок»: демо-WAV §7.6; доки не згенеровано — заглушка з тултипом
        sample = GlassButton(tr("tts_voice_sample"))
        sample.setToolTip(tr("hint_tts_voice_sample"))
        if not _v.has_sample(preset.id):
            sample.setEnabled(False)             # чесна заглушка (WAV на білді)
        sample.clicked.connect(lambda: on_sample and on_sample(preset.id))
        row_l.addWidget(sample)

        row_l.addStretch(1)
        lay.addWidget(row)


class VoiceManagerDialog(QDialog):
    def __init__(self, parent=None, *, root=None, active_by_lang=None,
                 tts_enabled=False, on_toggle_enabled=None,
                 on_download=None, on_sample=None, on_delete=None,
                 on_activate=None, on_add_custom=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tts_voices_eyebrow"))
        # достатня ширина, щоб ряд кнопок картки (Завантажити/Активний/Видалити/
        # Почути приклад) не обрізався (visual_gate --strict smaller_than_hint)
        self.setMinimumWidth(600)
        self.resize(600, 720)
        active_by_lang = active_by_lang or {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)

        eyebrow = QLabel(tr("tts_voices_eyebrow"))
        eyebrow.setProperty("eyebrow", True)
        outer.addWidget(eyebrow)

        # §9-10 (ревізія Sol): тумблер увімкнення пакета — БЕЗ нього звичайний
        # користувач не міг штатно активувати озвучення (tts_enabled=False дефолт).
        self._enable = QCheckBox(tr("set_tts_enable"))
        self._enable.setChecked(bool(tts_enabled))
        self._enable.setToolTip(tr("set_tts_enable_hint"))
        self._enable.toggled.connect(
            lambda on: on_toggle_enabled and on_toggle_enabled(bool(on)))
        outer.addWidget(self._enable)
        hint = QLabel(tr("set_tts_enable_hint"))
        hint.setWordWrap(True)
        hint.setProperty("card_note", True)
        outer.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_l = QVBoxLayout(body)
        body_l.setSpacing(12)

        # групуємо за мовою; порядок мов — uk, en, решта
        langs = []
        for p in _v.VOICE_PRESETS.values():
            for lg in p.languages:
                if lg not in langs:
                    langs.append(lg)
        langs = sorted(langs, key=lambda x: (x != "uk", x != "en", x))

        self._groups = {}
        for lg in langs:
            header = QLabel(tr("tts_voice_lang", langs=lg))
            header.setProperty("group_header", True)
            body_l.addWidget(header)
            grp = QButtonGroup(self)
            self._groups[lg] = grp
            for preset in _v.voices_for_language(lg):
                avail = _v.voice_available(preset.id, root) if root is not None else False
                card = VoiceCard(
                    preset, available=avail,
                    active=(active_by_lang.get(lg) == preset.id), lang=lg,
                    on_download=on_download, on_sample=on_sample,
                    on_delete=on_delete, on_activate=on_activate, group=grp)
                body_l.addWidget(card)

        body_l.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, stretch=1)

        # «Інша модель…» — власний voice-pack (§4.4 безпека)
        custom = GlassButton(tr("tts_voice_custom"))
        custom.setToolTip(tr("hint_tts_voice_custom"))
        custom.clicked.connect(lambda: on_add_custom and on_add_custom())
        outer.addWidget(custom)
