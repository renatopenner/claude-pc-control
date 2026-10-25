#!/usr/bin/env python3
"""CLI do cofre: o jeito mais seguro de cadastrar um segredo (getpass, sem eco).

    python vault/vault_cli.py status
    python vault/vault_cli.py add                          # login: pede rótulo, site, usuário e senha
    python vault/vault_cli.py add-net                      # pasta de rede: \\\\servidor\\pasta + usuário
    python vault/vault_cli.py import --target "TERMSRV/x"  # importa do Gerenciador de Credenciais
    python vault/vault_cli.py list
    python vault/vault_cli.py remove <handle|label>
    python vault/vault_cli.py mount <handle|label> [--drive Z:]
    python vault/vault_cli.py check <\\\\servidor\\pasta>
    python vault/vault_cli.py elevate "net share"

A senha é digitada no terminal do próprio usuário (getpass) e vai direto para o
DPAPI — não passa por argumento de linha de comando nem por histórico.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "browser"))

import vault_core as vc  # noqa: E402
import vaulttools as vt  # noqa: E402


def _ask(prompt: str, default: str = "") -> str:
    if default:
        value = input(f"{prompt} [{default}]: ").strip()
        return value or default
    return input(f"{prompt}: ").strip()


def cmd_status(_args) -> int:
    print(json.dumps(vc.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_list(args) -> int:
    items = vc.list_items(args.kind)
    if not items:
        print("cofre vazio")
        return 0
    for item in items:
        meta = ", ".join(f"{k}={v}" for k, v in (item.get("meta") or {}).items() if v)
        print(f"{item['handle']:<12} {item['kind']:<8} {item['label']:<28} {meta}")
    return 0


def cmd_add(args) -> int:
    kind = args.kind
    label = args.label or _ask("Rótulo (ex.: Sistema Banpará)")
    if kind == "login":
        origin = args.origin or _ask("Origem exata (ex.: https://sistema.banpara.b.br)")
        username = args.username or _ask("Usuário")
    elif kind == "network":
        origin = args.target or _ask(r"Servidor e pasta (ex.: \\servidor\pasta)")
        username = args.username or _ask(r"Usuário (ex.: DOMINIO\usuario)")
    else:
        origin = ""
        username = args.username or _ask("Usuário (opcional)", "")
    secret = getpass.getpass("Segredo (não aparece): ")
    if not secret:
        print("nada digitado — cancelado")
        return 1
    meta = {"username": username}
    if kind == "login":
        meta["origin"] = origin
    elif kind == "network":
        meta["target"] = origin
    try:
        entry = vc.add_item(kind, label, secret, **meta)
    except vc.VaultError as exc:
        print(f"ERRO: {exc}")
        return 1
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def cmd_import(args) -> int:
    cred = vc.cred_read(args.target)
    if not cred:
        print(f"credencial não encontrada: {args.target}")
        print("destinos disponíveis:")
        for item in vc.cred_list():
            print(f"  {item.get('target')}  ({item.get('username', '?')})")
        return 1
    label = args.label or args.target.split("=")[-1]
    meta = {"username": cred.get("username") or ""}
    if args.kind == "login":
        meta["origin"] = args.origin or _ask("Origem exata (ex.: https://site)")
    elif args.kind == "network":
        meta["target"] = args.target_share or _ask(r"Servidor e pasta (ex.: \\servidor\pasta)")
    entry = vc.add_item(args.kind, label, cred["secret"], **meta)
    print(json.dumps(entry, ensure_ascii=False, indent=2))
    return 0


def cmd_remove(args) -> int:
    print(json.dumps(vc.remove(args.ref), ensure_ascii=False, indent=2))
    return 0


def cmd_mount(args) -> int:
    payload = json.loads(vt.tool_net_mount({"entry": args.ref, "drive": args.drive}))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def cmd_check(args) -> int:
    payload = json.loads(vt.tool_net_check({"target": args.target}))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def cmd_elevate(args) -> int:
    payload = json.loads(vt.tool_elevate({"command": args.command, "mode": args.mode}))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Cofre DPAPI do claude-pc-control")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    p = sub.add_parser("list")
    p.add_argument("--kind", choices=vc.KINDS)
    p.set_defaults(fn=cmd_list)

    for name in ("add", "add-net", "add-generic"):
        p = sub.add_parser(name)
        p.add_argument("label", nargs="?")
        p.add_argument("--origin")
        p.add_argument("--target")
        p.add_argument("--username")
        p.set_defaults(
            fn=cmd_add,
            kind="network" if name == "add-net" else ("generic" if name == "add-generic" else "login"),
        )

    p = sub.add_parser("import")
    p.add_argument("--target", required=True, help="destino no Gerenciador de Credenciais (cmdkey /list)")
    p.add_argument("--label")
    p.add_argument("--kind", choices=vc.KINDS, default="login")
    p.add_argument("--origin")
    p.add_argument("--target-share", dest="target_share")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("remove")
    p.add_argument("ref")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("mount")
    p.add_argument("ref")
    p.add_argument("--drive")
    p.set_defaults(fn=cmd_mount)

    p = sub.add_parser("check")
    p.add_argument("target")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("elevate")
    p.add_argument("command")
    p.add_argument("--mode", choices=["auto", "task"], default="auto")
    p.set_defaults(fn=cmd_elevate)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
