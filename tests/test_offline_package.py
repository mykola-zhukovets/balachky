"""Тести офлайн-пакета моделей: збирання на носій і встановлення з носія.

Працюємо на СПРАВЖНІХ файлах у тимчасовій папці: пакет збирається, псується
й читається так само, як це станеться на флешці. Підмінюємо лише те, що не
створити в тесті: перелік наявних компонентів, тип файлової системи носія,
вільне місце на диску та %LOCALAPPDATA% (щоб імпорт не чіпав дані розробника).

Кожен тест позначено мутацією, яку він ловить, — див. коментар «ловить:».
"""
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from whisper_core import cuda_runtime, offline_package, paths
from whisper_core.config import Config
from whisper_core.offline_package import (
    ComponentExportInfo,
    ImportResult,
    InsufficientSpaceError,
    OfflinePackageCancelled,
    OfflinePackageError,
    PackageFormatError,
    UnsafePackageError,
    _get_dir_file_count_and_size,
    _safe_member,
    check_fat32_warning,
    compute_sha256,
    export_package,
    has_symlinks_or_reparse,
    import_destination,
    import_package,
    read_manifest,
)


class OfflinePackageBase(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmp_dir.name)
        self.target_dir = self.base_path / "flash_drive"
        self.target_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = Config()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _cuda_component(self, name="source_cuda", payload=(b"DLL_ONE", b"DLL_TWO")):
        """Справжня папка-джерело з двома файлами + опис компонента для експорту."""
        src = self.base_path / name
        src.mkdir(parents=True, exist_ok=True)
        (src / "cublas64_12.dll").write_bytes(payload[0])
        (src / "cublasLt64_12.dll").write_bytes(payload[1])
        count, size = _get_dir_file_count_and_size(src)
        return ComponentExportInfo(
            id="cuda_runtime",
            type="cuda_runtime",
            display_name="CUDA Runtime (NVIDIA)",
            size_bytes=size,
            file_count=count,
            source_dir=src,
            payload_rel_path="payload/cuda/runtime",
            checksum_file="checksums/cuda-runtime.sha256",
            details={"version": "12.8.4.1"},
        )

    def _build_package(self, comp=None, target=None):
        """Зібраний пакет на “носії” — той самий шлях, що й у бойовому експорті."""
        comp = comp or self._cuda_component()
        target = target or self.target_dir
        target.mkdir(parents=True, exist_ok=True)
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]):
            return export_package(target, self.cfg)

    def _local_appdata(self):
        """Підміна %LOCALAPPDATA% на тимчасову папку: імпорт пише лише в неї."""
        appdata = self.base_path / "appdata"
        appdata.mkdir(parents=True, exist_ok=True)
        return patch.dict(os.environ, {"LOCALAPPDATA": str(appdata)}), appdata

    def _update_manifest_and_marker(self, pkg: Path, data: dict) -> None:
        """Перезаписати manifest.json, manifest.sha256 та BALACHKY_COMPONENTS.marker у тесті."""
        manifest_path = pkg / "manifest.json"
        manifest_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        new_sha = compute_sha256(manifest_path)
        (pkg / "manifest.sha256").write_text(f"{new_sha}  manifest.json\n", encoding="utf-8")
        marker_path = pkg / "BALACHKY_COMPONENTS.marker"
        if marker_path.exists():
            marker = json.loads(marker_path.read_text("utf-8"))
            marker["manifest_sha256"] = new_sha
            marker_path.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")


