#!/usr/bin/env python3
"""Selftest dos dois servidores MCP, exatamente como o Claude Code os inicia.

    python tests/selftest_mcp.py            # pc-control + browser-control
    python tests/selftest_mcp.py --pc       # só o pc-control
    python tests/selftest_mcp.py --browser  # só o navegador

Sobe cada servidor por `cmd /c <launcher>.cmd` (o mesmo comando que está no
.mcp.json), faz o handshake, lista as tools e chama uma tool de leitura.
Se isto passa, o problema (se houver) está no cliente MCP, não no plugin.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SERVERS = {
    "pc": {
        "launcher": ROOT / "engine" / "launch.cmd",
        "expect_tools": 17,
        "call": ("pc_status", {}),
        "expect_key": "session",
    },
    "browser": {
        "launcher": ROOT / "browser" / "launch.cmd",
        "expect_tools": 15,
        "call": ("browser_status", {}),
        "expect_key": "browsers",
    },
    "vault": {
        "launcher": ROOT / "vault" / "launch.cmd",
        "expect_tools": 10,
        "call": ("vault_status", {}),
        "expect_key": "encryption",
    },
}


class MCPClient:
    def __init__(self, launcher: Path):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        self.proc = subprocess.Popen(
            ["cmd", "/c", str(launcher)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._q.put(line)

    def _rpc(self, method: str, params: dict | None = None, timeout: float = 90.0) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._q.get(timeout=max(0.2, deadline - time.time()))
            except queue.Empty:
                break
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if data.get("id") == self._id:
                return data
        raise TimeoutError(f"{method} sem resposta em {timeout}s")

    def initialize(self) -> dict:
        res = self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "selftest", "version": "1.0"},
            },
        )
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return res.get("result") or {}

    def tools(self) -> list[dict]:
        return (self._rpc("tools/list").get("result") or {}).get("tools") or []

    def call(self, name: str, args: dict, timeout: float = 120.0) -> dict:
        res = self._rpc("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        result = res.get("result") or {}
        text = ""
        for item in result.get("content") or []:
            if item.get("type") == "text":
                text += item.get("text") or ""
        try:
            return json.loads(text)
        except ValueError:
            return {"_raw": text, "_isError": result.get("isError")}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def run(key: str) -> bool:
    spec = SERVERS[key]
    launcher = spec["launcher"]
    print(f"\n=== {key}: {launcher.name} ===")
    if not launcher.is_file():
        print(f"  FAIL launcher não existe: {launcher}")
        return False
    t0 = time.time()
    client = MCPClient(launcher)
    ok = True
    try:
        info = client.initialize()
        server = (info.get("serverInfo") or {})
        print(f"  ok   initialize -> {server.get('name')} {server.get('version')} ({round(time.time()-t0,2)}s)")
        tools = client.tools()
        names = [t.get("name") for t in tools]
        print(f"  {'ok  ' if len(tools) == spec['expect_tools'] else 'FAIL'} tools/list -> {len(tools)} tools")
        ok &= len(tools) == spec["expect_tools"]
        missing_desc = [t.get("name") for t in tools if not t.get("description")]
        if missing_desc:
            print(f"  FAIL sem descrição: {missing_desc}")
            ok = False
        print(f"       {', '.join(names)}")
        tool_name, tool_args = spec["call"]
        out = client.call(tool_name, tool_args)
        good = out.get("ok") is True and spec["expect_key"] in out
        print(f"  {'ok  ' if good else 'FAIL'} {tool_name} -> ok={out.get('ok')} ({round(time.time()-t0,2)}s)")
        if not good:
            print(f"       {json.dumps(out, ensure_ascii=False)[:400]}")
            ok = False
        # erro conhecido deve vir como JSON com ok=false, não como exceção
        bad = client.call("__tool_inexistente__", {})
        if bad.get("ok") is not False:
            print(f"  FAIL tool inexistente deveria devolver ok=false -> {json.dumps(bad)[:200]}")
            ok = False
        else:
            print("  ok   tool inexistente devolve ok=false (sem derrubar o servidor)")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL {type(exc).__name__}: {exc}")
        ok = False
    finally:
        client.close()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pc", action="store_true")
    ap.add_argument("--browser", action="store_true")
    args = ap.parse_args()
    keys = [k for k in SERVERS if (not args.pc and not args.browser) or getattr(args, k)]
    results = {k: run(k) for k in keys}
    print()
    for k, v in results.items():
        print(f"{'OK  ' if v else 'FALHA'} {k}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
