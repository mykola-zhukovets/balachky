"""Контракти пакета медіа-зручностей; без Qt і без аудіопристроїв."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from whisper_core.meeting.media import (available_formats, export_audio,
                                        export_balanced_wav, mix_tracks,
                                        read_wav, soft_limit, timestamp_range,
                                        write_wav)
from whisper_core.meeting.session import MeetingMeta, add_bookmark, create_session, load_meta


class BookmarkRoundTripTests(unittest.TestCase):
    def test_bookmarks_round_trip_in_meeting_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            sess = create_session(Path(tmp), ["mic"])
            add_bookmark(sess.dir, 12.3456, "Домовились про реліз")
            fresh = load_meta(sess.dir)
            self.assertEqual(fresh.bookmarks, [{"timestamp": 12.346, "title": "Домовились про реліз"}])
            self.assertEqual(MeetingMeta.from_json(fresh.to_json()).bookmarks, fresh.bookmarks)


class MixTests(unittest.TestCase):
    def test_mix_preserves_longest_track_and_never_clips(self):
        mixed = mix_tracks([np.full(5, 0.8, dtype=np.float32), np.full(3, 0.8, dtype=np.float32)])
        self.assertEqual(len(mixed), 5)
        self.assertLessEqual(float(np.max(np.abs(mixed))), 1.0)
        self.assertAlmostEqual(float(mixed[-1]), 0.8, places=5)

    def test_two_loud_tracks_stay_below_ceiling(self):
        # Дві гучні доріжки одночасно (сума 1.8) → лімітер утримує піки < 1.0.
        loud = np.full(64, 0.9, dtype=np.float32)
        mixed = mix_tracks([loud, loud])
        self.assertLess(float(np.max(np.abs(mixed))), 1.0)

    def test_quiet_pair_is_unchanged_linear_sum(self):
        # Тиха пара (сума 0.5 < поріг) не чіпається — чиста лінійна сума.
        a = np.full(16, 0.2, dtype=np.float32)
        b = np.full(16, 0.3, dtype=np.float32)
        mixed = mix_tracks([a, b])
        np.testing.assert_allclose(mixed, np.full(16, 0.5, dtype=np.float32), atol=1e-6)

    def test_soft_limit_preserves_sign_and_below_threshold(self):
        x = np.array([-0.9, -0.1, 0.0, 0.1, 0.9], dtype=np.float32)
        out = soft_limit(x)
        # Підпорогові значення не змінені; надпорогові стиснуті, знак збережено.
        self.assertAlmostEqual(float(out[1]), -0.1, places=6)
        self.assertAlmostEqual(float(out[3]), 0.1, places=6)
        self.assertLess(out[0], 0.0)
        self.assertGreater(out[4], 0.0)
        self.assertLessEqual(float(np.max(np.abs(out))), 0.999)

    def test_timestamp_to_frame_range_is_clamped(self):
        self.assertEqual(timestamp_range(-1, 9, 10, 35), (0, 35))
        self.assertEqual(timestamp_range(1.2, 2.7, 10, 100), (12, 27))

    def test_weights_scale_each_track_before_summing(self):
        # 0.4*1.0 + 0.2*0.5 = 0.5 (< поріг лімітера) — чиста лінійна перевірка ваг.
        a = np.full(10, 0.4, dtype=np.float32)
        b = np.full(10, 0.2, dtype=np.float32)
        mixed = mix_tracks([a, b], weights=[1.0, 0.5])
        np.testing.assert_allclose(mixed, np.full(10, 0.5, dtype=np.float32), atol=1e-6)

    def test_zero_weight_track_is_fully_muted(self):
        # 0.5 лишається у лінійній зоні лімітера (< поріг 0.8): рівність з
        # неглушеною доріжкою перевіряє саме ефект ваги, а не побічний tanh-стиск.
        a = np.full(10, 0.5, dtype=np.float32)
        b = np.full(10, 0.5, dtype=np.float32)
        mixed = mix_tracks([a, b], weights=[1.0, 0.0])
        np.testing.assert_allclose(mixed, a, atol=1e-6)

    def test_missing_weights_default_to_one(self):
        a = np.full(6, 0.15, dtype=np.float32)
        without_weights = mix_tracks([a, a])
        with_unit_weights = mix_tracks([a, a], weights=[1.0, 1.0])
        np.testing.assert_allclose(without_weights, with_unit_weights)


class SaveBalancedMixTests(unittest.TestCase):
    """feature/save-mix-balance: власник підняв тиху доріжку слайдером у плеєрі —
    зведення на диску має звучати з ТИМ САМИМ балансом (не рівною сумою)."""

    def test_saved_mix_reflects_slider_ratio_in_actual_amplitude(self):
        # Мутація: доріжка "mic" тиха (0.2), доріжка "sys" гучна (0.8) — власник
        # накрутив мікрофон до 100%, а системний звук притишив до 25%. Перевіряємо
        # ФАКТИЧНУ амплітуду вихідного WAV, а не факт виклику мокнутої функції.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rate = 16000
            mic = write_wav(root / "mic.wav", np.full(rate, 0.2, dtype=np.float32), rate)
            sys_ = write_wav(root / "sys.wav", np.full(rate, 0.8, dtype=np.float32), rate)
            out = export_balanced_wav([mic, sys_], root / "зведення-з-балансом.wav",
                                      weights=[1.0, 0.25])
            audio, out_rate = read_wav(out)
            self.assertEqual(out_rate, rate)
            expected = 0.2 * 1.0 + 0.8 * 0.25   # = 0.4, лінійна зона (< 0.8 поріг)
            # PCM16-квантування ДВІЧІ (mic.wav/sys.wav самі писались через
            # write_wav, тоді зведення знову write_wav→read_wav) — допуск кілька
            # сходинок 1/32767, а не одну.
            self.assertAlmostEqual(float(np.mean(audio)), expected, delta=1.0 / 32767.0 * 4)

    def test_muted_track_leaves_no_trace_in_saved_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rate = 8000
            mic = write_wav(root / "mic.wav", np.full(rate, 0.6, dtype=np.float32), rate)
            sys_ = write_wav(root / "sys.wav", np.full(rate, 0.9, dtype=np.float32), rate)
            out = export_balanced_wav([mic, sys_], root / "mix.wav", weights=[1.0, 0.0])
            audio, _rate = read_wav(out)
            self.assertAlmostEqual(float(np.mean(audio)), 0.6, delta=1.0 / 32767.0 * 2)

    def test_missing_track_raises_instead_of_shifting_weights(self):
        """Суд 31.07 (BLOCK): файл доріжки зник між побудовою списку і фоновим
        зведенням → мовчазний фільтр з'їжджав ваги на чужі доріжки, а журнал
        цілісності записував коефіцієнти, яких у файлі немає. Тепер відсутня
        доріжка — явна помилка, зведення не публікується."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rate = 8000
            mic = write_wav(root / "mic.wav", np.full(rate, 0.4, dtype=np.float32), rate)
            third = write_wav(root / "third.wav", np.full(rate, 0.8, dtype=np.float32), rate)
            gone = root / "sys.wav"          # ніколи не існував (або вже видалений)
            with self.assertRaises(ValueError) as ctx:
                export_balanced_wav([mic, gone, third], root / "mix.wav",
                                    weights=[0.5, 0.0, 1.0])
            self.assertIn("sys.wav", str(ctx.exception))
            self.assertFalse((root / "mix.wav").exists(),
                             "зведення з хибним балансом не має публікуватись")

    def test_originals_stay_byte_identical_after_saving_mix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rate = 8000
            mic = write_wav(root / "mic.wav", np.full(rate, 0.3, dtype=np.float32), rate)
            sys_ = write_wav(root / "sys.wav", np.full(rate, 0.7, dtype=np.float32), rate)
            mic_before = mic.read_bytes()
            sys_before = sys_.read_bytes()
            export_balanced_wav([mic, sys_], root / "mix.wav", weights=[1.0, 0.5])
            self.assertEqual(mic.read_bytes(), mic_before,
                             "оригінал mic.wav змінився після збереження зведення")
            self.assertEqual(sys_.read_bytes(), sys_before,
                             "оригінал sys.wav змінився після збереження зведення")

    def test_saved_mix_is_a_separate_file_next_to_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rate = 8000
            mic = write_wav(root / "mic.wav", np.full(rate, 0.5, dtype=np.float32), rate)
            out_path = root / "зведення-з-балансом.wav"
            out = export_balanced_wav([mic], out_path, weights=[1.0])
            self.assertEqual(out, out_path)
            self.assertTrue(out.is_file())
            self.assertNotEqual(out, mic)