class TestExportBasics(OfflinePackageBase):
    def test_compute_sha256(self):
        # ловить: підміну алгоритму суми (sha1/md5) або читання не всього файла
        sample_file = self.base_path / "sample.bin"
        sample_file.write_bytes(b"hello balachky offline package")
        self.assertEqual(
            compute_sha256(sample_file),
            "6451a443911bdb0bdaa11d1affa1878fe53e4cac0410a72db39da0cb3f3a9e29",
        )

    def test_size_counts_symlinked_cache_files(self):
        # ловить: повернення `if not fp.is_symlink(): continue` — вага моделі
        # знову стала б 0 і перевірка вільного місця нічого не важила
        cache = self.base_path / "hf_cache"
        blobs = cache / "blobs"
        snap = cache / "snapshots" / "rev1"
        blobs.mkdir(parents=True)
        snap.mkdir(parents=True)
        blob = blobs / "abc123"
        blob.write_bytes(b"W" * 4096)
        (snap / "config.json").write_bytes(b"{}" * 8)
        try:
            os.symlink(blob, snap / "model.bin")
        except (OSError, NotImplementedError):
            self.skipTest("немає прав на символьні посилання")

        count, size = _get_dir_file_count_and_size(snap)
        self.assertEqual(count, 2, "файл-посилання кешу мусить бути порахований")
        self.assertEqual(size, 4096 + 16, "вага має братись із файла, на який показує посилання")

    def test_has_symlinks_or_reparse(self):
        # ловить: заглушку `return False` у перевірці посилань
        dir_clean = self.base_path / "clean_dir"
        dir_clean.mkdir()
        (dir_clean / "regular.txt").write_text("data", encoding="utf-8")
        self.assertFalse(has_symlinks_or_reparse(dir_clean))

        dir_sym = self.base_path / "sym_dir"
        dir_sym.mkdir()
        real_file = self.base_path / "real.txt"
        real_file.write_text("real data", encoding="utf-8")
        try:
            os.symlink(real_file, dir_sym / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("немає прав на символьні посилання")
        self.assertTrue(has_symlinks_or_reparse(dir_sym))

    def test_export_package_success(self):
        # ловить: пропуск маркера, маніфесту, сум або самих файлів у пакеті
        pkg_path = self._build_package()
        self.assertTrue(pkg_path.name.startswith("Balachky-components-"))

        marker = json.loads((pkg_path / "BALACHKY_COMPONENTS.marker").read_text("utf-8"))
        self.assertEqual(marker["product"], "Balachky")
        self.assertEqual(marker["package_format"], 1)

        manifest = json.loads((pkg_path / "manifest.json").read_text("utf-8"))
        self.assertEqual([c["id"] for c in manifest["components"]], ["cuda_runtime"])

        manifest_sha_text = (pkg_path / "manifest.sha256").read_text("utf-8")
        self.assertIn(compute_sha256(pkg_path / "manifest.json"), manifest_sha_text)

        dll = pkg_path / "payload" / "cuda" / "runtime" / "cublas64_12.dll"
        self.assertEqual(dll.read_bytes(), b"DLL_ONE")
        self.assertEqual(
            len((pkg_path / "checksums" / "cuda-runtime.sha256").read_text("utf-8").split("\n")),
            3, "по рядку на кожен файл компонента",
        )

    def test_export_package_symlink_dereference(self):
        # ловить: копіювання посилання як посилання — на іншій машині воно мертве
        blobs_dir = self.base_path / "blobs"
        blobs_dir.mkdir()
        blob_file = blobs_dir / "hash12345"
        blob_file.write_bytes(b"REAL_WEIGHTS_BINARY_DATA")

        snap_dir = self.base_path / "snapshots" / "rev1"
        snap_dir.mkdir(parents=True)
        (snap_dir / "config.json").write_text('{"vocab": 50000}', encoding="utf-8")
        try:
            os.symlink(blob_file, snap_dir / "model.bin")
        except (OSError, NotImplementedError):
            self.skipTest("немає прав на символьні посилання")

        count, size = _get_dir_file_count_and_size(snap_dir)
        fake_asr = ComponentExportInfo(
            id="asr_test_repo",
            type="asr_model",
            display_name="Модель розпізнавання (тест)",
            size_bytes=size,
            file_count=count,
            source_dir=snap_dir,
            payload_rel_path="payload/asr/test--repo/snapshots/rev1",
            checksum_file="checksums/asr-test--repo.sha256",
            details={"repo_id": "test/repo", "revision": "rev1"},
        )
        with patch("whisper_core.offline_package.get_available_components", return_value=[fake_asr]), \
             patch("whisper_core.models.dereference_snapshot", return_value=True):
            pkg_path = export_package(self.target_dir, self.cfg)

        weights = pkg_path / "payload" / "asr" / "test--repo" / "snapshots" / "rev1" / "model.bin"
        self.assertFalse(weights.is_symlink())
        self.assertEqual(weights.read_bytes(), b"REAL_WEIGHTS_BINARY_DATA")

    def test_export_package_cancellation(self):
        # ловить: неприбрану тимчасову папку після скасування (напівживий пакет)
        src = self.base_path / "source_cancel"
        src.mkdir()
        (src / "file.txt").write_bytes(b"DATA" * 1000)
        comp = ComponentExportInfo(
            id="voice_test",
            type="voice",
            display_name="Голос озвучення (тест)",
            size_bytes=4000,
            file_count=1,
            source_dir=src,
            payload_rel_path="payload/voices/test",
            checksum_file="checksums/voice-test.sha256",
            details={"voice_id": "test"},
        )
        cancelled = {"flag": False}
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]):
            with self.assertRaises(OfflinePackageCancelled):
                export_package(
                    self.target_dir, self.cfg,
                    progress_cb=lambda copied, total, name: cancelled.update(flag=copied > 100),
                    cancel_check=lambda: cancelled["flag"],
                )
        self.assertEqual(list(self.target_dir.iterdir()), [])

    def test_export_package_insufficient_space(self):
        # ловить: зняту перевірку вільного місця перед копіюванням
        comp = self._cuda_component()
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]), \
             patch("shutil.disk_usage", return_value=MagicMock(free=1)):
            with self.assertRaises(InsufficientSpaceError):
                export_package(self.target_dir, self.cfg)

    def test_export_warns_but_continues_on_fat32(self):
        """FAT32 з великим файлом — ПОПЕРЕДЖЕННЯ, не заборона.

        Рішення власника 25.07: «чотири гігабайта це зараз мало, нехай просто буде
        попередження». Носій FAT32 не приймає файл понад 4 ГБ, але людина може мати
        причину продовжити — менші складові поїдуть. Раніше ядро тут кидало помилку
        й не збирало нічого; тепер лише пише в журнал, а діалог вивантаження показує
        попередження ДО початку роботи."""
        comp = self._cuda_component(payload=(b"X" * 64, b"Y" * 8))
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp]), \
             patch("whisper_core.offline_package.get_filesystem_type", return_value="FAT32"), \
             patch("whisper_core.offline_package.FOUR_GIB", 32):
            with self.assertLogs("balachky.offline_package", level="WARNING") as logs:
                pkg = export_package(self.target_dir, self.cfg)
        self.assertTrue(any("FAT32" in line for line in logs.output),
                        "попередження про FAT32 не потрапило в журнал")
        self.assertTrue(pkg.exists(), "пакет мусив зібратись попри попередження")

    def test_fat32_warning_text_offered_to_interface(self):
        """check_fat32_warning віддає ключ тексту, який показує діалог вивантаження."""
        comp = self._cuda_component(payload=(b"X" * 64, b"Y" * 8))
        with patch("whisper_core.offline_package.get_filesystem_type", return_value="FAT32"), \
             patch("whisper_core.offline_package.FOUR_GIB", 32):
            self.assertEqual(check_fat32_warning(self.target_dir, [comp]),
                             "offline_pkg_fat32_warning")

    def test_fat32_check_silent_on_ntfs_and_small_files(self):
        # ловить: попередження, що спрацьовує завжди (тоді NTFS-носій теж лаявся б)
        comp = self._cuda_component()
        with patch("whisper_core.offline_package.get_filesystem_type", return_value="NTFS"), \
             patch("whisper_core.offline_package.FOUR_GIB", 4):
            self.assertIsNone(check_fat32_warning(self.target_dir, [comp]))
        with patch("whisper_core.offline_package.get_filesystem_type", return_value="FAT32"):
            self.assertIsNone(check_fat32_warning(self.target_dir, [comp]))


