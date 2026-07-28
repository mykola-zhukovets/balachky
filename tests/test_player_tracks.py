"""feature/player-tracks — чиста логіка синхронного багатодоріжкового плеєра.

Поріг ресинку, поява панелі (лише >= 2 доріжок) і обчислення гучності
(увімкнення/соло) — без Qt і без QtMultimedia, тож безпечно у спільному
`unittest discover`. Рендер панелі (accessibleName, транспорт) і трансляцію
транспорту відомим перевіряє render_tracks_smoke.py (окремий процес — живе Qt).
"""
import os
import unittest
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fronts.desktop.player_tracks import (
    OFFSET_SEED_MAX_MS, RESYNC_TOLERANCE_MS, FollowerGroup, blend_offset,
    effective_volume, plan_resync, resync_needed, should_show_track_panel,
)


class ResyncThresholdTests(unittest.TestCase):
    """Ресинк спрацьовує СУВОРО за дрейфу > допуску (120 мс), не на межі.

    120 мс — вище шуму репорту position() двох плеєрів (~86 мс, живий тест):
    ресинк не смикає синхронний звук, а ловить лише справжній розсинхрон."""

    def test_below_tolerance_no_resync(self):
        self.assertFalse(resync_needed(1000, 1000))
        self.assertFalse(resync_needed(1000, 1030))       # дрейф 30 мс
        self.assertFalse(resync_needed(1000, 970))

    def test_reporting_jitter_does_not_trigger(self):
        # ~86 мс — виміряний шум репорту при синхронному звуку → НЕ ресинк
        self.assertFalse(resync_needed(1000, 1086))

    def test_exactly_tolerance_no_resync(self):
        self.assertEqual(RESYNC_TOLERANCE_MS, 120)
        self.assertFalse(resync_needed(1000, 1120))       # рівно 120 — ще ні

    def test_above_tolerance_resyncs(self):
        self.assertTrue(resync_needed(1000, 1121))        # 121 мс — вже так
        self.assertTrue(resync_needed(1000, 700))         # 300 мс — справжній розсинхрон
        self.assertTrue(resync_needed(5000, 4500))        # дрейф в обидва боки

    def test_custom_tolerance(self):
        self.assertFalse(resync_needed(0, 100, tol_ms=100))
        self.assertTrue(resync_needed(0, 101, tol_ms=100))


class VideoOffsetResyncTests(unittest.TestCase):
    """Синхронізація ВІДЕО-майстра: position() відео звітує ~180 мс попереду
    аудіо (сталий зсув конвеєра, не розсинхрон). Ресинк має міряти дрейф ПОНАД
    зсув, інакше смикав би аудіо щотакту — знахідка суду 22.07."""

    def test_steady_video_lead_is_not_desync(self):
        # відео на 180 мс попереду, відомий рівно там, де має бути → НЕ ресинк
        self.assertFalse(resync_needed(1180, 1000, offset_ms=180))
        self.assertFalse(resync_needed(11180, 11000, offset_ms=180))

    def test_drift_within_tolerance_around_offset(self):
        # відомий на 100 мс від очікуваного (у межах порога) → НЕ ресинк
        self.assertFalse(resync_needed(1180, 900, offset_ms=180))
        self.assertFalse(resync_needed(1180, 1100, offset_ms=180))

    def test_real_stall_beyond_offset_resyncs(self):
        # відомий на 200 мс ПОЗА очікуваним (справжній підвис) → ресинк
        self.assertTrue(resync_needed(1180, 800, offset_ms=180))

    def test_zero_offset_is_plain_audio_behaviour(self):
        # аудіо-майстер (зсув ≈0) поводиться рівно як раніше
        self.assertFalse(resync_needed(1000, 1120, offset_ms=0))
        self.assertTrue(resync_needed(1000, 1121, offset_ms=0))


class OffsetEstimateTests(unittest.TestCase):
    """EMA-оцінка сталого зсуву майстер↔відомий."""

    def test_blend_moves_toward_measurement(self):
        self.assertAlmostEqual(blend_offset(100, 200, alpha=0.25), 125.0)

    def test_blend_of_stable_value_is_stable(self):
        self.assertAlmostEqual(blend_offset(180, 180), 180.0)

    def test_blend_converges(self):
        off = 0.0
        for _ in range(10):
            off = blend_offset(off, 180.0)
        self.assertAlmostEqual(off, 180.0, delta=4.0)   # ~3 мс залишку за 10 тактів


class PlanResyncTests(unittest.TestCase):
    """Рішення ресинку одного відомого (чиста логіка без Qt)."""

    def test_seeds_natural_pipeline_offset_without_seek(self):
        # baseline не вивчено, розрив у межах природного зсуву → вчимо, не сікаємо
        new_offset, seek_to = plan_resync(1180, 1000, None)
        self.assertAlmostEqual(new_offset, 180.0)
        self.assertIsNone(seek_to)

    def test_big_gap_hard_aligns_and_stays_unlearned(self):
        # майстер стартував не з нуля (resume 30 с), відомий із 0 → жорстко вирівняти
        new_offset, seek_to = plan_resync(30000, 100, None)
        self.assertIsNone(new_offset)
        self.assertEqual(seek_to, 30000)
        self.assertGreater(30000 - 100, OFFSET_SEED_MAX_MS)   # це справді «великий розрив»

    def test_stall_seeks_to_master_minus_offset(self):
        # вивчений зсув 180, відомий підвис → сік на (майстер−зсув), зсув збережено
        new_offset, seek_to = plan_resync(1180, 800, 180)
        self.assertEqual(new_offset, 180)
        self.assertEqual(seek_to, 1000)          # НЕ 1180 — зберігаємо природний зсув

    def test_within_tolerance_refines_offset_no_seek(self):
        new_offset, seek_to = plan_resync(1180, 1000, 180)
        self.assertIsNone(seek_to)
        self.assertAlmostEqual(new_offset, 180.0)   # рівно на місці → зсув не рухається


