"""Хвиля 1: менеджер голосів — мовна логіка, resolve без фолбеку, LANGUAGE_MISMATCH."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whisper_core.tts import voices as V


class TestDefaults(unittest.TestCase):
    def test_default_voice_by_language(self):
        self.assertEqual(V.default_voice_for("uk"), "styletts2_ua")
        self.assertEqual(V.default_voice_for("en"), "kokoro_en")

    def test_unknown_language_gets_multilingual(self):
        self.assertEqual(V.default_voice_for("de"), V.FALLBACK_MULTILINGUAL)

    def test_voices_for_language_recommended_first(self):
        uk = V.voices_for_language("uk")
        self.assertTrue(uk)
        self.assertTrue(uk[0].recommended)
        self.assertEqual(uk[0].id, "styletts2_ua")


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="voices-")

    def test_unknown_id_returns_none(self):
        self.assertIsNone(V.resolve("немає_такого", "uk", root=self.root))

    def test_unsafe_id_returns_none(self):
        self.assertIsNone(V.resolve("../evil", "uk", root=self.root))

    def test_valid_resolve(self):
        r = V.resolve("styletts2_ua", "uk", root=self.root)
        self.assertIsInstance(r, V.ResolvedVoice)
        self.assertEqual(r.engine_kind, "styletts2")
        self.assertTrue(r.manifest_path.endswith("styletts2_ua"))

    def test_language_mismatch_signal(self):
        # uk-голос під en-текст → LANGUAGE_MISMATCH (не None, не тихий синтез)
        r = V.resolve("styletts2_ua", "en", root=self.root)
        self.assertEqual(r, V.LANGUAGE_MISMATCH)

    def test_no_lang_skips_mismatch(self):
        r = V.resolve("styletts2_ua", "", root=self.root)
        self.assertIsInstance(r, V.ResolvedVoice)


class TestDetectLanguage(unittest.TestCase):
    def test_ukrainian(self):
        self.assertEqual(V.detect_language("Слава Україні, ґречні їжаки"), "uk")

    def test_english(self):
        self.assertEqual(V.detect_language("The quick brown fox jumps"), "en")

    def test_russian_markers_not_uk(self):
        # російські маркери ы/ъ/э → unknown (не вгадуємо uk!)
        self.assertEqual(V.detect_language("Электрический сыр объективный"), "unknown")

    def test_polish_not_en(self):
        self.assertEqual(V.detect_language("Zażółć gęślą jaźń wątły"), "unknown")

    def test_mixed_scripts(self):
        self.assertEqual(V.detect_language("Привіт hello світ world разом"), "mixed")

    def test_too_short_unknown(self):
        self.assertEqual(V.detect_language("a"), "unknown")


class TestDownloadInstall(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="voices-dl-")

    def test_unknown_voice_raises(self):
        with self.assertRaises(V.VoiceDownloadError):
            V.download_and_install("немає", root=self.root)

    def test_empty_files_preset_raises(self):
        # sherpa-пресети без запінованих файлів → чесна помилка (жива SHA-звірка)
        with self.assertRaises(V.VoiceDownloadError):
            V.download_and_install("kokoro_en", root=self.root)

    def test_voice_available_false_when_absent(self):
        self.assertFalse(V.voice_available("styletts2_ua", root=self.root))

    def test_voice_available_true_when_ready(self):
        # фабрикуємо встановлений голос: READY + усі файли пресета потрібного розміру
        vdir = Path(self.root) / "styletts2_ua"
        vdir.mkdir(parents=True)
        (vdir / "READY").write_text("ok")
        for (_u, fn, min_bytes, _s) in V.VOICE_PRESETS["styletts2_ua"].files:
            fp = vdir / fn
            fp.parent.mkdir(parents=True, exist_ok=True)
            fp.write_bytes(b"\x00" * (int(min_bytes) + 1))
        self.assertTrue(V.voice_available("styletts2_ua", root=self.root))

    def test_delete_voice(self):
        vdir = Path(self.root) / "styletts2_ua"
        vdir.mkdir(parents=True)
        (vdir / "READY").write_text("ok")
        self.assertTrue(V.delete_voice("styletts2_ua", root=self.root))
        self.assertFalse(vdir.exists())

    def test_corrupted_content_rejected(self):
        # мутація суду: зіпсований sha-пін тієї САМОЇ довжини лишав 283 passed. Тут
        # мокаємо мережу вмістом правильної довжини, але НЕвірним sha → _verify_file
        # відхиляє → VoiceDownloadError, тека голосу НЕ створена (не тиха каліч).
        import hashlib
        good = b"\x00" * 2048
        bad = b"\xff" * 2048                  # та сама довжина, інший вміст → інший sha
        preset = V.VoicePreset(
            id="styletts2_ua", engine_kind="styletts2", languages=("uk",),
            files=(("https://huggingface.co/x/resolve/main/config.yml",
                    "config.yml", 1000, hashlib.sha256(good).hexdigest()),),
            approx_size_bytes=2048, label_key="k", hint_key="k")
        orig_presets = dict(V.VOICE_PRESETS)
        orig_dl = V._download_file
        try:
            V.VOICE_PRESETS["styletts2_ua"] = preset
            V._download_file = lambda url, dest, *a, **k: dest.write_bytes(bad)
            with self.assertRaises(V.VoiceDownloadError):
                V.download_and_install("styletts2_ua", root=self.root)
        finally:
            V._download_file = orig_dl
            V.VOICE_PRESETS.clear()
            V.VOICE_PRESETS.update(orig_presets)
        self.assertFalse((Path(self.root) / "styletts2_ua").exists())


class TestPresetPins(unittest.TestCase):
    """Пресети uk-голосів запіновано живими SHA + безпечними відносними шляхами."""

    def test_uk_presets_have_sha_and_safe_paths(self):
        for vid in ("styletts2_ua", "radtts_uk"):
            preset = V.VOICE_PRESETS[vid]
            self.assertTrue(preset.files, f"{vid}: files не порожні")
            for (url, fn, min_bytes, sha256) in preset.files:
                self.assertTrue(url.startswith("https://huggingface.co/"))
                self.assertTrue(sha256 and len(sha256) == 64,
                                f"{vid}/{fn}: SHA-256 має бути запінований")
                self.assertGreater(int(min_bytes), 0)
                self.assertTrue(V._safe_rel_filename(fn), f"{vid}/{fn} небезпечний")

    def test_styletts2_files_are_engine_local(self):
        # styletts2 рушій читає config.yml + pytorch_model.bin + style.pt поруч
        names = {fn for (_u, fn, _m, _s) in V.VOICE_PRESETS["styletts2_ua"].files}
        self.assertEqual(names, {"config.yml", "pytorch_model.bin", "style.pt"})

    def test_radtts_model_is_cwd_relative(self):
        # tts_uk вантажить модель CWD-відносно з ./models/...
        names = [fn for (_u, fn, _m, _s) in V.VOICE_PRESETS["radtts_uk"].files]
        self.assertIn("models/radtts-pp-dap-model/model_dap_84000_state.pt", names)
        # вокодер розкладено у HF-кеш-структуру snapshots/<commit>
        self.assertTrue(any("/snapshots/" in fn and fn.endswith("config.yaml")
                            for fn in names))


class TestSafeRelFilename(unittest.TestCase):
    def test_rejects_traversal_and_absolute(self):
        for bad in ("../evil", "a/../../b", "/etc/passwd", "C:/x", "\\\\srv\\s",
                    "", "sub/../../out"):
            self.assertFalse(V._safe_rel_filename(bad), bad)

    def test_allows_nested_relative(self):
        for ok in ("config.yml", "models/a/b.pt",
                   "hf/models--x/snapshots/abc/config.yaml"):
            self.assertTrue(V._safe_rel_filename(ok), ok)


class TestNestedStage(unittest.TestCase):
    """download_and_install кладе вкладені шляхи (mkdir parent) і блокує небезпечні."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="voices-nest-")

    def test_nested_filename_staged_and_verified(self):
        # підмінюємо мережу: пишемо байти замість завантаження
        blob = b"\x00" * 2048
        import hashlib
        sha = hashlib.sha256(blob).hexdigest()
        preset = V.VoicePreset(
            id="styletts2_ua", engine_kind="styletts2", languages=("uk",),
            files=(("https://huggingface.co/x/resolve/main/a",
                    "hf/models--x/snapshots/deadbeef/config.yaml", 1000, sha),),
            approx_size_bytes=2048, label_key="k", hint_key="k")
        orig_presets = dict(V.VOICE_PRESETS)
        orig_dl = V._download_file
        try:
            V.VOICE_PRESETS["styletts2_ua"] = preset
            V._download_file = lambda url, dest, *a, **k: dest.write_bytes(blob)
            V.download_and_install("styletts2_ua", root=self.root)
        finally:
            V._download_file = orig_dl
            V.VOICE_PRESETS.clear()
            V.VOICE_PRESETS.update(orig_presets)
        staged = (Path(self.root) / "styletts2_ua" /
                  "hf/models--x/snapshots/deadbeef/config.yaml")
        self.assertTrue(staged.is_file())
        self.assertTrue((Path(self.root) / "styletts2_ua" / "READY").is_file())

    def test_unsafe_filename_rejected(self):
        preset = V.VoicePreset(
            id="styletts2_ua", engine_kind="styletts2", languages=("uk",),
            files=(("https://x/a", "../../escape.bin", 1, None),),
            approx_size_bytes=1, label_key="k", hint_key="k")
        orig_presets = dict(V.VOICE_PRESETS)
        orig_dl = V._download_file
        try:
            V.VOICE_PRESETS["styletts2_ua"] = preset
            V._download_file = lambda url, dest, *a, **k: dest.write_bytes(b"x")
            with self.assertRaises(V.VoiceDownloadError):
                V.download_and_install("styletts2_ua", root=self.root)
        finally:
            V._download_file = orig_dl
            V.VOICE_PRESETS.clear()
            V.VOICE_PRESETS.update(orig_presets)


