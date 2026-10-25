#!/usr/bin/env python3
"""Cofre local do plugin: segredos cifrados com DPAPI, model-blind.

Mesmo princípio do cofre do Hermes Agent (``agent/vault_store.py``): o modelo vê
apenas handles e metadados; o valor secreto é resolvido **dentro do processo** e
nunca entra em resultado de tool, log ou transcrição.

Diferença de implementação: aqui não há arquivo de chave. Cada campo secreto é
cifrado individualmente com **DPAPI** (``CryptProtectData``, escopo do usuário
Windows) — a chave é derivada do logon do próprio Renato, então copiar o
``vault.json`` para outra máquina/usuário não abre nada.

Três tipos de item:

- ``login``   → ``password`` (opcional ``username``, ``origin`` = site exato)
- ``network`` → ``password`` (``target`` = ``\\\\servidor\\pasta``, ``username`` = ``DOMINIO\\user``)
- ``generic`` → ``secret`` (token, chave de API; ``note`` livre)

Nada aqui devolve segredo. Nem em erro.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

BASE = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "claude-pc-control"
VAULT_FILE = BASE / "vault.json"
VAULT_VERSION = 1

KINDS = ("login", "network", "generic")
SECRET_FIELD = {"login": "password", "network": "password", "generic": "secret"}
META_FIELDS = {
    "login": ("username", "origin", "note"),
    "network": ("username", "target", "note"),
    "generic": ("note",),
}
REQUIRED_META = {"login": ("origin",), "network": ("target",), "generic": ()}


class VaultError(Exception):
    """Falha do cofre. Mensagem sempre segura: nunca contém segredo."""


# --------------------------------------------------------------------------- #
# DPAPI — CryptProtectData / CryptUnprotectData (sem arquivo de chave)
# --------------------------------------------------------------------------- #
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _dpapi(data: bytes, protect: bool, entropy: bytes = b"claude-pc-control") -> bytes:
    """Chama CryptProtectData/CryptUnprotectData com entropia fixa (sem UI, sem prompt)."""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(
        ctypes.byref(_blob(data)),
        None,
        ctypes.byref(_blob(entropy)) if entropy else None,
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(out),
    )
    if not ok:
        raise VaultError(f"DPAPI falhou (erro {ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def encrypt(secret: str) -> str:
    if not secret:
        raise VaultError("segredo vazio")
    return base64.b64encode(_dpapi(secret.encode("utf-8"), protect=True)).decode("ascii")


def decrypt(blob: str) -> str:
    try:
        raw = base64.b64decode(blob.encode("ascii"))
    except Exception as exc:  # noqa: BLE001
        raise VaultError(f"blob inválido: {exc}") from exc
    return _dpapi(raw, protect=False).decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Windows Credential Manager — importar sem digitar nada
# --------------------------------------------------------------------------- #
class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wt.DWORD),
        ("Type", wt.DWORD),
        ("TargetName", wt.LPWSTR),
        ("Comment", wt.LPWSTR),
        ("LastWritten", wt.FILETIME),
        ("CredentialBlobSize", wt.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
        ("Persist", wt.DWORD),
        ("AttributeCount", wt.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wt.LPWSTR),
        ("UserName", wt.LPWSTR),
    ]


def cred_read(target: str) -> dict | None:
    """Lê uma credencial do Gerenciador de Credenciais do Windows (CRED_TYPE_GENERIC=1)."""
    advapi = ctypes.windll.advapi32
    ptr = ctypes.POINTER(_CREDENTIAL)()
    CRED_TYPE_GENERIC = 1
    if not advapi.CredReadW(ctypes.c_wchar_p(target), CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return None
    try:
        cred = ptr.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        try:
            secret = blob.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            secret = blob.decode("latin-1")
        return {
            "target": cred.TargetName,
            "username": cred.UserName,
            "secret": secret,
            "type": int(cred.Type),
        }
    finally:
        ctypes.windll.advapi32.CredFree(ptr)


def cred_list() -> list[dict]:
    """Lista destinos no Gerenciador de Credenciais (sem segredo)."""
    out = subprocess.run(
        ["cmdkey", "/list"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("latin-1", "replace")
    items: list[dict] = []
    current: dict = {}
    for raw in out.splitlines():
        line = raw.strip()
        if line.startswith("Destino:") or line.startswith("Target:"):
            if current:
                items.append(current)
            current = {"target": line.split(":", 1)[1].strip()}
        elif line.startswith("Usu") and current:  # Usuário / User
            current["username"] = line.split(":", 1)[1].strip()
    if current:
        items.append(current)
    return items


# --------------------------------------------------------------------------- #
# Armazenamento
# --------------------------------------------------------------------------- #
def _load() -> dict:
    try:
        data = json.loads(VAULT_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": VAULT_VERSION, "items": {}}
    except ValueError as exc:
        raise VaultError(f"{VAULT_FILE} corrompido: {exc}") from exc
    data.setdefault("items", {})
    return data


def _save(data: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    tmp = VAULT_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    tmp.replace(VAULT_FILE)


def _harden_acl() -> None:
    """Só o usuário atual lê o cofre (o DPAPI já impede outro usuário de decifrar,
    isto é a segunda tranca)."""
    try:
        user = os.environ.get("USERNAME") or ""
        subprocess.run(
            ["icacls", str(VAULT_FILE), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def add_item(kind: str, label: str, secret: str, **meta) -> dict:
    kind = (kind or "").lower()
    if kind not in KINDS:
        raise VaultError(f"kind inválido: {kind!r} (use {', '.join(KINDS)})")
    label = (label or "").strip()
    if not label:
        raise VaultError("label é obrigatório")
    for required in REQUIRED_META[kind]:
        if not (meta.get(required) or "").strip():
            raise VaultError(f"{required} é obrigatório para kind={kind}")
    data = _load()
    for handle, item in data["items"].items():
        if item.get("label", "").lower() == label.lower() and item.get("kind") == kind:
            raise VaultError(f"já existe {kind} com label {label!r} ({handle}) — apague antes")
    handle = f"{kind[:2]}-{secrets.token_hex(4)}"
    item = {
        "handle": handle,
        "kind": kind,
        "label": label,
        "meta": {k: (meta.get(k) or "") for k in META_FIELDS[kind]},
        "secret_field": SECRET_FIELD[kind],
        "blob": encrypt(secret),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "uses": 0,
    }
    data["items"][handle] = item
    _save(data)
    _harden_acl()
    return public(item)


def public(item: dict) -> dict:
    """Versão segura para o modelo: handles + metadados, jamais o segredo."""
    return {
        "handle": item.get("handle"),
        "kind": item.get("kind"),
        "label": item.get("label"),
        "meta": item.get("meta") or {},
        "created": item.get("created"),
        "uses": item.get("uses", 0),
    }


def list_items(kind: str | None = None) -> list[dict]:
    items = _load()["items"].values()
    if kind:
        items = [i for i in items if i.get("kind") == kind]
    return [public(i) for i in sorted(items, key=lambda i: (i.get("kind", ""), i.get("label", "")))]


def find(ref: str, kind: str | None = None) -> dict | None:
    """Acha por handle, prefixo de handle ou label (case-insensitive)."""
    ref = (ref or "").strip()
    if not ref:
        return None
    items = _load()["items"]
    if ref in items:
        return items[ref]
    lowered = ref.lower()
    for item in items.values():
        if item.get("handle", "").lower() == lowered:
            return item
    for item in items.values():
        if kind and item.get("kind") != kind:
            continue
        if item.get("label", "").lower() == lowered:
            return item
    for item in items.values():
        if kind and item.get("kind") != kind:
            continue
        if item.get("handle", "").startswith(ref):
            return item
    return None


def resolve(ref: str, kind: str | None = None) -> tuple[dict, str]:
    """Item + segredo em claro. Uso interno — nunca devolver isto a uma tool."""
    item = find(ref, kind)
    if not item:
        raise VaultError(f"entrada não encontrada: {ref!r}")
    secret = decrypt(item["blob"])
    data = _load()
    if item["handle"] in data["items"]:
        data["items"][item["handle"]]["uses"] = int(data["items"][item["handle"]].get("uses", 0)) + 1
        data["items"][item["handle"]]["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save(data)
    return item, secret


def remove(ref: str) -> dict:
    item = find(ref)
    if not item:
        raise VaultError(f"entrada não encontrada: {ref!r}")
    data = _load()
    data["items"].pop(item["handle"], None)
    _save(data)
    return public(item)


def status() -> dict:
    try:
        items = list_items()
        err = ""
    except VaultError as exc:
        items, err = [], str(exc)
    return {
        "vault_file": str(VAULT_FILE),
        "exists": VAULT_FILE.is_file(),
        "encryption": "DPAPI (CryptProtectData, escopo do usuário Windows)",
        "model_blind": True,
        "items": len(items),
        "by_kind": {k: sum(1 for i in items if i["kind"] == k) for k in KINDS},
        "error": err,
    }
