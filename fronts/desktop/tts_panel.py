"""Панель «Прослухати» (§8, §10-11): видимий UI озвучення з КАРАОКЕ (Хвиля 2).

Read-only редактор показує текст, що озвучується, і є ЦІЛЛЮ караоке-підсвічування
(setExtraSelections). Транспорт (InlinePlayer) + перемикач гранулярності слово/речення
+ навігація по реченнях (◀/▶) + «Стоп» + «Зберегти озвучення…». Порожній стан
«завантажте голос», коли активного голосу немає. Реєструється у scripts/visual_gate.py.

Синхронізація: InlinePlayer.position_changed (media-ms) → highlighter.update. Нав по
реченнях сікає плеєр на sentence_start (media-ms). БЕЗ мережі/воркерів — скан гейта ок."""
from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QTextEdit,
                               QVBoxLayout, QWidget)

from .glass import GlassButton
from .empty_state import EmptyState
from .i18n import tr
from .player import InlinePlayer
from .tts_karaoke import (GRANULARITY_SENTENCE, GRANULARITY_WORD,
                          KaraokeHighlighter)


class ListenPanel(QDialog):
    def __init__(self, parent=None, *, source=None, has_voice=True, text="",
                 on_stop=None, on_save=None, on_fix_word=None, on_voice_fix=None,
                 on_close=None):
        super().__init__(parent)
        self.setWindowTitle(tr("tts_listen"))
        self._on_stop = on_stop or (lambda: None)
        self._on_save = on_save or (lambda: None)
        self._on_fix_word = on_fix_word or (lambda word: None)
        self._on_voice_fix = on_voice_fix or (lambda: None)   # §9.2 reverse-звʼязка
        self._on_close = on_close or (lambda: None)
        self._sentence_starts = []
        self._granularity = GRANULARITY_WORD
        self._editor = None
        self._player = None
        self._highlighter = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        eyebrow = QLabel(tr("set_tts_eyebrow"))
        eyebrow.setProperty("eyebrow", True)
        outer.addWidget(eyebrow)

        if not has_voice:
            outer.addWidget(EmptyState("fa6s.volume-high",
                                       tr("tts_no_voice_title"),
                                       tr("tts_no_voice_hint")))
            return

        self._status_lbl = QLabel(tr("tts_preparing"))
        self._status_lbl.setProperty("card_note", True)
        self._status_lbl.setToolTip(tr("tts_preparing"))
        self._status_lbl.setAccessibleName(tr("tts_preparing"))
        outer.addWidget(self._status_lbl)

        # редактор-ціль караоке (read-only): показує озвучуваний текст
        self._editor = QTextEdit()
        self._editor.setReadOnly(True)
        self._editor.setPlainText(text or tr("tts_listen_selection"))
        self._editor.setToolTip(tr("hint_tts_pron_preview"))
        self._editor.viewport().installEventFilter(self)
        outer.addWidget(self._editor)
        self._highlighter = KaraokeHighlighter(self._editor)

        # транспорт
        self._player = InlinePlayer(source, self)
        self._player.position_changed.connect(self._on_position)
        outer.addWidget(self._player)
        # СТРІМІНГ (§3.2 TTFS): чанки (= речення) грають послідовно, перший одразу.
        # Тримаємо ВЕСЬ список + індекс поточного — для навігації ◀/▶ між реченнями
        # (суд хвилі 3: у стрімінгу nav іде по чанках, не по ms-таймлайну).
        self._chunks = []              # [(wav, tagged_timings)] усі речення по порядку
        self._cur_index = -1
        self._resume_index = 0         # §9.2: з якого речення почати (reverse-resume)
        self._resume_armed = False     # суд 5.2 Б1: resume дійсний РІВНО для наступного
        #                                synth-потоку; будь-який інший (не armed) потік
        #                                скидає його — застарілий індекс не глушить панель
        self._resume_gen = None        # суд 5.3: ЦІЛЬОВА генерація arm (generation-токен
        #                                контролера) — arm споживає лише чанк СВОЄЇ генерації
        self._active_gen = None        # генерація, що зараз програється (для відсіву чужих чанків)
        self._text_rev = 0             # суд 5.2 Б2: лічильник заміни ВМІСТУ панелі
        #                                (ревізія-гейт проти гонки правки під час діалогу)
        qmp = getattr(self._player, "_player", None)
        if qmp is not None:
            qmp.mediaStatusChanged.connect(self._on_media_status)

        # ряд керування: гранулярність + навігація + стоп + зберегти
        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.setSpacing(10)

        self._gran_btn = GlassButton(tr("tts_granularity_word"))
        self._gran_btn.setToolTip(tr("hint_tts_granularity"))
        self._gran_btn.clicked.connect(self._toggle_granularity)
        row_l.addWidget(self._gran_btn)

        self._prev_btn = GlassButton("◀")
        self._prev_btn.setToolTip(tr("tts_prev_sentence"))
        self._prev_btn.clicked.connect(self._prev_sentence)
        row_l.addWidget(self._prev_btn)

        self._next_btn = GlassButton("▶")
        self._next_btn.setToolTip(tr("tts_next_sentence"))
        self._next_btn.clicked.connect(self._next_sentence)
        row_l.addWidget(self._next_btn)

        self._stop_btn = GlassButton(tr("tts_stop"))
        self._stop_btn.setToolTip(tr("hint_tts_listen"))
        self._stop_btn.clicked.connect(self._handle_stop)
        row_l.addWidget(self._stop_btn)

        self._save_btn = GlassButton(tr("tts_save"))
        self._save_btn.setToolTip(tr("hint_tts_save"))
        self._save_btn.clicked.connect(lambda: self._on_save())
        row_l.addWidget(self._save_btn)

        row_l.addStretch(1)
        outer.addWidget(row)

        # §9.2 (Хвиля 5): «Виправити голосом» — ЄДИНИЙ вхід reverse-звʼязки з TTS;
        # правка стосується ТЕКСТУ ЦІЄЇ панелі (не спільного voice_edit_selection).
        self._voice_fix_btn = GlassButton(tr("tts_reverse_fix"))
        self._voice_fix_btn.setToolTip(tr("hint_tts_reverse_fix"))
        self._voice_fix_btn.clicked.connect(lambda: self._on_voice_fix())
        outer.addWidget(self._voice_fix_btn)

    # --- API для контролера ---
    def player(self):
        return getattr(self, "_player", None)

    def set_text(self, text: str) -> None:
        if self._editor is not None:
            self._editor.setPlainText(text or "")
        self._text_rev += 1              # суд 5.2 Б2: кожна заміна вмісту = нова ревізія

    def text_revision(self) -> int:
        """Суд 5.2 Б2: поточна ревізія тексту панелі. Інкрементується при КОЖНІЙ
        заміні вмісту (set_text). Правка порівнює ревізію старту з поточною — якщо
        текст підмінили під час модального діалогу, правку скасовуємо (чужий текст)."""
        return self._text_rev

    def set_timings(self, word_timings, sentence_starts=None) -> None:
        """Наскрізні word_timings (media-ms, raw-координати редактора) + старти
        речень; запустити караоке."""
        self._sentence_starts = list(sentence_starts or [])
        if self._highlighter is not None:
            self._highlighter.set_granularity(self._granularity)
            self._highlighter.start(word_timings, self._sentence_starts)

    def highlighter(self):
        return self._highlighter

    def eventFilter(self, obj, event):           # noqa: N802 (Qt override)
        from PySide6.QtCore import QEvent
        if (self._editor is not None
                and obj is self._editor.viewport()
                and event.type() == QEvent.Type.MouseButtonDblClick):
            # дати QTextEdit виділити слово, тоді відкрити діалог вимови для нього
            cursor = self._editor.cursorForPosition(event.position().toPoint())
            cursor.select(cursor.SelectionType.WordUnderCursor)
            word = cursor.selectedText().strip()
            if word:
                self._on_fix_word(word)
                return True
        return super().eventFilter(obj, event)

    def fix_word(self, word: str) -> None:
        """Публічний вхід (тест/меню): відкрити діалог вимови для слова."""
        if word:
            self._on_fix_word(word)

    # --- СТРІМІНГ чанків (§3.2 TTFS) ---
    def enqueue_chunk(self, wav_path, chunk_timings, is_first: bool,
                      generation=None) -> None:
        """Готовий чанк (= речення). Перший — грати НЕГАЙНО (перший звук швидкий);
        наступні — накопичуються; грають по завершенні поточного (EndOfMedia). Живий
        потік заповнює _sentence_starts і членство «sentence» (суд хвилі 3 БЛОКЕР 1).

        Суд 5.3: generation — токен synth-запиту контролера. Чанк ЧУЖОЇ (скасованої/
        старої) генерації ІГНОРУЄМО повністю, щоб уже поставлений у чергу Qt чанк
        обірваного потоку не глушив/не зсував актуальне відтворення."""
        if generation is not None and self._active_gen is not None:
            if is_first:
                if generation <= self._active_gen:
                    return               # застаріла/повторна генерація — ігнор
            elif generation != self._active_gen:
                return                   # чанк чужої генерації — ігнор
        if is_first:
            if getattr(self, "_status_lbl", None) is not None:
                self._status_lbl.hide()
            self._active_gen = generation
            self._chunks = []
            self._sentence_starts = []
            self._cur_index = -1
            # generation-гейт (суд 5.2 Б1 + 5.3): resume-індекс діє РІВНО для тієї
            # генерації, під яку його armed. Інша (не своя) генерація → скидаємо до 0,
            # інакше застарілий індекс від обірваного resume заглушив би це відтворення.
            if self._resume_armed and self._resume_gen in (None, generation):
                pass                     # своя генерація — зберігаємо resume_index
            else:
                self._resume_index = 0
            self._resume_armed = False       # одноразово: далі потік «чистий»
            self._resume_gen = None
        # у межах ОДНОГО чанку всі слова належать йому як речення 0 (sentence-гранулярність
        # підсвічує ціле речення; членство ставимо тут, бо merge_sentences стрімінг обійшов)
        tagged = [dict(w, sentence=0) for w in (chunk_timings or [])]
        idx = len(self._chunks)
        self._chunks.append((wav_path, tagged))
        self._sentence_starts.append(idx)          # НЕ порожній → ◀/▶ активні
        # §9.2 reverse-resume: почати відтворення з ПЕРШОГО чанка на/після _resume_index
        # (раніші зберігаємо для ◀-навігації, але не програємо). Норма — resume_index=0.
        if self._cur_index == -1 and idx >= self._resume_index:
            self._play_index(idx)
            self._resume_index = 0                 # разова дія — далі норма

    def _play_index(self, i: int) -> None:
        if not (0 <= i < len(self._chunks)) or self._player is None:
            return
        self._cur_index = i
        wav, timings = self._chunks[i]
        self._player.set_source(wav)
        self._player.play_from(0)
        if self._highlighter is not None and timings:
            self._highlighter.set_granularity(self._granularity)
            self._highlighter.start(timings, [0])  # per-чанк караоке (media-ms від 0)

    def _on_media_status(self, status) -> None:
        from PySide6.QtMultimedia import QMediaPlayer
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            if self._cur_index + 1 < len(self._chunks):
                self._play_index(self._cur_index + 1)   # авто-перехід до наступного речення

    # --- обробники ---
    def _on_position(self, ms: int) -> None:
        if self._highlighter is not None:
            self._highlighter.update(int(ms))

    def _toggle_granularity(self) -> None:
        self._granularity = (GRANULARITY_SENTENCE
                             if self._granularity == GRANULARITY_WORD
                             else GRANULARITY_WORD)
        self._gran_btn.setText(tr("tts_granularity_sentence")
                               if self._granularity == GRANULARITY_SENTENCE
                               else tr("tts_granularity_word"))
        if self._highlighter is not None:
            self._highlighter.set_granularity(self._granularity)

    def _next_sentence(self) -> None:
        # у стрімінгу навігація — по чанках (кожен чанк = речення), не по ms
        if self._cur_index + 1 < len(self._chunks):
            self._play_index(self._cur_index + 1)

    def _prev_sentence(self) -> None:
        if self._cur_index - 1 >= 0:
            self._play_index(self._cur_index - 1)

    def current_sentence_index(self) -> int:
        return self._cur_index

    def set_resume_index(self, idx: int, generation=None) -> None:
        """§9.2: після правки зворотним диктуванням продовжити з цього речення.
        Суд 5.3: armed прив'язаний до КОНКРЕТНОЇ генерації (generation — токен того
        synth-запиту, що зараз стартує resume). Лише чанк цієї генерації споживає arm;
        якщо той потік скасовано/впав ДО першого чанка — arm знімається (disarm)."""
        self._resume_index = max(0, int(idx))
        self._resume_armed = True
        self._resume_gen = generation

    def disarm(self, generation) -> None:
        """Суд 5.3/5.5: зняти resume-arm, коли synth-потік скасовано/впав/відхилено ДО
        першого чанка — щоб НАСТУПНА (чужа) генерація не успадкувала позицію. СТРОГО
        генераційний: скидаємо ТІЛЬКИ якщо arm належить САМЕ цій генерації
        (_resume_gen == generation). Інакше запізнілий/чужий drop після реальної доставки
        нової генерації хибно обнулив би її ще не спожитий resume_index."""
        if generation is not None and self._resume_gen == generation:
            self._resume_armed = False
            self._resume_gen = None
            self._resume_index = 0

    def _handle_stop(self) -> None:
        p = getattr(self, "_player", None)
        if p is not None:
            try:
                p.stop()
            except Exception:                    # noqa: BLE001
                pass
        if self._highlighter is not None:
            self._highlighter.stop()
        self._on_stop()

    def set_source(self, path) -> None:
        p = getattr(self, "_player", None)
        if p is not None:
            p.set_source(path)

    def closeEvent(self, event):                 # noqa: N802 (Qt override)
        self._handle_stop()
        if getattr(self, "_on_close", None) is not None:
            try:
                self._on_close()
            except Exception:                    # noqa: BLE001
                pass
        super().closeEvent(event)
