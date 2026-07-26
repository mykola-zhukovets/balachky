"""feature/mcp-server — локальний MCP-сервер Балачок (stdio JSON-RPC на stdlib).

Перевіряємо: реєстр інструментів (tools/list зі схемами), JSON-RPC-каркас
(initialize/ping/невідомий метод), кожен інструмент кличе правильну наявну логіку
(мок рушія транскрипції та мок генератора протоколу — без важких моделей),
і структуровані помилки (брак файлу/сесії/аргументу, невідомий інструмент).
"""
import io
import json
import tempfile
import unittest
from pathlib import Path

from whisper_core import mcp_server as srv
from whisper_core import profiles


def _fake_transcribe(cfg, terms, path):
    segs = [(0.0, 1.5, "перше речення"), (1.5, 3.0, "друге речення")]
    return ("сире", "перше речення друге речення", 3.0, [], segs)


def _fake_protocol_run(utterances):
    return ("## Підсумок\nНарада відбулась.\n\n## Рішення\n- Ухвалено план\n\n"
            "## Задачі\n| Хто | Що | Термін | Час у записі |\n"
            "|-----|-----|--------|--------------|\n\n## Розділи наради\n")


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.meetings = self.root / "meetings"
        self.meetings.mkdir()
        self.prof = profiles.get_active(self.root)
        self.ctx = srv.Context(
            root=self.root, meetings_root=self.meetings,
            transcribe_fn=_fake_transcribe, protocol_run=_fake_protocol_run)

    def _session(self, sid="session-1"):
        d = self.meetings / sid
        d.mkdir(parents=True)
        (d / "transcript.json").write_text(json.dumps([
            {"start": 0.0, "end": 1.2, "speaker": "s1", "text": "Вітаю всіх"},
            {"start": 1.2, "end": 2.5, "speaker": "s2", "text": "Починаємо нараду"},
        ], ensure_ascii=False), encoding="utf-8")
        return sid

    def call(self, name, arguments):
        return srv.call_tool(name, arguments, self.ctx)


class RegistryTests(_Base):
    def test_six_tools_registered(self):
        names = {t["name"] for t in srv.TOOLS}
        self.assertEqual(names, {
            "transcribe_file", "search_history", "list_dictionary",
            "add_dictionary_term", "export_transcript", "generate_protocol"})

    def test_tools_list_exposes_schema_without_handler(self):
        resp = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                  self.ctx)
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 6)
        for t in tools:
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertNotIn("handler", t)


class ToolLogicTests(_Base):
    def test_transcribe_uses_injected_engine(self):
        f = self.root / "a.wav"
        f.write_bytes(b"x")
        res = self.call("transcribe_file", {"path": str(f)})
        self.assertEqual(res["text"], "перше речення друге речення")
        self.assertEqual(len(res["segments"]), 2)

    def test_search_history(self):
        from whisper_core.history import log_history
        log_history(self.prof.history_path, "привіт світе", "привіт світе", source="cli")
        res = self.call("search_history", {"query": "світ"})
        self.assertEqual(res["query"], "світ")
        self.assertGreaterEqual(len(res["results"]), 1)

    def test_add_then_list_dictionary(self):
        self.call("add_dictionary_term", {"canon": "кар'єра", "variant": "карʼєра"})
        res = self.call("list_dictionary", {})
        self.assertIn("кар'єра", [t["canon"] for t in res["terms"]])

    def test_export_session_srt(self):
        sid = self._session()
        res = self.call("export_transcript",
                        {"session_or_file": sid, "format": "srt"})
        self.assertEqual(res["format"], "srt")
        self.assertIn("-->", res["content"])
        self.assertIn("Вітаю всіх", res["content"])

    def test_generate_protocol_saves_file(self):
        sid = self._session()
        res = self.call("generate_protocol", {"session": sid})
        self.assertIn("## Підсумок", res["protocol"])
        saved = Path(res["saved_to"])
        self.assertTrue(saved.exists())
        self.assertEqual(saved.name, "protocol.md")

    def test_generate_protocol_can_skip_saving(self):
        sid = self._session()
        res = self.call("generate_protocol", {"session": sid, "save": False})
        self.assertNotIn("saved_to", res)


class ErrorTests(_Base):
    def test_missing_file_raises_toolerror(self):
        with self.assertRaises(srv.ToolError):
            self.call("transcribe_file", {"path": "нема.wav"})

    def test_missing_session_raises_toolerror(self):
        with self.assertRaises(srv.ToolError):
            self.call("generate_protocol", {"session": "нема"})

    def test_missing_required_arg_raises_toolerror(self):
        with self.assertRaises(srv.ToolError):
            self.call("add_dictionary_term", {})

    def test_unknown_tool_raises_toolerror(self):
        with self.assertRaises(srv.ToolError):
            self.call("bogus", {})

    def test_protocol_backend_failure_is_structured(self):
        sid = self._session()

        def _boom(utterances):
            raise RuntimeError("Модель мовної генерації недоступна")

        ctx = srv.Context(root=self.root, meetings_root=self.meetings,
                          transcribe_fn=_fake_transcribe, protocol_run=_boom)
        with self.assertRaises(srv.ToolError) as cm:
            srv.call_tool("generate_protocol", {"session": sid}, ctx)
        self.assertIn("недоступна", str(cm.exception))


