#!/usr/bin/env python3
"""Cliente mínimo do Chrome DevTools Protocol — só stdlib.

Serve para Chrome e Edge (mesmo protocolo): descoberta de abas por HTTP em
``/json`` e comandos por WebSocket (RFC 6455) implementado aqui em ~120 linhas.
Sem Playwright, sem Selenium, sem pip: o plugin do Claude Code tem de rodar em
qualquer máquina que já tenha Chrome/Edge, e o processo MCP é stdio puro.

Uma conexão por operação (``with connect(port, tab) as page:``) — abrir e
fechar o socket custa ~1 ms e evita estado pendurado entre chamadas de tool.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_TIMEOUT = 30.0
# 127.0.0.1 nunca passa por proxy: um HTTP(S)_PROXY no ambiente faria cada
# /json/version esperar o proxy e transformaria cada tool em 2 s de atraso.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CDPError(RuntimeError):
    """Falha de transporte ou erro devolvido pelo próprio DevTools."""


# --------------------------------------------------------------------------- #
# endpoints HTTP (/json/*)
# --------------------------------------------------------------------------- #
def _http(url: str, method: str = "GET", timeout: float = 3.0):
    req = urllib.request.Request(url, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise CDPError(f"HTTP {exc.code} em {url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CDPError(f"porta CDP não respondeu: {exc}") from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"raw": raw}


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def version(port: int, timeout: float = 2.0) -> dict:
    """``/json/version`` — existe só quando a porta de debug está aberta."""
    return _http(f"{base_url(port)}/json/version", timeout=timeout) or {}


def socket_probe(port: int, timeout: float = 0.15) -> bool:
    """Teste rápido de porta. Porta fechada neste host demora ~2 s para recusar
    (firewall/EDR), então o probe usa timeout curto antes de qualquer HTTP."""
    try:
        sock = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def port_open(port: int, timeout: float = 1.0) -> bool:
    if not socket_probe(port):
        return False
    try:
        version(port, timeout=timeout)
        return True
    except CDPError:
        return False


def targets(port: int) -> list[dict]:
    data = _http(f"{base_url(port)}/json/list")
    return data if isinstance(data, list) else []


def pages(port: int) -> list[dict]:
    return [t for t in targets(port) if (t.get("type") or "") == "page"]


def open_tab(port: int, url: str = "about:blank") -> dict:
    """Abre aba. Chrome 111+ exige PUT em ``/json/new``; GET ainda existe em forks."""
    quoted = urllib.request.quote(url, safe="")
    try:
        data = _http(f"{base_url(port)}/json/new?{quoted}", method="PUT")
    except CDPError:
        data = _http(f"{base_url(port)}/json/new?{quoted}", method="GET")
    if not isinstance(data, dict) or not data.get("id"):
        raise CDPError(f"resposta inesperada de /json/new: {data!r}")
    return data


def close_tab(port: int, target_id: str) -> bool:
    try:
        _http(f"{base_url(port)}/json/close/{target_id}")
        return True
    except CDPError:
        return False


def activate_tab(port: int, target_id: str) -> bool:
    try:
        _http(f"{base_url(port)}/json/activate/{target_id}")
        return True
    except CDPError:
        return False


# --------------------------------------------------------------------------- #
# WebSocket (RFC 6455) — cliente mínimo
# --------------------------------------------------------------------------- #
class _WebSocket:
    def __init__(self, url: str, timeout: float = DEFAULT_TIMEOUT):
        u = urlparse(url)
        if u.scheme not in ("ws", ""):
            raise CDPError(f"url websocket inválida: {url}")
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise CDPError(f"não conectou em {host}:{port}: {exc}") from exc
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise CDPError("handshake: conexão fechada pelo navegador")
            head += chunk
            if len(head) > 65536:
                raise CDPError("handshake: cabeçalho grande demais")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise CDPError(f"handshake recusado: {status.decode('latin-1', 'replace')}")
        self._buf = head.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    # -- envio ------------------------------------------------------------- #
    def send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        header = bytearray([0x81])  # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        masked = _xor_mask(payload, mask)
        self.sock.sendall(bytes(header) + mask + masked)

    def _send_control(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode, 0x80 | len(payload)])
        body = _xor_mask(payload, mask)
        self.sock.sendall(bytes(header) + mask + body)

    # -- recepção ---------------------------------------------------------- #
    def _recv_exact(self, n: int) -> bytes:
        out = self._buf
        while len(out) < n:
            chunk = self.sock.recv(max(65536, n - len(out)))
            if not chunk:
                raise CDPError("websocket fechado pelo navegador")
            out += chunk
        self._buf = out[n:]
        return out[:n]

    def _frame(self):
        b0, b1 = self._recv_exact(2)
        opcode = b0 & 0x0F
        fin = bool(b0 & 0x80)
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        data = self._recv_exact(length) if length else b""
        if mask:
            data = _xor_mask(data, mask)
        return fin, opcode, data

    def recv_text(self, timeout: float | None = None) -> str:
        if timeout is not None:
            self.sock.settimeout(timeout)
        buf = b""
        while True:
            fin, opcode, data = self._frame()
            if opcode == 0x9:  # ping
                self._send_control(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                raise CDPError("navegador fechou o websocket")
            if opcode in (0x1, 0x0):
                buf += data
                if fin:
                    return buf.decode("utf-8", "replace")
                continue
            # opcode binário: o CDP não usa, mas não deixa travar
            if fin:
                return data.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self._send_control(0x8)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass


_XOR_CACHE: dict[bytes, bytes] = {}


def _xor_mask(data: bytes, mask: bytes) -> bytes:
    """XOR posicional de 4 bytes (RFC 6455) — via int grande, em C, não byte a byte."""
    n = len(data)
    if not n:
        return b""
    key = _XOR_CACHE.get(mask)
    if key is None or len(key) < n:
        reps = (n + 3) // 4
        key = (mask * reps)[:n]
        _XOR_CACHE[mask] = key
    return (int.from_bytes(data, "big") ^ int.from_bytes(key[:n], "big")).to_bytes(n, "big")


# --------------------------------------------------------------------------- #
# sessão por aba
# --------------------------------------------------------------------------- #
class Page:
    """Sessão CDP presa a um target (aba). Uso: ``with connect(port, tab) as p:``."""

    def __init__(self, port: int, target: dict, timeout: float = DEFAULT_TIMEOUT):
        self.port = int(port)
        self.target = dict(target or {})
        self.ws_url = self.target.get("webSocketDebuggerUrl") or ""
        if not self.ws_url:
            raise CDPError("aba sem webSocketDebuggerUrl (target de outro tipo?)")
        self.ws = _WebSocket(self.ws_url, timeout=timeout)
        self._id = 0

    # -- ciclo de vida ----------------------------------------------------- #
    def __enter__(self) -> "Page":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass

    # -- comandos ---------------------------------------------------------- #
    def call(self, method: str, timeout: float | None = None, **params) -> dict:
        self._id += 1
        msg_id = self._id
        self.ws.send_text(json.dumps({"id": msg_id, "method": method, "params": params}))
        deadline = time.time() + (timeout or DEFAULT_TIMEOUT)
        while True:
            remaining = max(0.5, deadline - time.time())
            raw = self.ws.recv_text(timeout=remaining)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != msg_id:
                continue  # evento (Page.*, Runtime.*) — ignorado de propósito
            if "error" in msg:
                err = msg["error"] or {}
                raise CDPError(f"{method}: {err.get('message') or err}")
            return msg.get("result") or {}

    # -- atalhos ----------------------------------------------------------- #
    def eval(self, expression: str, await_promise: bool = True, timeout: float | None = None):
        res = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=await_promise,
            userGesture=True,
            timeout=timeout or DEFAULT_TIMEOUT,
        )
        if res.get("exceptionDetails"):
            det = res["exceptionDetails"]
            desc = (det.get("exception") or {}).get("description") or det.get("text") or "erro JS"
            raise CDPError(f"JS: {desc}")
        return (res.get("result") or {}).get("value")

    def url(self) -> str:
        try:
            return self.eval("location.href", await_promise=False) or ""
        except CDPError:
            return self.target.get("url") or ""

    def title(self) -> str:
        try:
            return self.eval("document.title", await_promise=False) or ""
        except CDPError:
            return self.target.get("title") or ""


def connect(port: int, target_or_id) -> Page:
    """Abre sessão para um target dict ou targetId."""
    if isinstance(target_or_id, str):
        for t in targets(port):
            if t.get("id") == target_or_id:
                return Page(port, t)
        raise CDPError(f"aba {target_or_id} não existe mais")
    return Page(port, target_or_id)


def wait_for_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(port):
            return True
        time.sleep(0.25)
    return False
