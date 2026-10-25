#!/usr/bin/env python3
"""Libera as tools do plugin no settings.json do Claude Code (sem prompt por tool).

Uso:
    python scripts/merge_settings.py            # adiciona as regras que faltarem
    python scripts/merge_settings.py --dry-run  # só mostra o que faria
    python scripts/merge_settings.py --remove   # tira as regras do plugin

Escreve em ``~/.claude/settings.json`` preservando tudo o que já existe. Regras
de permissão de tools MCP aceitam ``*`` apenas no nome da tool, depois do prefixo
``mcp__<servidor>__`` — por isso ``mcp__plugin_pc-control_pc__*``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RULES = [
    "mcp__plugin_pc-control_pc__*",
    "mcp__plugin_pc-control_browser__*",
    "mcp__plugin_pc-control_vault__*",
]


def settings_path() -> Path:
    override = os.environ.get("CLAUDE_SETTINGS")
    return Path(override) if override else Path.home() / ".claude" / "settings.json"


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        print(f"ERRO: {path} não é JSON válido ({exc}) — não vou sobrescrever.", file=sys.stderr)
        raise SystemExit(2)


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    path = settings_path()
    data = load(path)
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    deny = perms.setdefault("deny", [])
    # as regras não podem estar nas duas listas: deny vence allow
    for rule in RULES:
        if rule in deny:
            deny.remove(rule)

    changed = []
    for rule in RULES:
        present = rule in allow
        if args.remove and present:
            allow.remove(rule)
            changed.append(f"- {rule}")
        elif not args.remove and not present:
            allow.append(rule)
            changed.append(f"+ {rule}")

    if not changed:
        print(f"nada a fazer: {path} já está como esperado")
        return 0
    if args.dry_run:
        print(f"[dry-run] {path}")
        for line in changed:
            print("  " + line)
        return 0
    save(path, data)
    print(f"{path}:")
    for line in changed:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
