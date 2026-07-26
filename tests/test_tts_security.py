"""Хвиля 1: безпека voice-pack (§4.4, §11.2).

pickle/YAML/../ /junction/ONNX-data/TOFU — жоден код користувача не виконується."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import security as S


def _make_pack(manifest: dict, extra_files=None):
    d = tempfile.mkdtemp(prefix="pack-")
    (Path(d) / "voice.json").write_text(__import__("json").dumps(manifest),
                                        encoding="utf-8")
    for name in (extra_files or []):
        (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
        (Path(d) / name).write_bytes(b"\x00" * 16)
    return d


_GOOD = {"schema": 1, "kind": "sherpa", "label": "Мій голос", "languages": ["uk"],
         "files": {"model": "model.onnx", "tokens": "tokens.txt"},
         "sample_rate": 24000}


class TestVoicePack(unittest.TestCase):
    def test_valid_pack(self):
        d = _make_pack(_GOOD, extra_files=["model.onnx", "tokens.txt"])
        m = S.validate_voice_pack(d)
        self.assertEqual(m.kind, "sherpa")
        self.assertEqual(m.sample_rate, 24000)

    def test_kind_outside_registry_rejected(self):
        bad = dict(_GOOD, kind="evilengine")
        d = _make_pack(bad, extra_files=["model.onnx", "tokens.txt"])
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_unknown_schema_rejected(self):
        bad = dict(_GOOD, schema=99)
        d = _make_pack(bad, extra_files=["model.onnx", "tokens.txt"])
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_executable_file_rejected(self):
        d = _make_pack(_GOOD, extra_files=["model.onnx", "tokens.txt", "evil.py"])
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_parent_traversal_rejected(self):
        # РЕАЛЬНИЙ файл ЗА межами pack: якщо зняти канонізацію (safe_under), target
        # ІСНУЄ → лише safe_under його відхиляє (мутація канонізації → тест червоніє).
        bad = dict(_GOOD, files={"model": "../outside.onnx", "tokens": "tokens.txt"})
        d = _make_pack(bad, extra_files=["tokens.txt"])
        (Path(d).parent / "outside.onnx").write_bytes(b"\x00" * 16)   # реально існує!
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_absolute_path_rejected(self):
        # РЕАЛЬНИЙ абсолютний файл поза pack — існує, тож рятує лише канонізація.
        outside = tempfile.NamedTemporaryFile(delete=False, suffix=".onnx")
        outside.write(b"\x00" * 16)
        outside.close()
        bad = dict(_GOOD, files={"model": outside.name, "tokens": "t.txt"})
        d = _make_pack(bad, extra_files=["t.txt"])
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_symlink_outside_rejected(self):
        d = _make_pack(_GOOD, extra_files=["tokens.txt"])
        outside = tempfile.NamedTemporaryFile(delete=False, suffix=".onnx")
        outside.write(b"\x00" * 16)
        outside.close()
        link = Path(d) / "model.onnx"
        try:
            os.symlink(outside.name, link)
        except (OSError, NotImplementedError, PermissionError):
            self.skipTest("symlink недоступний без прав адміністратора")
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)


class TestOnnxFailClosed(unittest.TestCase):
    def test_external_data_without_onnx_lib_rejected(self):
        # CRITICAL §4.4: без onnx-lib і за наявності .data-сусіда → FAIL-CLOSED (reject)
        import importlib.util
        if importlib.util.find_spec("onnx") is not None:
            self.skipTest("onnx присутній — гілка fail-closed не активується")
        d = _make_pack(dict(_GOOD, model_type="vits",
                            files={"model": "m.onnx", "tokens": "t.txt"}),
                       extra_files=["m.onnx", "t.txt", "m.data"])   # .data-доказ
        with self.assertRaises(S.VoicePackError):
            S.validate_voice_pack(d)

    def test_self_contained_onnx_ok_without_lib(self):
        import importlib.util
        if importlib.util.find_spec("onnx") is not None:
            self.skipTest("onnx присутній")
        d = _make_pack(dict(_GOOD, model_type="vits",
                            files={"model": "m.onnx", "tokens": "t.txt"}),
                       extra_files=["m.onnx", "t.txt"])   # без external-data — ок
        m = S.validate_voice_pack(d)
        self.assertEqual(m.kind, "sherpa")


class TestPackSizeBound(unittest.TestCase):
    def test_oversized_pack_rejected(self):
        S._MAX_PACK_BYTES  # sanity
        d = _make_pack(_GOOD, extra_files=["model.onnx", "tokens.txt"])
        orig = S._MAX_PACK_BYTES
        S._MAX_PACK_BYTES = 4     # штучно крихітний ліміт
        try:
            with self.assertRaises(S.VoicePackError):
                S.validate_voice_pack(d)
        finally:
            S._MAX_PACK_BYTES = orig


class TestTensorLoad(unittest.TestCase):
    def test_load_tensor_enforces_weights_only(self):
        # доводимо, що ЗАВЖДИ передаємо weights_only=True (без реального torch)
        captured = {}

        class FakeTorch:
            @staticmethod
            def load(path, weights_only=False):
                captured["weights_only"] = weights_only
                return "tensor"

        out = S.load_tensor_file("x.pt", torch_module=FakeTorch)
        self.assertEqual(out, "tensor")
        self.assertTrue(captured["weights_only"])


class TestYamlSafety(unittest.TestCase):
    def test_yaml_tag_rejected(self):
        d = tempfile.mkdtemp(prefix="cfg-")
        cfg = Path(d) / "config.yml"
        cfg.write_text("model: !!python/object/apply:os.system ['calc']\n",
                       encoding="utf-8")
        with self.assertRaises(S.VoicePackError):
            S.safe_yaml_config(cfg)

    def test_yaml_allowlist_drops_unknown(self):
        d = tempfile.mkdtemp(prefix="cfg-")
        cfg = Path(d) / "config.yml"
        cfg.write_text("sample_rate: 24000\nsecret_field: 42\n", encoding="utf-8")
        out = S.safe_yaml_config(cfg)
        self.assertIn("sample_rate", out)
        self.assertNotIn("secret_field", out)


class TestHfLockTofu(unittest.TestCase):
    def test_mutable_revision_needs_lock(self):
        self.assertTrue(S.hf_needs_lock(""))
        self.assertTrue(S.hf_needs_lock("main"))
        self.assertTrue(S.hf_needs_lock("HEAD"))

    def test_immutable_commit_no_lock(self):
        self.assertFalse(S.hf_needs_lock("a1b2c3d4e5f6"))

    def test_tofu_lock_detects_sha_change(self):
        lock = S.VoiceLock(commit="abc123def456", file_shas={"model.onnx": "aa"})
        self.assertFalse(lock.changed(commit="abc123def456",
                                      file_shas={"model.onnx": "aa"}))
        self.assertTrue(lock.changed(commit="abc123def456",
                                     file_shas={"model.onnx": "BB"}))
        self.assertTrue(lock.changed(commit="zzz999", file_shas={"model.onnx": "aa"}))

    def test_lock_roundtrip(self):
        lock = S.VoiceLock(commit="c0ffee1234", file_shas={"a": "1", "b": "2"})
        again = S.VoiceLock.from_json(lock.to_json())
        self.assertEqual(again.commit, "c0ffee1234")
        self.assertEqual(again.file_shas["b"], "2")


if __name__ == "__main__":
    unittest.main()
