#!/usr/bin/env python3
"""Tools de navegador (Chrome e Edge) para o plugin pc-control do Claude Code.

Mesma filosofia do motor do PC: **segundo plano primeiro**. Tudo aqui fala CDP
com a porta de debug do navegador, o que significa que clicar, digitar, ler e
tirar print **não movem o cursor nem roubam o foco**. O único tool que traz algo
para a frente é `browser_activate`, e ele diz isso na resposta.

Sem dependências: cdp.py (stdlib) faz HTTP /json + WebSocket.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cdp  # noqa: E402

BASE = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "claude-pc-control"
STATE_FILE = BASE / "state.json"
CONFIG_FILE = BASE / "config.json"
SHOT_DIR = BASE / "shots"

BROWSERS: dict[str, dict] = {
    "chrome": {
        "exe": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        ],
        "process": "chrome.exe",
        "default_port": 9222,
        "user_data": Path(os.environ.get("LOCALAPPDATA") or "")
        / "Google"
        / "Chrome"
        / "User Data",
    },
    "edge": {
        "exe": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ],
        "process": "msedge.exe",
        "default_port": 9223,
        "user_data": Path(os.environ.get("LOCALAPPDATA") or "")
        / "Microsoft"
        / "Edge"
        / "User Data",
    },
}

MODS = {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4, "win": 4, "shift": 8}
KEYS = {
    "enter": ("Enter", "\r", 13),
    "return": ("Enter", "\r", 13),
    "tab": ("Tab", "\t", 9),
    "escape": ("Escape", "", 27),
    "esc": ("Escape", "", 27),
    "backspace": ("Backspace", "", 8),
    "delete": ("Delete", "", 46),
    "arrowup": ("ArrowUp", "", 38),
    "up": ("ArrowUp", "", 38),
    "arrowdown": ("ArrowDown", "", 40),
    "down": ("ArrowDown", "", 40),
    "arrowleft": ("ArrowLeft", "", 37),
    "left": ("ArrowLeft", "", 37),
    "arrowright": ("ArrowRight", "", 39),
    "right": ("ArrowRight", "", 39),
    "home": ("Home", "", 36),
    "end": ("End", "", 35),
    "pageup": ("PageUp", "", 33),
    "pagedown": ("PageDown", "", 34),
    "space": (" ", " ", 32),
}


class ToolError(RuntimeError):
    pass


_PORT_CACHE: dict[str, int] = {}


# --------------------------------------------------------------------------- #
# foco (o agente não pode roubar o teclado do usuário)
# --------------------------------------------------------------------------- #
def _fg_window() -> int:
    if os.name != "nt":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


def _set_fg_window(hwnd: int) -> bool:
    """Devolve o foco à janela que estava na frente antes do launch."""
    if os.name != "nt" or not hwnd:
        return False
    try:
        import ctypes

        u32 = ctypes.windll.user32
        if u32.GetForegroundWindow() == hwnd:
            return True
        # o Windows só deixa SetForegroundWindow de processo em segundo plano
        # quando há uma tecla "recém" pressionada: ALT down/up é o truque clássico.
        u32.keybd_event(0x12, 0, 0, 0)
        ok = bool(u32.SetForegroundWindow(hwnd))
        u32.keybd_event(0x12, 0, 2, 0)
        return ok
    except Exception:
        return False


def ok(**kw) -> str:
    payload = {"ok": True}
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False, default=str)


def fail(msg: str, **kw) -> str:
    payload = {"ok": False, "error": str(msg)}
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# configuração / estado
# --------------------------------------------------------------------------- #
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def config() -> dict:
    return _read_json(CONFIG_FILE, {})


def state() -> dict:
    return _read_json(STATE_FILE, {})


def remember(browser: str, value: dict) -> None:
    st = state()
    st.setdefault("last", {})[browser] = value
    st["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write_json(STATE_FILE, st)


def _name(browser: str | None) -> str:
    name = (browser or config().get("default_browser") or "chrome").strip().lower()
    if name not in BROWSERS:
        raise ToolError(f"navegador desconhecido: {browser!r} (use chrome ou edge)")
    return name


def _configured_port(name: str) -> int:
    cfg = config().get(name) or {}
    try:
        return int(cfg.get("port") or BROWSERS[name]["default_port"])
    except Exception:
        return int(BROWSERS[name]["default_port"])


def _active_port_file(user_data: Path) -> int | None:
    """Chrome/Edge gravam a porta real em <user-data-dir>/DevToolsActivePort."""
    try:
        first = (Path(user_data) / "DevToolsActivePort").read_text(encoding="utf-8", errors="replace").splitlines()
        if first and first[0].strip().isdigit():
            return int(first[0].strip())
    except Exception:
        pass
    return None


def _candidate_ports(name: str) -> list[int]:
    seen: list[int] = []
    ordered = [
        _active_port_file(BASE / "profiles" / name),
        (state().get("ports") or {}).get(name),
        _configured_port(name),
        int(BROWSERS[name]["default_port"]),
        _active_port_file(BROWSERS[name]["user_data"]),
    ]
    for port in ordered:
        try:
            p = int(port)
        except Exception:
            continue
        if p and p not in seen:
            seen.append(p)
    return seen


def _resolve_port(name: str) -> int | None:
    """Porta de debug realmente em uso: cache → perfil do agente → configurada → padrão."""
    cached = _PORT_CACHE.get(name)
    if cached and cdp.socket_probe(cached):
        return cached
    for port in _candidate_ports(name):
        if cdp.socket_probe(port) and cdp.port_open(port):
            _PORT_CACHE[name] = port
            _remember_port(name, port)
            return port
    _PORT_CACHE.pop(name, None)
    return None


def _remember_port(name: str, port: int) -> None:
    st = state()
    ports = st.setdefault("ports", {})
    if ports.get(name) != port:
        ports[name] = port
        _write_json(STATE_FILE, st)


def _exe(name: str) -> str:
    for cand in BROWSERS[name]["exe"]:
        if cand and Path(cand).is_file():
            return cand
    return ""


def _port_open(name: str) -> dict | None:
    port = _resolve_port(name)
    if port is None:
        return None
    try:
        return {"port": port, "version": cdp.version(port)}
    except cdp.CDPError:
        return None


def _require_debug(name: str) -> int:
    port = _resolve_port(name)
    if port is None:
        raise ToolError(
            f"{name} não está com porta de debug aberta. Rode browser_launch "
            f"(profile=isolated|user) ou feche o {name} e abra com --remote-debugging-port="
            f"{_configured_port(name)}."
        )
    return port


def _running(process: str) -> int:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process}", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.decode("latin-1", "replace")
    except Exception:
        return 0
    return sum(1 for line in out.splitlines() if process.lower() in line.lower())


# --------------------------------------------------------------------------- #
# alvos (abas)
# --------------------------------------------------------------------------- #
def _tab_list(name: str) -> list[dict]:
    port = _require_debug(name)
    return cdp.pages(port)


def _pick_tab(name: str, tab: str | int | None) -> dict:
    pages = _tab_list(name)
    if not pages:
        raise ToolError(f"{name} está aberto mas sem abas (page targets). Use browser_open.")
    if tab in (None, "", "last", "current"):
        last = (state().get("last") or {}).get(name) or {}
        wanted = last.get("id")
        if wanted:
            for t in pages:
                if t.get("id") == wanted:
                    return t
        return pages[0]
    if isinstance(tab, int) or (isinstance(tab, str) and tab.isdigit()):
        idx = int(tab)
        if idx == 0:
            return pages[0]
        if 1 <= idx <= len(pages):
            return pages[idx - 1]
        raise ToolError(f"índice {tab} fora do intervalo (1..{len(pages)})")
    raw = str(tab)
    for t in pages:
        if t.get("id") == raw or t.get("id", "").startswith(raw):
            return t
    for t in pages:
        if raw.lower() in (t.get("title") or "").lower() or raw in (t.get("url") or ""):
            return t
    raise ToolError(f"aba não encontrada: {raw}")


def _remember_tab(name: str, target: dict) -> None:
    remember(name, {"id": target.get("id"), "url": target.get("url"), "title": target.get("title")})


class Session:
    """Abre a página, executa e guarda o target usado como 'aba atual'."""

    def __init__(self, browser: str | None, tab=None):
        self.name = _name(browser)
        self.target = _pick_tab(self.name, tab)
        self.port = _require_debug(self.name)
        self.page: cdp.Page | None = None

    def __enter__(self) -> "Session":
        self.page = cdp.connect(self.port, self.target)
        _remember_tab(self.name, self.target)
        return self

    def __exit__(self, *exc) -> None:
        if self.page:
            self.page.close()

    def summary(self) -> dict:
        return {
            "browser": self.name,
            "tab_id": self.target.get("id"),
            "tab_url": self.page.url() if self.page else self.target.get("url"),
            "tab_title": self.page.title() if self.page else self.target.get("title"),
        }


# --------------------------------------------------------------------------- #
# JS de apoio
# --------------------------------------------------------------------------- #
_FIND_ELEMENT = r"""
(function () {
  const sel = %(sel)s, text = %(text)s, nth = %(nth)d;
  let el = null;
  if (sel) {
    el = document.querySelector(sel);
  } else if (text) {
    const wanted = String(text).toLowerCase();
    const nodes = Array.from(document.querySelectorAll(
      'a,button,[role=button],[role=link],[role=tab],[role=menuitem],input[type=submit],' +
      'input[type=button],label,summary,li,td,th,span,div,p,h1,h2,h3,h4'
    ));
    const hits = nodes.filter(function (e) {
      if (e.offsetParent === null && e.tagName !== 'BODY') { return false; }
      const t = (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().toLowerCase();
      return t.indexOf(wanted) !== -1;
    });
    hits.sort(function (a, b) {
      return (a.innerText || '').length - (b.innerText || '').length;
    });
    el = hits[nth] || null;
  } else {
    el = document.activeElement;
  }
  if (!el) { return { found: false }; }
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  return {
    found: true,
    tag: el.tagName,
    type: el.type || '',
    text: (el.innerText || el.value || '').trim().slice(0, 120),
    x: Math.round(r.left + r.width / 2),
    y: Math.round(r.top + r.height / 2),
    w: Math.round(r.width),
    h: Math.round(r.height),
    visible: !!(r.width && r.height) && style.visibility !== 'hidden' && style.display !== 'none',
    disabled: !!el.disabled
  };
})()
"""

_SET_VALUE = r"""
(function () {
  const sel = %(sel)s, clear = %(clear)s;
  const el = sel ? document.querySelector(sel) : document.activeElement;
  if (!el) { return { found: false }; }
  el.scrollIntoView({ block: 'center' });
  if (el.focus) { el.focus(); }
  if (clear) {
    if (el.isContentEditable) {
      el.textContent = '';
    } else if ('value' in el) {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype
        : el.tagName === 'SELECT' ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (desc && desc.set) { desc.set.call(el, ''); } else { el.value = ''; }
    }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
  if (el.setSelectionRange && 'value' in el) {
    try { el.setSelectionRange(el.value.length, el.value.length); } catch (e) {}
  }
  return { found: true, tag: el.tagName, type: el.type || '', editable: !!(el.isContentEditable || 'value' in el) };
})()
"""

_READ = r"""
(function (sel, mode, limit) {
  const el = sel ? document.querySelector(sel) : document.body;
  if (!el) { return { found: false }; }
  if (mode === 'html') { return { found: true, content: el.outerHTML.slice(0, limit) }; }
  if (mode === 'value') {
    return { found: true, content: String(el.value !== undefined ? el.value : el.textContent).slice(0, limit) };
  }
  if (mode === 'links') {
    const out = [];
    document.querySelectorAll('a[href]').forEach(function (a) {
      if (out.length < 300) { out.push({ text: (a.innerText || '').trim().slice(0, 80), href: a.href }); }
    });
    return { found: true, links: out, content: out.map(function (l) { return l.text + ' -> ' + l.href; }).join('\n').slice(0, limit) };
  }
  const txt = (el.innerText || el.textContent || '').replace(/\n{3,}/g, '\n\n');
  return { found: true, content: txt.slice(0, limit), length: txt.length };
})(%(sel)s, %(mode)s, %(limit)d)
"""


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
def tool_status(args: dict) -> str:
    info = {
        "config_file": str(CONFIG_FILE),
        "state_file": str(STATE_FILE),
        "shots_dir": str(SHOT_DIR),
        "browsers": {},
    }
    for name in BROWSERS:
        exe = _exe(name)
        open_info = _port_open(name) if exe else None
        port = (open_info or {}).get("port") or _configured_port(name)
        agent_profile = BASE / "profiles" / name
        entry = {
            "installed": bool(exe),
            "exe": exe,
            "process_running": _running(BROWSERS[name]["process"]),
            "debug_port": port,
            "debug_open": bool(open_info),
            "tabs": 0,
            "agent_profile": str(agent_profile),
            "agent_profile_port": _active_port_file(agent_profile),
            "user_data_dir": str(BROWSERS[name]["user_data"]),
        }
        if open_info:
            ver = open_info.get("version") or {}
            entry["browser_version"] = ver.get("Browser")
            entry["protocol"] = ver.get("Protocol-Version")
            try:
                entry["tabs"] = len(cdp.pages(port))
            except cdp.CDPError:
                pass
        info["browsers"][name] = entry
    last = (state().get("last") or {})
    if last:
        info["last_tab"] = last
    return ok(**info)


def tool_launch(args: dict) -> str:
    name = _name(args.get("browser"))
    profile = (args.get("profile") or "isolated").lower()
    if profile not in ("isolated", "user"):
        raise ToolError("profile deve ser 'isolated' (perfil dedicado) ou 'user' (seu perfil real, exige navegador fechado)")
    headless = bool(args.get("headless"))
    background = args.get("background", True) is not False

    exe = _exe(name)
    if not exe:
        raise ToolError(f"{name} não está instalado neste PC (procurei: {BROWSERS[name]['exe']})")

    if profile == "user":
        user_data = BROWSERS[name]["user_data"]
    else:
        user_data = BASE / "profiles" / name

    # já existe instância com debug aberta NESTE perfil? então não inicia nada
    own_port = _active_port_file(user_data)
    if own_port and cdp.port_open(own_port):
        _remember_port(name, own_port)
        _PORT_CACHE[name] = own_port
        return ok(
            already_running=True,
            browser=name,
            port=own_port,
            profile=profile,
            browser_version=(cdp.version(own_port) or {}).get("Browser"),
            note="este perfil já está com a porta de debug aberta — nada foi iniciado",
        )

    # Alguma instância já atende CDP (do agente ou do próprio usuário)? então o
    # trabalho do launch já está feito — anexar é melhor que abrir outra janela.
    existing = _resolve_port(name)
    if existing:
        return ok(
            already_running=True,
            browser=name,
            port=existing,
            profile=profile,
            browser_version=(cdp.version(existing) or {}).get("Browser"),
            note=f"porta de debug {existing} já está aberta — nada foi iniciado; use browser_tabs",
        )

    if profile == "user" and _running(BROWSERS[name]["process"]):
        raise ToolError(
            f"{name} está em execução com o seu perfil; ele ignora --remote-debugging-port. "
            f"Feche todas as janelas do {name} e chame de novo, ou use profile=isolated "
            f"(perfil dedicado: faça login uma vez e ele fica salvo)."
        )
    user_data.mkdir(parents=True, exist_ok=True)
    # porta 0 = o navegador escolhe e grava a real em <user-data-dir>/DevToolsActivePort;
    # assim duas instâncias (a do usuário e a do agente) nunca brigam pela mesma porta.
    requested = args.get("port")
    port = int(requested) if requested else 0

    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling",
        "--disable-backgrounding-occluded-windows",
    ]
    if headless:
        cmd.append("--headless=new")
    elif background:
        # janela fora da área visível e aberta sem ativar: o agente trabalha sem
        # aparecer na tela; nada disso rouba foco.
        cmd += ["--window-position=-32000,-32000", "--window-size=1280,900"]
    for extra in (args.get("extra_args") or []):
        cmd.append(str(extra))
    cmd.append(args.get("url") or "about:blank")

    startupinfo = None
    creationflags = 0
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE — abre minimizado, sem ativar
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    prev_fg = _fg_window()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        startupinfo=startupinfo,
        creationflags=creationflags,
        close_fds=True,
    )

    # O navegador sempre ativa a própria janela, mesmo minimizada e fora da tela.
    # Devolve o foco ao que estava na frente — em headless nem chega a existir janela.
    time.sleep(0.6)
    focus_restored = _set_fg_window(prev_fg) if not headless else True

    # Descobre a porta real: a pedida, a que o navegador gravou no perfil, ou uma
    # instância anterior que já dominava esse perfil.
    deadline = time.time() + float(args.get("timeout_s") or 25)
    actual = None
    while time.time() < deadline:
        if port and cdp.port_open(port):
            actual = port
            break
        from_file = _active_port_file(user_data)
        if from_file and cdp.port_open(from_file):
            actual = from_file
            break
        time.sleep(0.3)

    if actual is None:
        # o navegador pode ter encaminhado para uma instância já existente (mesmo
        # perfil) — se ela responde CDP, isso não é falha
        recovered = _resolve_port(name)
        if recovered:
            return ok(
                already_running=True,
                browser=name,
                port=recovered,
                profile=profile,
                note="havia instância com este perfil; anexei na porta que já respondia",
            )
        raise ToolError(
            f"{name} iniciou (pid {proc.pid}) mas a porta de debug não abriu. "
            f"Pode haver uma instância anterior presa nesse perfil ({user_data}) — "
            f"feche-a ou apague a pasta e repita."
        )

    _remember_port(name, actual)
    _PORT_CACHE[name] = actual
    remember(name, {"pid": proc.pid, "port": actual, "profile": profile, "user_data": str(user_data)})
    ver = cdp.version(actual)
    pages = cdp.pages(actual)
    return ok(
        launched=True,
        browser=name,
        pid=proc.pid,
        port=actual,
        profile=profile,
        headless=headless,
        background_window=bool(background and not headless),
        user_data_dir=str(user_data),
        browser_version=ver.get("Browser"),
        tabs=len(pages),
        focus_restored=focus_restored,
        note=(
            "perfil dedicado: faça login uma vez e a sessão fica salva para as próximas execuções"
            if profile == "isolated"
            else "perfil real do usuário"
        ),
    )


def tool_tabs(args: dict) -> str:
    name = _name(args.get("browser"))
    pages = _tab_list(name)
    last = ((state().get("last") or {}).get(name) or {}).get("id")
    out = []
    for i, t in enumerate(pages, start=1):
        out.append(
            {
                "index": i,
                "id": t.get("id"),
                "short_id": (t.get("id") or "")[:8],
                "title": t.get("title"),
                "url": t.get("url"),
                "is_last": t.get("id") == last,
            }
        )
    return ok(browser=name, port=_require_debug(name), count=len(out), tabs=out)


def tool_open(args: dict) -> str:
    name = _name(args.get("browser"))
    port = _require_debug(name)
    url = args.get("url") or "about:blank"
    prev_fg = _fg_window()
    target = cdp.open_tab(port, url)
    # abrir aba numa instância existente ativa a janela do navegador; devolve o foco
    time.sleep(0.25)
    focus_restored = _set_fg_window(prev_fg)
    _remember_tab(name, target)
    timeout = float(args.get("timeout_s") or 20)
    deadline = time.time() + timeout
    title = target.get("title")
    while time.time() < deadline:
        with cdp.connect(port, target) as page:
            try:
                if page.eval("document.readyState", await_promise=False) == "complete":
                    title = page.title() or title
                    break
            except cdp.CDPError:
                pass
        time.sleep(0.2)
    return ok(
        browser=name,
        tab_id=target.get("id"),
        short_id=(target.get("id") or "")[:8],
        url=url,
        title=title,
        focus_restored=focus_restored,
        foreground_used=False,
    )


def tool_navigate(args: dict) -> str:
    url = args.get("url")
    if not url:
        raise ToolError("informe url")
    with Session(args.get("browser"), args.get("tab")) as s:
        res = s.page.call("Page.navigate", url=url)
        if res.get("errorText"):
            raise ToolError(f"navegação recusada: {res['errorText']}")
        timeout = float(args.get("timeout_s") or 25)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if s.page.eval("document.readyState", await_promise=False) == "complete":
                    break
            except cdp.CDPError:
                pass
            time.sleep(0.2)
        _remember_tab(s.name, {**(s.target or {}), "url": url})
        return ok(foreground_used=False, url=s.page.url(), title=s.page.title(), **{"tab_id": s.target.get("id")})


def tool_read(args: dict) -> str:
    mode = (args.get("mode") or "text").lower()
    if mode not in ("text", "html", "value", "links"):
        raise ToolError("mode deve ser text|html|value|links")
    limit = max(500, min(int(args.get("max_chars") or 8000), 40000))
    js = _READ % {
        "sel": json.dumps(args.get("selector")),
        "mode": json.dumps(mode),
        "limit": limit,
    }
    with Session(args.get("browser"), args.get("tab")) as s:
        data = s.page.eval(js)
        if not isinstance(data, dict) or not data.get("found"):
            raise ToolError(f"elemento não encontrado: {args.get('selector')!r}")
        out = {
            "mode": mode,
            "url": s.page.url(),
            "title": s.page.title(),
            "content": data.get("content") or "",
            "truncated": bool(data.get("length") and data["length"] > limit),
        }
        if data.get("length"):
            out["total_chars"] = data["length"]
        if mode == "links":
            out["links"] = data.get("links") or []
        out["tab_id"] = s.target.get("id")
        out["foreground_used"] = False
        return ok(**out)


def tool_eval(args: dict) -> str:
    expr = args.get("expression")
    if not expr:
        raise ToolError("informe expression")
    with Session(args.get("browser"), args.get("tab")) as s:
        value = s.page.eval(expr, await_promise=args.get("await_promise", True) is not False)
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        limit = int(args.get("max_chars") or 20000)
        truncated = len(text) > limit
        return ok(
            value=text[:limit],
            truncated=truncated,
            url=s.page.url(),
            tab_id=s.target.get("id"),
            foreground_used=False,
        )


def tool_click(args: dict) -> str:
    selector = args.get("selector")
    text = args.get("text")
    if not selector and not text:
        raise ToolError("informe selector (CSS) ou text (texto visível do elemento)")
    nth = int(args.get("nth") or 0)
    real = bool(args.get("real"))
    js = _FIND_ELEMENT % {
        "sel": json.dumps(selector),
        "text": json.dumps(text),
        "nth": nth,
    }
    with Session(args.get("browser"), args.get("tab")) as s:
        target = s.page.eval(js)
        if not isinstance(target, dict) or not target.get("found"):
            raise ToolError(f"elemento não encontrado (selector={selector!r}, text={text!r}, nth={nth})")
        if not target.get("visible"):
            raise ToolError(f"elemento existe mas não está visível: {target.get('tag')} {target.get('text')!r}")
        if target.get("disabled"):
            raise ToolError(f"elemento desabilitado: {target.get('text')!r}")
        method = "js-click"
        if real:
            clicks = 2 if args.get("double") else 1
            x, y = target["x"], target["y"]
            s.page.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none")
            for count in range(1, clicks + 1):
                s.page.call(
                    "Input.dispatchMouseEvent",
                    type="mousePressed",
                    x=x,
                    y=y,
                    button="left",
                    clickCount=count,
                )
                s.page.call(
                    "Input.dispatchMouseEvent",
                    type="mouseReleased",
                    x=x,
                    y=y,
                    button="left",
                    clickCount=count,
                )
            method = "input-real(background-tab)"
        else:
            s.page.eval(
                """(function(){const el=document.elementFromPoint(%d,%d);
                   if(!el) return false; el.click(); return true;})()"""
                % (target["x"], target["y"])
            )
        out = {
            "clicked": True,
            "method": method,
            "element": {k: target.get(k) for k in ("tag", "text", "type", "x", "y")},
            "foreground_used": False,
            "url": s.page.url(),
            "tab_id": s.target.get("id"),
        }
        if args.get("wait_after_s"):
            time.sleep(float(args["wait_after_s"]))
            out["url_after"] = s.page.url()
            out["title_after"] = s.page.title()
        return ok(**out)


def tool_type(args: dict) -> str:
    text = args.get("text")
    if text is None:
        raise ToolError("informe text")
    selector = args.get("selector")
    clear = args.get("clear", True) is not False
    js = _SET_VALUE % {"sel": json.dumps(selector), "clear": json.dumps(bool(clear))}
    with Session(args.get("browser"), args.get("tab")) as s:
        info = s.page.eval(js)
        if not isinstance(info, dict) or not info.get("found"):
            raise ToolError(f"campo não encontrado: {selector!r}")
        if not info.get("editable"):
            raise ToolError(f"elemento não é editável: {info.get('tag')} {info.get('type')!r}")
        s.page.call("Input.insertText", text=str(text))
        if args.get("submit"):
            _press(s.page, "enter", [])
        value_after = s.page.eval(
            "(function(){const el=document.activeElement;return el && 'value' in el ? String(el.value) : (el ? String(el.innerText||'').slice(0,200) : '');})()"
        )
        return ok(
            typed=True,
            chars=len(str(text)),
            submitted=bool(args.get("submit")),
            value_after=value_after,
            field={"tag": info.get("tag"), "type": info.get("type")},
            foreground_used=False,
            url=s.page.url(),
            tab_id=s.target.get("id"),
        )


def _press(page: cdp.Page, key: str, modifiers: list[str], repeat: int = 1) -> dict:
    normalized = (key or "").strip()
    spec = KEYS.get(normalized.lower())
    if spec:
        name, text, vk = spec
    elif len(normalized) == 1:
        name, text, vk = normalized, normalized, ord(normalized.upper())
    else:
        raise ToolError(f"tecla desconhecida: {key}")
    bits = 0
    for mod in modifiers or []:
        bits |= MODS.get(str(mod).lower(), 0)
    for _ in range(max(1, int(repeat))):
        down = {"type": "keyDown", "key": name, "windowsVirtualKeyCode": vk, "modifiers": bits}
        if text:
            down["text"] = text
        page.call("Input.dispatchKeyEvent", **down)
        page.call(
            "Input.dispatchKeyEvent",
            type="keyUp",
            key=name,
            windowsVirtualKeyCode=vk,
            modifiers=bits,
        )
    return {"key": name, "modifiers": modifiers or [], "repeat": repeat}


def tool_key(args: dict) -> str:
    key = args.get("key")
    if not key:
        raise ToolError("informe key (Enter, Tab, Escape, ArrowDown, F5, ...)")
    with Session(args.get("browser"), args.get("tab")) as s:
        res = _press(s.page, key, args.get("modifiers") or [], int(args.get("repeat") or 1))
        return ok(**res, foreground_used=False, url=s.page.url(), tab_id=s.target.get("id"))


def tool_scroll(args: dict) -> str:
    direction = (args.get("direction") or "down").lower()
    amount = int(args.get("amount") or 600)
    selector = args.get("selector")
    js = r"""
(function (dir, amount, sel) {
  const el = sel ? document.querySelector(sel) : null;
  if (sel && !el) { return { found: false }; }
  if (dir === 'top') { (el || window).scrollTo(0, 0); }
  else if (dir === 'bottom') {
    const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
    (el || window).scrollTo(0, h);
  } else {
    const delta = dir === 'up' ? -amount : amount;
    if (el) { el.scrollBy(0, delta); } else { window.scrollBy(0, delta); }
  }
  const y = el ? el.scrollTop : window.scrollY;
  return { found: true, y: Math.round(y),
           max: el ? el.scrollHeight - el.clientHeight
                   : Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - window.innerHeight };
})(%(dir)s, %(amount)d, %(sel)s)
""" % {"dir": json.dumps(direction), "amount": amount, "sel": json.dumps(selector)}
    with Session(args.get("browser"), args.get("tab")) as s:
        data = s.page.eval(js)
        if not isinstance(data, dict) or not data.get("found"):
            raise ToolError(f"elemento não encontrado: {selector!r}")
        return ok(**data, foreground_used=False, tab_id=s.target.get("id"))


def tool_wait(args: dict) -> str:
    selector = args.get("selector")
    text = args.get("text")
    url_contains = args.get("url_contains")
    timeout = float(args.get("timeout_s") or 15)
    if not any((selector, text, url_contains, args.get("ready"))):
        raise ToolError("informe selector, text, url_contains ou ready=true")
    probe = r"""
(function (sel, txt) {
  if (sel && !document.querySelector(sel)) { return 'waiting'; }
  if (txt && (document.body ? document.body.innerText : '').indexOf(txt) === -1) { return 'waiting'; }
  if (document.readyState !== 'complete') { return 'waiting'; }
  return 'done';
})(%(sel)s, %(txt)s)
""" % {"sel": json.dumps(selector), "txt": json.dumps(text)}
    deadline = time.time() + timeout
    with Session(args.get("browser"), args.get("tab")) as s:
        last = None
        while time.time() < deadline:
            if url_contains and url_contains not in (s.page.url() or ""):
                time.sleep(0.25)
                continue
            try:
                last = s.page.eval(probe, await_promise=False)
            except cdp.CDPError as exc:
                last = f"erro: {exc}"
            if last == "done":
                return ok(
                    ready=True,
                    waited_s=round(timeout - max(0.0, deadline - time.time()), 2),
                    url=s.page.url(),
                    title=s.page.title(),
                    foreground_used=False,
                )
            time.sleep(0.25)
        raise ToolError(f"tempo esgotado ({timeout}s) esperando {selector or text or url_contains} — estado: {last}")


def tool_screenshot(args: dict) -> str:
    full = bool(args.get("full_page"))
    with Session(args.get("browser"), args.get("tab")) as s:
        params = {"format": "png", "captureBeyondViewport": full}
        if full:
            metrics = s.page.call("Page.getLayoutMetrics")
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            width = int(size.get("width") or 0)
            height = int(size.get("height") or 0)
            if width and height:
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": width,
                    "height": min(height, 20000),
                    "scale": 1,
                }
        res = s.page.call("Page.captureScreenshot", timeout=60, **params)
        data = res.get("data") or ""
        if not data:
            raise ToolError("captura vazia (aba em branco?)")
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() else "-" for c in (s.page.title() or "tab"))[:40].strip("-")
        path = SHOT_DIR / f"{s.name}-{time.strftime('%Y%m%d-%H%M%S')}-{safe or 'tab'}.png"
        path.write_bytes(__import__("base64").b64decode(data))
        return ok(
            path=str(path),
            bytes=path.stat().st_size,
            full_page=full,
            url=s.page.url(),
            title=s.page.title(),
            foreground_used=False,
            tab_id=s.target.get("id"),
        )


def tool_close(args: dict) -> str:
    name = _name(args.get("browser"))
    port = _require_debug(name)
    target = _pick_tab(name, args.get("tab"))
    prev_fg = _fg_window()
    if not cdp.close_tab(port, target.get("id")):
        raise ToolError("não consegui fechar a aba (ela pode ter sido fechada antes)")
    time.sleep(0.2)
    focus_restored = _set_fg_window(prev_fg)
    st = state()
    last = (st.get("last") or {}).get(name) or {}
    if last.get("id") == target.get("id"):
        remaining = cdp.pages(port) if cdp.port_open(port) else []
        st.setdefault("last", {})[name] = ({"id": remaining[0].get("id")} if remaining else {})
        _write_json(STATE_FILE, st)
    return ok(closed=True, closed_tab={"id": target.get("id"), "url": target.get("url")}, focus_restored=focus_restored, foreground_used=False)


def tool_activate(args: dict) -> str:
    name = _name(args.get("browser"))
    port = _require_debug(name)
    target = _pick_tab(name, args.get("tab"))
    cdp.activate_tab(port, target.get("id"))
    with cdp.connect(port, target) as page:
        try:
            page.call("Page.bringToFront")
        except cdp.CDPError:
            pass
        _remember_tab(name, target)
        return ok(
            activated=True,
            tab_id=target.get("id"),
            url=page.url(),
            title=page.title(),
            foreground_used=True,
            warning="esta é a única tool de navegador que traz a janela para a frente — use só quando precisar ver na tela",
        )


SCHEMAS: dict[str, dict] = {
    "browser_status": {
        "description": "Estado do Chrome/Edge: instalado, processo rodando, porta de debug aberta, abas, versão e caminhos de perfil/estado. Comece por aqui.",
        "parameters": {"type": "object", "properties": {}},
    },
    "browser_launch": {
        "description": "Inicia Chrome ou Edge com porta de debug (CDP) em SEGUNDO PLANO: janela fora da tela e aberta sem ativar, ou headless. profile=isolated usa perfil dedicado do plugin (faça login uma vez); profile=user usa o perfil real e exige o navegador fechado.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "profile": {"type": "string", "enum": ["isolated", "user"], "default": "isolated"},
                "port": {"type": "integer", "description": "porta de debug (padrão 9222 chrome / 9223 edge)"},
                "headless": {"type": "boolean", "default": False},
                "background": {"type": "boolean", "default": True, "description": "false mostra a janela na tela"},
                "url": {"type": "string"},
                "extra_args": {"type": "array", "items": {"type": "string"}},
                "timeout_s": {"type": "number"},
            },
            "required": ["browser"],
        },
    },
    "browser_tabs": {
        "description": "Lista as abas abertas (índice, id, título, url). Use o índice ou o short_id em qualquer outra tool.",
        "parameters": {
            "type": "object",
            "properties": {"browser": {"type": "string", "enum": ["chrome", "edge"]}},
        },
    },
    "browser_open": {
        "description": "Abre uma aba nova na URL e espera o carregamento. Não traz a janela para a frente.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "url": {"type": "string"},
                "timeout_s": {"type": "number"},
            },
            "required": ["url"],
        },
    },
    "browser_navigate": {
        "description": "Navega a aba escolhida para outra URL e espera o load.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"], "description": "índice (1-based), id, prefixo do id ou texto do título; padrão = última aba usada"},
                "url": {"type": "string"},
                "timeout_s": {"type": "number"},
            },
            "required": ["url"],
        },
    },
    "browser_read": {
        "description": "Lê o conteúdo da página ou de um seletor: text (visível), html, value (valor de campo) ou links. Sem foco, sem cursor.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "selector": {"type": "string"},
                "mode": {"type": "string", "enum": ["text", "html", "value", "links"], "default": "text"},
                "max_chars": {"type": "integer", "default": 8000},
            },
        },
    },
    "browser_eval": {
        "description": "Executa JavaScript na aba (Runtime.evaluate) e devolve o valor JSON. O caminho mais direto e mais barato para ler dados da página.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "expression": {"type": "string"},
                "await_promise": {"type": "boolean", "default": True},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["expression"],
        },
    },
    "browser_click": {
        "description": "Clica por seletor CSS ou por texto visível. Padrão é click via JS no próprio elemento (não move cursor, não rouba foco); real=true dispara eventos de mouse reais na aba em segundo plano.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "nth": {"type": "integer", "default": 0},
                "real": {"type": "boolean", "default": False},
                "double": {"type": "boolean", "default": False},
                "wait_after_s": {"type": "number"},
            },
        },
    },
    "browser_type": {
        "description": "Foca um campo e digita via Input.insertText (funciona em React/Vue, sem foco de janela). clear=true limpa antes; submit=true envia Enter.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "selector": {"type": "string", "description": "se omitido, usa o elemento em foco"},
                "text": {"type": "string"},
                "clear": {"type": "boolean", "default": True},
                "submit": {"type": "boolean", "default": False},
            },
            "required": ["text"],
        },
    },
    "browser_key": {
        "description": "Pressiona uma tecla na aba (Enter, Tab, Escape, Backspace, ArrowDown, F5, letra única) com modificadores ctrl/alt/shift/meta.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "key": {"type": "string"},
                "modifiers": {"type": "array", "items": {"type": "string"}},
                "repeat": {"type": "integer", "default": 1},
            },
            "required": ["key"],
        },
    },
    "browser_scroll": {
        "description": "Rola a página ou um container (direction up|down|top|bottom).",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "direction": {"type": "string", "enum": ["up", "down", "top", "bottom"], "default": "down"},
                "amount": {"type": "integer", "default": 600},
                "selector": {"type": "string"},
            },
        },
    },
    "browser_wait": {
        "description": "Espera aparecer um seletor, um texto, uma URL contendo algo, ou o load terminar.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "url_contains": {"type": "string"},
                "ready": {"type": "boolean"},
                "timeout_s": {"type": "number", "default": 15},
            },
        },
    },
    "browser_screenshot": {
        "description": "Captura PNG da aba (viewport ou página inteira) direto do renderer — funciona com a janela atrás de outras. Devolve o caminho do arquivo.",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
                "full_page": {"type": "boolean", "default": False},
            },
        },
    },
    "browser_close": {
        "description": "Fecha a aba escolhida (não fecha o navegador).",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
            },
        },
    },
    "browser_activate": {
        "description": "Traz a aba e a janela para a frente (ÚNICO tool que rouba foco — só quando o usuário precisa ver na tela).",
        "parameters": {
            "type": "object",
            "properties": {
                "browser": {"type": "string", "enum": ["chrome", "edge"]},
                "tab": {"type": ["string", "integer"]},
            },
        },
    },
}

HANDLERS = {
    "browser_status": tool_status,
    "browser_launch": tool_launch,
    "browser_tabs": tool_tabs,
    "browser_open": tool_open,
    "browser_navigate": tool_navigate,
    "browser_read": tool_read,
    "browser_eval": tool_eval,
    "browser_click": tool_click,
    "browser_type": tool_type,
    "browser_key": tool_key,
    "browser_scroll": tool_scroll,
    "browser_wait": tool_wait,
    "browser_screenshot": tool_screenshot,
    "browser_close": tool_close,
    "browser_activate": tool_activate,
}


def make_handler(name: str):
    fn = HANDLERS[name]

    def handler(args: dict) -> str:
        try:
            return fn(args or {})
        except ToolError as exc:
            return fail(str(exc))
        except cdp.CDPError as exc:
            return fail(f"CDP: {exc}")
        except Exception as exc:  # nunca derruba o processo MCP
            return fail(f"{type(exc).__name__}: {exc}")

    return handler