class ExportWrapperTests(unittest.TestCase):
    def test_export_refuses_before_overwrite_when_adjacent_stage_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "existing.wav"
            output.write_bytes(b"old export")
            with mock.patch.object(
                    tempfile, "mkstemp",
                    side_effect=PermissionError("export directory denied")):
                with self.assertRaisesRegex(
                        PermissionError, "export directory denied"):
                    write_wav(
                        output,
                        np.array([0.0, 0.25, -0.25], dtype=np.float32),
                        16000,
                    )
            self.assertEqual(output.read_bytes(), b"old export")

    def test_wav_fsync_failure_preserves_old_export_and_removes_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "existing.wav"
            output.write_bytes(b"old export")
            with mock.patch(
                    "whisper_core.meeting.media.os.fsync",
                    side_effect=OSError("fsync failed")):
                with self.assertRaisesRegex(OSError, "fsync failed"):
                    write_wav(
                        output,
                        np.array([0.0, 0.25, -0.25], dtype=np.float32),
                        16000,
                    )
            self.assertEqual(output.read_bytes(), b"old export")
            self.assertEqual(list(output.parent.iterdir()), [output])

    def test_encoded_export_refuses_before_overwrite_when_stage_fails(self):
        formats = available_formats()
        if not formats:
            self.skipTest("PyAV export codec unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = write_wav(
                root / "source.wav",
                np.array([0.0, 0.25, -0.25], dtype=np.float32),
                16000,
            )
            ext = "mp3" if "mp3" in formats else next(iter(formats))
            output = root / f"existing.{ext}"
            output.write_bytes(b"old export")
            with mock.patch.object(
                    tempfile, "mkstemp",
                    side_effect=PermissionError("export directory denied")):
                with self.assertRaisesRegex(
                        PermissionError, "export directory denied"):
                    export_audio([source], output, ext)
            self.assertEqual(output.read_bytes(), b"old export")

    def test_exports_small_synthetic_audio_when_codec_exists(self):
        formats = available_formats()
        if not formats:
            self.skipTest("PyAV export codec unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = write_wav(root / "tiny.wav", np.sin(np.linspace(0, 20, 3200)).astype(np.float32) * 0.1, 16000)
            ext = "mp3" if "mp3" in formats else next(iter(formats))
            out = export_audio([wav], root / f"tiny.{ext}", ext, 96)
            self.assertTrue(out.is_file())
            self.assertGreater(out.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