class TestCustomVoice(unittest.TestCase):
    def test_valid_local(self):
        cv = V.CustomVoice(id="custom_abc", label="Мій", kind="sherpa",
                           source=V.CUSTOM_KIND_LOCAL, manifest_path="C:/x",
                           languages=("uk",))
        self.assertTrue(cv.valid())

    def test_invalid_kind(self):
        cv = V.CustomVoice(id="custom_abc", label="Мій", kind="evil",
                           source=V.CUSTOM_KIND_LOCAL, manifest_path="C:/x")
        self.assertFalse(cv.valid())

    def test_hf_needs_repo(self):
        cv = V.CustomVoice(id="custom_abc", label="Мій", kind="sherpa",
                           source=V.CUSTOM_KIND_HF, repo_id="owner/name")
        self.assertTrue(cv.valid())
        bad = V.CustomVoice(id="custom_abc", label="Мій", kind="sherpa",
                            source=V.CUSTOM_KIND_HF, repo_id="no-slash")
        self.assertFalse(bad.valid())

    def test_roundtrip(self):
        cv = V.CustomVoice(id="custom_ab12", label="Голос", kind="sherpa",
                           source=V.CUSTOM_KIND_LOCAL, manifest_path="C:/p",
                           languages=("uk", "en"))
        again = V.CustomVoice.from_json(cv.to_json())
        self.assertEqual(again.id, "custom_ab12")
        self.assertEqual(again.languages, ("uk", "en"))

    def test_from_pack_validates_security(self):
        # безпечний pack → CustomVoice; небезпечний → VoicePackError (з security.py)
        import json as _json
        d = tempfile.mkdtemp(prefix="pack-")
        (Path(d) / "voice.json").write_text(_json.dumps({
            "schema": 1, "kind": "sherpa", "label": "Мій голос",
            "languages": ["uk"], "files": {"model": "m.onnx", "tokens": "t.txt"},
            "sample_rate": 24000}), encoding="utf-8")
        (Path(d) / "m.onnx").write_bytes(b"\x00" * 16)
        (Path(d) / "t.txt").write_bytes(b"\x00" * 16)
        cv = V.custom_voice_from_pack(d, label="Мій голос")
        self.assertEqual(cv.kind, "sherpa")
        self.assertEqual(cv.languages, ("uk",))
        # небезпечний pack (.py) → VoicePackError
        from whisper_core.tts.security import VoicePackError
        (Path(d) / "evil.py").write_text("x")
        with self.assertRaises(VoicePackError):
            V.custom_voice_from_pack(d)

    def test_custom_styletts2_rejected(self):
        # БЛОКЕР 2 суду: власний styletts2-pack ВІДХИЛЯЄТЬСЯ на ДОДАВАННІ (обходить
        # weights_only через бібліотеку). Причина = tts_voice_custom_engine.
        import json as _json
        from whisper_core.tts.security import VoicePackError
        d = tempfile.mkdtemp(prefix="pack-st-")
        (Path(d) / "voice.json").write_text(_json.dumps({
            "schema": 1, "kind": "styletts2", "label": "Мій", "languages": ["uk"],
            "files": {"model": "m.safetensors"}, "sample_rate": 24000}),
            encoding="utf-8")
        (Path(d) / "m.safetensors").write_bytes(b"\x00" * 16)
        with self.assertRaises(VoicePackError) as ctx:
            V.custom_voice_from_pack(d)
        self.assertEqual(ctx.exception.reason_key, "tts_voice_custom_engine")

    def test_custom_radtts_rejected(self):
        import json as _json
        from whisper_core.tts.security import VoicePackError
        d = tempfile.mkdtemp(prefix="pack-rt-")
        (Path(d) / "voice.json").write_text(_json.dumps({
            "schema": 1, "kind": "radtts", "label": "Мій", "languages": ["uk"],
            "files": {"model": "m.pt"}, "sample_rate": 44100}), encoding="utf-8")
        (Path(d) / "m.pt").write_bytes(b"\x00" * 16)
        with self.assertRaises(VoicePackError):
            V.custom_voice_from_pack(d)

    def test_custom_voice_kind_restricted(self):
        cv = V.CustomVoice(id="custom_x", label="x", kind="styletts2",
                           source=V.CUSTOM_KIND_LOCAL, manifest_path="C:/p")
        self.assertFalse(cv.valid())             # styletts2 не дозволений для власних


