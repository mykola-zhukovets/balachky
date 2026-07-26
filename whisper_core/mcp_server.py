"""whisper_core/mcp_server.py — локальний MCP-сервер Балачок (stdio, лише stdlib).

Експонує функціонал застосунку як інструменти для ШІ-агентів (Claude Desktop тощо)
через Model Context Protocol. 100% офлайн: транспорт — stdio (stdin/stdout), жодного
мережевого слухача; шляхи — лише в межах даних застосунку (профілі, теки нарад):
кожен цільовий шлях резолвиться й звіряється ``paths.safe_under`` перед доступом,
тож traversal ("../.." у session/target/profile) відхиляється структурованою
помилкою й НЕ читає файл за межами (захист від промт-інʼєкції в контексті агента).

Чому власний JSON-RPC, а не пакет ``mcp``/``fastmcp``:
  • venv СПІЛЬНИЙ для 70+ worktree і frozen-білдів PyInstaller — ставити туди важку
    залежність ризиковано (зачепить сусідні гілки й розмір інсталятора);
  • MCP-stdio — це JSON-RPC 2.0 з розділювачем-переносом рядка; ядро (initialize /
    tools/list / tools/call) реалізується на stdlib у пару десятків рядків;
  • без опційного імпорту → ``dev/check_lazy_imports.py`` не ламається (модуль
    самодостатній, усі імпорти резолвляться).

Бізнес-логіка НЕ дублюється: інструменти transcribe/history/dictionary/export
викликають ті самі ``fronts.cli.cmd_*`` (структурований --json вивід), а протокол —
``whisper_core.protocol`` напряму. Точки інжекції (root, transcribe_fn, protocol_run)
дозволяють тестам підміняти рушій/модель.

Запуск (dev):  python -m whisper_core.mcp_server
Frozen-білд: ``python -m`` у PyInstaller недоступний — потрібна окрема exe-точка входу
(див. docs/MCP-SERVER.md); для локального тесту достатньо dev-режиму.
"""
from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fronts import cli
from whisper_core import paths

SERVER_NAME = "balachky"
SERVER_VERSION = "1.2.1"
# Версія протоколу, яку підтримуємо (echo-имо запит клієнта, якщо він її прислав).
PROTOCOL_VERSION = "2025-06-18"


class ToolError(Exception):
    """Помилка інструмента → структурована відповідь isError, а не краш сервера."""


# ───────────────────────────── контекст (інжекція) ─────────────────────────────
@dataclass
class Context:
    """Залежності інструментів. Прод бере реальні шляхи/рушій; тести — підміну."""
    root: Path
    meetings_root: Path
    transcribe_fn: Callable
    protocol_run: Callable


def _default_protocol_run(utterances):
    """Прод-генерація протоколу: локальна LLM у сайдкарі через ProtocolGenerator.
    Брак моделі/бекенду → ProtocolError із зрозумілим повідомленням (ловиться вище)."""
    from whisper_core.protocol.model_manager import DEFAULT_PRESET
    from whisper_core.protocol.service import ProtocolGenerator
    return ProtocolGenerator(DEFAULT_PRESET).run(utterances)


def default_context() -> Context:
    return Context(
        root=paths.profiles_root(),
        meetings_root=paths.meetings_dir(),
        transcribe_fn=cli._engine_transcribe,
        protocol_run=_default_protocol_run,
    )


