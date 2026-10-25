#!/usr/bin/env python3
"""Janela local onde o próprio usuário digita o segredo — o agente nunca o vê.

Chamada por ``vault_save source=dialog`` (o servidor MCP abre este processo).
Grava no cofre DPAPI e devolve uma linha JSON no stdout:

    {"ok": true, "entry": {...}}     ou     {"ok": false, "error": "..."}

Uso direto:
    python vault/vault_dialog.py login "Sistema Banpará" "banpara\\rsoares" "https://sistema"
    python vault/vault_dialog.py network "Pasta GESER" "banpara\\rsoares" "\\\\servidor\\pasta"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import vault_core as vc  # noqa: E402

TITLES = {
    "login": "Senha do site",
    "network": "Senha da pasta de rede",
    "generic": "Segredo (token, chave)",
}


def main() -> int:
    kind = (sys.argv[1] if len(sys.argv) > 1 else "login").lower()
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    username = sys.argv[3] if len(sys.argv) > 3 else ""
    origin = sys.argv[4] if len(sys.argv) > 4 else ""
    if kind not in vc.KINDS:
        print(json.dumps({"ok": False, "error": f"kind inválido: {kind}"}, ensure_ascii=False))
        return 2

    result: dict = {"ok": False, "error": "cancelado"}

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"tkinter indisponível: {exc}"}, ensure_ascii=False))
        return 3

    root = tk.Tk()
    root.title(f"claude-pc-control — {TITLES.get(kind, kind)}")
    root.attributes("-topmost", True)
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=16)
    frame.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frame, text="Rótulo").grid(row=0, column=0, sticky="w", pady=(0, 4))
    label_var = tk.StringVar(value=label)
    label_entry = ttk.Entry(frame, textvariable=label_var, width=46)
    label_entry.grid(row=1, column=0, columnspan=2, sticky="we", pady=(0, 10))

    ttk.Label(frame, text="Usuário").grid(row=2, column=0, sticky="w", pady=(0, 4))
    user_var = tk.StringVar(value=username)
    ttk.Entry(frame, textvariable=user_var, width=46).grid(row=3, column=0, columnspan=2, sticky="we", pady=(0, 10))

    ttk.Label(frame, text="Senha (não aparece na tela)").grid(row=4, column=0, sticky="w", pady=(0, 4))
    pwd_var = tk.StringVar()
    pwd_entry = ttk.Entry(frame, textvariable=pwd_var, show="\u2022", width=46)
    pwd_entry.grid(row=5, column=0, columnspan=2, sticky="we", pady=(0, 12))

    info = ttk.Label(
        frame,
        text=f"{'Alvo: ' + origin if origin else ''}\nO valor é cifrado com DPAPI e nunca volta para o agente.",
        foreground="#666666",
        justify="left",
    )
    info.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 10))

    def save() -> None:
        nonlocal result
        pwd = pwd_var.get()
        lbl = label_var.get().strip()
        usr = user_var.get().strip()
        if not lbl:
            info.configure(text="Informe um rótulo.", foreground="#b00020")
            return
        if not pwd:
            info.configure(text="Informe a senha.", foreground="#b00020")
            return
        meta = {"username": usr}
        if kind == "login":
            meta["origin"] = origin
        elif kind == "network":
            meta["target"] = origin
        try:
            entry = vc.add_item(kind, lbl, pwd, **meta)
        except vc.VaultError as exc:
            info.configure(text=str(exc), foreground="#b00020")
            return
        result = {"ok": True, "entry": entry}
        root.destroy()

    def cancel() -> None:
        root.destroy()

    btns = ttk.Frame(frame)
    btns.grid(row=7, column=0, columnspan=2, sticky="e")
    ttk.Button(btns, text="Salvar", command=save).grid(row=0, column=0, padx=(0, 8))
    ttk.Button(btns, text="Cancelar", command=cancel).grid(row=0, column=1)

    root.bind("<Return>", lambda _e: save())
    root.bind("<Escape>", lambda _e: cancel())
    pwd_entry.focus_set()
    root.mainloop()

    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 4


if __name__ == "__main__":
    sys.exit(main())
