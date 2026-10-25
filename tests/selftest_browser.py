#!/usr/bin/env python3
"""Selftest das tools de navegador — exercita CDP de verdade no Chrome/Edge.

    python tests/selftest_browser.py                 # usa o Chrome que já estiver na porta 9222
    python tests/selftest_browser.py --launch        # inicia um perfil isolado em segundo plano
    python tests/selftest_browser.py --browser edge --launch
    python tests/selftest_browser.py --headless      # sem janela nenhuma

Verifica o que importa: abrir aba, ler, digitar em React-safe, clicar por seletor
e por texto, esperar, capturar PNG — e que o foco da janela NÃO muda.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "browser"))

import browsertools as bt  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def fg() -> str:
    h = ctypes.windll.user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetWindowTextW(h, buf, 256)
    return buf.value


def check(label: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok   {label}")
        return
    FAILS.append(f"{label} {detail}".strip())
    print(f"  FAIL {label} {detail}")


def call(name: str, **args) -> dict:
    t0 = time.time()
    out = json.loads(bt.make_handler(name)(args))
    took = round(time.time() - t0, 2)
    status = "ok" if out.get("ok") else f"ERRO: {out.get('error')}"
    print(f"  {name:<20} {status} ({took}s)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="chrome", choices=["chrome", "edge"])
    ap.add_argument("--launch", action="store_true", help="inicia perfil isolado em segundo plano")
    ap.add_argument("--profile", default="isolated", choices=["isolated", "user"])
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()
    browser = args.browser

    print(f"foreground inicial: {fg()!r}")
    status = call("browser_status")
    info = (status.get("browsers") or {}).get(browser) or {}
    check("navegador instalado", bool(info.get("installed")), str(info.get("exe")))

    if args.launch or not info.get("debug_open"):
        launch = call("browser_launch", browser=browser, profile=args.profile, headless=args.headless)
        check("porta de debug aberta", bool(launch.get("ok")), str(launch.get("error") or ""))
    fg_before = fg()

    fixture = (ROOT / "tests" / "fixture.html").as_uri()
    opened = call("browser_open", browser=browser, url=fixture)
    check("aba aberta", bool(opened.get("ok")), str(opened.get("error") or ""))

    read = call("browser_read", browser=browser, mode="text", max_chars=800)
    check("texto da página", "Ola Claude" in (read.get("content") or ""))

    typed = call("browser_type", browser=browser, selector="#q", text="hermes 123")
    check("digitação", typed.get("value_after") == "hermes 123", str(typed.get("value_after")))

    clicked = call("browser_click", browser=browser, selector="#b")
    out_text = call("browser_read", browser=browser, selector="#out")
    check("clique por seletor", "clicou:hermes 123" in (out_text.get("content") or ""), str(out_text.get("content")))

    by_text = call("browser_click", browser=browser, text="Devagar")
    check("clique por texto", bool(by_text.get("ok")), str(by_text.get("error") or ""))
    waited = call("browser_wait", browser=browser, selector="#log", text="pronto", timeout_s=6)
    check("wait por texto", bool(waited.get("ok")), str(waited.get("error") or ""))

    shot = call("browser_screenshot", browser=browser)
    path = Path(shot.get("path") or "")
    check("screenshot PNG", path.is_file() and path.stat().st_size > 1000, str(path))

    ev = call("browser_eval", browser=browser, expression="({title: document.title, links: document.links.length})")
    check("eval", "Fixture pc-control" in (ev.get("value") or ""))

    tabs = call("browser_tabs", browser=browser)
    check("lista de abas", (tabs.get("count") or 0) >= 1)

    closed = call("browser_close", browser=browser)
    check("aba fechada", bool(closed.get("ok")), str(closed.get("error") or ""))

    fg_after = fg()
    check("foco da janela não mudou", fg_before == fg_after, f"{fg_before!r} -> {fg_after!r}")

    print()
    if FAILS:
        print(f"{len(FAILS)} falha(s) de {CHECKS} checagens:")
        for f in FAILS:
            print("  -", f)
        return 1
    print(f"{CHECKS}/{CHECKS} checagens ok — {browser} em segundo plano, sem roubar foco.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