class TestImportPathSafety(OfflinePackageBase):
    HOSTILE = (
        "..",
        "../evil.txt",
        "payload/../../evil.txt",
        "..\\..\\evil.txt",
        "payload\\..\\..\\evil.txt",
        "C:\\Windows\\System32\\evil.dll",
        "/etc/passwd",
        "\\\\server\\share\\evil.dll",
        "",
        "   ",
        # службовий сегмент і хвостовий пробіл: Windows тихо зріже пробіл,
        # тому такі імена відкидаємо самі, а не покладаємось на resolve()
        "payload/./evil.dll",
        "payload/evil.dll ",
        "payload/ evil.dll",
    )

    def test_safe_member_rejects_escapes(self):
        # ловить: зняту перевірку шляхів — пакет писав би куди завгодно на диску
        base = self.base_path / "dest"
        base.mkdir()
        for rel in self.HOSTILE:
            with self.subTest(rel=rel):
                with self.assertRaises(UnsafePackageError):
                    _safe_member(base, rel)

    def test_safe_member_accepts_normal_names(self):
        # ловить: надто сувору перевірку, що відкидає звичайні імена всередині пакета
        base = self.base_path / "dest"
        base.mkdir()
        self.assertEqual(
            _safe_member(base, "payload/cuda/runtime/cublas64_12.dll"),
            base / "payload" / "cuda" / "runtime" / "cublas64_12.dll",
        )

    def test_import_rejects_package_with_parent_escape(self):
        # ловить: довіру до payload_path з опису пакета (вихід за межі призначення)
        pkg = self._build_package()
        outside = self.base_path / "outside"
        outside.mkdir()
        self._retarget_payload(pkg, "../../outside/stolen")

        env_patch, _appdata = self._local_appdata()
        with env_patch:
            with self.assertRaises(UnsafePackageError):
                import_package(pkg, self.cfg)
        self.assertEqual(list(outside.iterdir()), [], "нічого не мусило вилізти за межі")

    def test_import_rejects_symlinked_payload_file(self):
        # ловить: копіювання файла-посилання з пакета (підміна цілі на чужий файл)
        pkg = self._build_package()
        secret = self.base_path / "secret.txt"
        secret.write_text("не для пакета", encoding="utf-8")
        victim = pkg / "payload" / "cuda" / "runtime" / "cublas64_12.dll"
        victim.unlink()
        try:
            os.symlink(secret, victim)
        except (OSError, NotImplementedError):
            self.skipTest("немає прав на символьні посилання")

        env_patch, _appdata = self._local_appdata()
        with env_patch:
            with self.assertRaises(UnsafePackageError):
                import_package(pkg, self.cfg)



    def _retarget_payload(self, pkg: Path, new_payload_path: str) -> None:
        """Перезаписати payload_path в описі пакета (як це зробив би зловмисник)."""
        manifest_path = pkg / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        data["components"][0]["payload_path"] = new_payload_path
        self._update_manifest_and_marker(pkg, data)


