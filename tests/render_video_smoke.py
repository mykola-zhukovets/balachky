"""Offscreen-рендер відеоплеєра — ОКРЕМИЙ процес (як render_screen_smoke):
QMediaPlayer/QVideoWidget крутять нативний бекенд, тож тримаємо їх поза спільним
`unittest discover` і жорстко чистимо у teardown, щоб не було 0xC000041D на
нативній деструкції offscreen-Qt.

Перевіряємо:
- усі контролі присутні й мають accessibleName (канон a11y);
- pitchCompensation увімкнено (тон не пливе на 0,5-2×);
- відсутній/битий файл → людський банер, кнопка play вимкнена, БЕЗ краху.

Запуск:
    python -m unittest tests.render_video_smoke
    python tests/render_video_smoke.py
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _write_tiny_mp4(path) -> None:
    """3-секундний 64×64 mp4 через PyAV, кодек libvpx-vp9 — той самий кодек,
    що вже в проєкті для запису екрана наради (whisper_core/screen/recorder.py,
    whisper_core/meeting/screen_record.py; libx264 навмисно уникають через GPL) —
    реальний декодований файл, а НЕ мок, бо мутаційний тест перемотки мусить
    бачити СПРАВЖНЮ зміну позиції плеєра, не виклик підробленого об'єкта."""
    import av
    import numpy as np
    container = av.open(str(path), mode="w")
    stream = container.add_stream("libvpx-vp9", rate=25)
    stream.width = 64
    stream.height = 64
    stream.pix_fmt = "yuv420p"
    for i in range(75):                              # 75 кадрів @ 25fps = 3с
        frame = av.VideoFrame.from_ndarray(
            np.full((64, 64, 3), i % 255, dtype=np.uint8), format="rgb24")
        for packet in stream.encode(frame):
            container.mux(packet)
    for packet in stream.encode():
        container.mux(packet)
    container.close()


class VideoPlayerRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer          # наявність бекенда
            from PySide6.QtMultimediaWidgets import QVideoWidget   # наявність полотна
            _ = (QMediaPlayer, QVideoWidget)
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

    def setUp(self):
        from fronts.desktop.i18n import current_language, set_language
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self._live = []

    def tearDown(self):
        for dlg in self._live:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _dialog(self, path=None):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(path, None)
        self._live.append(dlg)
        dlg.resize(760, 540)
        dlg.show()
        for _ in range(4):
            self._app.processEvents()
        return dlg

    def test_controls_present_and_named(self):
        dlg = self._dialog()
        self.assertIsNotNone(dlg._video)
        self.assertEqual(dlg._video.accessibleName(), "Відео")
        self.assertEqual(dlg._play_btn.accessibleName(), "Відтворити")
        self.assertEqual(dlg._speed_btn.accessibleName(), "Швидкість")
        self.assertEqual(dlg._seek.accessibleName(), "Позиція відтворення")
        self.assertEqual(dlg._vol.accessibleName(), "Гучність")
        self.assertEqual(dlg._speed_label(), "1×")

    def test_pitch_compensation_enabled(self):
        # Головна вимога MVP: тон не «бурундук» на 0,5-2×.
        dlg = self._dialog()
        self.assertTrue(dlg._player.pitchCompensation())

    def test_speed_cycle_sets_playback_rate(self):
        dlg = self._dialog()
        self.assertEqual(dlg._player.playbackRate(), 1.0)
        dlg._cycle_speed()                       # → 1,25×
        self.assertEqual(dlg._player.playbackRate(), 1.25)
        for _ in range(3):                       # 1,5× → 2× → 0,5×
            dlg._cycle_speed()
        self.assertEqual(dlg._player.playbackRate(), 0.5)
        self.assertEqual(dlg._speed_label(), "0,5×")

    def test_missing_file_shows_human_error_no_crash(self):
        dlg = self._dialog(str(_ROOT / "tests" / "no_such_video.mp4"))
        # showEvent із неіснуючим файлом синхронно показує банер (os.path.exists)
        self.assertTrue(dlg._status.isVisible())
        self.assertIn("Не вдалося відтворити відео", dlg._status.text())
        self.assertFalse(dlg._play_btn.isEnabled())

    def test_render_not_null(self):
        dlg = self._dialog()
        self.assertFalse(dlg.grab().isNull())

    # ---- повний екран (feature/fullscreen-player) ----
    # Головна перевірка цієї роботи: вихід із повного екрана НЕ повинен чорнити
    # відео. Пастка Qt — реперентинг QVideoWidget між контейнерами руйнує
    # поверхню відтворення. Тест ловить це напряму: батько відеовіджета і сам
    # об'єкт віджета мають лишатися ТИМИ САМИМИ до/після перемикання, а плеєр
    # має й далі вказувати videoOutput на той самий віджет.
    def test_fullscreen_toggle_keeps_same_video_widget_and_parent(self):
        dlg = self._dialog()
        original_video = dlg._video
        original_parent = dlg._video.parentWidget()
        self.assertIs(original_parent, dlg)

        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertTrue(dlg._is_fullscreen)
        self.assertTrue(dlg.isFullScreen())
        self.assertIs(dlg._video, original_video)
        self.assertIs(dlg._video.parentWidget(), original_parent)
        self.assertIs(dlg._player.videoOutput(), dlg._video)

        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertFalse(dlg._is_fullscreen)
        self.assertFalse(dlg.isFullScreen())
        self.assertIs(dlg._video, original_video)
        self.assertIs(dlg._video.parentWidget(), original_parent)
        self.assertIs(dlg._player.videoOutput(), dlg._video)
        # діалог не закрився — просто вийшов із повного екрана
        self.assertTrue(dlg.isVisible())

    def test_fullscreen_button_toggles_icon_and_tooltip(self):
        dlg = self._dialog()
        self.assertEqual(dlg._fs_btn.toolTip(), "На весь екран")
        dlg._fs_btn.click()
        self._app.processEvents()
        self.assertTrue(dlg._is_fullscreen)
        self.assertEqual(dlg._fs_btn.toolTip(), "Вийти з повного екрана")
        dlg._fs_btn.click()
        self._app.processEvents()
        self.assertFalse(dlg._is_fullscreen)
        self.assertEqual(dlg._fs_btn.toolTip(), "На весь екран")

    def test_double_click_on_video_toggles_fullscreen(self):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        dlg = self._dialog()
        pos = QPointF(dlg._video.rect().center())
        ev = QMouseEvent(QEvent.MouseButtonDblClick, pos, pos,
                          Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        self._app.sendEvent(dlg._video, ev)
        self.assertTrue(dlg._is_fullscreen)

        ev2 = QMouseEvent(QEvent.MouseButtonDblClick, pos, pos,
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
        self._app.sendEvent(dlg._video, ev2)
        self.assertFalse(dlg._is_fullscreen)

    def test_f_and_f11_keys_toggle_fullscreen(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        dlg = self._dialog()
        QTest.keyClick(dlg, Qt.Key_F11)
        self.assertTrue(dlg._is_fullscreen)
        QTest.keyClick(dlg, Qt.Key_F11)
        self.assertFalse(dlg._is_fullscreen)

        QTest.keyClick(dlg, Qt.Key_F)
        self.assertTrue(dlg._is_fullscreen)
        QTest.keyClick(dlg, Qt.Key_F)
        self.assertFalse(dlg._is_fullscreen)

    def test_escape_exits_fullscreen_without_closing_dialog(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        dlg = self._dialog()
        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertTrue(dlg._is_fullscreen)

        QTest.keyClick(dlg, Qt.Key_Escape)
        self._app.processEvents()
        self.assertFalse(dlg._is_fullscreen)
        self.assertFalse(dlg.isFullScreen())
        self.assertTrue(dlg.isVisible())    # Esc вийшов із fullscreen, НЕ закрив діалог

    def test_no_error_banner_appears_from_fullscreen_toggle_alone(self):
        """Перемикання повного екрана саме по собі не повинно спричиняти
        помилку відтворення (непрямий доказ «не чорніє»: якби реперентинг
        ламав QVideoSink, бекенд Windows видав би errorOccurred/чорний банер)."""
        dlg = self._dialog()
        dlg._toggle_fullscreen()      # enter
        self._app.processEvents()
        dlg._toggle_fullscreen()      # exit
        self._app.processEvents()
        self.assertFalse(dlg._status.isVisible())


class VideoTranscriptPanelRenderTests(unittest.TestCase):
    """Етап 2 «Єдиного робочого екрана наради» (feature/meeting-video-text):
    розшифровка поруч із відео, клацання по репліці перемотує відео, активна
    репліка підсвічується, повний екран ховає й панель тексту. МУТАЦІЙНИЙ
    тест перемотки ганяється на СПРАВЖНЬОМУ QMediaPlayer + реальному mp4
    (``_write_tiny_mp4``) — перевіряємо фактичну ``player.position()``, а
    не виклик мока."""

    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
            import av
            _ = (QMediaPlayer, QVideoWidget, av)           # лише перевірка наявності
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia або PyAV недоступні")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

        import tempfile
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._video_path = str(Path(cls._tmpdir.name) / "tiny.mp4")
        _write_tiny_mp4(cls._video_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        from fronts.desktop.i18n import current_language, set_language
        from whisper_core.meeting import postprocess as mpost
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self._live = []
        # Три репліки в межах 3-секундного відео — друга й третя дають чіткі
        # неспівпадаючі цілі перемотки для мутаційного тесту.
        self._utterances = [
            mpost.Utterance(0.0, 1.0, mpost.SPK_SINGLE, "Перша репліка"),
            mpost.Utterance(1.0, 2.0, mpost.SPK_SINGLE, "Друга репліка"),
            mpost.Utterance(2.0, 3.0, mpost.SPK_SINGLE, "Третя репліка"),
        ]

    def tearDown(self):
        for dlg in self._live:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _dialog(self, utterances=None, speaker_names=None):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(
            self._video_path, None,
            utterances=utterances if utterances is not None else self._utterances,
            speaker_names=speaker_names)
        self._live.append(dlg)
        dlg.resize(1200, 700)
        dlg.show()
        self._wait_for_duration(dlg)
        # Пауза одразу після завантаження: інакше відео саме грає далі під
        # час очікування, і позиція «пливе» повз ціль перемотки — це не
        # похибка seek, а звичайне подальше відтворення в тому ж вікні тесту.
        dlg._player.pause()
        self._app.processEvents()
        return dlg

    def _wait_for_duration(self, dlg, timeout_ms=4000):
        from PySide6.QtCore import QEventLoop, QTimer
        if dlg._player.duration() > 0:
            self._app.processEvents()
            return
        loop = QEventLoop()
        dlg._player.durationChanged.connect(loop.quit)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        for _ in range(3):
            self._app.processEvents()

    def _pump(self, ms=300):
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    # ---- розкладка ----
    def test_utterances_build_splitter_with_transcript_panel(self):
        from PySide6.QtWidgets import QSplitter
        dlg = self._dialog()
        self.assertIsInstance(dlg._splitter, QSplitter)
        self.assertIsNotNone(dlg._transcript_panel)
        # Відео — дитина лівого контейнера спліттера, встановлена ОДИН раз
        # при побудові; критично, щоб це не змінювалось потім (перевіряє
        # окремий тест на повний екран).
        self.assertIsNotNone(dlg._video.parentWidget())

    def test_no_utterances_keeps_plain_layout_no_regression(self):
        dlg = self._dialog(utterances=[])
        self.assertIsNone(dlg._splitter)
        self.assertIsNone(dlg._transcript_panel)

    # ---- МУТАЦІЯ: клацання по репліці справді перемотує ВІДЕО ----
    def test_click_on_utterance_row_seeks_video_to_its_actual_start(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        dlg = self._dialog()
        view = dlg._transcript_panel._view
        index = dlg._transcript_panel._model.index(2, 0)   # «Третя репліка», старт 2.0с
        rect = view.visualRect(index)
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=rect.center())
        self._pump()

        # Перевіряємо ФАКТИЧНУ позицію справжнього QMediaPlayer, не виклик мока.
        self.assertAlmostEqual(dlg._player.position(), 2000, delta=250)

        # Друга ціль — переконатися, що це не «завжди 2000», а справді читає
        # старт клацнутої репліки.
        index0 = dlg._transcript_panel._model.index(0, 0)   # «Перша репліка», старт 0
        rect0 = view.visualRect(index0)
        QTest.mouseClick(view.viewport(), Qt.LeftButton, pos=rect0.center())
        self._pump()
        self.assertAlmostEqual(dlg._player.position(), 0, delta=250)

    # ---- зворотний зв'язок: підсвічування активної репліки ----
    def test_playing_position_highlights_matching_utterance_row(self):
        dlg = self._dialog()
        dlg._on_position(1500)      # належить другій репліці (1.0–2.0с)
        self.assertEqual(dlg._transcript_panel._model._active_row, 1)
        dlg._on_position(50)        # належить першій (0.0–1.0с)
        self.assertEqual(dlg._transcript_panel._model._active_row, 0)

    def test_gap_between_utterances_clears_highlight(self):
        from whisper_core.meeting import postprocess as mpost
        from fronts.desktop.meeting_transcript_panel import _ACTIVE_WORD_RANGE_ROLE
        gapped = [
            mpost.Utterance(0.0, 1.0, mpost.SPK_SINGLE, "Перша"),
            mpost.Utterance(2.0, 3.0, mpost.SPK_SINGLE, "Друга"),
        ]
        dlg = self._dialog(utterances=gapped)
        dlg._on_position(1500)      # 1.0-2.0с — пауза між репліками
        model = dlg._transcript_panel._model
        self.assertEqual(model._active_row, -1)
        # У паузі МІЖ репліками жодне слово не підсвічене (немає активного
        # рядка взагалі — role повертає None на будь-якому індексі).
        self.assertIsNone(model.index(0, 0).data(_ACTIVE_WORD_RANGE_ROLE))
        self.assertIsNone(model.index(1, 0).data(_ACTIVE_WORD_RANGE_ROLE))

    # ---- Етап 3: підсвічування ОКРЕМОГО слова в міру відтворення ----
    def test_playing_position_highlights_matching_word_within_active_utterance(self):
        from fronts.desktop.meeting_transcript_panel import _ACTIVE_WORD_RANGE_ROLE
        dlg = self._dialog()      # «Перша репліка» 0.0-1.0с — 2 слова, по 500мс
        model = dlg._transcript_panel._model
        idx0 = model.index(0, 0)

        dlg._on_position(100)     # перше слово: "Перша"
        span_first = idx0.data(_ACTIVE_WORD_RANGE_ROLE)
        self.assertEqual(span_first, (0, len("Перша")))

        dlg._on_position(700)     # друге слово: "репліка"
        span_second = idx0.data(_ACTIVE_WORD_RANGE_ROLE)
        self.assertEqual(span_second, (len("Перша ") , len("репліка")))
        self.assertNotEqual(span_first, span_second)

    def test_word_highlight_toggle_disables_and_reenables_active_word(self):
        from fronts.desktop.meeting_transcript_panel import _ACTIVE_WORD_RANGE_ROLE
        dlg = self._dialog()
        model = dlg._transcript_panel._model
        idx0 = model.index(0, 0)

        dlg._on_position(100)
        self.assertIsNotNone(idx0.data(_ACTIVE_WORD_RANGE_ROLE))

        dlg._transcript_panel._word_hilite_btn.setChecked(False)
        self.assertIsNone(idx0.data(_ACTIVE_WORD_RANGE_ROLE))
        # Рядок лишається активним — вимикається лише підсвітка СЛОВА.
        self.assertEqual(model._active_row, 0)

        dlg._transcript_panel._word_hilite_btn.setChecked(True)
        self.assertIsNotNone(idx0.data(_ACTIVE_WORD_RANGE_ROLE))

    def test_delegate_draws_active_word_via_qtextlayout_without_crash(self):
        from PySide6.QtGui import QPainter, QPixmap
        from PySide6.QtCore import QRect
        from fronts.desktop.meeting_transcript_panel import _UtteranceDelegate
        delegate = _UtteranceDelegate()
        pixmap = QPixmap(300, 78)
        pixmap.fill()
        painter = QPainter(pixmap)
        body_rect = QRect(10, 30, 280, 40)
        delegate._draw_body(painter, body_rect, "Перше слово тут", (0, 5))
        painter.end()
        img = pixmap.toImage()
        background = img.pixelColor(0, 0)
        painted = any(
            img.pixelColor(x, y) != background
            for x in range(0, 300, 4) for y in range(0, 78, 4))
        self.assertTrue(painted, "QTextLayout нічого не намалював у body_rect")

    # ---- повний екран ховає й панель тексту ----
    def test_fullscreen_hides_transcript_panel_and_restores_on_exit(self):
        dlg = self._dialog()
        self.assertTrue(dlg._transcript_panel.isVisible())
        original_parent = dlg._video.parentWidget()
        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertFalse(dlg._transcript_panel.isVisible())
        self.assertIs(dlg._video.parentWidget(), original_parent)   # батько не змінився
        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertTrue(dlg._transcript_panel.isVisible())

    def test_manually_collapsed_panel_stays_hidden_after_fullscreen_roundtrip(self):
        dlg = self._dialog()
        dlg._toggle_transcript_panel()     # користувач сам згорнув панель
        self.assertFalse(dlg._transcript_panel.isVisible())
        dlg._toggle_fullscreen()
        self._app.processEvents()
        dlg._toggle_fullscreen()
        self._app.processEvents()
        self.assertFalse(dlg._transcript_panel.isVisible())   # лишилась згорнутою

    def test_toggle_button_shows_and_hides_panel(self):
        dlg = self._dialog()
        self.assertTrue(dlg._transcript_panel.isVisible())
        dlg._transcript_btn.click()
        self.assertFalse(dlg._transcript_panel.isVisible())
        dlg._transcript_btn.click()
        self.assertTrue(dlg._transcript_panel.isVisible())

    def test_transcript_view_accessible_name(self):
        dlg = self._dialog()
        self.assertEqual(dlg._transcript_panel._view.accessibleName(), "Розшифровка")


class VideoPlayerReplayRenderTests(unittest.TestCase):
    """Аудит чесності (31.07, знахідка 3): після природного кінця відео
    кнопка «відтворити» мовчки нічого не робила — `_toggle` перевіряв
    ``player.source().isEmpty()``, але кінець медіа НІКОЛИ не звільняв
    джерело (не було обробника ``mediaStatusChanged``/``EndOfMedia``, на
    відміну від аудіо-плеєра player.py). МУТАЦІЙНИЙ тест ганяється на
    СПРАВЖНЬОМУ QMediaPlayer + реальному mp4 (``_write_tiny_mp4``) — ловимо
    фактичний ``playbackState()``/``position()``, а не виклик мока."""

    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
            import av
            _ = (QMediaPlayer, QVideoWidget, av)
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia або PyAV недоступні")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        motion.init_config(SimpleNamespace(animations=False))

        import tempfile
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls._video_path = str(Path(cls._tmpdir.name) / "tiny_replay.mp4")
        _write_tiny_mp4(cls._video_path)

    @classmethod
    def tearDownClass(cls):
        cls._tmpdir.cleanup()

    def setUp(self):
        from fronts.desktop.i18n import current_language, set_language
        self._language = current_language()
        self.addCleanup(set_language, self._language)
        set_language("uk")
        self._live = []

    def tearDown(self):
        for dlg in self._live:
            try:
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _dialog(self):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(self._video_path, None)
        self._live.append(dlg)
        dlg.resize(760, 540)
        dlg.show()
        for _ in range(4):
            self._app.processEvents()
        return dlg

    def _wait_for_duration(self, dlg, timeout_ms=4000):
        from PySide6.QtCore import QEventLoop, QTimer
        if dlg._player.duration() > 0:
            self._app.processEvents()
            return
        loop = QEventLoop()
        dlg._player.durationChanged.connect(loop.quit)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        for _ in range(3):
            self._app.processEvents()

    def _wait_for_end_of_media(self, dlg, timeout_ms=8000):
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtMultimedia import QMediaPlayer
        loop = QEventLoop()

        def _on_status(status):
            if status == QMediaPlayer.EndOfMedia:
                loop.quit()

        dlg._player.mediaStatusChanged.connect(_on_status)
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        dlg._player.mediaStatusChanged.disconnect(_on_status)
        for _ in range(3):
            self._app.processEvents()

    def test_play_button_restarts_playback_after_natural_end(self):
        from PySide6.QtMultimedia import QMediaPlayer

        dlg = self._dialog()
        self._wait_for_duration(dlg)
        # стрибок майже в кінець 3-секундного ролика — коротке очікування
        # до природного EndOfMedia, а не повне 3с відтворення з нуля.
        dlg._player.setPosition(max(0, dlg._player.duration() - 250))
        dlg._player.play()
        self._wait_for_end_of_media(dlg)

        # Факт кінця: джерело звільнене, повзунок скинутий (ідіом
        # player.py._on_media_status, перенесений у video_player).
        self.assertTrue(dlg._player.source().isEmpty())
        self.assertEqual(dlg._seek.fraction(), 0.0)
        self.assertNotEqual(dlg._player.playbackState(), QMediaPlayer.PlayingState)

        # МУТАЦІЯ-ціль: натискання «відтворити» після кінця мусить фактично
        # почати відтворення з початку, а не тихо нічого не робити.
        dlg._toggle()
        for _ in range(6):
            self._app.processEvents()
        self._pump(500)
        self.assertEqual(dlg._player.playbackState(), QMediaPlayer.PlayingState)
        self.assertLess(dlg._player.position(), dlg._player.duration() - 500)

    def _pump(self, ms=300):
        from PySide6.QtCore import QEventLoop, QTimer
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()


if __name__ == "__main__":
    _loader = unittest.TestLoader()
    _suite = unittest.TestSuite([
        _loader.loadTestsFromTestCase(VideoPlayerRenderTests),
        _loader.loadTestsFromTestCase(VideoTranscriptPanelRenderTests),
        _loader.loadTestsFromTestCase(VideoPlayerReplayRenderTests),
    ])
    result = unittest.TextTestRunner(verbosity=2).run(_suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
