"""feature/llm-model-picker: вибір моделі ШІ для AI-протоколу.

Покриває власні (кастомні) моделі понад пресети: round-trip у конфізі, валідацію,
resolve активної моделі (пресет | локальний файл | інтернет-репозиторій) БЕЗ
тихого фолбеку, і чесну відмову, коли файл активної моделі зник.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._isolation import reset_process_caches
from whisper_core import config as cfgmod
from whisper_core.config import Config
from whisper_core.protocol import model_manager as mm
from whisper_core.protocol import service
from whisper_core.protocol.service import ProtocolGenerator, ProtocolModelMissing

HF_REVISION = "a" * 40
HF_SHA256 = "b" * 64


def _write_gguf(path: Path, size: int = 4096):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + b"\x00" * size)


def _make_ready_dir(root: Path, model_id: str):
    """Готова «завантажена» модель у власній підтеці (READY + model.gguf)."""
    d = root / model_id
    _write_gguf(d / mm.MODEL_FILENAME)
    (d / mm._READY_MARKER).write_text("ok", encoding="utf-8")
    return d


class TestValidators(unittest.TestCase):
    def test_is_gguf_name(self):
        self.assertTrue(mm.is_gguf_name("model.gguf"))
        self.assertTrue(mm.is_gguf_name(r"C:\models\Foo.GGUF"))
        self.assertFalse(mm.is_gguf_name("model.bin"))
        self.assertFalse(mm.is_gguf_name(""))

    def test_is_repo_id(self):
        self.assertTrue(mm.is_repo_id("unsloth/gemma-4-E4B-it-GGUF"))
        self.assertFalse(mm.is_repo_id("no-slash"))
        self.assertFalse(mm.is_repo_id("/leading"))
        self.assertFalse(mm.is_repo_id("a/b/c"))

    def test_new_custom_id_is_safe(self):
        cid = mm.new_custom_id()
        self.assertTrue(mm._SAFE_ID_RE.match(cid))
        self.assertNotIn(cid, mm.PRESETS)
        self.assertNotEqual(cid, mm.new_custom_id())   # унікальний

    def test_custom_hf_url(self):
        cm = mm.CustomModel(id="custom_a", label="x", kind=mm.CUSTOM_KIND_HF,
                            repo_id="owner/name", filename="m.gguf",
                            revision=HF_REVISION, sha256=HF_SHA256)
        self.assertEqual(mm.custom_hf_url(cm),
                         "https://huggingface.co/owner/name/resolve/"
                         f"{HF_REVISION}/m.gguf")

    def test_custom_hf_requires_immutable_revision_and_sha256(self):
        common = dict(id="custom_a", label="x", kind=mm.CUSTOM_KIND_HF,
                      repo_id="owner/name", filename="m.gguf")
        self.assertFalse(mm.CustomModel(**common).valid())
        self.assertFalse(mm.CustomModel(
            **common, revision="main", sha256=HF_SHA256).valid())
        self.assertFalse(mm.CustomModel(
            **common, revision=HF_REVISION, sha256="0" * 63).valid())
        self.assertTrue(mm.CustomModel(
            **common, revision=HF_REVISION, sha256=HF_SHA256).valid())


class TestCustomModelRoundTrip(unittest.TestCase):
    def test_local_json_round_trip(self):
        cm = mm.CustomModel(id="custom_local1", label="Моя модель",
                            kind=mm.CUSTOM_KIND_LOCAL, path=r"D:\m\a.gguf",
                            approx_size_bytes=123)
        back = mm.CustomModel.from_json(cm.to_json())
        self.assertEqual(back, cm)

    def test_hf_json_round_trip(self):
        cm = mm.CustomModel(id="custom_hf1", label="owner/name · m.gguf",
                            kind=mm.CUSTOM_KIND_HF, repo_id="owner/name",
                            filename="m.gguf", revision=HF_REVISION,
                            sha256=HF_SHA256)
        self.assertEqual(mm.CustomModel.from_json(cm.to_json()), cm)

    def test_from_json_rejects_unsafe_id(self):
        raw = ('{"id":"../evil","label":"x","kind":"local",'
               '"path":"a.gguf"}')
        self.assertIsNone(mm.CustomModel.from_json(raw))

    def test_from_json_rejects_unknown_kind(self):
        raw = '{"id":"custom_x","label":"x","kind":"weird","path":"a.gguf"}'
        self.assertIsNone(mm.CustomModel.from_json(raw))

    def test_from_json_rejects_non_gguf_local(self):
        raw = '{"id":"custom_x","label":"x","kind":"local","path":"a.bin"}'
        self.assertIsNone(mm.CustomModel.from_json(raw))

    def test_from_json_rejects_garbage(self):
        self.assertIsNone(mm.CustomModel.from_json("not json"))
        self.assertIsNone(mm.CustomModel.from_json("[1,2,3]"))

    def test_label_defaults_to_id(self):
        raw = (
            '{"id":"custom_x","label":"","kind":"hf","repo_id":"o/n",'
            f'"filename":"m.gguf","revision":"{HF_REVISION}",'
            f'"sha256":"{HF_SHA256}"}}'
        )
        cm = mm.CustomModel.from_json(raw)
        self.assertEqual(cm.label, "custom_x")


class TestConfigPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="llmcfg-"))
        self.cfg_path = self.tmp / "config.toml"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_custom_models_survive_save_load(self):
        cm1 = mm.CustomModel(id="custom_1", label="Локальна",
                             kind=mm.CUSTOM_KIND_LOCAL, path=r"C:\x\a.gguf",
                             approx_size_bytes=999)
        cm2 = mm.CustomModel(id="custom_2", label="owner/name · m.gguf",
                             kind=mm.CUSTOM_KIND_HF, repo_id="owner/name",
                             filename="m.gguf", revision=HF_REVISION,
                             sha256=HF_SHA256)
        c = Config()
        c.custom_models = [cm1.to_json(), cm2.to_json()]
        c.protocol_model = "custom_2"
        c.save(self.cfg_path)

        loaded = Config.load(self.cfg_path)
        self.assertEqual(loaded.protocol_model, "custom_2")
        models = cfgmod.protocol_custom_models(loaded)
        self.assertEqual([m.id for m in models], ["custom_1", "custom_2"])
        self.assertEqual(models[0], cm1)
        self.assertEqual(models[1], cm2)

    def test_empty_custom_models_not_written(self):
        c = Config()
        c.save(self.cfg_path)
        self.assertNotIn("custom_models", self.cfg_path.read_text(encoding="utf-8"))

    def test_helper_drops_invalid_and_dedups(self):
        cm = mm.CustomModel(id="custom_ok", label="ok", kind=mm.CUSTOM_KIND_LOCAL,
                            path="a.gguf")
        cfg = type("C", (), {"custom_models": [
            cm.to_json(),
            cm.to_json(),                       # дубль за id
            '{"id":"bad","kind":"local"}',      # без .gguf → відкинути
            "garbage",
        ]})()
        models = cfgmod.protocol_custom_models(cfg)
        self.assertEqual([m.id for m in models], ["custom_ok"])


class TestResolveActive(unittest.TestCase):
    def setUp(self):
        reset_process_caches()
        self.tmp = Path(tempfile.mkdtemp(prefix="llmres-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_preset_resolves_under_root(self):
        r = mm.resolve("fast", self.tmp, [])
        self.assertIsNotNone(r)
        self.assertEqual(r.kind, "preset")
        self.assertEqual(r.model_path, self.tmp / "fast" / mm.MODEL_FILENAME)

    def test_unknown_id_no_silent_fallback(self):
        # НЕ підміняємо нишком на "fast": невідомий id → None (чесна відмова).
        self.assertIsNone(mm.resolve("totally-unknown", self.tmp, []))
        self.assertFalse(service.model_available("totally-unknown", self.tmp, []))

    def test_local_available_when_file_present(self):
        gguf = self.tmp / "mine.gguf"
        _write_gguf(gguf)
        cm = mm.CustomModel(id="custom_l", label="l", kind=mm.CUSTOM_KIND_LOCAL,
                            path=str(gguf))
        self.assertTrue(service.model_available("custom_l", self.tmp, [cm]))

    def test_local_unavailable_when_file_missing(self):
        cm = mm.CustomModel(id="custom_l", label="l", kind=mm.CUSTOM_KIND_LOCAL,
                            path=str(self.tmp / "gone.gguf"))
        self.assertFalse(service.model_available("custom_l", self.tmp, [cm]))

    def test_hf_available_when_downloaded(self):
        cm = mm.CustomModel(id="custom_h", label="h", kind=mm.CUSTOM_KIND_HF,
                            repo_id="owner/name", filename="m.gguf",
                            revision=HF_REVISION, sha256=HF_SHA256)
        self.assertFalse(service.model_available("custom_h", self.tmp, [cm]))
        _make_ready_dir(self.tmp, "custom_h")
        self.assertTrue(service.model_available("custom_h", self.tmp, [cm]))

    def test_active_selection_switches_target(self):
        # Дві власні моделі; активна визначає, яка перевіряється.
        g = self.tmp / "g.gguf"; _write_gguf(g)
        local = mm.CustomModel(id="custom_a", label="a", kind=mm.CUSTOM_KIND_LOCAL,
                               path=str(g))
        hf = mm.CustomModel(id="custom_b", label="b", kind=mm.CUSTOM_KIND_HF,
                            repo_id="o/n", filename="m.gguf",
                            revision=HF_REVISION, sha256=HF_SHA256)
        custom = [local, hf]
        self.assertTrue(service.model_available("custom_a", self.tmp, custom))
        self.assertFalse(service.model_available("custom_b", self.tmp, custom))


class TestGeneratorHonestError(unittest.TestCase):
    def setUp(self):
        reset_process_caches()
        self.tmp = Path(tempfile.mkdtemp(prefix="llmgen-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_raises_when_active_local_file_gone(self):
        cm = mm.CustomModel(id="custom_l", label="l", kind=mm.CUSTOM_KIND_LOCAL,
                            path=str(self.tmp / "never.gguf"))
        gen = ProtocolGenerator("custom_l", model_root=self.tmp, custom_models=[cm])
        self.assertFalse(gen.available())
        with self.assertRaises(ProtocolModelMissing):
            gen.run([])                          # чесна помилка ще до сайдкара

    def test_run_raises_for_unknown_active_id(self):
        gen = ProtocolGenerator("ghost", model_root=self.tmp, custom_models=[])
        with self.assertRaises(ProtocolModelMissing):
            gen.run([])


if __name__ == "__main__":
    unittest.main()
