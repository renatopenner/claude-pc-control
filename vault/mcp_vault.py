#!/usr/bin/env python3
"""Servidor MCP (stdio) do cofre: credenciais, pastas de rede e elevação.

Mesmo protocolo dos outros dois servidores do plugin: JSON-RPC 2.0 por linha no
stdin/stdout; stdout é só protocolo, log vai para stderr. Nenhum segredo entra em
resposta de tool — o cofre resolve o valor dentro do processo e o descarta.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

SERVER_NAME = "vault"
SERVER_VERSION = "1.0.0"
PROTOCOL_DEFAULT = "2024-11-05"

try:
    import vaulttools  # noqa: E402
    IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    vaulttools = None
    IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


def log(msg: str) -> None:
    sys.stderr.write(f"[vault] {msg}\n")
    sys.stderr.flush()


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_list() -> list[dict]:
    if vaulttools is None:
        return []
    return [
        {
            "name": name,
            "description": schema.get("description", ""),
            "inputSchema": schema.get("parameters") or {"type": "object", "properties": {}},
        }
        for name, schema in vaulttools.SCHEMAS.items()
    ]


def call_tool(name: str, arguments: dict) -> dict:
    if vaulttools is None:
        return {
            "content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"vault não carregou: {IMPORT_ERROR}"}, ensure_ascii=False)}],
            "isError": True,
        }
    if name not in vaulttools.HANDLERS:
        return {
            "content": [{"type": "text", "text": json.dumps({"ok": False, "error": f"tool desconhecida: {name}"}, ensure_ascii=False)}],
            "isError": True,
        }
    try:
        raw = vaulttools.make_handler(name)(dict(arguments or {}))
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
                        "Cofre local (DPAPI) do usuário. vault_list mostra handles e metadados; "
                        "o segredo nunca volta. Para cadastrar, use vault_save source=dialog "
                        "(janela para o usuário digitar) ou source=credential_manager (importa do "
                        "Windows). net_mount monta \\\\servidor\\pasta com a credencial do cofre; "
                        "elevate_run roda comando como administrador sem pedir senha."
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
    log(f"start (python {sys.version.split()[0]}, tools={'ok' if vaulttools else 'ERRO: ' + IMPORT_ERROR})")
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
