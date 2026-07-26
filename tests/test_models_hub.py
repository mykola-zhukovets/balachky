"""Unit tests for Models Hub status aggregation, disk space calculation, and recommended presets."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whisper_core.config import Config
from whisper_core.models_hub import (
    ModelHubItem,
    get_dir_size,
    get_model_dirs,
    get_models_hub_status,
    get_total_models_disk_size,
)
from fronts.desktop.i18n import STRINGS


class TestModelsHub(unittest.TestCase):
    def setUp(self):
        self.cfg = Config()

    def test_i18n_keys_exist_and_match_in_uk_and_en(self):
        """Перевірка наявності та парності нових ключів i18n для центру моделей."""
        hub_keys = [
            "set_tab_models",
            "models_hub_eyebrow",
            "models_hub_title",
            "models_hub_hint",
            "models_hub_stt_title",
            "models_hub_diar_title",
            "models_hub_protocol_title",
            "models_hub_tts_title",
            "models_hub_punc_title",
            "models_hub_status_downloaded",
            "models_hub_status_missing",
            "models_hub_active_label",
            "models_hub_btn_recommended",
            "models_hub_btn_advanced",
            "models_hub_total_disk",
            "models_hub_open_folder",
            "models_hub_open_folder_acc",
            "models_hub_stt_rec_acc",
            "models_hub_stt_adv_acc",
            "models_hub_diar_rec_acc",
            "models_hub_diar_adv_acc",
            "models_hub_protocol_rec_acc",
            "models_hub_protocol_adv_acc",
            "models_hub_tts_rec_acc",
            "models_hub_tts_adv_acc",
            "models_hub_punc_rec_acc",
            "models_hub_punc_adv_acc",
            "models_hub_stt_vram",
            "models_hub_diar_vram",
            "models_hub_protocol_fast_ram",
            "models_hub_tts_ram",
            "models_hub_punc_ram",
            "models_hub_preset_turbo",
            "models_hub_preset_large_v3",
            "models_hub_preset_pyannote",
            "models_hub_not_configured",
            "models_hub_preset_gemma_fast",
            "models_hub_preset_gemma_quality",
            "models_hub_preset_styletts2",
            "models_hub_preset_pcs",
            "models_hub_not_downloaded",
            "models_hub_download_stt_hint",
            "models_hub_download_diar_hint",
            "models_hub_download_proto_hint",
            "models_hub_download_tts_hint",
            "models_hub_download_punc_hint",
            "models_hub_folder_stt",
            "models_hub_folder_diar",
            "models_hub_folder_proto",
            "models_hub_folder_tts",
            "models_hub_folder_punc",
            "models_hub_folder_user_dir",
        ]
        for key in hub_keys:
            self.assertIn(key, STRINGS["uk"], f"Key '{key}' missing in UK i18n")
            self.assertIn(key, STRINGS["en"], f"Key '{key}' missing in EN i18n")
            val_uk = STRINGS["uk"][key]
            self.assertNotIn("«", val_uk, f"Forbidden quote '«' in UK key '{key}'")
            self.assertNotIn("»", val_uk, f"Forbidden quote '»' in UK key '{key}'")

    def test_get_models_hub_status_returns_five_components(self):
        """Центр моделей повертає статус для всіх 5 компонентів."""
        items = get_models_hub_status(self.cfg)
        self.assertEqual(len(items), 5)

        comp_ids = [item.component_id for item in items]
        self.assertIn("stt", comp_ids)
        self.assertIn("diarization", comp_ids)
        self.assertIn("protocol", comp_ids)
        self.assertIn("tts", comp_ids)
        self.assertIn("punctuator", comp_ids)

        for item in items:
            self.assertIsInstance(item, ModelHubItem)
            self.assertTrue(item.title_key)
            self.assertTrue(item.active_name_key)
            self.assertTrue(item.memory_note_key)

    def test_get_dir_size_exact_calculation(self):
        """Підрахунок розміру папки повертає ТОЧНУ суму байтів файлів усередині."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            f1 = tmp_path / "model1.bin"
            f2 = tmp_path / "sub" / "model2.bin"
            f2.parent.mkdir(parents=True, exist_ok=True)

            f1.write_bytes(b"A" * 1000)
            f2.write_bytes(b"B" * 2500)

            total = get_dir_size(tmp_path)
            self.assertEqual(total, 3500)

    def test_get_total_models_disk_size_with_known_files(self):
        """Розрахунок сумарного обсягу моделей по 5 теках дає точну суму."""
        with tempfile.TemporaryDirectory() as tmp1, \
             tempfile.TemporaryDirectory() as tmp2, \
             tempfile.TemporaryDirectory() as tmp3, \
             tempfile.TemporaryDirectory() as tmp4, \
             tempfile.TemporaryDirectory() as tmp5:

            p1 = Path(tmp1) / "stt.bin"; p1.write_bytes(b"1" * 100)
            p2 = Path(tmp2) / "diar.bin"; p2.write_bytes(b"2" * 200)
            p3 = Path(tmp3) / "proto.bin"; p3.write_bytes(b"3" * 300)
            p4 = Path(tmp4) / "tts.bin"; p4.write_bytes(b"4" * 400)
            p5 = Path(tmp5) / "punc.bin"; p5.write_bytes(b"5" * 500)

            with patch("whisper_core.models.resolve_cache_dir", return_value=Path(tmp1)), \
                 patch("whisper_core.paths.diarization_models_dir", return_value=Path(tmp2)), \
                 patch("whisper_core.paths.protocol_models_dir", return_value=Path(tmp3)), \
                 patch("whisper_core.paths.tts_voices_dir", return_value=Path(tmp4)), \
                 patch("whisper_core.paths.punctuator_model_dir", return_value=Path(tmp5)):

                size = get_total_models_disk_size(self.cfg)
                self.assertEqual(size, 1500)

    def test_get_model_dirs_returns_six_folders(self):
        """get_model_dirs повертає 6 папок (5 компонентів + user_dir)."""
        dirs = get_model_dirs(self.cfg)
        self.assertEqual(len(dirs), 6)
        keys = [d[0] for d in dirs]
        self.assertIn("models_hub_folder_stt", keys)
        self.assertIn("models_hub_folder_diar", keys)
        self.assertIn("models_hub_folder_proto", keys)
        self.assertIn("models_hub_folder_tts", keys)
        self.assertIn("models_hub_folder_punc", keys)
        self.assertIn("models_hub_folder_user_dir", keys)


if __name__ == "__main__":
    unittest.main()
