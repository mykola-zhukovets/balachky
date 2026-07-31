"""Тести доставки оновлення: завантаження, SHA-256, resume, HTTPS-гейт.

Локальний HTTPS-сервер (stdlib + self-signed cert) імітує GitHub-asset —
без інтернету й без сторонніх мереж-бібліотек. Перевіряємо реальний код
шляху (справжній TLS, справжній Range), а не моки: цілісний файл, обірване/
докачане завантаження, відмову при битому SHA, гейт «лише https».
"""
import datetime
import hashlib
import os
import shutil
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests._isolation import reset_process_caches
from whisper_core import updater


def _self_signed(tmp: Path):
    """Згенерувати self-signed cert для 127.0.0.1; повернути (certfile, keyfile)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(__import__("ipaddress").ip_address("127.0.0.1"))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    cf, kf = tmp / "c.pem", tmp / "k.pem"
    cf.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    kf.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    return cf, kf


def _make_server(payload: bytes, certfile, keyfile, *,
                 support_range=True, cut_at=None):
    """HTTPS-сервер, що віддає `payload`. cut_at — обірвати після N байтів
    (розрив звʼязку). support_range — чи слухати Range (докачування)."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            start = 0
            rng = self.headers.get("Range")
            if support_range and rng and rng.startswith("bytes="):
                try:
                    start = int(rng.split("=", 1)[1].split("-", 1)[0])
                except ValueError:
                    start = 0
            if support_range and start:
                body = payload[start:]
                self.send_response(206)
                self.send_header("Content-Range",
                                 f"bytes {start}-{len(payload)-1}/{len(payload)}")
            else:
                body = payload  # без Range — все з нуля
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body[:cut_at] if cut_at is not None else body)
            except (BrokenPipeError, ConnectionError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class MakeServerTlsTests(unittest.TestCase):
    def test_explicitly_requires_tls_1_2(self):
        constructor_protocols = []
        minimum_version_assignments = []
        cert_chain_calls = []
        wrap_socket_calls = []
        server_socket = mock.sentinel.server_socket
        wrapped_socket = mock.sentinel.wrapped_socket
        server = mock.Mock()
        server.socket = server_socket
        thread = mock.Mock()

        class TrackingSSLContext:
            def __init__(self, protocol):
                constructor_protocols.append(protocol)

            @property
            def minimum_version(self):
                raise AssertionError("minimum_version must be assigned explicitly")

            @minimum_version.setter
            def minimum_version(self, version):
                minimum_version_assignments.append(version)

            def load_cert_chain(self, certfile, keyfile):
                cert_chain_calls.append((certfile, keyfile))

            def wrap_socket(self, sock, *, server_side):
                wrap_socket_calls.append((sock, server_side))
                return wrapped_socket

        certfile = mock.sentinel.certfile
        keyfile = mock.sentinel.keyfile
        with (
            mock.patch(
                f"{__name__}.ThreadingHTTPServer", return_value=server
            ) as server_type,
            mock.patch.object(
                ssl, "SSLContext", TrackingSSLContext
            ),
            mock.patch.object(
                threading, "Thread", return_value=thread
            ) as thread_type,
        ):
            result = _make_server(b"payload", certfile, keyfile)

        self.assertEqual(constructor_protocols, [ssl.PROTOCOL_TLS_SERVER])
        self.assertEqual(
            minimum_version_assignments, [ssl.TLSVersion.TLSv1_2]
        )
        self.assertEqual(cert_chain_calls, [(certfile, keyfile)])
        self.assertEqual(wrap_socket_calls, [(server_socket, True)])
        self.assertIs(server.socket, wrapped_socket)
        self.assertIs(result, server)
        server_type.assert_called_once_with(("127.0.0.1", 0), mock.ANY)
        thread_type.assert_called_once_with(
            target=server.serve_forever, daemon=True
        )
        thread.start.assert_called_once_with()


class NormalizeShaTests(unittest.TestCase):
    def test_variants(self):
        hexd = "a" * 64
        self.assertEqual(updater.normalize_sha256("sha256:" + hexd.upper()), hexd)
        self.assertEqual(updater.normalize_sha256(f"{hexd} *setup.exe"), hexd)
        self.assertEqual(updater.normalize_sha256(hexd), hexd)
        self.assertIsNone(updater.normalize_sha256(""))
        self.assertIsNone(updater.normalize_sha256(None))
        self.assertIsNone(updater.normalize_sha256("deadbeef"))  # закоротко


class DownloadTests(unittest.TestCase):
    def setUp(self):
        reset_process_caches()
        self.payload = bytes(range(256)) * 400  # ~100 КБ
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self._tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.cert, self.key = _self_signed(self._tmp)
        # клієнтський контекст, що приймає наш self-signed cert (не вимикає TLS)
        self.ctx = ssl.create_default_context(cafile=str(self.cert))
        self.ctx.check_hostname = False

    def _dest(self):
        d = tempfile.mkdtemp(dir=self._tmp)
        return d

    def _url(self, srv):
        return f"https://127.0.0.1:{srv.server_address[1]}/setup.exe"

    def test_full_download_ok(self):
        srv = _make_server(self.payload, self.cert, self.key)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        seen = []
        out = updater.download_installer(
            self._url(srv), self.sha, dest_dir=dest, context=self.ctx,
            progress=lambda d, t: seen.append((d, t)))
        self.assertTrue(out.exists())
        self.assertEqual(updater.sha256_of(out), self.sha)
        self.assertEqual(seen[-1][0], len(self.payload))
        self.assertEqual(seen[-1][1], len(self.payload))  # total відомий
        self.assertFalse(out.with_name(out.name + ".part").exists())

    def test_https_required(self):
        with self.assertRaises(updater.InsecureURLError):
            updater.download_installer(
                "http://127.0.0.1/setup.exe", self.sha, dest_dir=self._dest())

    def test_bad_sha_deletes_and_raises(self):
        srv = _make_server(self.payload, self.cert, self.key)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        with self.assertRaises(updater.ChecksumError):
            updater.download_installer(
                self._url(srv), "b" * 64, dest_dir=dest, context=self.ctx)
        final = updater.local_installer_path(self._url(srv), dest)
        self.assertFalse(final.exists())
        self.assertFalse(final.with_name(final.name + ".part").exists())

    def test_missing_expected_sha_refuses(self):
        with self.assertRaises(updater.ChecksumError):
            updater.download_installer(
                "https://127.0.0.1/setup.exe", "", dest_dir=self._dest())

    def test_interrupted_then_resume(self):
        cut = 40000
        srv = _make_server(self.payload, self.cert, self.key, cut_at=cut)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        part = updater.local_installer_path(self._url(srv), dest)
        part = part.with_name(part.name + ".part")
        # 1) обрив: .part лишається, файл не готовий
        with self.assertRaises(updater.DownloadError):
            updater.download_installer(
                self._url(srv), self.sha, dest_dir=dest, context=self.ctx)
        self.assertTrue(part.exists())
        self.assertEqual(part.stat().st_size, cut)
        # 2) цілий сервер + Range → докачуємо решту в той самий dest
        srv.shutdown()
        srv2 = _make_server(self.payload, self.cert, self.key)
        self.addCleanup(srv2.shutdown)
        out = updater.download_installer(
            self._url(srv2), self.sha, dest_dir=dest, resume=True, context=self.ctx)
        self.assertEqual(updater.sha256_of(out), self.sha)
        self.assertFalse(part.exists())

    def test_no_range_support_clean_restart(self):
        # частковий .part є, але сервер не вміє Range → скачуємо з нуля, не псуємо
        srv = _make_server(self.payload, self.cert, self.key, support_range=False)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        part = updater.local_installer_path(self._url(srv), dest)
        part = part.with_name(part.name + ".part")
        part.write_bytes(b"garbage-partial")  # «недокачане» сміття
        out = updater.download_installer(
            self._url(srv), self.sha, dest_dir=dest, context=self.ctx)
        self.assertEqual(updater.sha256_of(out), self.sha)

    def test_is_downloaded(self):
        srv = _make_server(self.payload, self.cert, self.key)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        url = self._url(srv)
        self.assertFalse(updater.is_downloaded(url, self.sha, dest))
        updater.download_installer(url, self.sha, dest_dir=dest, context=self.ctx)
        self.assertTrue(updater.is_downloaded(url, self.sha, dest))
        self.assertFalse(updater.is_downloaded(url, "c" * 64, dest))

    def test_installer_ready_rehashes_same_size_cached_file(self):
        dest = self._dest()
        url = "https://example.invalid/setup.exe"
        final = updater.local_installer_path(url, dest)
        final.write_bytes(self.payload)
        self.assertEqual(
            updater.installer_ready(url, self.sha, dest), final)

        final.write_bytes(b"x" * len(self.payload))
        self.assertIsNone(
            updater.installer_ready(url, self.sha, dest))

    def test_download_forgets_cached_invalid_final_with_same_fingerprint(self):
        srv = _make_server(self.payload, self.cert, self.key)
        self.addCleanup(srv.shutdown)
        dest = self._dest()
        url = self._url(srv)
        final = updater.local_installer_path(url, dest)
        final.write_bytes(b"x" * len(self.payload))
        fingerprint = (len(self.payload), 1)

        with mock.patch.object(
                updater, "_integrity_fingerprint",
                return_value=fingerprint):
            self.assertIsNone(
                updater.installer_ready(url, self.sha, dest))
            self.assertEqual(
                updater.download_installer(
                    url, self.sha, dest_dir=dest, context=self.ctx),
                final)
            self.assertEqual(
                updater.installer_ready(url, self.sha, dest), final)


class InstallerReadyCacheTests(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def test_integrity_cache_isolated_by_installer_path(self):
        payload = b"trusted installer payload"
        expected_sha = hashlib.sha256(payload).hexdigest()
        url = "https://example.invalid/setup.exe"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted_dir = root / "trusted"
            corrupt_dir = root / "corrupt"
            trusted_dir.mkdir()
            corrupt_dir.mkdir()
            trusted = updater.local_installer_path(url, trusted_dir)
            corrupt = updater.local_installer_path(url, corrupt_dir)
            trusted.write_bytes(payload)
            corrupt.write_bytes(payload[::-1])
            stamp = 1_700_000_000_000_000_000
            os.utime(trusted, ns=(stamp, stamp))
            os.utime(corrupt, ns=(stamp, stamp))
            self.assertEqual(
                updater._integrity_fingerprint(trusted),
                updater._integrity_fingerprint(corrupt))

            self.assertEqual(
                updater.installer_ready(url, expected_sha, trusted_dir),
                trusted)
            self.assertIsNone(
                updater.installer_ready(url, expected_sha, corrupt_dir))

    def test_installer_ready_rehashes_only_after_mtime_change(self):
        payload = bytes(range(256)) * 400
        expected_sha = hashlib.sha256(payload).hexdigest()
        dest = (Path.cwd() / ".test-artifacts" /
                f"installer-{next(tempfile._get_candidate_names())}")
        dest.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(dest, ignore_errors=True))
        url = "https://example.invalid/setup.exe"
        final = updater.local_installer_path(url, dest)
        final.write_bytes(payload)
        real_sha256_of = updater.sha256_of
        calls = []

        def counted_sha256_of(*args, **kwargs):
            calls.append(True)
            return real_sha256_of(*args, **kwargs)

        with mock.patch.object(
                updater, "sha256_of", side_effect=counted_sha256_of):
            self.assertEqual(
                updater.installer_ready(url, expected_sha, dest), final)
            self.assertEqual(len(calls), 1)
            self.assertEqual(
                updater.installer_ready(url, expected_sha, dest), final)
            self.assertEqual(len(calls), 1)
            old_stat = final.stat()
            os.utime(
                final,
                ns=(old_stat.st_atime_ns,
                    old_stat.st_mtime_ns + 2_000_000_000))
            self.assertEqual(
                updater.installer_ready(url, expected_sha, dest), final)
            self.assertEqual(len(calls), 2)


class LaunchInstallerTests(unittest.TestCase):
    def test_corrupt_cache_is_not_started(self):
        from fronts.desktop.app import DesktopApp
        from PySide6.QtCore import QProcess

        app = mock.Mock()
        controller = mock.Mock()
        controller.app = app
        controller.delivery_state.return_value = (
            "https://example.invalid/setup.exe", "a" * 64, None)
        with mock.patch(
                "whisper_core.updater.installer_ready",
                return_value=None) as ready, \
                mock.patch.object(QProcess, "startDetached") as start:
            DesktopApp.launch_installer_and_quit(
                controller, "C:/cache/setup.exe")

        ready.assert_called_once_with(
            "https://example.invalid/setup.exe", "a" * 64, rehash=True)
        start.assert_not_called()
        app.quit.assert_not_called()


class PickInstallerTests(unittest.TestCase):
    def test_extract_from_release_json(self):
        from whisper_core import updates
        assets = [
            {"name": "notes.txt", "browser_download_url": "https://x/notes.txt"},
            {"name": "Balachky-Setup-1.1.0.exe",
             "browser_download_url": "https://x/s.exe",
             "digest": "sha256:" + "A" * 64},
        ]
        url, sha = updates._pick_installer(assets)
        self.assertEqual(url, "https://x/s.exe")
        self.assertEqual(sha, "a" * 64)

    def test_no_exe_or_bad_input(self):
        from whisper_core import updates
        self.assertEqual(updates._pick_installer([{"name": "x.zip"}]), (None, None))
        self.assertEqual(updates._pick_installer(None), (None, None))
        # exe без digest → url є, sha None (UI сховає кнопку)
        self.assertEqual(
            updates._pick_installer([{"name": "s.exe",
                                      "browser_download_url": "https://x/s.exe"}]),
            ("https://x/s.exe", None))


if __name__ == "__main__":
    unittest.main()