class AudioMasterResyncTests(unittest.TestCase):
    """Аудіо-майстер (нема відео): конвеєр не дає сталого зсуву репорту, тож
    базлайн = 0. Будь-який початковий розбіг понад поріг — СПРАВЖНЯ
    десинхронізація, її треба вирівнювати, а не приймати за «природний зсув»."""

    def test_initial_gap_aligns_not_seeded(self):
        # Ревізія №2: раніше повертало (400.0, None) — розбіг 400 мс приймався
        # за офсет для будь-якого майстра. Для аудіо це реальна десинхронізація.
        new_offset, seek_to = plan_resync(2000, 1600, None, audio_master=True)
        self.assertEqual(seek_to, 2000)             # вимагає вирівнювання на майстра
        self.assertNotEqual((new_offset, seek_to), (400, None))
        self.assertEqual(new_offset, 0.0)           # базлайн лишається 0

    def test_within_tolerance_no_seek(self):
        # малий розбіг (< поріг) — шум репорту, не чіпаємо
        new_offset, seek_to = plan_resync(2000, 1950, None, audio_master=True)
        self.assertIsNone(seek_to)
        self.assertEqual(new_offset, 0.0)

    def test_offset_pinned_zero_on_later_ticks(self):
        # на наступних тактах базлайн НЕ відпливає від 0 (без EMA-згладжування)
        new_offset, seek_to = plan_resync(2000, 1970, 0.0, audio_master=True)
        self.assertIsNone(seek_to)
        self.assertEqual(new_offset, 0.0)

    def test_video_master_default_unchanged(self):
        # відео-майстер (за замовчуванням) — стара логіка сталого зсуву
        new_offset, seek_to = plan_resync(2000, 1600, None)
        self.assertAlmostEqual(new_offset, 400.0)
        self.assertIsNone(seek_to)

    def test_resync_tick_threads_audio_master_flag(self):
        # аудіо-майстер FollowerGroup → _resync_tick мусить донести прапорець у
        # plan_resync (розбіг 400 мс → вирівнювання, а не мовчазний офсет)
        seeks = []
        follower = SimpleNamespace(
            sync_offset_ms=None,
            player=SimpleNamespace(position=lambda: 1600),
            seek=lambda ms: seeks.append(ms),
        )
        group = SimpleNamespace(
            _audio_master=True,
            _master=SimpleNamespace(position=lambda: 2000),
            _followers=[follower],
        )
        FollowerGroup._resync_tick(group)
        self.assertEqual(seeks, [2000])
        self.assertEqual(follower.sync_offset_ms, 0.0)


class BroadcastRateResetsOffsetTests(unittest.TestCase):
    """Ревізія №2: зміна швидкості мусить скинути вивчені офсети відомих —
    інакше базлайн, вивчений на 1x, стає хибним на 2x і дає вічні пере-сіки."""

    def test_rate_change_resets_all_offsets(self):
        rates = []
        f1 = SimpleNamespace(sync_offset_ms=180.0,
                             set_rate=lambda r: rates.append(("f1", r)))
        f2 = SimpleNamespace(sync_offset_ms=0.0,
                             set_rate=lambda r: rates.append(("f2", r)))
        group = SimpleNamespace(_followers=[f1, f2])
        FollowerGroup.broadcast_rate(group, 2.0)
        self.assertIsNone(f1.sync_offset_ms)        # офсети скинуто — перевчаться
        self.assertIsNone(f2.sync_offset_ms)
        self.assertEqual(rates, [("f1", 2.0), ("f2", 2.0)])   # швидкість застосовано


class PanelVisibilityTests(unittest.TestCase):
    """Панель доріжок має сенс лише коли доріжок >= 2."""

    def test_single_track_no_panel(self):
        self.assertFalse(should_show_track_panel(0))
        self.assertFalse(should_show_track_panel(1))

    def test_two_or_more_tracks_show_panel(self):
        self.assertTrue(should_show_track_panel(2))
        self.assertTrue(should_show_track_panel(3))


class EffectiveVolumeTests(unittest.TestCase):
    """Підсумкова гучність доріжки: увімкнення + логіка соло."""

    def test_enabled_no_solo_plays_at_user_volume(self):
        self.assertEqual(effective_volume(0.8, True, False, False), 0.8)

    def test_disabled_is_silent(self):
        self.assertEqual(effective_volume(0.9, False, False, False), 0.0)

    def test_solo_elsewhere_mutes_this_track(self):
        # десь активне соло, ця доріжка не соло → німо, навіть якщо ввімкнена
        self.assertEqual(effective_volume(0.9, True, False, True), 0.0)

    def test_this_track_soloed_plays(self):
        self.assertEqual(effective_volume(0.7, True, True, True), 0.7)

    def test_disabled_soloed_still_silent(self):
        # знятий чекбокс сильніший за соло — доріжка все одно німа
        self.assertEqual(effective_volume(0.9, False, True, True), 0.0)


if __name__ == "__main__":
    unittest.main()
