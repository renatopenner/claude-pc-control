#!/usr/bin/env python3
"""Servidor MCP (stdio) das tools de navegador — Chrome/Edge por CDP.

Mesmo protocolo do mcp_server.py do pc-control: JSON-RPC 2.0 delimitado por
linha no stdin/stdout. Nada além de protocolo pode ir para o stdout.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

SERVER_NAME = "browser-control"
SERVER_VERSION = "1.0.0"
PROTOCOL_DEFAULT = "2024-11-05"

try:
    import browsertools  # noqa: E402
    IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    browsertools = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def log(msg: str) -> None:
    sys.stderr.write(f"[browser-control] {msg}\n")
    sys.stderr.flush()


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_list() -> list[dict]:
    if browsertools is None:
        return []
    return [
        {
            "name": name,
            "description": schema.get("description", ""),
            "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
        }
        for name, schema in browsertools.SCHEMAS.items()
    ]


def call_tool(name: str, arguments: dict) -> dict:
    if browsertools is None:
        return {
            "content": [
                {"type": "text", "text": json.dumps({"ok": False, "error": f"browser-control não carregou: {IMPORT_ERROR}"}, ensure_ascii=False)}
            ],
            "isError": True,
        }
    if name not in browsertools.HANDLERS:
        return {
            "content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"tool desconhecida: {name}"}, ensure_ascii=False)}],
            "isError": True,
        }
    try:
        raw = browsertools.make_handler(name)(dict(arguments or {}))
    except Exception as exc:  # noqa: BLE001
        raw = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
    is_error = False
    try:
        is_error = json.loads(raw).get("ok") is False
    except Exception:
        pass
    return {"content": [{"type": "text", "text": raw}], "isError": is_error}


def handle(request: dict) -> None:
    method = request.get("method") or ""
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_DEFAULT,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": (
                        "Chrome e Edge por CDP, em segundo plano. Comece por browser_status "
                        "(porta de debug aberta?) e browser_launch (profile=isolated) se preciso. "
                        "browser_eval/browser_read são o caminho mais barato para ler; "
                        "browser_click/browser_type agem na aba sem mover o cursor nem roubar foco. "
                        "Só browser_activate traz a janela para a frente."
                    ),
                },
            }
        )
        return

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return

    if method == "ping":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tool_list()}})
        return

    if method == "tools/call":
        _send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": call_tool(params.get("name") or "", params.get("arguments") or {}),
            }
        )
        return

    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        _send({"jsonrpc": "2.0", "id": req_id, "result": {key: []}})
        return

    if req_id is not None:
        _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}})


def main() -> int:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log(f"start (python {sys.version.split()[0]}, tools={'ok' if browsertools else 'ERRO: ' + IMPORT_ERROR})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except Exception:
            log(f"JSON inválido: {line[:200]}")
            continue
        if isinstance(request, list):
            for item in request:
                try:
                    handle(item)
                except Exception:
                    log(traceback.format_exc())
            continue
        try:
            handle(request)
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            if request.get("id") is not None:
                _send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": f"internal error: {exc}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
