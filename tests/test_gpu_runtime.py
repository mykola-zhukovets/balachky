"""Тести опційної докачки CUDA-рантайму (feature/gpu).

БЕЗ мережі й БЕЗ реальних DLL: піни/sha256, розпакування лише потрібних файлів,
runtime_ready на фейковій теці та нормалізація startup-конфігу під GPU. Повну
докачку (~541 МБ) тут НЕ ганяємо — лише фейковий zip у tempfile.
"""
import hashlib
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from whisper_core import cuda_runtime


class PinTests(unittest.TestCase):
    def test_pins_are_frozen(self):
        self.assertEqual(cuda_runtime.CUBLAS_WHEEL_VERSION, "12.8.4.1")
        self.assertEqual(len(cuda_runtime.CUBLAS_WHEEL_SHA256), 64)
        self.assertEqual(cuda_runtime.RUNTIME_DLLS,
                         ("cublas64_12.dll", "cublasLt64_12.dll"))

    def test_verify_sha256_accepts_match_and_rejects_tampered(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "w.whl"
            f.write_bytes(b"hello gpu")
            digest = hashlib.sha256(b"hello gpu").hexdigest()
            with patch.object(cuda_runtime, "CUBLAS_WHEEL_SHA256", digest):
                cuda_runtime._verify_sha256(str(f))          # не кидає
            with patch.object(cuda_runtime, "CUBLAS_WHEEL_SHA256", "0" * 64):
                with self.assertRaises(cuda_runtime.CudaDownloadError):
                    cuda_runtime._verify_sha256(str(f))


class ExtractTests(unittest.TestCase):
    def test_extract_takes_only_required_dlls(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "fake.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("nvidia/cublas/bin/cublas64_12.dll", b"CUBLAS")
                zf.writestr("nvidia/cublas/bin/cublasLt64_12.dll", b"CUBLASLT")
                zf.writestr("nvidia/cublas/bin/nvblas64_12.dll", b"DECOY")
                zf.writestr("nvidia/cublas/__init__.py", b"x")
                zf.writestr("nvidia_cublas_cu12-12.8.4.1.dist-info/RECORD", b"r")
            dest = Path(tmp) / "cuda"
            dest.mkdir()
            cuda_runtime._extract_dlls(str(wheel), dest)
            got = {p.name for p in dest.iterdir()}
            self.assertEqual(got, set(cuda_runtime.RUNTIME_DLLS))
            self.assertEqual((dest / "cublas64_12.dll").read_bytes(), b"CUBLAS")
            self.assertEqual((dest / "cublasLt64_12.dll").read_bytes(), b"CUBLASLT")

    def test_extract_raises_when_dll_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            wheel = Path(tmp) / "fake.whl"
            with zipfile.ZipFile(wheel, "w") as zf:
                zf.writestr("nvidia/cublas/bin/cublas64_12.dll", b"only one")
            dest = Path(tmp) / "cuda"
            dest.mkdir()
            with self.assertRaises(cuda_runtime.CudaDownloadError):
                cuda_runtime._extract_dlls(str(wheel), dest)


class RuntimeReadyTests(unittest.TestCase):
    def test_false_without_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cuda_runtime, "cuda_dir", return_value=Path(tmp)), \
                    patch.object(cuda_runtime.sys, "platform", "win32"):
                self.assertFalse(cuda_runtime.runtime_ready())

    def test_true_when_files_present_and_loadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in cuda_runtime.RUNTIME_DLLS:
                (d / name).write_bytes(b"x")
            loaded = []
            fake_ctypes = types.ModuleType("ctypes")
            fake_ctypes.WinDLL = lambda p: loaded.append(p) or object()
            with patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                    patch.object(cuda_runtime, "cuda_dir", return_value=d), \
                    patch.object(cuda_runtime.sys, "platform", "win32"):
                self.assertTrue(cuda_runtime.runtime_ready())
            # обидва DLL пробували завантажити з абсолютним шляхом
            self.assertEqual([Path(p).name for p in loaded],
                             list(cuda_runtime.RUNTIME_DLLS))

    def test_false_when_load_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            for name in cuda_runtime.RUNTIME_DLLS:
                (d / name).write_bytes(b"x")

            def boom(_p):
                raise OSError("not a valid DLL")

            fake_ctypes = types.ModuleType("ctypes")
            fake_ctypes.WinDLL = boom
            with patch.dict(sys.modules, {"ctypes": fake_ctypes}), \
                    patch.object(cuda_runtime, "cuda_dir", return_value=d), \
                    patch.object(cuda_runtime.sys, "platform", "win32"):
                self.assertFalse(cuda_runtime.runtime_ready())


class DownloadInstallTests(unittest.TestCase):
    @staticmethod
    def _fake_wheel_bytes():
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.whl"
            with zipfile.ZipFile(src, "w") as zf:
                zf.writestr("nvidia/cublas/bin/cublas64_12.dll", b"A")
                zf.writestr("nvidia/cublas/bin/cublasLt64_12.dll", b"B")
            return src.read_bytes()

    def test_end_to_end_with_fakes(self):
        data = self._fake_wheel_bytes()
        digest = hashlib.sha256(data).hexdigest()

        def fake_download(url, destfile, cb, cancel):
            Path(destfile).write_bytes(data)
            if cb:
                cb(len(data), len(data))

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cuda"
            with patch.object(cuda_runtime, "cuda_dir", return_value=dest), \
                    patch.object(cuda_runtime, "_wheel_url",
                                 return_value="http://x/w.whl"), \
                    patch.object(cuda_runtime, "_download_file",
                                 side_effect=fake_download), \
                    patch.object(cuda_runtime, "CUBLAS_WHEEL_SHA256", digest):
                cuda_runtime.download_and_install()
            self.assertTrue((dest / "cublas64_12.dll").is_file())
            self.assertTrue((dest / "cublasLt64_12.dll").is_file())
            # тимчасовий wheel прибрано
            self.assertEqual([p.name for p in dest.iterdir() if p.suffix == ".whl"],
                             [])

    def test_bad_checksum_aborts_and_cleans_up(self):
        data = self._fake_wheel_bytes()   # sha НЕ збігається з реальним піном

        def fake_download(url, destfile, cb, cancel):
            Path(destfile).write_bytes(data)

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cuda"
            with patch.object(cuda_runtime, "cuda_dir", return_value=dest), \
                    patch.object(cuda_runtime, "_wheel_url",
                                 return_value="http://x/w.whl"), \
                    patch.object(cuda_runtime, "_download_file",
                                 side_effect=fake_download):
                with self.assertRaises(cuda_runtime.CudaDownloadError):
                    cuda_runtime.download_and_install()
            # DLL не з'явились, тимчасовий файл прибрано
            self.assertFalse((dest / "cublas64_12.dll").exists())
            self.assertEqual(list(dest.iterdir()), [])


class NormalizeStartupTests(unittest.TestCase):
    """Нормалізація startup-конфігу під GPU (mock cuda_runtime)."""

    class _Cfg:
        def __init__(self, device, compute_type):
            self.device = device
            self.compute_type = compute_type
            self.saved = False

        def save(self):
            self.saved = True

    def test_cuda_invalid_compute_defaults_to_int8_float16(self):
        # порожній/несумісний compute_type на GPU → безпечний дефолт int8_float16
        from fronts.desktop import app as desktop_app

        cfg = self._Cfg("cuda", "")
        with patch.object(desktop_app, "cuda_runtime_available",
                          return_value=True), \
                patch.object(desktop_app.cuda_runtime, "activate") as activate:
            fell_back = desktop_app._normalize_startup_config(cfg, frozen=True)
        self.assertFalse(fell_back)
        self.assertEqual((cfg.device, cfg.compute_type), ("cuda", "int8_float16"))
        activate.assert_called_once()
        self.assertTrue(cfg.saved)

    def test_cuda_keeps_explicit_int8(self):
        # int8 — валідний ЯВНИЙ вибір «Економна» на GPU: НЕ підвищувати до
        # int8_float16 (інакше налаштунок точності не тримався б)
        from fronts.desktop import app as desktop_app

        cfg = self._Cfg("cuda", "int8")
        with patch.object(desktop_app, "cuda_runtime_available",
                          return_value=True), \
                patch.object(desktop_app.cuda_runtime, "activate"):
            fell_back = desktop_app._normalize_startup_config(cfg, frozen=True)
        self.assertFalse(fell_back)
        self.assertEqual((cfg.device, cfg.compute_type), ("cuda", "int8"))
        self.assertFalse(cfg.saved)          # уже валідний GPU-тип — без запису

    def test_cuda_keeps_existing_float16(self):
        from fronts.desktop import app as desktop_app

        cfg = self._Cfg("cuda", "float16")
        with patch.object(desktop_app, "cuda_runtime_available",
                          return_value=True), \
                patch.object(desktop_app.cuda_runtime, "activate"):
            fell_back = desktop_app._normalize_startup_config(cfg, frozen=True)
        self.assertFalse(fell_back)
        self.assertEqual((cfg.device, cfg.compute_type), ("cuda", "float16"))
        self.assertFalse(cfg.saved)          # уже валідний GPU-тип — без запису

    def test_cuda_without_runtime_falls_back_to_cpu_int8(self):
        from fronts.desktop import app as desktop_app

        cfg = self._Cfg("cuda", "int8_float16")
        with patch.object(desktop_app, "cuda_runtime_available",
                          return_value=False):
            fell_back = desktop_app._normalize_startup_config(cfg, frozen=True)
        self.assertTrue(fell_back)
        self.assertEqual((cfg.device, cfg.compute_type), ("cpu", "int8"))
        self.assertTrue(cfg.saved)


if __name__ == "__main__":
    unittest.main()
