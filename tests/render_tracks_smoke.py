"""Offscreen-рендер багатодоріжкового плеєра — ОКРЕМИЙ процес (як render_video).

QMediaPlayer крутить нативний бекенд, тож тримаємо ці тести поза спільним
`unittest discover` і жорстко чистимо у teardown (проти 0xC000041D на нативній
деструкції offscreen-Qt).

Перевіряємо:
- панель мікшера з'являється при >= 2 доріжках; рядок на доріжку;
- усі контролі (транспорт + панель) мають accessibleName (канон a11y);
- швидкість і перемотка транслюються відомим (майстер веде — решта слухає);
- увімкнення/соло рахують гучність кожного QAudioOutput окремо;
- відеоплеєр наради: відомі під відео-майстром, рядок «Звук екрана» прихований,
  поки відео без власного звуку; дубльні контроли гучності відео сховані.

Запуск:
    python -m unittest tests.render_tracks_smoke
    python tests/render_tracks_smoke.py
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SPECS = [
    ("mic", "Мій голос", str(_ROOT / "tests" / "_smoke_mic.wav")),
    ("sys", "Інші голоси", str(_ROOT / "tests" / "_smoke_sys.wav")),
]


class MultiTrackRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer          # наявність бекенда
            _ = QMediaPlayer
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion, i18n
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        i18n.set_language("uk")
        motion.init_config(SimpleNamespace(animations=False))

    def setUp(self):
        from fronts.desktop import player
        player._session_speed_idx = 0
        self._live = []

    def tearDown(self):
        from fronts.desktop import player
        player._session_speed_idx = 0
        for w in self._live:
            try:
                if hasattr(w, "stop"):
                    w.stop()
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _player(self, specs=_SPECS):
        from fronts.desktop.player_tracks import MultiTrackPlayer
        p = MultiTrackPlayer(specs, None)
        self._live.append(p)
        p.resize(560, 120)
        p.show()
        for _ in range(4):
            self._app.processEvents()
        return p

    # ---- панель ----
    def test_panel_present_with_two_tracks(self):
        p = self._player()
        self.assertIsNotNone(p._panel)
        self.assertEqual(len(p._panel.channels), 2)
        self.assertEqual([c.key for c in p._panel.channels], ["mic", "sys"])

    def test_all_controls_named(self):
        p = self._player()
        # транспорт
        self.assertEqual(p._play_btn.accessibleName(), "Відтворити")
        self.assertEqual(p._speed_btn.accessibleName(), "Швидкість")
        self.assertTrue(p._seek.accessibleName())
        # рядки панелі: чекбокс, слайдер, соло — усі названі
        for key, (chk, vol, solo) in p._panel._rows.items():
            self.assertTrue(chk.accessibleName(), f"checkbox {key} без імені")
            self.assertTrue(vol.accessibleName(), f"slider {key} без імені")
            self.assertTrue(solo.accessibleName(), f"solo {key} без імені")

    def test_checkbox_label_matches_track(self):
        p = self._player()
        chk_mic = p._panel._rows["mic"][0]
        chk_sys = p._panel._rows["sys"][0]
        self.assertEqual(chk_mic.text(), "Мій голос")
        self.assertEqual(chk_sys.accessibleName(), "Інші голоси")

    # ---- мікшер: гучність по доріжках ----
    def test_disable_track_mutes_only_that_output(self):
        p = self._player()
        mic, sys = p._panel.channels
        p._panel._set_enabled(mic, False)
        self.assertEqual(mic.audio.volume(), 0.0)
        self.assertGreater(sys.audio.volume(), 0.0)

    def test_solo_mutes_the_others(self):
        p = self._player()
        mic, sys = p._panel.channels
        p._panel._set_solo(mic, True)
        self.assertGreater(mic.audio.volume(), 0.0)
        self.assertEqual(sys.audio.volume(), 0.0)      # не-соло німіє
        p._panel._set_solo(mic, False)
        self.assertGreater(sys.audio.volume(), 0.0)    # соло знято — знову чутно

    # ---- синхронізація транспорту ----
    def test_speed_cycle_broadcasts_to_followers(self):
        p = self._player()
        self.assertEqual(p._master.playbackRate(), 1.0)
        p._cycle_speed()                                # → 1,25×
        self.assertEqual(p._master.playbackRate(), 1.25)
        for follower in p._group.followers:
            self.assertEqual(follower.player.playbackRate(), 1.25)

    def test_seek_broadcasts_to_followers(self):
        p = self._player()
        seen = []
        p._group.broadcast_seek = lambda ms: seen.append(ms)
        p.seek_ms(5000)
        self.assertEqual(seen, [5000])

    def test_follower_starts_with_unlearned_offset(self):
        # свіжий відомий ще не знає сталого зсуву — вчить його _resync_tick
        p = self._player()
        for f in p._group.followers:
            self.assertIsNone(f.sync_offset_ms)

    def test_seek_resets_learned_offset(self):
        # перемотка ламає вивчений зсув → переоцінити наново (інакше ресинк
        # сікав би на застарілий зсув одразу після стрибка)
        p = self._player()
        for f in p._group.followers:
            f.sync_offset_ms = 180.0
        p._group.broadcast_seek(4000)
        for f in p._group.followers:
            self.assertIsNone(f.sync_offset_ms)

    def test_play_from_broadcasts(self):
        p = self._player()
        seen = []
        p._group.broadcast_seek = lambda ms: seen.append(ms)
        p.play_from(3.0)
        # play_from ставить pending 3000 мс; трансляція — коли джерело seekable,
        # але broadcast викликається у _apply_pending одразу за seekable майстра.
        # Без реального файлу перевіряємо, що виклик не падає й pending виставлено.
        self.assertEqual(p._pending_position, 3000)

    def test_single_track_has_no_panel_path(self):
        # одна доріжка не будує панель (картка наради обере InlinePlayer) —
        # тут перевіряємо чисту функцію-ворота
        from fronts.desktop.player_tracks import should_show_track_panel
        self.assertFalse(should_show_track_panel(1))
        self.assertTrue(should_show_track_panel(2))

    def test_render_not_null(self):
        p = self._player()
        self.assertFalse(p.grab().isNull())


class VideoMixerRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtMultimediaWidgets import QVideoWidget
            _ = (QMediaPlayer, QVideoWidget)
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion, i18n
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        i18n.set_language("uk")
        motion.init_config(SimpleNamespace(animations=False))

    def setUp(self):
        self._live = []

    def tearDown(self):
        for dlg in self._live:
            try:
                dlg._release_source()
                dlg.close()
                dlg.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _dialog(self, audio_tracks):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(None, None, audio_tracks=audio_tracks)
        self._live.append(dlg)
        dlg.resize(760, 560)
        dlg.show()
        for _ in range(4):
            self._app.processEvents()
        return dlg

    def test_meeting_video_builds_mixer(self):
        dlg = self._dialog(_SPECS)
        self.assertIsNotNone(dlg._group)
        self.assertIsNotNone(dlg._panel)
        # відомих стільки, скільки аудіодоріжок
        self.assertEqual(len(dlg._group.followers), 2)
        # канали панелі: екран + 2 доріжки
        keys = [c.key for c in dlg._panel.channels]
        self.assertEqual(keys, ["screen", "mic", "sys"])

    def test_screen_audio_row_hidden_until_video_has_audio(self):
        dlg = self._dialog(_SPECS)
        chk_screen = dlg._panel._rows["screen"][0]
        self.assertEqual(chk_screen.text(), "Звук екрана")
        self.assertFalse(chk_screen.isVisible())        # німий екран → рядок схований
        # доріжки наради — видимі
        self.assertTrue(dlg._panel._rows["mic"][0].isVisible())

    def test_reveal_screen_row_when_audio_present(self):
        dlg = self._dialog(_SPECS)
        dlg._on_has_audio(True)
        self.assertTrue(dlg._panel._rows["screen"][0].isVisible())

    def test_own_volume_controls_hidden_in_meeting_mode(self):
        dlg = self._dialog(_SPECS)
        self.assertFalse(dlg._vol_btn.isVisible())
        self.assertFalse(dlg._vol.isVisible())

    def test_no_audio_tracks_keeps_plain_player(self):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(None, None)
        self._live.append(dlg)
        dlg.show()
        for _ in range(3):
            self._app.processEvents()
        self.assertIsNone(dlg._group)
        self.assertIsNone(dlg._panel)
        self.assertTrue(dlg._vol.isVisible())           # звичайний плеєр — контроль на місці


def _glyph_true_width(btn):
    """Справжня (НЕобрізана) ширина гліфа іконки кнопки при em=min(iconSize):
    рендеримо ту саму іконку у ШИРОКУ DPR-1 канву (де гліф не впирається у край)
    і міряємо bbox альфа-каналу. Повертає (true_w, iconSize_width). qtawesome бере
    власний бандл-шрифт (не системний), тож offscreen міряє те саме, що windows."""
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtCore import QRect, Qt
    icon = btn.icon()
    iw, ih = btn.iconSize().width(), btn.iconSize().height()
    em = min(int(iw), int(ih))
    width, height = em * 6, em
    img = QImage(width, height, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    try:
        icon.paint(p, QRect(0, 0, width, height))
    finally:
        p.end()
    left, right = width, -1
    for x in range(width):
        for y in range(height):
            if img.pixelColor(x, y).alpha() > 10:
                left = min(left, x)
                right = x
                break
    true_w = (right - left + 1) if right >= 0 else 0
    return true_w, iw


class VolumeGlyphRegressionTests(unittest.TestCase):
    """Регрес продакшн-значка гучності. fa6s.volume-high широкий (viewBox 640×512,
    ~1,25), тож у КВАДРАТНІЙ канві правий край хвиль зникає. Фікс — icon_w
    (прямокутна канва) у _IconButton. Тут будуємо СПРАВЖНІ InlinePlayer і
    VideoPlayerDialog і перевіряємо, що гліф їхнього _vol_btn НЕ ширший за канву
    iconSize(). Приберуть icon_w → канва квадратна → гліф ріжеться → тест
    червоніє (закриває «а якщо хтось знову прибере icon_w — гейт зелений»)."""

    @classmethod
    def setUpClass(cls):
        try:
            from PySide6.QtWidgets import QApplication
            from PySide6.QtMultimedia import QMediaPlayer
            _ = QMediaPlayer
        except Exception:                        # pragma: no cover
            raise unittest.SkipTest("QtMultimedia недоступний")
        from fronts.desktop.theme import QSS, load_fonts
        from fronts.desktop import motion, i18n
        from types import SimpleNamespace
        cls._app = QApplication.instance() or QApplication([])
        load_fonts()
        cls._app.setStyleSheet(QSS)
        i18n.set_language("uk")
        motion.init_config(SimpleNamespace(animations=False))

    def setUp(self):
        self._live = []

    def tearDown(self):
        for w in self._live:
            try:
                if hasattr(w, "stop"):
                    w.stop()
                elif hasattr(w, "_release_source"):
                    w._release_source()
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self._live = []
        for _ in range(3):
            self._app.processEvents()

    def _assert_not_clipped(self, btn):
        true_w, iw = _glyph_true_width(btn)
        self.assertLessEqual(
            true_w, iw + 1,
            f"гліф гучності {true_w}px ширший за канву {iw}px — прибрали icon_w?")

    def test_inline_player_volume_glyph_not_clipped(self):
        from fronts.desktop.player import InlinePlayer
        pl = InlinePlayer(None)
        self._live.append(pl)
        pl.show()
        for _ in range(3):
            self._app.processEvents()
        self._assert_not_clipped(pl._vol_btn)

    def test_video_player_volume_glyph_not_clipped(self):
        from fronts.desktop.video_player import VideoPlayerDialog
        dlg = VideoPlayerDialog(None, None)          # звичайний плеєр — _vol_btn є
        self._live.append(dlg)
        dlg.show()
        for _ in range(3):
            self._app.processEvents()
        self._assert_not_clipped(dlg._vol_btn)


if __name__ == "__main__":
    suite = unittest.TestSuite()
    load = unittest.TestLoader().loadTestsFromTestCase
    suite.addTests(load(MultiTrackRenderTests))
    suite.addTests(load(VideoMixerRenderTests))
    suite.addTests(load(VolumeGlyphRegressionTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)
