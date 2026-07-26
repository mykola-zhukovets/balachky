"""Тести доставки оновлення: завантаження, SHA-256, resume, HTTPS-гейт.

Локальний HTTPS-сервер (stdlib + self-signed cert) імітує GitHub-asset —
без інтернету й без сторонніх мереж-бібліотек. Перевіряємо реальний код
шляху (справжній TLS, справжній Range), а не моки: цілісний файл, обірване/
докачане завантаження, відмову при битому SHA, гейт «лише https».
"""
import datetime
import hashlib
import shutil
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

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
    ctx.load_cert_chain(certfile, keyfile)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


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
