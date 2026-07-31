"""Цілісність керованих завантажень і повнота netlog."""
import ast
import hashlib
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from tests._isolation import reset_process_caches


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, payload: bytes, status=200):
        self._payload = payload
        self._done = False
        self.status = status
        self.headers = {"Content-Length": str(len(payload))}

    def getcode(self):
        return self.status

    def read(self, _amount=-1):
        if self._done:
            return b""
        self._done = True
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class SttDownloadIntegrityTests(unittest.TestCase):
    def test_same_size_substituted_cached_file_is_not_accepted(self):
        from fronts.desktop.onboarding import resumable_download_file

        good = b"trusted-model-bytes"
        bad = b"x" * len(good)
        expected = hashlib.sha256(good).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.bin"
            destination.write_bytes(bad)
            with mock.patch(
                    "fronts.desktop.onboarding.urllib.request.urlopen",
                    return_value=_Response(good)) as urlopen:
                resumable_download_file(
                    "https://example.invalid/model.bin",
                    destination,
                    expected_size=len(good),
                    expected_sha256=expected,
                )

            urlopen.assert_called_once()
            self.assertEqual(destination.read_bytes(), good)

    def test_same_size_substituted_download_is_rejected(self):
        from fronts.desktop.onboarding import resumable_download_file

        good = b"trusted-model-bytes"
        bad = b"x" * len(good)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "model.bin"
            with mock.patch(
                    "fronts.desktop.onboarding.urllib.request.urlopen",
                    return_value=_Response(bad)):
                with self.assertRaises(OSError):
                    resumable_download_file(
                        "https://example.invalid/model.bin",
                        destination,
                        expected_size=len(good),
                        expected_sha256=hashlib.sha256(good).hexdigest(),
                    )

            self.assertFalse(destination.exists())
            self.assertFalse(
                destination.with_name("model.bin.incomplete").exists())

    def test_every_managed_stt_file_has_size_and_sha_pin(self):
        from whisper_core.engine import MODEL_REVISIONS
        from whisper_core.models import model_download_manifest, repo_for

        for model_name, revision in MODEL_REVISIONS.items():
            manifest = model_download_manifest(
                repo_for(model_name), revision)
            self.assertTrue(manifest, model_name)
            for asset in manifest:
                self.assertGreater(asset.size, 0, asset.filename)
                self.assertRegex(asset.sha256, r"^[0-9a-f]{64}$")

    def test_worker_uses_pinned_revision_and_manifest_checksums(self):
        from fronts.desktop.onboarding import DownloadWorker
        from whisper_core.models import model_download_manifest, revision_for

        repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
        revision = revision_for("large-v3-turbo")
        with tempfile.TemporaryDirectory() as tmp:
            worker = DownloadWorker(repo, tmp, revision=revision)
            with mock.patch(
                    "fronts.desktop.onboarding.check_free_space"), \
                    mock.patch(
                        "fronts.desktop.onboarding.resumable_download_file"
                    ) as download:
                worker.run()

        manifest = model_download_manifest(repo, revision)
        self.assertEqual(download.call_count, len(manifest))
        for call, asset in zip(download.call_args_list, manifest):
            url, destination = call.args[:2]
            self.assertIn(f"/resolve/{revision}/", url)
            self.assertEqual(Path(destination).name, asset.filename)
            self.assertEqual(call.kwargs["expected_size"], asset.size)
            self.assertEqual(
                call.kwargs["expected_sha256"], asset.sha256)

    def test_engine_snapshot_check_rejects_same_size_substitution(self):
        from whisper_core import models

        good = b"trusted-model"
        bad = b"x" * len(good)
        repo = "owner/model"
        revision = "a" * 40
        asset = models.ModelDownloadAsset(
            "model.bin", len(good), hashlib.sha256(good).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(
                    models._MODEL_DOWNLOAD_MANIFESTS,
                    {(repo, revision): (asset,)}, clear=False):
            snapshot = (
                Path(tmp) / "models--owner--model" /
                "snapshots" / revision)
            snapshot.mkdir(parents=True)
            (snapshot / "model.bin").write_bytes(bad)
            self.assertFalse(models.model_snapshot_integrity(
                tmp, repo, revision))
            (snapshot / "model.bin").write_bytes(good)
            self.assertTrue(models.model_snapshot_integrity(
                tmp, repo, revision))


class AutocorrectIntegrityTests(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def test_dictionary_url_uses_immutable_revision(self):
        from whisper_core import autocorrect_download as module

        self.assertNotIn("/master/", module.DICT_URL)
        self.assertIn(module.DICT_REVISION, module.DICT_URL)
        self.assertRegex(module.DICT_REVISION, r"^[0-9a-f]{40}$")
        self.assertRegex(module.DICT_SHA256, r"^[0-9a-f]{64}$")

    def test_same_size_substituted_cached_dictionary_is_replaced(self):
        from whisper_core import autocorrect_download as module

        good = b"trusted-dictionary"
        bad = b"x" * len(good)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "uk_freq.txt"
            destination.write_bytes(bad)

            def write_good(_url, target, *_args):
                target.write_bytes(good)

            with mock.patch.object(module, "MIN_DICT_BYTES", len(good)), \
                    mock.patch.object(
                        module, "DICT_SHA256",
                        hashlib.sha256(good).hexdigest()), \
                    mock.patch.object(
                        module, "_download", side_effect=write_good) as download:
                module.download_and_install(destination)

            download.assert_called_once()
            self.assertEqual(destination.read_bytes(), good)

    def test_same_size_substituted_dictionary_download_is_rejected(self):
        from whisper_core import autocorrect_download as module

        good = b"trusted-dictionary"
        bad = b"x" * len(good)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "uk_freq.txt"

            def write_bad(_url, target, *_args):
                target.write_bytes(bad)

            with mock.patch.object(module, "MIN_DICT_BYTES", len(good)), \
                    mock.patch.object(
                        module, "DICT_SHA256",
                        hashlib.sha256(good).hexdigest()), \
                    mock.patch.object(
                        module, "_download", side_effect=write_bad):
                with self.assertRaises(module.AutocorrectDownloadError):
                    module.download_and_install(destination)

            self.assertFalse(destination.exists())

    def test_application_availability_rejects_substituted_dictionary(self):
        from whisper_core import autocorrect

        good = b"trusted-dictionary"
        bad = b"x" * len(good)
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "uk_freq.txt"
            destination.write_bytes(bad)
            self.assertFalse(autocorrect.dictionary_available(
                destination,
                expected_sha256=hashlib.sha256(good).hexdigest(),
            ))


class PunctuatorIntegrityTests(unittest.TestCase):
    def test_assets_use_pinned_revision_and_sha256(self):
        from whisper_core import punctuator

        self.assertRegex(punctuator.MODEL_REVISION, r"^[0-9a-f]{40}$")
        self.assertTrue(punctuator.MODEL_ASSETS)
        for asset in punctuator.MODEL_ASSETS:
            self.assertIn(
                f"/resolve/{punctuator.MODEL_REVISION}/", asset.url)
            self.assertGreater(asset.size, 0)
            self.assertRegex(asset.sha256, r"^[0-9a-f]{64}$")

    def test_same_size_substituted_download_is_rejected(self):
        from whisper_core import punctuator

        good = b"trusted-punctuator"
        bad = b"x" * len(good)
        asset = punctuator.ModelAsset(
            url="https://example.invalid/model.onnx",
            filename="model.onnx",
            size=len(good),
            sha256=hashlib.sha256(good).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    punctuator, "MODEL_ASSETS", (asset,)), \
                mock.patch.object(
                    punctuator, "punctuators_available", return_value=True), \
                mock.patch(
                    "whisper_core.punctuator.urllib.request.urlopen",
                    return_value=_Response(bad)):
            with self.assertRaises(punctuator.PunctuatorDownloadError):
                punctuator.download_and_install(Path(tmp) / "punctuator")

    def test_same_size_substituted_cache_is_not_loaded(self):
        from whisper_core import punctuator
        import sys
        import types

        good = b"trusted-punctuator"
        bad = b"x" * len(good)
        asset = punctuator.ModelAsset(
            url="https://example.invalid/model.onnx",
            filename="model.onnx",
            size=len(good),
            sha256=hashlib.sha256(good).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / asset.filename).write_bytes(bad)
            (target / "READY").write_text("ok", encoding="utf-8")
            model_type = mock.Mock(return_value=object())
            model_module = types.ModuleType(
                "punctuators.models.punc_cap_seg_model")
            model_module.PunctCapSegConfigONNX = mock.Mock(
                return_value=object())
            model_module.PunctCapSegModelONNX = model_type
            with mock.patch.object(
                    punctuator, "MODEL_ASSETS", (asset,)), \
                    mock.patch.object(
                        punctuator, "punctuators_available",
                        return_value=True), \
                    mock.patch.dict(sys.modules, {
                        "punctuators.models.punc_cap_seg_model":
                            model_module,
                    }):
                self.assertIsNone(punctuator.load_model(target))
            model_type.assert_not_called()


class TtsVoiceIntegrityTests(unittest.TestCase):
    def test_curated_voice_urls_are_pinned_and_hashed(self):
        from whisper_core.tts.voices import VOICE_PRESETS

        for preset in VOICE_PRESETS.values():
            for url, filename, _min_bytes, sha256 in preset.files:
                self.assertNotIn("/resolve/main/", url, filename)
                self.assertRegex(
                    url, r"/resolve/[0-9a-f]{40}/", filename)
                self.assertRegex(sha256, r"^[0-9a-f]{64}$", filename)


class ProtocolPresetIntegrityTests(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def test_curated_protocol_urls_are_pinned_and_hashed(self):
        from whisper_core.protocol.model_manager import PRESETS

        for preset in PRESETS.values():
            self.assertNotIn("/resolve/main/", preset.url, preset.id)
            self.assertRegex(
                preset.url, r"/resolve/[0-9a-f]{40}/", preset.id)
            self.assertRegex(
                preset.sha256 or "", r"^[0-9a-f]{64}$", preset.id)

    def test_substituted_preset_cache_is_rejected_before_use(self):
        from dataclasses import replace
        from whisper_core.protocol import model_manager as module
        from whisper_core.protocol import service

        good = b"trusted-gguf"
        bad = b"x" * len(good)
        preset = replace(
            module.PRESETS["fast"],
            min_bytes=len(good),
            sha256=hashlib.sha256(good).hexdigest(),
        )
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(
                    module.PRESETS, {"fast": preset}, clear=False):
            target = Path(tmp) / "fast"
            target.mkdir()
            (target / module.MODEL_FILENAME).write_bytes(bad)
            (target / module._READY_MARKER).write_text(
                "ok", encoding="utf-8")

            with self.assertRaises(service.ProtocolModelMissing):
                service._resolve_ready_path(
                    "fast", tmp, [], service.ProtocolModelMissing)


class ProtocolCustomHfIntegrityTests(unittest.TestCase):
    def setUp(self):
        reset_process_caches()

    def _model(self, digest):
        from whisper_core.protocol import model_manager as module

        return module.CustomModel(
            id="custom_hf", label="custom", kind=module.CUSTOM_KIND_HF,
            repo_id="owner/model", filename="model.gguf",
            revision="a" * 40, sha256=digest,
        )

    def test_substituted_custom_download_is_rejected(self):
        from whisper_core.protocol import model_manager as module

        good = b"trusted-custom-gguf"
        bad = b"x" * len(good)
        model = self._model(hashlib.sha256(good).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    module, "CUSTOM_MIN_BYTES", len(good)), \
                mock.patch.object(
                    module, "_download",
                    side_effect=lambda _url, path, *_args, **_kw: path.write_bytes(bad)):
            with self.assertRaises(module.ModelDownloadError):
                module.download_custom_hf(Path(tmp) / model.id, model)

    def test_substituted_custom_cache_is_rejected_before_use(self):
        from whisper_core.protocol import model_manager as module
        from whisper_core.protocol import service

        good = b"trusted-custom-gguf"
        bad = b"x" * len(good)
        model = self._model(hashlib.sha256(good).hexdigest())
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(
                    module, "CUSTOM_MIN_BYTES", len(good)):
            target = Path(tmp) / model.id
            target.mkdir()
            (target / module.MODEL_FILENAME).write_bytes(bad)
            (target / module._READY_MARKER).write_text(
                "ok", encoding="utf-8")

            with self.assertRaises(service.ProtocolModelMissing):
                service._resolve_ready_path(
                    model.id, tmp, [model], service.ProtocolModelMissing)


_HTTP_METHODS = frozenset({
    "request", "get", "post", "put", "patch", "delete", "head", "options",
    "send", "stream",
})
_METHOD_EGRESS = {
    "requests": _HTTP_METHODS,
    "requests.api": _HTTP_METHODS,
    "requests.Session": _HTTP_METHODS,
    "requests.sessions.Session": _HTTP_METHODS,
    "httpx": _HTTP_METHODS,
    "httpx.Client": _HTTP_METHODS,
    "httpx.AsyncClient": _HTTP_METHODS,
    "aiohttp.ClientSession": _HTTP_METHODS | {"ws_connect"},
    "urllib3": {"request"},
    "urllib3.PoolManager": {"request", "urlopen"},
    "urllib3.ProxyManager": {"request", "urlopen"},
    "urllib3.HTTPConnectionPool": {"request", "urlopen"},
    "urllib3.HTTPSConnectionPool": {"request", "urlopen"},
    "http.client.HTTPConnection": {"connect", "request"},
    "http.client.HTTPSConnection": {"connect", "request"},
    "ftplib.FTP": {
        "connect", "retrbinary", "retrlines", "sendcmd", "storbinary",
        "storlines", "transfercmd",
    },
    "ftplib.FTP_TLS": {
        "connect", "retrbinary", "retrlines", "sendcmd", "storbinary",
        "storlines", "transfercmd",
    },
    "smtplib.SMTP": {
        "connect", "docmd", "ehlo", "helo", "login", "sendmail",
        "send_message", "starttls",
    },
    "smtplib.SMTP_SSL": {
        "connect", "docmd", "ehlo", "helo", "login", "sendmail",
        "send_message",
    },
    "smtplib.LMTP": {
        "connect", "docmd", "ehlo", "helo", "login", "sendmail",
        "send_message",
    },
    "websocket.WebSocket": {"connect", "send"},
    "websocket.WebSocketApp": {"run_forever", "send"},
    "huggingface_hub.HfApi": {
        "create_commit", "create_repo", "delete_file", "delete_repo",
        "file_exists", "hf_hub_download", "list_datasets", "list_models",
        "list_repo_commits", "list_repo_files", "list_repo_refs", "model_info",
        "repo_info", "snapshot_download", "upload_file", "upload_folder",
        "upload_large_folder",
    },
}
_EXACT_EGRESS = frozenset({
    "urllib.request.urlopen",
    "urllib.request.build_opener.open",
    "urllib.request.urlretrieve",
    "socket.create_connection",
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.gethostbyname_ex",
    "socket.socket.connect",
    "socket.socket.connect_ex",
    "socket.socket.sendmsg",
    "socket.socket.send",
    "socket.socket.sendall",
    "socket.socket.sendto",
    "websocket.create_connection",
    "websockets.connect",
    "websockets.asyncio.client.connect",
    "websockets.client.connect",
    "websockets.sync.client.connect",
    "huggingface_hub.file_exists",
    "huggingface_hub.get_hf_file_metadata",
    "huggingface_hub.hf_hub_download",
    "huggingface_hub.list_repo_files",
    "huggingface_hub.model_info",
    "huggingface_hub.snapshot_download",
})
_QT_NETWORK_METHODS = {
    "QNetworkAccessManager": {
        "connectToHost", "connectToHostEncrypted", "deleteResource", "get",
        "head", "post", "put", "sendCustomRequest",
    },
    "QTcpSocket": {"connectToHost"},
    "QSslSocket": {"connectToHostEncrypted"},
    "QUdpSocket": {"writeDatagram"},
    "QWebSocket": {"open"},
}
_CONNECTING_CONSTRUCTORS = frozenset({
    "ftplib.FTP", "ftplib.FTP_TLS", "smtplib.LMTP", "smtplib.SMTP",
    "smtplib.SMTP_SSL",
})
_SUBPROCESS_CALLS = frozenset({
    "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.run",
})
_PIP_CALLS = frozenset({
    "pip.main", "pip._internal.main", "pip._internal.cli.main.main",
})
_NETWORK_COMMANDS = frozenset({
    "curl", "curl.exe", "wget", "wget.exe",
})


def _call_name(node, aliases=None):
    aliases = aliases or {}
    if isinstance(node, ast.Call):
        return _call_name(node.func, aliases)
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value, aliases)
        return f"{parent}.{node.attr}" if parent else node.attr
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    return ""


def _literal_truth(node):
    try:
        return bool(ast.literal_eval(node))
    except (ValueError, TypeError):
        return None


def _static_words(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.replace('"', " ").replace("'", " ").split()
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        words = []
        for element in node.elts:
            words.extend(_static_words(element))
        return words
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _static_words(node.left) + _static_words(node.right)
    return []


class _EgressVisitor(ast.NodeVisitor):
    """Static negligence guard for obvious, directly named network egress.

    Imports, import aliases, direct attribute chains, constructor-call chains,
    and simple local bindings such as ``session = requests.Session()`` are
    resolved. This deliberately is not type inference or a hostile-code
    sandbox: ``getattr(module, "get")``, dynamic imports, factories, container
    lookups, monkey-patching, and sufficiently indirect aliases can evade it.
    Reachability is likewise a structural heuristic, not a full CFG. It skips
    literal-dead branches and statements after guaranteed local terminators,
    and rejects logs in sibling blocks. The threat model is forgotten or
    carelessly misplaced logging, not a malicious author controlling the code.
    """

    def __init__(self, relpath):
        self.relpath = relpath
        self.stack = []
        self.egress = []
        self._egress_sites = []
        self._log_sites = []
        self._aliases = [{}]
        self._block_path = ()
        self._order = 0

    @property
    def unlogged(self):
        failures = []
        for site in self._egress_sites:
            if not any(self._log_covers(log, site)
                       for log in self._log_sites):
                failures.append(site[:4])
        return failures

    @staticmethod
    def _log_covers(log, egress):
        log_function, log_path, log_order = log
        _path, _line, function, _kind, egress_path, egress_order = egress
        if log_function != function:
            return False
        if egress_path[:len(log_path)] != log_path:
            return False
        if log_order <= egress_order:
            return True
        return all(frame[3] for frame in egress_path[len(log_path):])

    @property
    def _scope(self):
        return self._aliases[-1]

    def visit_Import(self, node):
        for alias in node.names:
            local = alias.asname or alias.name.split(".", 1)[0]
            self._scope[local] = alias.name if alias.asname else local

    def visit_ImportFrom(self, node):
        if not node.module:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            local = alias.asname or alias.name
            self._scope[local] = f"{node.module}.{alias.name}"

    def visit_FunctionDef(self, node):
        previous_path = self._block_path
        self.stack.append(node.name)
        self._aliases.append(dict(self._scope))
        self._block_path = ()
        arguments = (
            list(node.args.posonlyargs) + list(node.args.args)
            + list(node.args.kwonlyargs))
        if node.args.vararg:
            arguments.append(node.args.vararg)
        if node.args.kwarg:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            self._scope.pop(argument.arg, None)
        self._visit_block(node.body)
        self._block_path = previous_path
        self._aliases.pop()
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node):
        self.visit(node.value)
        binding = self._binding_name(node.value)
        for target in node.targets:
            self._bind_target(target, binding)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
        self._bind_target(
            node.target,
            self._binding_name(node.value) if node.value else "")

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self._bind_target(node.target, self._binding_name(node.value))

    def _binding_name(self, node):
        if isinstance(node, ast.Call):
            return _call_name(node.func, self._scope)
        if isinstance(node, (ast.Name, ast.Attribute)):
            return _call_name(node, self._scope)
        return ""

    def _bind_target(self, target, binding):
        if isinstance(target, ast.Name):
            if binding:
                self._scope[target.id] = binding
            else:
                self._scope.pop(target.id, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind_target(element, "")

    def visit_Call(self, node):
        name = _call_name(node.func, self._scope)
        function = self.stack[-1] if self.stack else "<module>"
        self._order += 1
        if self._is_netlog(name):
            self._log_sites.append(
                (function, self._block_path, self._order))
        kind = self._egress_kind(name, node)
        if kind:
            self.egress.append((self.relpath, function, kind))
            self._egress_sites.append(
                (self.relpath, node.lineno, function, kind,
                 self._block_path, self._order))
        self.generic_visit(node)

    @staticmethod
    def _is_netlog(name):
        return (
            name in ("netlog.record", "netlog.record_url")
            or name.endswith(".netlog.record")
            or name.endswith(".netlog.record_url")
        )

    @staticmethod
    def _egress_kind(name, node):
        if name.endswith(".from_pretrained"):
            offline = any(
                keyword.arg == "local_files_only"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
                for keyword in node.keywords
            )
            return "" if offline else "third_party.from_pretrained"
        if name in _EXACT_EGRESS:
            return name
        for owner, methods in _METHOD_EGRESS.items():
            if any(name == f"{owner}.{method}" for method in methods):
                return name
        for class_name, methods in _QT_NETWORK_METHODS.items():
            if any(
                    name.endswith(f".{class_name}.{method}")
                    for method in methods):
                return name
        if name in _CONNECTING_CONSTRUCTORS and (
                node.args
                or any(keyword.arg in ("host", "source_address")
                       for keyword in node.keywords)):
            return name
        if name in _SUBPROCESS_CALLS:
            command = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords
                 if keyword.arg == "args"), None)
            words = _static_words(command)
            executables = {
                word.lower().replace("\\", "/").rsplit("/", 1)[-1]
                for word in words
            }
            if executables & _NETWORK_COMMANDS:
                return "subprocess.curl_wget"
            if executables & {"pip", "pip.exe", "pip3", "pip3.exe"}:
                return "subprocess.pip"
            lowered = [word.lower() for word in words]
            if "pip" in lowered and "-m" in lowered:
                return "subprocess.pip"
        if name in _PIP_CALLS:
            command = node.args[0] if node.args else None
            words = {word.lower() for word in _static_words(command)}
            return "pip.install" if words & {
                "download", "install", "wheel"} else "pip.command"
        return ""

    def visit_If(self, node):
        self.visit(node.test)
        truth = _literal_truth(node.test)
        if truth is True:
            self._visit_branch("if", node, True, node.body)
        elif truth is False:
            self._visit_branch("if", node, False, node.orelse)
        else:
            self._visit_branch("if", node, True, node.body)
            self._visit_branch("if", node, False, node.orelse)

    def visit_While(self, node):
        self.visit(node.test)
        truth = _literal_truth(node.test)
        if truth is not False:
            self._visit_branch("while", node, True, node.body)
        if truth is not True:
            self._visit_branch("while", node, False, node.orelse)

    def visit_For(self, node):
        self.visit(node.iter)
        self._bind_target(node.target, "")
        self._visit_branch("for", node, "body", node.body)
        self._visit_branch("for", node, "else", node.orelse)

    visit_AsyncFor = visit_For

    def visit_With(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars:
                self._bind_target(
                    item.optional_vars,
                    self._binding_name(item.context_expr))
        self._visit_branch("with", node, "body", node.body)

    visit_AsyncWith = visit_With

    def visit_Try(self, node):
        self._visit_branch("try", node, "body", node.body)
        for index, handler in enumerate(node.handlers):
            if handler.type:
                self.visit(handler.type)
            self._visit_branch("try", node, f"except-{index}", handler.body)
        self._visit_branch("try", node, "else", node.orelse)
        self._visit_branch("try", node, "finally", node.finalbody)

    visit_TryStar = visit_Try

    def visit_IfExp(self, node):
        self.visit(node.test)
        truth = _literal_truth(node.test)
        if truth is not False:
            self.visit(node.body)
        if truth is not True:
            self.visit(node.orelse)

    def visit_BoolOp(self, node):
        for value in node.values:
            self.visit(value)
            truth = _literal_truth(value)
            if isinstance(node.op, ast.And) and truth is False:
                break
            if isinstance(node.op, ast.Or) and truth is True:
                break

    def _visit_branch(self, kind, node, label, statements):
        if not statements:
            return
        frame = (
            kind, node.lineno, label, self._block_falls_through(statements))
        previous_path = self._block_path
        self._block_path = previous_path + (frame,)
        self._visit_block(statements)
        self._block_path = previous_path

    def _visit_block(self, statements):
        for statement in statements:
            self.visit(statement)
            if self._statement_always_terminates(statement):
                break

    @classmethod
    def _block_falls_through(cls, statements):
        return not any(
            cls._statement_always_terminates(statement)
            for statement in statements)

    @classmethod
    def _statement_always_terminates(cls, statement):
        if isinstance(statement, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return True
        if isinstance(statement, ast.If):
            truth = _literal_truth(statement.test)
            if truth is True:
                return not cls._block_falls_through(statement.body)
            if truth is False:
                return (
                    bool(statement.orelse)
                    and not cls._block_falls_through(statement.orelse))
            return (
                bool(statement.orelse)
                and not cls._block_falls_through(statement.body)
                and not cls._block_falls_through(statement.orelse))
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            return not cls._block_falls_through(statement.body)
        if isinstance(statement, (ast.Try, ast.TryStar)):
            return (
                bool(statement.finalbody)
                and not cls._block_falls_through(statement.finalbody))
        return False


class NetworkEgressGuardianTests(unittest.TestCase):
    @staticmethod
    def _fixture_visitor(source):
        visitor = _EgressVisitor("reviewer_fixture.py")
        visitor.visit(ast.parse(source, "reviewer_fixture.py"))
        return visitor

    @staticmethod
    def _unlogged_pairs(visitor):
        return {
            (function, kind)
            for _path, _line, function, kind
            in getattr(visitor, "unlogged", ())
        }

    def test_reviewer_dead_log_fixture_is_rejected(self):
        visitor = self._fixture_visitor(
            "import urllib.request\n"
            "\n"
            "def dead_log():\n"
            "    urllib.request.urlopen(url)\n"
            "    if False:\n"
            "        netlog.record_url(url)\n")

        self.assertIn(
            ("dead_log", "urllib.request.urlopen"),
            self._unlogged_pairs(visitor))
        self.assertEqual(
            ("reviewer_fixture.py", 4),
            visitor.unlogged[0][:2])

    def test_reviewer_requests_fixture_is_rejected(self):
        visitor = self._fixture_visitor(
            "import requests\n"
            "\n"
            "def requests_bypass():\n"
            "    requests.get(url)\n")

        self.assertIn(
            ("requests_bypass", "requests.get"),
            self._unlogged_pairs(visitor))

    def test_import_aliases_and_supported_network_apis_are_detected(self):
        cases = (
            ("import requests as r", "r.get(url)", "requests.get"),
            ("from requests import Session",
             "Session().send(request)", "requests.Session.send"),
            ("import httpx as h", "h.post(url)", "httpx.post"),
            ("from aiohttp import ClientSession as S",
             "S().get(url)", "aiohttp.ClientSession.get"),
            ("import socket as s",
             "s.socket().connect(address)", "socket.socket.connect"),
            ("import socket as s",
             "s.socket().sendall(data)", "socket.socket.sendall"),
            ("from http.client import HTTPConnection as Connection",
             "Connection(host).request('GET', '/')",
             "http.client.HTTPConnection.request"),
            ("from PySide6.QtNetwork import QNetworkAccessManager as Manager",
             "Manager().get(request)",
             "PySide6.QtNetwork.QNetworkAccessManager.get"),
            ("from urllib.request import urlretrieve as fetch",
             "fetch(url, path)", "urllib.request.urlretrieve"),
            ("from urllib.request import build_opener",
             "build_opener().open(url)", "urllib.request.build_opener.open"),
            ("import subprocess as process",
             "process.run(['curl', url])", "subprocess.curl_wget"),
            ("import ftplib as ftp",
             "ftp.FTP().connect(host)", "ftplib.FTP.connect"),
            ("from smtplib import SMTP as Mail",
             "Mail().send_message(message)", "smtplib.SMTP.send_message"),
            ("import websocket as ws",
             "ws.create_connection(url)", "websocket.create_connection"),
            ("from pip._internal import main as pip_main",
             "pip_main(['install', package])", "pip.install"),
            ("from huggingface_hub import hf_hub_download as download",
             "download(repo, name)", "huggingface_hub.hf_hub_download"),
            ("from huggingface_hub import HfApi",
             "HfApi().list_models()", "huggingface_hub.HfApi.list_models"),
        )

        for import_line, call_line, expected_kind in cases:
            with self.subTest(kind=expected_kind):
                visitor = self._fixture_visitor(
                    f"{import_line}\n"
                    "\n"
                    "def unlogged():\n"
                    f"    {call_line}\n")
                self.assertIn(
                    ("unlogged", expected_kind),
                    self._unlogged_pairs(visitor))

    def test_sibling_or_unreachable_log_does_not_cover_egress(self):
        sources = (
            "import requests\n"
            "def sibling(flag):\n"
            "    if flag:\n"
            "        requests.get(url)\n"
            "    else:\n"
            "        netlog.record_url(url)\n",
            "import requests\n"
            "def after_return():\n"
            "    requests.get(url)\n"
            "    return\n"
            "    netlog.record_url(url)\n",
            "import requests\n"
            "def after_raise():\n"
            "    requests.get(url)\n"
            "    raise RuntimeError\n"
            "    netlog.record_url(url)\n",
            "import requests\n"
            "def literal_zero():\n"
            "    requests.get(url)\n"
            "    if 0:\n"
            "        netlog.record_url(url)\n",
        )

        for source in sources:
            with self.subTest(source=source):
                visitor = self._fixture_visitor(source)
                self.assertEqual(1, len(visitor.unlogged))

    def test_reachable_same_or_enclosing_log_covers_egress(self):
        sources = (
            "import requests\n"
            "def enclosing(flag):\n"
            "    netlog.record_url(url)\n"
            "    if flag:\n"
            "        requests.get(url)\n",
            "import requests\n"
            "def same_block():\n"
            "    requests.get(url)\n"
            "    netlog.record_url(url)\n",
        )

        for source in sources:
            with self.subTest(source=source):
                visitor = self._fixture_visitor(source)
                self.assertEqual([], visitor.unlogged)

    def test_non_network_subprocess_and_offline_model_load_are_ignored(self):
        visitor = self._fixture_visitor(
            "import subprocess\n"
            "from transformers import AutoModel\n"
            "def local_only():\n"
            "    subprocess.run(['echo', 'ready'])\n"
            "    AutoModel.from_pretrained(repo, local_files_only=True)\n")

        self.assertEqual([], visitor.egress)

    EXPECTED = Counter({
        ("fronts/desktop/onboarding.py", "_has_network",
         "socket.create_connection"): 1,
        ("fronts/desktop/onboarding.py", "resumable_download_file",
         "urllib.request.urlopen"): 1,
        ("whisper_core/autocorrect_download.py", "_download",
         "urllib.request.urlopen"): 1,
        ("whisper_core/cuda_runtime.py", "_wheel_url",
         "urllib.request.urlopen"): 1,
        ("whisper_core/cuda_runtime.py", "_download_file",
         "urllib.request.urlopen"): 1,
        ("whisper_core/meeting/diarization_models.py", "_download_asset",
         "urllib.request.urlopen"): 1,
        ("whisper_core/protocol/model_manager.py", "_download",
         "urllib.request.urlopen"): 1,
        ("whisper_core/punctuator.py", "_download_asset",
         "urllib.request.urlopen"): 1,
        ("whisper_core/tts/engine_manager.py", "fetch_engine_manifest",
         "urllib.request.urlopen"): 1,
        ("whisper_core/tts/engine_manager.py", "download_file",
         "urllib.request.urlopen"): 1,
        ("whisper_core/tts/voices.py", "_download_file",
         "urllib.request.urlopen"): 1,
        ("whisper_core/updater.py", "download_installer",
         "urllib.request.urlopen"): 1,
        ("whisper_core/updates.py", "check_latest",
         "urllib.request.urlopen"): 1,
    })

    @staticmethod
    def _audit():
        all_egress = []
        all_unlogged = []
        for source_root in ("fronts", "whisper_core"):
            for path in (ROOT / source_root).rglob("*.py"):
                relpath = path.relative_to(ROOT).as_posix()
                tree = ast.parse(path.read_text(encoding="utf-8"), relpath)
                visitor = _EgressVisitor(relpath)
                visitor.visit(tree)
                all_egress.extend(visitor.egress)
                all_unlogged.extend(visitor.unlogged)
        return Counter(all_egress), all_unlogged

    def test_registry_matches_every_egress_callsite(self):
        actual, _unlogged = self._audit()
        self.assertEqual(actual, self.EXPECTED)

    def test_every_egress_callsite_has_a_netlog_record(self):
        _actual, unlogged = self._audit()
        failures = [
            f"{path}:{line}:{function}: {kind}"
            for path, line, function, kind in unlogged
        ]
        self.assertFalse(
            failures,
            "Незалоговані точки виходу:\n" + "\n".join(failures))

    def test_onboarding_connectivity_probe_is_logged(self):
        from fronts.desktop import onboarding

        connection = mock.Mock()
        with mock.patch(
                "socket.create_connection", return_value=connection), \
                mock.patch.object(onboarding.netlog, "record") as record:
            self.assertTrue(onboarding._has_network())

        record.assert_called_once_with(
            "1.1.1.1", kind=onboarding.netlog.OTHER, allowed=True,
            detail="connectivity-check")
        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
