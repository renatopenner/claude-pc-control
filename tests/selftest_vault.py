#!/usr/bin/env python3
"""Selftest do cofre: DPAPI, pastas de rede e elevação — sem tocar em segredo real.

    python tests/selftest_vault.py
    python tests/selftest_vault.py --no-share    # pula o teste de rede (não cria share)

O que ele prova de verdade:
  * cifrar/decifrar com DPAPI ida e volta, e que o segredo nunca aparece em
    nenhuma resposta de tool (varredura literal nas saídas);
  * elevação sem UAC: tarefa agendada RunLevel Highest devolve integridade Alta
    (S-1-16-12288) mesmo com o token do agente filtrado;
  * monta \\\\127.0.0.1\\<share temporário> por WNetAddConnection2W, lista e desmonta.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vault"))
sys.path.insert(0, str(ROOT / "browser"))

import vault_core as vc  # noqa: E402
import vaulttools as vt  # noqa: E402

FAILS: list[str] = []
CHECKS = 0
SECRET = "Senha-De-Teste-9f3a!nao-usar"
SHARE = "pccselftest"
SHARE_DIR = Path(r"C:\Temp\pccselftest")
SURFACES: list[str] = []  # tudo que saiu de uma tool, para a varredura de vazamento


def check(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok   {label}")
        return
    FAILS.append(f"{label} {detail}".strip())
    print(f"  FAIL {label} {detail}")


def call(name: str, **args) -> dict:
    raw = vt.make_handler(name)(args)
    SURFACES.append(raw)
    out = json.loads(raw)
    print(f"  {name:<16} ok={out.get('ok')} {('' if out.get('ok') else out.get('error', ''))[:120]}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-share", action="store_true")
    args = ap.parse_args()

    print("== DPAPI ==")
    blob = vc.encrypt(SECRET)
    check("cifra e decifra", vc.decrypt(blob) == SECRET)
    check("blob não contém o segredo", SECRET not in blob)

    print("\n== cofre ==")
    st = call("vault_status")
    check("status ok", st.get("ok") is True and st.get("model_blind") is True)
    item = vc.add_item("network", "__selftest__", SECRET, target=r"\\127.0.0.1\naoexiste", username="teste")
    listed = call("vault_list", kind="network")
    check("entrada aparece na lista", any(i["handle"] == item["handle"] for i in listed.get("items", [])))
    resolved, secret = vc.resolve(item["handle"])
    check("resolve devolve o segredo (uso interno)", secret == SECRET)
    check("lista não expõe segredo", SECRET not in json.dumps(listed, ensure_ascii=False))
    check("segredo por argumento é recusado", call("vault_save", kind="login", label="x", secret="abc").get("ok") is False)

    print("\n== elevação (sem UAC) ==")
    elevated_now = vt._is_elevated()
    print(f"  token do agente elevado? {elevated_now}")
    res = call("elevate_run", command='whoami /groups | findstr /c:"S-1-16-12288"', mode="task")
    check("tarefa agendada roda com integridade Alta", "S-1-16-12288" in (res.get("output") or ""), str(res.get("output"))[:120])
    check("elevação devolve output legível", (res.get("exit_code") == 0) and bool(res.get("output")))

    if not args.no_share:
        print("\n== pasta de rede ==")
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        (SHARE_DIR / "leiame.txt").write_text("ok", encoding="utf-8")
        target = rf"\\127.0.0.1\{SHARE}"
        made = call("elevate_run", command=f'net share {SHARE}="{SHARE_DIR}"')
        listed = call("elevate_run", command="net share")
        created = SHARE.lower() in (listed.get("output") or "").lower()
        check("share temporário criado", created, str(made.get("output"))[:160])
        if created:
            chk = call("net_check", target=target)
            check("net_check enxerga o share", chk.get("accessible") is True, str(chk.get("error"))[:120])
            mnt = call("net_mount", target=target)
            check("net_mount conecta e lista", mnt.get("verified") is True, str(mnt)[:160])
            unm = call("net_unmount", target=target)
            check("net_unmount desconecta", unm.get("ok") is True)
            call("elevate_run", command=f"net share {SHARE} /delete")

    print("\n== vazamento de segredo ==")
    joined = "\n".join(SURFACES)
    check("nenhuma resposta contém o segredo", SECRET not in joined, "VAZOU: corrija antes de usar")

    vc.remove(item["handle"])
    print()
    if FAILS:
        print(f"{len(FAILS)} falha(s) de {CHECKS} checagens:")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"{CHECKS}/{CHECKS} checagens ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
