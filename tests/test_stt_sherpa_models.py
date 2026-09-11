"""feature/stt-sherpa-parakeet: пакет моделі другого рушія (sherpa-onnx).

Дані пресета Parakeet-TDT-0.6B-v3, маніфест пакета (розмір + SHA-256 кожного
файла, незмінна ревізія HuggingFace), докачка з .part і HTTP Range, відхилення
битих байтів, атомарна активація з READY-маркером, тека компонента поза
інсталяцією. Мережа в тестах фейкова (власний opener), жодного реального
завантаження.
"""
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from whisper_core import stt_presets
from whisper_core import stt_sherpa_models as sm

PRESET = "parakeet-tdt-0.6b-v3"
REPO = "csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
REVISION = "2bda32ec70b097a55adaa07d9a7173915b43cc78"
EXPECTED = {
    "encoder.int8.onnx": (
        652184281,
        "acfc2b4456377e15d04f0243af540b7fe7c992f8d898d751cf134c3a55fd2247"),
    "decoder.int8.onnx": (
        11845275,
        "179e50c43d1a9de79c8a24149a2f9bac6eb5981823f2a2ed88d655b24248db4e"),
    "joiner.int8.onnx": (
        6355277,
        "3164c13fc2821009440d20fcb5fdc78bff28b4db2f8d0f0b329101719c0948b3"),
    "tokens.txt": (
        93939,
        "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_package(payloads: dict) -> sm.SherpaPackage:
    """Маленький тестовий пакет: файли з реальними SHA тестових байтів."""
    return sm.SherpaPackage(
        id="fake-pkg", repo_id="owner/fake", revision="a" * 40,
        assets=tuple(sm.SherpaAsset(name, len(data), _sha(data))
                     for name, data in payloads.items()),
        license_name="CC-BY-4.0", page_url="https://example.invalid/model",
    )


class _FakeResponse:
    """Мінімальний urlopen-об'єкт: status, headers, read() шматками, контекст."""

    def __init__(self, data: bytes, status=200, headers=None):
        self._buf = io.BytesIO(data)
        self.status = status
        self.headers = headers or {}

    def getcode(self):
        return self.status

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener_for(payloads: dict, *, serve=None, seen=None):
    """Фейковий opener: віддає байти файла за назвою з URL; підтримує Range.
    ``serve`` — підміна вмісту (для битих байтів); ``seen`` — список заголовків."""
    serve = serve or {}

    def opener(request, timeout=60):
        url = request.full_url
        name = url.rsplit("/", 1)[-1]
        data = serve.get(name, payloads[name])
        headers = dict(request.header_items())
        if seen is not None:
            seen.append((name, headers))
        rng = headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].rstrip("-"))
            return _FakeResponse(
                data[start:], status=206,
                headers={"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        return _FakeResponse(data)
    return opener


class PresetDataTests(unittest.TestCase):
    def test_preset_registered_as_sherpa_kind(self):
        preset = stt_presets.get_preset(PRESET)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.kind, "sherpa")
        self.assertEqual(preset.license_name, "CC-BY-4.0")
        self.assertEqual(preset.page_url,
                         "https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3")

    def test_engine_kind_helper(self):
        self.assertEqual(stt_presets.engine_kind(PRESET), "sherpa")
        self.assertEqual(stt_presets.engine_kind("large-v3-turbo"), "whisper")
        self.assertEqual(stt_presets.engine_kind("owner/custom-model"), "whisper")
        self.assertEqual(stt_presets.engine_kind(""), "whisper")

    def test_whisper_presets_untouched(self):
        for name in ("small", "medium", "large-v3-turbo", "large-v3"):
            self.assertEqual(stt_presets.get_preset(name).kind, "whisper", name)
        self.assertEqual(stt_presets.PRESETS[2].name, "large-v3-turbo")  # дефолт

    def test_package_manifest_exact(self):
        pkg = sm.package_for(PRESET)
        self.assertEqual(pkg.repo_id, REPO)
        self.assertEqual(pkg.revision, REVISION)
        by_name = {a.filename: (a.size, a.sha256) for a in pkg.assets}
        self.assertEqual(by_name, EXPECTED)
        self.assertEqual(pkg.total_bytes, sum(s for s, _ in EXPECTED.values()))
        self.assertIsNone(sm.package_for("large-v3"))

    def test_asset_urls_pin_full_revision(self):
        pkg = sm.package_for(PRESET)
        for asset in pkg.assets:
            url = sm.asset_url(pkg, asset)
            self.assertTrue(url.startswith(f"https://huggingface.co/{REPO}/resolve/{REVISION}/"))
            self.assertTrue(url.endswith(asset.filename))
            self.assertNotIn("/main/", url)

    def test_model_dir_lives_in_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("whisper_core.paths.user_dir", return_value=Path(tmp)):
                target = sm.model_dir(PRESET)
        self.assertEqual(target, Path(tmp) / "components" / "stt" / PRESET)

    def test_provenance_lists_license(self):
        rows = sm.model_provenance()
        self.assertTrue(any(r["id"] == PRESET and r["license"] == "CC-BY-4.0"
                            and "nvidia/parakeet-tdt-0.6b-v3" in r["repo_url"]
                            for r in rows))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.payloads = {"encoder.int8.onnx": b"E" * 5000, "decoder.int8.onnx": b"D" * 700,
                         "joiner.int8.onnx": b"J" * 300, "tokens.txt": b"<unk> 0\n"}
        self.pkg = _fake_package(self.payloads)

    def test_download_verifies_and_activates_with_ready_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stt" / "fake-pkg"
            progress = []
            sm.download_and_install(target, self.pkg, progress_cb=lambda d, t: progress.append((d, t)),
                                    opener=_opener_for(self.payloads))
            for name, data in self.payloads.items():
                self.assertEqual((target / name).read_bytes(), data)
            ready = json.loads((target / sm.READY_NAME).read_text(encoding="utf-8"))
            self.assertEqual(ready["schema"], sm.READY_SCHEMA)
            self.assertEqual({a["filename"] for a in ready["assets"]}, set(self.payloads))
            self.assertTrue(sm.models_available(target, self.pkg))
            self.assertTrue(sm.models_present_fast(target, self.pkg))
            # прогрес агрегатний до загального розміру пакета
            self.assertEqual(progress[-1], (self.pkg.total_bytes, self.pkg.total_bytes))
            # кеш докачки прибрано після успіху
            self.assertEqual(list((Path(tmp) / "stt" / ".stt-download").glob("*.part")), [])

    def test_bad_sha_rejected_and_nothing_activated(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stt" / "fake-pkg"
            bad = dict(self.payloads)
            opener = _opener_for(self.payloads, serve={"decoder.int8.onnx": b"X" * 700})
            with self.assertRaises(sm.SherpaModelDownloadError):
                sm.download_and_install(target, self.pkg, opener=opener)
            self.assertFalse(target.exists())
            self.assertFalse(sm.models_available(target, self.pkg))
            parts = list((Path(tmp) / "stt" / ".stt-download").glob("*.part"))
            # битий файл не лишається у кеші докачки
            self.assertFalse(any(p.stat().st_size == 700 and p.read_bytes() == b"X" * 700
                                 for p in parts))
            self.assertEqual(bad, self.payloads)   # тестовий словник не змінено

    def test_resume_sends_range_and_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stt" / "fake-pkg"
            cache = Path(tmp) / "stt" / ".stt-download"
            cache.mkdir(parents=True)
            enc = self.payloads["encoder.int8.onnx"]
            enc_sha = _sha(enc)
            (cache / f"{enc_sha}.part").write_bytes(enc[:2000])   # половина вже є
            seen = []
            sm.download_and_install(target, self.pkg, opener=_opener_for(self.payloads, seen=seen))
            enc_req = next(h for name, h in seen if name == "encoder.int8.onnx")
            self.assertEqual(enc_req.get("Range"), "bytes=2000-")
            self.assertEqual((target / "encoder.int8.onnx").read_bytes(), enc)

    def test_cancel_keeps_partial_file_for_next_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stt" / "fake-pkg"
            calls = {"n": 0}

            def cancel():
                calls["n"] += 1
                return calls["n"] > 1          # після першого шматка — скасувати

            with self.assertRaises(InterruptedError):
                sm.download_and_install(target, self.pkg, cancel_check=cancel,
                                        opener=_opener_for(self.payloads))
            self.assertFalse(target.exists())
            parts = list((Path(tmp) / "stt" / ".stt-download").glob("*.part"))
            self.assertTrue(parts, "частковий файл має лишитись для докачки")

    def test_models_available_false_when_missing_or_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fake-pkg"
            self.assertFalse(sm.models_available(target, self.pkg))
            target.mkdir()
            for name, data in self.payloads.items():
                (target / name).write_bytes(data)
            self.assertTrue(sm.models_available(target, self.pkg))
            (target / "joiner.int8.onnx").write_bytes(b"J" * 299)
            self.assertFalse(sm.models_available(target, self.pkg))
            self.assertFalse(sm.models_present_fast(target, self.pkg))

    def test_already_installed_is_noop_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fake-pkg"
            target.mkdir()
            for name, data in self.payloads.items():
                (target / name).write_bytes(data)

            def opener(*a, **k):
                raise AssertionError("мережа не мала викликатись")
            sm.download_and_install(target, self.pkg, opener=opener)


if __name__ == "__main__":
    unittest.main()