class TestImportPackage(OfflinePackageBase):
    def test_import_installs_component(self):
        # ловить: імпорт, який нічого не переносить (кнопка «Імпортувати» без діла)
        pkg = self._build_package()
        env_patch, appdata = self._local_appdata()
        with env_patch:
            dest = cuda_runtime.cuda_dir()
            result = import_package(pkg, self.cfg)

        self.assertIsInstance(result, ImportResult)
        self.assertEqual(result.installed, ["cuda_runtime"])
        self.assertEqual(result.bad_files, [])
        self.assertEqual((dest / "cublas64_12.dll").read_bytes(), b"DLL_ONE")
        self.assertEqual((dest / "cublasLt64_12.dll").read_bytes(), b"DLL_TWO")
        self.assertFalse([p for p in appdata.rglob(".balachky-import-*")], "тимчасове прибрано")

    def test_import_reports_progress(self):
        # ловить: втрачений зворотний виклик прогресу (вікно імпорту стояло б мертвим)
        pkg = self._build_package()
        seen = []
        env_patch, _appdata = self._local_appdata()
        with env_patch:
            import_package(pkg, self.cfg, progress=lambda done, total, name: seen.append(name))
        self.assertEqual(len(seen), 2)

    def test_import_catches_tampered_file(self):
        # ловить: встановлення без звіряння сум — підмінений файл поїхав би в моделі
        pkg = self._build_package()
        victim = pkg / "payload" / "cuda" / "runtime" / "cublasLt64_12.dll"
        victim.write_bytes(b"TROJAN_")            # той самий розмір, інший вміст

        env_patch, appdata = self._local_appdata()
        with env_patch:
            dest = cuda_runtime.cuda_dir()
            result = import_package(pkg, self.cfg)

        self.assertEqual(result.installed, [], "компонент із битим файлом не встановлюється")
        self.assertEqual(
            result.bad_files,
            ["payload/cuda/runtime/cublasLt64_12.dll"],
            "людині треба сказати, які саме файли не пройшли",
        )
        self.assertFalse(dest.exists(), "напівживої моделі не лишається")
        self.assertFalse([p for p in appdata.rglob(".balachky-import-*")])

    def test_import_catches_extra_and_missing_files(self):
        # ловить: файли поза переліком сум (дописані в пакет) і обіцяні, але зникли
        pkg = self._build_package()
        (pkg / "payload" / "cuda" / "runtime" / "payload.dll").write_bytes(b"EXTRA")
        env_patch, _appdata = self._local_appdata()
        with env_patch:
            result = import_package(pkg, self.cfg)
        self.assertEqual(result.installed, [])
        self.assertIn("payload/cuda/runtime/payload.dll", result.bad_files)

        pkg2 = self._build_package(self._cuda_component(name="source_cuda2"),
                                   target=self.base_path / "flash_drive2")
        (pkg2 / "payload" / "cuda" / "runtime" / "cublas64_12.dll").unlink()
        with self._local_appdata()[0]:
            result2 = import_package(pkg2, self.cfg)
        self.assertEqual(result2.installed, [])
        self.assertIn("payload/cuda/runtime/cublas64_12.dll", result2.bad_files)

    def test_interrupted_import_leaves_no_trash(self):
        # ловить: розпакування прямо в призначення — обрив лишав би півмоделі
        pkg = self._build_package()
        env_patch, appdata = self._local_appdata()
        real_copyfile = shutil.copyfile
        calls = {"n": 0}

        def flaky_copyfile(src, dst, *args, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(1117, "носій висмикнули")
            return real_copyfile(src, dst)

        with env_patch:
            dest = cuda_runtime.cuda_dir()
            dest.mkdir(parents=True)
            (dest / "cublas64_12.dll").write_bytes(b"OLD_VERSION")
            with patch("whisper_core.offline_package._copy_file_with_progress", side_effect=flaky_copyfile):
                with self.assertRaises(OSError):
                    import_package(pkg, self.cfg)

            self.assertEqual((dest / "cublas64_12.dll").read_bytes(), b"OLD_VERSION",
                             "попередній стан недоторканий")
            self.assertFalse([p for p in appdata.rglob(".balachky-import-*")], "сміття прибрано")
            self.assertFalse([p for p in appdata.rglob("*.old-*")])

    def test_import_stops_when_no_space(self):
        # ловить: зняту перевірку місця — імпорт валився б на середині
        pkg = self._build_package()
        env_patch, _appdata = self._local_appdata()
        with env_patch, patch("whisper_core.offline_package.shutil.disk_usage",
                              return_value=MagicMock(free=0)):
            with self.assertRaises(InsufficientSpaceError) as ctx:
                import_package(pkg, self.cfg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_space_error")

    def test_import_skips_unknown_component_type(self):
        # ловить: падіння на пакеті з новим типом компонента (замість чесного пропуску)
        pkg = self._build_package()
        manifest_path = pkg / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        data["components"][0]["type"] = "hologram"
        self._update_manifest_and_marker(pkg, data)

        env_patch, _appdata = self._local_appdata()
        with env_patch:
            result = import_package(pkg, self.cfg)
        self.assertEqual(result.installed, [])
        self.assertEqual(result.skipped, ["cuda_runtime"])

    def test_import_destination_rejects_crafted_names(self):
        # ловить: побудову шляху прямо з опису пакета (id моделі з «..»)
        for details in (
            {"repo_id": "../../evil", "revision": "rev1"},
            {"repo_id": "org/repo", "revision": ".."},
            {"repo_id": "org/repo", "revision": "C:\\Windows"},
        ):
            with self.subTest(details=details):
                self.assertIsNone(import_destination(
                    {"type": "asr_model", "details": details}, self.cfg))
        self.assertIsNone(import_destination(
            {"type": "voice", "details": {"voice_id": "../../evil"}}, self.cfg))
        self.assertIsNone(import_destination(
            {"type": "llm", "details": {"preset": "../../evil"}}, self.cfg))

    def test_import_destination_known_types(self):
        # ловить: розкладання всіх компонентів в одну папку
        with patch.object(paths, "USER_DIR", self.base_path / "userdata"):
            voice = import_destination(
                {"type": "voice", "details": {"voice_id": "styletts2_ua"}}, self.cfg)
            llm = import_destination({"type": "llm", "details": {"preset": "fast"}}, self.cfg)
        self.assertEqual(voice.name, "styletts2_ua")
        self.assertEqual(llm.name, "fast")
        self.assertNotEqual(voice.parent, llm.parent)


class TestManifestReading(OfflinePackageBase):
    def test_missing_marker(self):
        # ловить: спробу читати як пакет будь-яку папку з носія
        plain = self.base_path / "just_a_folder"
        plain.mkdir()
        with self.assertRaises(OfflinePackageError) as ctx:
            read_manifest(plain)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_not_a_package")

    def test_missing_manifest(self):
        # ловить: тиху відмову без пояснення, коли опис вмісту не доїхав
        pkg = self._build_package()
        (pkg / "manifest.json").unlink()
        with self.assertRaises(OfflinePackageError) as ctx:
            read_manifest(pkg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_no_manifest")

    def test_empty_manifest(self):
        # ловить: падіння з JSONDecodeError замість зрозумілого повідомлення
        pkg = self._build_package()
        manifest_path = pkg / "manifest.json"
        manifest_path.write_text("", encoding="utf-8")
        (pkg / "manifest.sha256").write_text(
            f"{compute_sha256(manifest_path)}  manifest.json\n", encoding="utf-8")
        with self.assertRaises(OfflinePackageError) as ctx:
            read_manifest(pkg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_manifest_broken")

    def test_manifest_sha_mismatch(self):
        # ловить: зняте звіряння суми самого опису вмісту (дописані компоненти)
        pkg = self._build_package()
        data = json.loads((pkg / "manifest.json").read_text("utf-8"))
        data["components"][0]["display_name"] = "щось інше"
        (pkg / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(OfflinePackageError) as ctx:
            read_manifest(pkg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_manifest_broken")

    def test_unsupported_format(self):
        # ловить: тихе читання пакета чужого формату (несумісна розкладка файлів)
        pkg = self._build_package()
        manifest_path = pkg / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        data["package_format"] = 99
        self._update_manifest_and_marker(pkg, data)
        with self.assertRaises(PackageFormatError) as ctx:
            read_manifest(pkg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_format_unsupported")

    def test_other_app_version_is_readable(self):
        # ловить: відмову читати пакет іншої версії програми при сумісному форматі
        pkg = self._build_package()
        manifest_path = pkg / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        data["created_by_app_version"] = "1.9.9"
        self._update_manifest_and_marker(pkg, data)
        self.assertEqual(read_manifest(pkg)["created_by_app_version"], "1.9.9")

    def test_broken_checksum_file(self):
        # ловить: приймання пакета з порожнім чи покаліченим переліком сум
        pkg = self._build_package()
        (pkg / "checksums" / "cuda-runtime.sha256").write_text("не сума\n", encoding="utf-8")
        env_patch, _appdata = self._local_appdata()
        with env_patch:
            with self.assertRaises(OfflinePackageError) as ctx:
                import_package(pkg, self.cfg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_manifest_broken")


class TestReadmeInstructionMatchesUI(unittest.TestCase):
    def test_readme_instruction_matches_ui_button_text(self):
        """Інструкція в пакеті описує кнопку імпорту буква в букву як у UI."""
        from fronts.desktop.i18n import STRINGS
        from whisper_core.offline_package import OFFLINE_IMPORT_BUTTON_TEXT_UK

        comp_dir = Path(tempfile.mkdtemp())
        (comp_dir / "model.bin").write_bytes(b"data")
        comp = ComponentExportInfo(
            id="test_comp",
            type="punctuator",
            display_name="Test Component",
            size_bytes=4,
            file_count=1,
            source_dir=comp_dir,
            payload_rel_path="payload/test",
            checksum_file="checksums/test.sha256",
            details={},
        )
        target = Path(tempfile.mkdtemp())
        cfg = Config()
        try:
            with patch("whisper_core.offline_package.get_available_components", return_value=[comp]), \
                 patch("shutil.disk_usage", return_value=MagicMock(free=10**9)):
                pkg_dir = export_package(target, cfg)
                readme_path = pkg_dir / "README-UK.txt"
                self.assertTrue(readme_path.exists())
                text = readme_path.read_text(encoding="utf-8")

                ui_button_text = STRINGS["uk"]["offline_pkg_btn_import"]
                self.assertEqual(ui_button_text, OFFLINE_IMPORT_BUTTON_TEXT_UK)
                self.assertIn(f"«{ui_button_text}»", text)
        finally:
            shutil.rmtree(comp_dir, ignore_errors=True)
            shutil.rmtree(target, ignore_errors=True)


class TestOfflinePackageHoleFixes(OfflinePackageBase):
    """Тести для 5 закритих дірок офлайн-пакета."""

    def test_import_rejects_checksum_file_with_parent_escape(self):
        # ловить: відсутність перевірки шляхів у файлі checksums/*.sha256 (виведення за межі призначення)
        pkg = self._build_package()
        cs_file = pkg / "checksums" / "cuda-runtime.sha256"
        lines = cs_file.read_text("utf-8").splitlines()
        bad_line = f"{lines[0].split()[0]}  ../outside_stolen.txt"
        cs_file.write_text("\n".join(lines) + "\n" + bad_line + "\n", encoding="utf-8")

        manifest_path = pkg / "manifest.json"
        data = json.loads(manifest_path.read_text("utf-8"))
        data["components"][0]["checksum_file_sha256"] = compute_sha256(cs_file)
        self._update_manifest_and_marker(pkg, data)

        env_patch, _appdata = self._local_appdata()
        with env_patch:
            with self.assertRaises(UnsafePackageError):
                import_package(pkg, self.cfg)

    def test_import_rejects_symlinked_marker_or_manifest(self):
        # ловить: читання символьних посилань замість звичайних файлів у read_manifest
        pkg = self._build_package()
        secret = self.base_path / "secret.txt"
        secret.write_text("secret_data", encoding="utf-8")
        marker = pkg / "BALACHKY_COMPONENTS.marker"
        marker.unlink()
        try:
            os.symlink(secret, marker)
        except (OSError, NotImplementedError):
            self.skipTest("немає прав на символьні посилання")

        with self.assertRaises(OfflinePackageError) as ctx:
            read_manifest(pkg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_not_a_package")

    def test_import_rejects_tampered_non_payload_files(self):
        # ловить: відсутність перевірки контрольних сум не-payload файлів (README-UK.txt, checksums)
        pkg = self._build_package()

        readme = pkg / "README-UK.txt"
        readme.write_text("Шкідливий вміст README", encoding="utf-8")
        env_patch, _appdata = self._local_appdata()
        with env_patch:
            with self.assertRaises(OfflinePackageError) as ctx:
                import_package(pkg, self.cfg)
        self.assertEqual(str(ctx.exception), "offline_pkg_import_manifest_broken")

        pkg2 = self._build_package(target=self.base_path / "flash_drive2")
        cs_file = pkg2 / "checksums" / "cuda-runtime.sha256"
        cs_file.write_text("0000000000000000000000000000000000000000000000000000000000000000  payload/cuda/runtime/cublas64_12.dll\n", encoding="utf-8")
        with env_patch:
            with self.assertRaises(OfflinePackageError) as ctx2:
                import_package(pkg2, self.cfg)
        self.assertEqual(str(ctx2.exception), "offline_pkg_import_manifest_broken")

    def test_import_checks_total_space_before_copying(self):
        # ловить: перевірку місця по компонентах під час копіювання замість сумарної перевірки ДО копіювання
        comp1 = self._cuda_component(name="source_cuda1")
        comp2 = ComponentExportInfo(
            id="voice_test",
            type="voice",
            display_name="Голос озвучення (тест)",
            size_bytes=1000,
            file_count=1,
            source_dir=self.base_path / "source_voice",
            payload_rel_path="payload/voices/test",
            checksum_file="checksums/voice-test.sha256",
            details={"voice_id": "test"},
        )
        comp2.source_dir.mkdir(parents=True, exist_ok=True)
        (comp2.source_dir / "voice.bin").write_bytes(b"V" * 1000)

        pkg_target = self.base_path / "flash_drive_space"
        with patch("whisper_core.offline_package.get_available_components", return_value=[comp1, comp2]):
            pkg = export_package(pkg_target, self.cfg)

        copied_names = []
        env_patch, _appdata = self._local_appdata()
        free_space = comp1.size_bytes + 500

        with env_patch, patch("shutil.disk_usage", return_value=MagicMock(free=free_space)):
            with self.assertRaises(InsufficientSpaceError):
                import_package(
                    pkg,
                    self.cfg,
                    progress=lambda done, total, name: copied_names.append(name),
                )

        self.assertEqual(copied_names, [], "Жоден файл не мав бути скопійований при недостатньому загальному місці")

    def test_import_package_cancellation(self):
        # ловить: відсутність перевірки скасування або блокуюче копіювання у import_package
        pkg = self._build_package()
        env_patch, appdata = self._local_appdata()
        cancelled_calls = {"count": 0}

        def cancel_check():
            cancelled_calls["count"] += 1
            return cancelled_calls["count"] > 1

        with env_patch:
            with self.assertRaises(OfflinePackageCancelled):
                import_package(
                    pkg,
                    self.cfg,
                    cancel_check=cancel_check,
                )

        self.assertFalse([p for p in appdata.rglob(".balachky-import-*")], "тимчасова папка має бути прибрана при скасуванні імпорту")

    def test_fat32_check_detects_single_file_oversize(self):
        # ловить: пропуск поодиноких файлів-компонентів у check_fat32_warning
        single_file = self.base_path / "single_model.bin"
        single_file.write_bytes(b"D" * 100)
        comp = ComponentExportInfo(
            id="single_comp",
            type="punctuator",
            display_name="Пунктуатор",
            size_bytes=100,
            file_count=1,
            source_dir=single_file,
            payload_rel_path="payload/punctuator",
            checksum_file="checksums/punctuator.sha256",
            details={},
        )
        with patch("whisper_core.offline_package.get_filesystem_type", return_value="FAT32"), \
             patch("whisper_core.offline_package.FOUR_GIB", 50):
            warning = check_fat32_warning(self.target_dir, [comp])
            self.assertEqual(warning, "offline_pkg_fat32_warning")

    def test_import_warns_but_continues_on_fat32_destination(self):
        """Цільова тека на FAT32 — попередження в журнал, робота не спиняється.

        Те саме рішення власника, що й для вивантаження. Тут блокування ще менш
        доречне: тека даних на FAT32-носії — рідкість, а якщо великий файл справді
        не влізе, копіювання впаде з чесною системною помилкою, а не з нашою
        забороною наперед."""
        pkg = self._build_package()
        env_patch, _appdata = self._local_appdata()
        with env_patch, patch("whisper_core.offline_package.get_filesystem_type", return_value="FAT32"), \
             patch("whisper_core.offline_package.FOUR_GIB", 2):
            with self.assertLogs("balachky.offline_package", level="WARNING") as logs:
                result = import_package(pkg, self.cfg)
        self.assertTrue(any("FAT32" in line for line in logs.output),
                        "попередження про FAT32 не потрапило в журнал")
        self.assertIsNotNone(result, "встановлення мусило пройти попри попередження")

    def test_fat32_check_ignores_exfat(self):
        # ловить: помилкове блокування носія exFAT через пошук рядка "FAT"
        comp = self._cuda_component(payload=(b"X" * 64, b"Y" * 8))
        with patch("whisper_core.offline_package.get_filesystem_type", return_value="exFAT"), \
             patch("whisper_core.offline_package.FOUR_GIB", 32):
            self.assertIsNone(check_fat32_warning(self.target_dir, [comp]))


if __name__ == "__main__":
    unittest.main()