class TestHfTofu(unittest.TestCase):
    """§4.4: TOFU/HF lock ПІДКЛЮЧЕНО до завантаження (не лише helper у security)."""

    def test_mutable_revision_needs_consent(self):
        action, lock = V.hf_download_action("main", "abc123def", {"m.onnx": "aa"})
        self.assertEqual(action, "reconsent")    # mutable revision → повторна згода
        self.assertTrue(lock)

    def test_immutable_first_use_locks(self):
        action, _ = V.hf_download_action("abc123def456", "abc123def456",
                                         {"m.onnx": "aa"})
        self.assertEqual(action, "lock")         # TOFU: перше довіряння

    def test_matching_lock_proceeds(self):
        _a, lock = V.hf_download_action("abc123def456", "abc123def456",
                                        {"m.onnx": "aa"})
        action, _ = V.hf_download_action("abc123def456", "abc123def456",
                                         {"m.onnx": "aa"}, existing_lock_json=lock)
        self.assertEqual(action, "proceed")

    def test_changed_sha_after_lock_reconsent(self):
        _a, lock = V.hf_download_action("abc123def456", "abc123def456",
                                        {"m.onnx": "aa"})
        action, _ = V.hf_download_action("abc123def456", "abc123def456",
                                         {"m.onnx": "BB"}, existing_lock_json=lock)
        self.assertEqual(action, "reconsent")    # підміна SHA → повторна згода


class TestSamples(unittest.TestCase):
    def test_sample_absent_returns_none(self):
        # демо-WAV ще не згенеровано (генеруються на білді) → None (картка покаже
        # чесну заглушку, не мовчазну кнопку)
        self.assertIsNone(V.sample_wav_path("styletts2_ua"))
        self.assertFalse(V.has_sample("styletts2_ua"))


if __name__ == "__main__":
    unittest.main()
