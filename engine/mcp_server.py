#!/usr/bin/env python3
"""pc-control MCP server — the same 14 tools, spoken over MCP stdio.

Lets any MCP client (Google Antigravity, Claude Desktop, Cursor, another Hermes)
drive this machine: background-first input via UI Automation, Win32 messages where
they work, SendInput when foreground is required, plus windows/process/service/
power/screenshot/clipboard.

Transport: newline-delimited JSON-RPC 2.0 on stdin/stdout. Nothing but protocol
messages may touch stdout — logs go to stderr.

Run:  python mcp_server.py            (or let the client spawn it)
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

SERVER_NAME = "pc-control"
SERVER_VERSION = "1.0.0"
PROTOCOL_DEFAULT = "2024-11-05"

try:
    import pctools  # noqa: E402
    IMPORT_ERROR = ""
except Exception as exc:  # surfaced per tool call instead of killing the process
    pctools = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def log(msg: str) -> None:
    sys.stderr.write(f"[pc-control] {msg}\n")
    sys.stderr.flush()


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def tool_list() -> list[dict]:
    if pctools is None:
        return []
    tools = []
    for name, schema in pctools.SCHEMAS.items():
        tools.append({
            "name": name,
            "description": schema.get("description", ""),
            "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
        })
    return tools


def call_tool(name: str, arguments: dict) -> dict:
    if pctools is None:
        return {"content": [{"type": "text",
                             "text": json.dumps({"ok": False,
                                                 "error": f"pc-control não carregou: {IMPORT_ERROR}"},
                                                ensure_ascii=False)}],
                "isError": True}
    if name not in pctools.HANDLERS:
        return {"content": [{"type": "text", "text": json.dumps(
            {"ok": False, "error": f"tool desconhecida: {name}"}, ensure_ascii=False)}],
            "isError": True}
    try:
        raw = pctools.make_handler(name)(dict(arguments or {}))
    except Exception as exc:
        raw = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    is_error = False
    try:
        parsed = json.loads(raw)
        is_error = parsed.get("ok") is False
    except Exception:
        pass
    return {"content": [{"type": "text", "text": raw}], "isError": is_error}


def handle(request: dict) -> None:
    method = request.get("method") or ""
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": params.get("protocolVersion") or PROTOCOL_DEFAULT,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "Controle do Windows. Prefira pc_uia (dump + invoke/set_value) para agir sem "
                "roubar foco; pc_windows/pc_elements para descobrir alvos; pc_input "
                "mode=foreground só quando o app ignora background; pc_screenshot para verificar. "
                "pc_status informa sessão bloqueada e capacidades do killswitch."
            ),
        })
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return  # notifications carry no id and expect no reply

    if method == "ping":
        _result(req_id, {})
        return

    if method == "tools/list":
        _result(req_id, {"tools": tool_list()})
        return

    if method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        _result(req_id, call_tool(name, args))
        return

    if method == "resources/list":
        _result(req_id, {"resources": []})
        return

    if method == "prompts/list":
        _result(req_id, {"prompts": []})
        return

    if req_id is not None:
        _error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    # MCP stdio is UTF-8 by spec; Windows pipes default to the OEM code page and
    # would raise on any accented tool output.
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log(f"start (python {sys.version.split()[0]}, tools={'ok' if pctools else 'ERRO: ' + IMPORT_ERROR})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            log(f"JSON inválido: {line[:200]}")
            continue
        if isinstance(request, list):  # batch
            for item in request:
                try:
                    handle(item)
                except Exception:
                    log(traceback.format_exc())
            continue
        try:
            handle(request)
        except Exception as exc:
            log(traceback.format_exc())
            if request.get("id") is not None:
                _error(request.get("id"), -32603, f"internal error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