class TraversalSecurityTests(_Base):
    """Path-traversal гейт: MCP-агент під недовіреним контекстом не має вміти
    вислизнути за teky даних застосунку. Кожна спроба → ToolError, файл поза
    межами НЕ читається; легітимний шлях у межах працює як раніше."""

    def _secret_outside_root(self):
        # Файл-«секрет» поза коренем даних застосунку — його не можна прочитати.
        outside = Path(self._tmp.name).parent / f"secret_{Path(self._tmp.name).name}.txt"
        outside.write_text("СЕКРЕТ ПОЗА МЕЖАМИ", encoding="utf-8")
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        return outside

    def test_generate_protocol_rejects_relative_traversal(self):
        # Підкладена сесія ПОЗА meetings з валідним transcript.json.
        evil = Path(self._tmp.name).parent / f"evil_{Path(self._tmp.name).name}"
        (evil).mkdir()
        (evil / "transcript.json").write_text(json.dumps(
            [{"start": 0.0, "end": 1.0, "speaker": "x", "text": "витік"}],
            ensure_ascii=False), encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(evil, ignore_errors=True))
        called = []
        ctx = srv.Context(root=self.root, meetings_root=self.meetings,
                          transcribe_fn=_fake_transcribe,
                          protocol_run=lambda u: called.append(u) or "x")
        rel = f"../evil_{Path(self._tmp.name).name}"
        with self.assertRaises(srv.ToolError) as cm:
            srv.call_tool("generate_protocol", {"session": rel}, ctx)
        self.assertIn("поза межами", str(cm.exception).lower())
        self.assertEqual(called, [])            # розшифровку поза межами не читали

    def test_generate_protocol_rejects_absolute_outside_root(self):
        with self.assertRaises(srv.ToolError) as cm:
            self.call("generate_protocol", {"session": str(self.meetings.parent)})
        self.assertIn("поза межами", str(cm.exception).lower())

    def test_export_rejects_file_outside_root(self):
        secret = self._secret_outside_root()
        called = []
        ctx = srv.Context(root=self.root, meetings_root=self.meetings,
                          transcribe_fn=lambda *a: called.append(a) or (
                              "", "", 0.0, [], []),
                          protocol_run=_fake_protocol_run)
        with self.assertRaises(srv.ToolError) as cm:
            srv.call_tool("export_transcript",
                          {"session_or_file": str(secret), "format": "txt"}, ctx)
        self.assertIn("поза межами", str(cm.exception).lower())
        self.assertEqual(called, [])            # файл-секрет рушію не передали

    def test_export_rejects_session_traversal(self):
        # "../.." як session id не має прочитати чужий transcript.json.
        with self.assertRaises(srv.ToolError) as cm:
            self.call("export_transcript",
                      {"session_or_file": "../../etc", "format": "srt"})
        self.assertIn("поза межами", str(cm.exception).lower())

    def test_export_allows_file_within_root(self):
        # Легітимно: аудіофайл у межах даних застосунку працює як раніше.
        f = self.root / "audio.wav"
        f.write_bytes(b"x")
        res = self.call("export_transcript",
                        {"session_or_file": str(f), "format": "md"})
        self.assertEqual(res["format"], "md")
        self.assertIn("перше речення", res["content"])

    def test_dictionary_rejects_profile_traversal(self):
        # Тека-«профіль» ПОЗА profiles/ зі своїм словником.
        evil_dir = self.root / "evilprofile"
        evil_dir.mkdir()
        (evil_dir / "terms.toml").write_text(
            '[terms]\nсекрет = ["витік"]\n', encoding="utf-8")
        with self.assertRaises(srv.ToolError):
            self.call("list_dictionary", {"profile": "../evilprofile"})
        with self.assertRaises(srv.ToolError):
            self.call("add_dictionary_term",
                      {"canon": "зло", "profile": "../evilprofile"})
        # Запис не потрапив у чужий terms.toml поза profiles/.
        self.assertNotIn("зло", (evil_dir / "terms.toml").read_text(encoding="utf-8"))


class JsonRpcTests(_Base):
    def test_initialize_echoes_protocol_version(self):
        resp = srv.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"}}, self.ctx)
        self.assertEqual(resp["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "balachky")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_ping(self):
        resp = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "ping"},
                                  self.ctx)
        self.assertEqual(resp["result"], {})

    def test_notification_returns_none(self):
        resp = srv.handle_request(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, self.ctx)
        self.assertIsNone(resp)

    def test_unknown_method_is_rpc_error(self):
        resp = srv.handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "no/such"}, self.ctx)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_tools_call_success_shape(self):
        f = self.root / "a.wav"
        f.write_bytes(b"x")
        resp = srv.handle_request({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "transcribe_file", "arguments": {"path": str(f)}}},
            self.ctx)
        result = resp["result"]
        self.assertFalse(result["isError"])
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["text"], "перше речення друге речення")
        self.assertEqual(result["structuredContent"]["duration"], 3.0)

    def test_tools_call_error_sets_iserror(self):
        resp = srv.handle_request({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "transcribe_file", "arguments": {"path": "нема.wav"}}},
            self.ctx)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("Нема файлу", resp["result"]["content"][0]["text"])


class ServeLoopTests(_Base):
    def test_serve_reads_lines_and_writes_responses(self):
        lines = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            "   ",                                    # порожній рядок — пропустити
            "не json",                                # → parse error
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        ])
        stdin, stdout = io.StringIO(lines), io.StringIO()
        srv.serve(stdin=stdin, stdout=stdout, ctx=self.ctx)
        out = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertEqual(out[0]["id"], 1)
        self.assertEqual(out[1]["error"]["code"], -32700)   # не json
        self.assertEqual(len(out[2]["result"]["tools"]), 6)


if __name__ == "__main__":
    unittest.main()