# ───────────────────────────── реюз CLI ─────────────────────────────
def _cli_json(fn, ns, **inject) -> dict:
    """Викликати cli.cmd_* із перехопленням stdout(JSON)/stderr. rc≠0 → ToolError."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(ns, **inject)
    if rc != 0:
        raise ToolError(err.getvalue().strip() or "Помилка інструмента")
    text = out.getvalue()
    return json.loads(text) if text.strip() else {}


def _load_utterances(session_dir):
    """transcript.json сесії → [Utterance, …] (порожні/безтаймкодові репліки геть)."""
    from whisper_core.meeting.postprocess import Utterance
    try:
        from whisper_core.meeting.session import read_artifact
        data = json.loads(read_artifact(
            Path(session_dir), "transcript.json").decode("utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for u in data:
        text = (u.get("text") or "").strip()
        if not text or u.get("start") is None or u.get("end") is None:
            continue
        out.append(Utterance(float(u["start"]), float(u["end"]),
                             u.get("speaker") or "", text, source=u.get("source", "")))
    return out


# ───────────────────────────── інструменти ─────────────────────────────
def _t_transcribe(a, ctx):
    ns = SimpleNamespace(file=a["path"], model=a.get("model"), lang=a.get("lang"),
                         profile=a.get("profile"), json=True)
    return _cli_json(cli.cmd_transcribe, ns, root=ctx.root,
                     transcribe_fn=ctx.transcribe_fn)


def _t_search_history(a, ctx):
    ns = SimpleNamespace(op="search", query=[a["query"]], json=True)
    return _cli_json(cli.cmd_history, ns, root=ctx.root)


def _t_list_dictionary(a, ctx):
    ns = SimpleNamespace(op="list", canon=None, variant=None,
                         profile=a.get("profile"), json=True)
    return _cli_json(cli.cmd_dictionary, ns, root=ctx.root)


def _t_add_dictionary_term(a, ctx):
    ns = SimpleNamespace(op="add", canon=a["canon"], variant=a.get("variant", ""),
                         profile=a.get("profile"), json=True)
    return _cli_json(cli.cmd_dictionary, ns, root=ctx.root)


def _t_export_transcript(a, ctx):
    ns = SimpleNamespace(target=a["session_or_file"], format=a["format"],
                         out=None, profile=a.get("profile"))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        # confine_root: джерело (аудіофайл) має лежати в межах даних застосунку —
        # traversal-гейт для недовіреного контексту (промт-інʼєкція). Сесія за id
        # додатково звіряється з meetings_root у самому cmd_export.
        rc = cli.cmd_export(ns, root=ctx.root, meetings_root=ctx.meetings_root,
                            transcribe_fn=ctx.transcribe_fn, confine_root=ctx.root)
    if rc != 0:
        raise ToolError(err.getvalue().strip() or "Помилка експорту")
    return {"format": a["format"], "content": out.getvalue()}


def _t_generate_protocol(a, ctx):
    session = a["session"]
    session_dir = Path(ctx.meetings_root) / session
    # Traversal-гейт: "session" — сирий рядок від агента. "../../../Windows/..."
    # вислизнув би за teky нарад і читав/писав за межами даних застосунку.
    if not paths.safe_under(ctx.meetings_root, session_dir):
        raise ToolError("Шлях поза межами даних застосунку")
    if not session_dir.is_dir():
        raise ToolError(f"Нема сесії наради: {session}")
    utterances = _load_utterances(session_dir)
    if not utterances:
        raise ToolError(f"Сесія “{session}” без розшифровки (нема transcript.json)")
    try:
        markdown = ctx.protocol_run(utterances)
    except ToolError:
        raise
    except Exception as exc:                 # noqa: BLE001 — модель/бекенд → чесна помилка
        raise ToolError(str(exc)) from exc
    result = {"session": session, "protocol": markdown}
    if a.get("save", True) and markdown.strip():
        from whisper_core.protocol.service import save_protocol
        result["saved_to"] = str(save_protocol(session_dir, markdown))
    return result


# Реєстр: name → {description, inputSchema, handler}. Схеми — JSON Schema (draft-07).
TOOLS = [
    {
        "name": "transcribe_file",
        "description": "Розшифрувати аудіофайл локальною моделлю Whisper. "
                       "Повертає текст, сегменти з таймкодами, модель і тривалість.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Шлях до аудіофайлу"},
                "model": {"type": "string", "description": "Перекрити модель (напр. large-v3)"},
                "lang": {"type": "string", "description": "Перекрити мову (напр. uk)"},
                "profile": {"type": "string", "description": "Профіль словника/памʼяті"},
            },
            "required": ["path"],
        },
        "handler": _t_transcribe,
    },
    {
        "name": "search_history",
        "description": "Пошук по історії розшифровок усіх профілів. "
                       "Повертає збіги зі снипетами, датою й профілем.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Слова або дата запиту"},
            },
            "required": ["query"],
        },
        "handler": _t_search_history,
    },
    {
        "name": "list_dictionary",
        "description": "Перелік термінів словника профілю (канон + почуті варіанти).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Профіль (типово активний)"},
            },
        },
        "handler": _t_list_dictionary,
    },
    {
        "name": "add_dictionary_term",
        "description": "Додати термін у словник профілю: канон і (необовʼязково) варіант.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "canon": {"type": "string", "description": "Канонічний термін"},
                "variant": {"type": "string", "description": "Почутий варіант"},
                "profile": {"type": "string", "description": "Профіль (типово активний)"},
            },
            "required": ["canon"],
        },
        "handler": _t_add_dictionary_term,
    },
    {
        "name": "export_transcript",
        "description": "Експортувати розшифровку сесії наради (за id) або аудіофайлу "
                       "у формат srt|txt|md. Повертає готовий текст.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_or_file": {"type": "string",
                                    "description": "id сесії наради або шлях до аудіофайлу"},
                "format": {"type": "string", "enum": ["srt", "txt", "md"]},
                "profile": {"type": "string", "description": "Профіль (для розшифровки файлу)"},
            },
            "required": ["session_or_file", "format"],
        },
        "handler": _t_export_transcript,
    },
    {
        "name": "generate_protocol",
        "description": "Створити структурований протокол наради (Підсумок/Рішення/"
                       "Задачі/Розділи) з розшифровки сесії локальною LLM. "
                       "Зберігає protocol.md поруч із записом.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "description": "id сесії наради"},
                "save": {"type": "boolean",
                         "description": "Зберегти protocol.md (типово так)"},
            },
            "required": ["session"],
        },
        "handler": _t_generate_protocol,
    },
]


def _find_tool(name):
    for t in TOOLS:
        if t["name"] == name:
            return t
    return None


def _public_tool(t):
    """Публічний опис для tools/list — без внутрішнього handler."""
    return {"name": t["name"], "description": t["description"],
            "inputSchema": t["inputSchema"]}


def call_tool(name, arguments, ctx) -> dict:
    """Виконати інструмент за іменем. → результат-словник. ToolError/KeyError → нагору."""
    tool = _find_tool(name)
    if tool is None:
        raise ToolError(f"Немає інструмента: {name}")
    try:
        return tool["handler"](arguments or {}, ctx)
    except KeyError as exc:
        raise ToolError(f"Бракує обовʼязкового аргументу: {exc}") from exc


# ───────────────────────────── JSON-RPC ─────────────────────────────
def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _rpc_error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_result(mid, result):
    return _ok(mid, {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "structuredContent": result,
        "isError": False,
    })


def _tool_error(mid, message):
    # Помилка інструмента — це РЕЗУЛЬТАТ із isError (за MCP), а не JSON-RPC-помилка.
    return _ok(mid, {"content": [{"type": "text", "text": message}], "isError": True})


def _handle_tools_call(mid, params, ctx):
    name = params.get("name")
    arguments = params.get("arguments") or {}
    try:
        result = call_tool(name, arguments, ctx)
    except ToolError as exc:
        return _tool_error(mid, str(exc))
    except Exception as exc:                  # noqa: BLE001 — інструмент не має валити сервер
        return _tool_error(mid, f"Непередбачена помилка: {exc}")
    return _tool_result(mid, result)


def handle_request(msg, ctx):
    """Один JSON-RPC-запит → відповідь-словник, або None для нотифікацій."""
    method = msg.get("method")
    mid = msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        version = params.get("protocolVersion") or PROTOCOL_VERSION
        return _ok(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": [_public_tool(t) for t in TOOLS]})
    if method == "tools/call":
        return _handle_tools_call(mid, msg.get("params") or {}, ctx)
    if mid is None:
        return None                           # нотифікація (initialized тощо) — без відповіді
    return _rpc_error(mid, -32601, f"Метод не підтримується: {method}")


# ───────────────────────────── stdio-цикл ─────────────────────────────
def _write(stream, obj):
    stream.write(json.dumps(obj, ensure_ascii=False) + "\n")
    stream.flush()


def serve(stdin=None, stdout=None, ctx=None):
    """Читати JSON-RPC-повідомлення (по рядку) зі stdin, писати відповіді у stdout."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    ctx = ctx if ctx is not None else default_context()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _write(stdout, _rpc_error(None, -32700, "Не вдалося розібрати JSON"))
            continue
        resp = handle_request(msg, ctx)
        if resp is not None:
            _write(stdout, resp)


def main(argv=None):
    # MCP-stdio — суворо UTF-8. На Windows реальні std-потоки часто cp1251 і
    # падають на укр. символах (напр. U+02BC ʼ) — перевлаштовуємо на UTF-8.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", newline="\n")
        except (AttributeError, ValueError):
            pass
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
