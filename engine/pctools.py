"""pc-control tools — schemas + handlers.

Every handler returns a JSON string and never raises: the agent gets a readable
error instead of a tool crash. Every side-effecting call passes the killswitch
and is written to an audit log the desktop pane can read.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import pccore  # noqa: E402

TOOLSET = "computer_use"
PLUGIN_ID = "pc-control"

CAPABILITIES = (
    "input", "foreground", "background", "window", "elements",
    "process", "service", "power", "screenshot", "clipboard",
    "com", "messages",
)

DEFAULT_STATE: dict[str, Any] = {
    "enabled": True,
    "allow": {cap: True for cap in CAPABILITIES},
    "audit": [],
}


# --------------------------------------------------------------------------- #
# State / killswitch / audit
# --------------------------------------------------------------------------- #

def data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    d = base / "hermes" / "plugin-data" / PLUGIN_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return data_dir() / "state.json"


def load_state() -> dict[str, Any]:
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("state is not an object")
    except Exception:
        raw = {}
    state = {**DEFAULT_STATE, **raw}
    allow = dict(DEFAULT_STATE["allow"])
    allow.update(raw.get("allow") or {})
    state["allow"] = allow
    if not isinstance(state.get("audit"), list):
        state["audit"] = []
    return state


def save_state(state: dict[str, Any]) -> None:
    tmp = state_path().with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_path())


def audit(tool: str, args: dict[str, Any], result: dict[str, Any]) -> None:
    try:
        state = load_state()
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "tool": tool,
            "action": str(args.get("action") or ""),
            "args": _trim_args(args),
            "ok": bool(result.get("ok", True)),
            "summary": _summarize(result),
        }
        state["audit"] = (state.get("audit") or [])[-99:] + [entry]
        save_state(state)
    except Exception:
        pass


def _trim_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 120:
            out[k] = v[:117] + "..."
        else:
            out[k] = v
    return out


def _summarize(result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result["error"])[:200]
    for key in ("path", "title", "state", "count", "chars", "chars_sent", "pid", "hwnd"):
        if key in result:
            return f"{key}={result[key]}"
    return "ok"


def _blocked(cap: str) -> dict[str, Any] | None:
    state = load_state()
    if not state.get("enabled", True):
        return {"ok": False, "error": "pc-control DESLIGADO (killswitch). Ligue com pc_control action=enable."}
    if not state.get("allow", {}).get(cap, True):
        return {"ok": False, "error": f"capacidade '{cap}' revogada. Libere com pc_control action=grant capability={cap}."}
    return None


def _res(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _wrap(tool: str, cap: str, fn, args: dict[str, Any]) -> str:
    blocked = _blocked(cap)
    if blocked:
        audit(tool, args, blocked)
        return _res(blocked)
    try:
        out = fn(args)
        if not isinstance(out, dict):
            out = {"ok": True, "result": out}
        out.setdefault("ok", True)
    except Exception as exc:  # never raise into the agent loop
        out = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    audit(tool, args, out)
    return _res(out)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #

def h_status(args: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    st = pccore.session_state()
    return {
        "ok": True,
        "enabled": state.get("enabled", True),
        "allow": state.get("allow"),
        "session": st,
        "delivery": {
            "foreground": "SendInput (mouse/teclado reais, precisa de foco)",
            "background": "PostMessage/WM_SETTEXT/BM_CLICK (sem foco; Win32 honra, Chromium/Electron costuma ignorar)",
            "uia": "pc_uia (IUIAutomation via .NET) — background que funciona em app moderno",
            "uia_available": pccore.uia_available(),
            "cua_driver": "computer_use — árvore AX + clique por elemento",
        },
        "audit_tail": (state.get("audit") or [])[-8:],
    }


def h_control(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "status").lower()
    state = load_state()
    cap = str(args.get("capability") or "").strip().lower()
    if action == "status":
        return {"ok": True, "enabled": state.get("enabled"), "allow": state.get("allow"),
                "capabilities": list(CAPABILITIES)}
    if action == "enable":
        state["enabled"] = True
        save_state(state)
        return {"ok": True, "enabled": True}
    if action == "disable":
        state["enabled"] = False
        save_state(state)
        return {"ok": True, "enabled": False}
    if action in ("grant", "revoke"):
        if cap not in CAPABILITIES:
            return {"ok": False, "error": f"capability inválida: {cap!r}. Use uma de {list(CAPABILITIES)}"}
        state["allow"][cap] = action == "grant"
        save_state(state)
        return {"ok": True, "allow": state["allow"]}
    if action in ("audit", "log"):
        limit = int(args.get("limit") or 30)
        return {"ok": True, "count": len(state.get("audit") or []),
                "items": (state.get("audit") or [])[-limit:]}
    if action == "reset":
        save_state({**DEFAULT_STATE, "allow": {c: True for c in CAPABILITIES}})
        return {"ok": True, "reset": True}
    return {"ok": False, "error": f"action desconhecida {action!r}"}


def h_windows(args: dict[str, Any]) -> dict[str, Any]:
    if args.get("hwnd"):
        win = pccore.find_window(hwnd=int(args["hwnd"]))
        return {"ok": True, "count": 1, "windows": [win]}
    wins = pccore.list_windows(
        include_hidden=bool(args.get("include_hidden")),
        title_filter=args.get("title"),
        limit=int(args.get("limit") or 200),
    )
    pn = (args.get("process") or "").lower()
    if pn:
        wins = [w for w in wins if pn in (w["process"] or "").lower()]
    return {"ok": True, "count": len(wins), "windows": wins}


def h_window(args: dict[str, Any]) -> dict[str, Any]:
    target = _target_hwnd(args)
    out = pccore.window_action(target, str(args.get("action") or "focus"),
                               x=args.get("x"), y=args.get("y"),
                               width=args.get("width"), height=args.get("height"))
    return {"ok": True, **out}


def h_elements(args: dict[str, Any]) -> dict[str, Any]:
    target = _target_hwnd(args)
    items = pccore.elements(target, depth=int(args.get("depth") or 4),
                            max_items=int(args.get("max_items") or 200),
                            include_empty=bool(args.get("include_empty")))
    return {"ok": True, "hwnd": target, "count": len(items), "elements": items}


def h_input(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "click").lower()
    mode = str(args.get("mode") or "auto").lower()
    hwnd = _maybe_hwnd(args)
    x, y = args.get("x"), args.get("y")

    if action == "wait":
        secs = min(float(args.get("seconds") or 0.5), 30.0)
        time.sleep(secs)
        return {"ok": True, "waited_s": secs}

    if action in ("click", "double_click", "right_click", "middle_click", "press_button"):
        if action == "press_button":
            if not hwnd:
                return {"ok": False, "error": "press_button precisa do hwnd do botão (veja pc_elements)"}
            return {"ok": True, **pccore.bg_press_button(hwnd)}
        button = {"right_click": "right", "middle_click": "middle"}.get(action, "left")
        double = action == "double_click" or bool(args.get("double"))
        if mode in ("auto", "background") and hwnd:
            if x is None or y is None:
                cx, cy = _center_of(hwnd)
                x, y = cx, cy
            out = pccore.bg_click(hwnd, int(x), int(y), button=button, double=double,
                                 coords=str(args.get("coords") or "screen"))
            out["hint"] = ("background entregue; se o app for Chromium/Electron/UWP e nada mudar, "
                           "repita com mode=foreground")
            return {"ok": True, **out}
        if hwnd and bool(args.get("focus", True)):
            pccore.window_action(hwnd, "focus")
            time.sleep(0.08)
        if x is None or y is None:
            if not hwnd:
                x, y = pccore.cursor_pos()
            else:
                x, y = _center_of(hwnd)
        pccore.fg_click(int(x), int(y), button=button, double=double,
                        hold_ms=int(args.get("hold_ms") or 0))
        return {"ok": True, "mode": "foreground", "button": button, "double": double,
                "point": [int(x), int(y)], "cursor": list(pccore.cursor_pos())}

    if action == "move":
        if x is None or y is None:
            return {"ok": False, "error": "move precisa de x,y"}
        pccore.fg_move(int(x), int(y))
        return {"ok": True, "cursor": list(pccore.cursor_pos())}

    if action == "drag":
        if None in (x, y, args.get("x2"), args.get("y2")):
            return {"ok": False, "error": "drag precisa de x,y,x2,y2"}
        pccore.fg_drag(int(x), int(y), int(args["x2"]), int(args["y2"]),
                       button=str(args.get("button") or "left"),
                       steps=int(args.get("steps") or 12),
                       duration_ms=int(args.get("duration_ms") or 260))
        return {"ok": True, "mode": "foreground", "from": [x, y], "to": [args["x2"], args["y2"]]}

    if action == "scroll":
        amount = int(args.get("amount") or 3)
        if mode in ("auto", "background") and hwnd:
            return {"ok": True, **pccore.bg_wheel(hwnd, amount, x=x, y=y,
                                                  coords=str(args.get("coords") or "screen"))}
        pccore.fg_scroll(amount, horizontal=bool(args.get("horizontal")))
        return {"ok": True, "mode": "foreground", "amount": amount}

    if action == "type":
        text = str(args.get("text") or "")
        if not text:
            return {"ok": False, "error": "type precisa de text"}
        if mode in ("auto", "background") and hwnd:
            return {"ok": True, **pccore.bg_type(hwnd, text)}
        if hwnd and bool(args.get("focus", True)):
            pccore.window_action(hwnd, "focus")
            time.sleep(0.08)
        n = pccore.fg_type(text)
        return {"ok": True, "mode": "foreground", "chars": n}

    if action in ("key", "hotkey", "press"):
        combo = str(args.get("key") or args.get("keys") or args.get("combo") or "")
        if not combo:
            return {"ok": False, "error": "key/hotkey precisa de key (ex: 'ctrl+s', 'enter')"}
        if mode in ("auto", "background") and hwnd:
            return {"ok": True, **pccore.bg_key(hwnd, combo)}
        if hwnd and bool(args.get("focus", True)):
            pccore.window_action(hwnd, "focus")
            time.sleep(0.08)
        keys = pccore.fg_key(combo, hold_ms=int(args.get("hold_ms") or 0))
        return {"ok": True, "mode": "foreground", "keys": keys}

    return {"ok": False, "error": f"action desconhecida {action!r}"}


def h_settext(args: dict[str, Any]) -> dict[str, Any]:
    hwnd = _maybe_hwnd(args)
    if not hwnd and (args.get("element_text") or args.get("element_id") is not None):
        win = _target_hwnd(args)
        el = pccore.find_element(win, text=args.get("element_text"),
                                 control_id=args.get("element_id"))
        hwnd = el["hwnd"]
    if not hwnd:
        return {"ok": False, "error": "pc_settext precisa de hwnd (ou hwnd da janela + element_text/element_id)"}
    return {"ok": True, **pccore.bg_settext(hwnd, str(args.get("text") or ""))}


def h_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    target = str(args.get("target") or "screen").lower()
    if target in ("window", "win"):
        hwnd = _target_hwnd(args)
        return {"ok": True, **pccore.capture_window(hwnd, args.get("path"))}
    return {"ok": True, **pccore.capture_screen(path=args.get("path"),
                                                monitor=args.get("monitor"),
                                                bbox=args.get("bbox"))}


def h_clipboard(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "get").lower()
    if action == "get":
        return {"ok": True, **pccore.clipboard_get()}
    if action == "set":
        return {"ok": True, **pccore.clipboard_set(str(args.get("text") or ""))}
    return {"ok": False, "error": f"action desconhecida {action!r}"}


def h_process(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "list").lower()
    if action == "list":
        items = pccore.process_list(name=args.get("name"), limit=int(args.get("limit") or 100))
        return {"ok": True, "count": len(items), "processes": items}
    if action == "start":
        if not args.get("command"):
            return {"ok": False, "error": "start precisa de command"}
        return {"ok": True, **pccore.process_start(str(args["command"]), cwd=args.get("cwd"))}
    if action in ("stop", "kill"):
        if not (args.get("pid") or args.get("name")):
            return {"ok": False, "error": "stop/kill precisa de pid ou name"}
        force = action == "kill" or bool(args.get("force"))
        return {"ok": True, **pccore.process_stop(pid=args.get("pid"), name=args.get("name"),
                                                  tree=bool(args.get("tree", True)), force=force)}
    return {"ok": False, "error": f"action desconhecida {action!r}"}


def h_service(args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("name"):
        return {"ok": False, "error": "name do serviço é obrigatório"}
    return {"ok": True, **pccore.service(str(args.get("action") or "status"), str(args["name"]))}


def h_power(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").lower()
    if action in ("shutdown", "restart", "reboot", "logoff"):
        if str(args.get("confirm") or "").lower() not in ("yes", "sim", "true", "1"):
            return {"ok": False, "error": "ação destrutiva: repita com confirm='yes'"}
    return {"ok": True, **pccore.power(action, delay_s=int(args.get("delay_s") or 30))}


def h_audit(args: dict[str, Any]) -> dict[str, Any]:
    state = load_state()
    limit = int(args.get("limit") or 30)
    return {"ok": True, "count": len(state.get("audit") or []),
            "items": (state.get("audit") or [])[-limit:]}


def h_uia(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "dump").lower()
    limit = int(args.get("limit") or 200)
    common = {
        "hwnd": _maybe_hwnd(args) or int(pccore.user32.GetForegroundWindow() or 0),
        "name": args.get("name"),
        "automation_id": args.get("automation_id"),
        "type_": args.get("type"),
        "index": int(args.get("index") or 0),
        "contains": bool(args.get("contains")),
    }
    if action in ("dump", "tree"):
        out = pccore.uia_dump(common["hwnd"], limit=limit)
        if out.get("ok"):
            out["tip"] = ("use action=find/invoke/set_value com name/automation_id/type "
                          "para agir sem roubar foco")
        return out
    if action in ("find", "get", "info"):
        return pccore.uia_find(**common)
    if action == "click":
        return pccore.uia_click_element(**common)
    if action == "set_value":
        return pccore.uia_do("set_value", value=str(args.get("value") if args.get("value") is not None
                                                    else args.get("text") or ""), **common)
    if action in ("invoke", "toggle", "select", "expand", "collapse", "scroll_into_view", "focus", "get_value"):
        return pccore.uia_do(action, **common)
    return {"ok": False, "error": f"action desconhecida {action!r}"}


def h_act(args: dict[str, Any]) -> dict[str, Any]:
    """Universal ladder: uia-pattern -> uia-legacy -> win32 -> foreground."""
    action = str(args.get("action") or "click").lower()
    hwnd = _maybe_hwnd(args)
    if not hwnd:
        hwnd = int(pccore.user32.GetForegroundWindow() or 0)
    out = pccore.act(
        action,
        hwnd=hwnd,
        name=args.get("name"),
        automation_id=args.get("automation_id"),
        type_=args.get("type"),
        text=args.get("text"),
        x=args.get("x"),
        y=args.get("y"),
        coords=str(args.get("coords") or "screen"),
        allow_foreground=bool(args.get("allow_foreground", True)),
        index=int(args.get("index") or 0),
        contains=bool(args.get("contains")),
        direction=args.get("direction"),
        value=args.get("value"),
    )
    if out.get("foreground_used"):
        out["visible_now"] = True
        out["note"] = ("esta ação precisou de primeiro plano: a janela foi trazida para frente "
                       "e o cursor foi usado")
    return out


def h_com(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "attach").lower()
    progid = str(args.get("progid") or "").strip()
    if not progid:
        return {"ok": False, "error": "progid obrigatório, ex: 'Word.Application', 'Excel.Application', "
                                      "'Shell.Application', 'WScript.Shell', 'Outlook.Application'"}
    return pccore.com_do(
        action, progid,
        member=str(args.get("member") or ""),
        args=args.get("args"),
        value=args.get("value"),
        create=bool(args.get("create")),
        limit=int(args.get("limit") or 200),
    )


def h_win32(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "msg").lower()
    hwnd = _maybe_hwnd(args)
    if action in ("command", "find") and not hwnd:
        return {"ok": False, "error": "hwnd (ou window_title/window_process) é obrigatório"}
    if action == "command":
        ctl = None
        if args.get("control_id") is not None:
            try:
                ctl = pccore.find_control(int(hwnd), control_id=int(args["control_id"]),
                                          text=args.get("control_text"), class_=args.get("control_class"))
            except Exception:
                ctl = None
        return pccore.wm_command(int(hwnd), int(args.get("control_id") or (ctl or {}).get("id") or 0),
                                 notify=int(args.get("notify") or 0),
                                 post=bool(args.get("post", True)),
                                 child_hwnd=int((ctl or {}).get("hwnd") or args.get("child_hwnd") or 0))
    if action == "find":
        try:
            return {"ok": True, "control": pccore.find_control(int(hwnd),
                                                               control_id=args.get("control_id"),
                                                               text=args.get("control_text"),
                                                               class_=args.get("control_class"))}
        except LookupError as exc:
            return {"ok": False, "error": str(exc)}
    if action in ("msg", "send"):
        if args.get("msg") is None:
            return {"ok": False, "error": "msg obrigatório (decimal ou 0x...)"}
        msg = args["msg"]
        msg_int = int(str(msg), 16) if isinstance(msg, str) and str(msg).lower().startswith("0x") else int(msg)
        return pccore.win32_send(int(hwnd or 0), msg_int,
                                 wparam=int(args.get("wparam") or 0),
                                 lparam=int(args.get("lparam") or 0),
                                 post=(action == "msg") and bool(args.get("post", True)))
    return {"ok": False, "error": f"action desconhecida {action!r}",
            "actions": ["command", "msg", "send", "find"]}


# --------------------------------------------------------------------------- #
# Target resolution helpers
# --------------------------------------------------------------------------- #

def _maybe_hwnd(args: dict[str, Any]) -> int | None:
    h = args.get("hwnd")
    if h:
        return int(h)
    if args.get("window_title") or args.get("window_process"):
        try:
            return int(pccore.find_window(title=args.get("window_title"),
                                          process=args.get("window_process"))["hwnd"])
        except Exception:
            return None
    return None


def _target_hwnd(args: dict[str, Any]) -> int:
    h = _maybe_hwnd(args)
    if h:
        return h
    return int(pccore.user32.GetForegroundWindow() or 0)


def _center_of(hwnd: int) -> tuple[int, int]:
    l, t, r, b = pccore.frame_bounds(hwnd)
    return (l + r) // 2, (t + b) // 2


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

def _s(name: str, description: str, props: dict[str, Any] | None = None,
       required: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": required or []},
    }


_TGT = {
    "hwnd": {"type": "integer", "description": "Window/control handle (from pc_windows / pc_elements)."},
    "window_title": {"type": "string", "description": "Substring of the window title (alternative to hwnd)."},
    "window_process": {"type": "string", "description": "Process name substring, e.g. 'notepad' (alternative to hwnd)."},
}

SCHEMAS: dict[str, dict[str, Any]] = {
    "pc_status": _s(
        "pc_status",
        "Estado do controle do PC: sessão interativa/bloqueada, janela em foco, monitores, DPI, elevação, "
        "killswitch e capacidades liberadas. Chame antes de automatizar para saber se o desktop aceita input.",
    ),
    "pc_control": _s(
        "pc_control",
        "Killswitch e permissões do pc-control. action=status|enable|disable|grant|revoke|audit|reset. "
        "grant/revoke exigem capability (input, foreground, background, window, elements, process, service, power, screenshot, clipboard).",
        {"action": {"type": "string", "enum": ["status", "enable", "disable", "grant", "revoke", "audit", "reset"]},
         "capability": {"type": "string", "description": "Capacidade para grant/revoke."},
         "limit": {"type": "integer", "description": "Itens de auditoria (default 30)."}},
        ["action"],
    ),
    "pc_windows": _s(
        "pc_windows",
        "Lista janelas abertas (hwnd, título, classe, processo, PID, retângulo, foco, minimizada). "
        "Filtre por title/process; include_hidden inclui janelas invisíveis.",
        {**_TGT,
         "title": {"type": "string", "description": "Filtro por substring do título."},
         "process": {"type": "string", "description": "Filtro por nome do processo."},
         "include_hidden": {"type": "boolean", "description": "Inclui janelas invisíveis/sem título."},
         "limit": {"type": "integer"}},
    ),
    "pc_window": _s(
        "pc_window",
        "Ação numa janela: focus|minimize|maximize|restore|close|topmost|untopmost|move|resize. "
        "focus/activate traz para frente (muda o foco do usuário — use só quando precisar de input em primeiro plano).",
        {**_TGT,
         "action": {"type": "string", "enum": ["focus", "activate", "minimize", "maximize", "restore",
                                               "close", "topmost", "untopmost", "move", "resize"]},
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "width": {"type": "integer"}, "height": {"type": "integer"}},
        ["action"],
    ),
    "pc_elements": _s(
        "pc_elements",
        "Árvore de controles (child windows) de uma janela: hwnd de cada controle, classe, texto, id, retângulo, "
        "se é clicável/editável. É o mapa para automatizar EM SEGUNDO PLANO: pegue o hwnd do botão/edit e use "
        "pc_input action=press_button ou pc_settext, sem roubar foco. Sem COM/UIA — funciona em apps Win32 clássicos.",
        {**_TGT,
         "depth": {"type": "integer", "description": "Profundidade da árvore (default 4)."},
         "max_items": {"type": "integer"},
         "include_empty": {"type": "boolean", "description": "Inclui controles sem texto."}},
    ),
    "pc_input": _s(
        "pc_input",
        "Mouse e teclado com DOIS modos. mode=background (default quando há hwnd): PostMessage direto no HWND — "
        "não move o cursor nem rouba foco, mas Chromium/Electron/UWP e apps XAML costumam ignorar. mode=foreground: "
        "SendInput real, funciona em tudo, mas move o cursor e precisa de foco. mode=auto escolhe background se houver hwnd.\n"
        "Antes de usar aqui em um app desconhecido, prefira pc_uia (dump + invoke/set_value): é background que "
        "funciona em app moderno. Use pc_input quando o alvo for Win32 clássico, canvas/jogo, ou quando o "
        "background já falhou e você aceita mover o cursor.\n"
        "action: click|double_click|right_click|middle_click|move|drag|scroll|type|key|hotkey|press_button|wait. "
        "press_button usa BM_CLICK no hwnd do botão (pc_elements) — clique de verdade em segundo plano.",
        {**_TGT,
         "action": {"type": "string", "enum": ["click", "double_click", "right_click", "middle_click", "move",
                                               "drag", "scroll", "type", "key", "hotkey", "press_button", "wait"]},
         "mode": {"type": "string", "enum": ["auto", "background", "foreground"]},
         "x": {"type": "integer", "description": "X (coords de tela; no background, cliente quando coords=client)."},
         "y": {"type": "integer"},
         "x2": {"type": "integer", "description": "Destino do drag."},
         "y2": {"type": "integer"},
         "text": {"type": "string", "description": "Texto para action=type."},
         "key": {"type": "string", "description": "Atalho: 'enter', 'tab', 'ctrl+s', 'alt+f4', 'f5'."},
         "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "amount": {"type": "integer", "description": "Scroll: ticks (positivo = cima/esquerda)."},
         "coords": {"type": "string", "enum": ["screen", "client"]},
         "focus": {"type": "boolean", "description": "Foreground: focar a janela antes (default true)."},
         "steps": {"type": "integer"}, "duration_ms": {"type": "integer"},
         "hold_ms": {"type": "integer"}, "seconds": {"type": "number"}},
        ["action"],
    ),
    "pc_settext": _s(
        "pc_settext",
        "Escreve texto direto num controle via WM_SETTEXT (o jeito mais confiável de preencher campo em segundo "
        "plano). Passe o hwnd do Edit (de pc_elements) ou o hwnd da janela + element_text/element_id.",
        {**_TGT,
         "text": {"type": "string"},
         "element_text": {"type": "string", "description": "Texto/rótulo do controle de destino."},
         "element_id": {"type": "integer", "description": "ID do controle (campo 'id' de pc_elements)."}},
        ["text"],
    ),
    "pc_screenshot": _s(
        "pc_screenshot",
        "Captura tela inteira, um monitor ou uma janela específica (PrintWindow: funciona mesmo se a janela estiver "
        "atrás de outras). Salva PNG e devolve o caminho para entregar com MEDIA:.",
        {"target": {"type": "string", "enum": ["screen", "monitor", "window"]},
         **_TGT,
         "monitor": {"type": "integer", "description": "Índice do monitor (pc_status)."},
         "bbox": {"type": "array", "items": {"type": "integer"}, "description": "[l,t,r,b] em pixels físicos."},
         "path": {"type": "string", "description": "Caminho .png de destino (opcional)."}},
    ),
    "pc_clipboard": _s(
        "pc_clipboard",
        "Lê ou define a área de transferência (texto/arquivos). Útil para colar conteúdo grande em qualquer app.",
        {"action": {"type": "string", "enum": ["get", "set"]},
         "text": {"type": "string"}},
        ["action"],
    ),
    "pc_process": _s(
        "pc_process",
        "Processos: list (com filtro name), start (comando, detached), stop/kill (por pid ou nome, "
        "opcionalmente a árvore). Use kill só quando stop não bastar.",
        {"action": {"type": "string", "enum": ["list", "start", "stop", "kill"]},
         "name": {"type": "string", "description": "Nome do processo (ex: 'notepad.exe')."},
         "pid": {"type": "integer"},
         "command": {"type": "string", "description": "Linha de comando para start."},
         "cwd": {"type": "string"},
         "tree": {"type": "boolean", "description": "Inclui filhos (default true)."},
         "force": {"type": "boolean"},
         "limit": {"type": "integer"}},
        ["action"],
    ),
    "pc_service": _s(
        "pc_service",
        "Serviços do Windows via sc.exe: status|start|stop|restart|pause|continue. start/stop precisam de Hermes elevado.",
        {"action": {"type": "string", "enum": ["status", "start", "stop", "restart", "pause", "continue"]},
         "name": {"type": "string", "description": "Nome do serviço (ex: 'Spooler')."}},
        ["action", "name"],
    ),
    "pc_power": _s(
        "pc_power",
        "Energia/sessão: lock|sleep|hibernate|restart|shutdown|logoff|cancel. shutdown/restart/logoff exigem "
        "confirm='yes' e usam delay_s (default 30s) — dá para abortar com action=cancel.",
        {"action": {"type": "string", "enum": ["lock", "sleep", "hibernate", "restart", "shutdown", "logoff", "cancel"]},
         "confirm": {"type": "string", "description": "'yes' para ações destrutivas."},
         "delay_s": {"type": "integer"}},
        ["action"],
    ),
    "pc_act": _s(
        "pc_act",
        "AÇÃO UNIVERSAL — tenta todas as camadas em ordem e diz qual resolveu. É a tool preferida para "
        "controlar qualquer app: clique/invoke/toggle, digitar, tecla, ler, mover/minimizar/fechar janela. "
        "Ordem: UIA pattern → LegacyIAccessible (MSAA) → mensagem Win32 (BM_CLICK/WM_SETTEXT/WM_CHAR) → "
        "clique/tecla em primeiro plano. `foreground_used=false` significa que a ação foi INVISÍVEL (sem "
        "mover o cursor nem roubar foco); `verified` diz se o estado mudou; `layers` mostra o que falhou e por quê. "
        "Use allow_foreground=false quando o usuário estiver trabalhando e você quiser só tentativas invisíveis.",
        {**_TGT,
         "action": {"type": "string", "enum": ["click", "invoke", "toggle", "type", "set", "key", "read",
                                               "minimize", "maximize", "restore", "close", "move", "resize"]},
         "name": {"type": "string", "description": "Nome acessível do elemento (ex: 'Salvar')."},
         "automation_id": {"type": "string"},
         "type": {"type": "string", "description": "ControlType (Button, Edit, Document, MenuItem...)."},
         "text": {"type": "string", "description": "Texto a digitar (type/set) ou combo de teclas (key)."},
         "value": {"type": "string", "description": "Alias de text para set."},
         "x": {"type": "integer"}, "y": {"type": "integer"},
         "coords": {"type": "string", "enum": ["screen", "client"]},
         "allow_foreground": {"type": "boolean", "description": "Permite o último degrau visível (default true)."},
         "contains": {"type": "boolean", "description": "Casa name por substring."},
         "index": {"type": "integer"},
         "direction": {"type": "string", "description": "Para window_state: maximize|minimize|normal."}},
        ["action"],
    ),
    "pc_com": _s(
        "pc_com",
        "Comanda o OBJETO COM do app — automação nativa, como se fosse o código dele. action=attach (pega a "
        "instância já aberta via GetActiveObject), create, members (lista propriedades/métodos para descobrir "
        "o que dá para fazer), get, set, call (método com args posicionais). ProgIDs úteis: Word.Application, "
        "Excel.Application, Outlook.Application, PowerPoint.Application, Shell.Application, WScript.Shell, "
        "Scripting.FileSystemObject, AcroExch.App. Caminhos com ponto funcionam em member (ex: 'Documents.Count').",
        {"action": {"type": "string", "enum": ["attach", "create", "members", "get", "set", "call"]},
         "progid": {"type": "string", "description": "ProgID registrado, ex: 'Excel.Application'."},
         "member": {"type": "string", "description": "Propriedade/método; aceita caminho com pontos."},
         "args": {"type": "array", "items": {}, "description": "Argumentos posicionais do método (call)."},
         "value": {"description": "Valor para set (string, número, booleano ou objeto JSON)."},
         "create": {"type": "boolean", "description": "Força instância nova em vez de anexar."},
         "limit": {"type": "integer", "description": "Itens em members (default 200)."}},
        ["action", "progid"],
    ),
    "pc_win32": _s(
        "pc_win32",
        "Mensagens Win32 cruas — o caminho interno do app. action=command envia WM_COMMAND ao parent com o id "
        "do controle (a mesma via de um clique real em menu/botão); msg/send postam/sendam qualquer mensagem "
        "(msg aceita 0x..); find localiza controle por id/texto/classe. É a camada que salva apps clássicos "
        "que ignoram UIA e PostMessage de mouse.",
        {**_TGT,
         "action": {"type": "string", "enum": ["command", "msg", "send", "find"]},
         "control_id": {"type": "integer", "description": "ID do controle (de pc_elements)."},
         "control_text": {"type": "string"},
         "control_class": {"type": "string"},
         "notify": {"type": "integer", "description": "Código de notificação do WM_COMMAND (0 = clique)."},
         "msg": {"type": "string", "description": "Número da mensagem, decimal ou 0x...."},
         "wparam": {"type": "integer"}, "lparam": {"type": "integer"},
         "post": {"type": "boolean", "description": "true=PostMessage (não bloqueia), false=SendMessage."}},
        ["action"],
    ),
    "pc_audit": _s(
        "pc_audit",
        "Últimas ações do pc-control (quem chamou o quê, quando, com que resultado).",
        {"limit": {"type": "integer"}},
    ),
    "pc_uia": _s(
        "pc_uia",
        "UI Automation — a camada de SEGUNDO PLANO que funciona em apps modernos (WinUI/XAML, Chromium, "
        "Electron, UWP) sem roubar foco nem mover o cursor. action=dump lista a árvore de acessibilidade "
        "(name, automation_id, type, rect, patterns); find/invoke/set_value/toggle/select/expand/focus/"
        "get_value agem num elemento por name/automation_id/type/index. Use get_value para LER texto de "
        "campo de outro processo (GetWindowText/WM_GETTEXT não atravessam processos). "
        "action=click tenta Invoke e, se o elemento não expõe pattern, clica no centro em primeiro plano.",
        {**_TGT,
         "action": {"type": "string", "enum": ["dump", "find", "invoke", "set_value", "get_value", "click",
                                               "toggle", "select", "expand", "collapse", "scroll_into_view",
                                               "focus"]},
         "name": {"type": "string", "description": "Nome acessível exato do elemento (ex: 'Salvar')."},
         "automation_id": {"type": "string", "description": "AutomationId, quando existir."},
         "type": {"type": "string", "description": "ControlType: Button, Edit, Text, CheckBox, MenuItem, TabItem, ComboBox, ListItem, Document, Hyperlink, Pane, Window..."},
         "index": {"type": "integer", "description": "N-ésimo elemento que casa (default 0)."},
         "value": {"type": "string", "description": "Texto para set_value."},
         "contains": {"type": "boolean", "description": "Casa por substring no name em vez de igualdade."},
         "limit": {"type": "integer", "description": "Itens no dump (default 200)."}},
        ["action"],
    ),
}

HANDLERS = {
    "pc_status": (h_status, "elements"),
    "pc_control": (h_control, "input"),
    "pc_windows": (h_windows, "window"),
    "pc_window": (h_window, "window"),
    "pc_elements": (h_elements, "elements"),
    "pc_uia": (h_uia, "background"),
    "pc_act": (h_act, "input"),
    "pc_com": (h_com, "com"),
    "pc_win32": (h_win32, "messages"),
    "pc_input": (h_input, "input"),
    "pc_settext": (h_settext, "background"),
    "pc_screenshot": (h_screenshot, "screenshot"),
    "pc_clipboard": (h_clipboard, "clipboard"),
    "pc_process": (h_process, "process"),
    "pc_service": (h_service, "service"),
    "pc_power": (h_power, "power"),
    "pc_audit": (h_audit, "elements"),
}


def make_handler(name: str):
    fn, cap = HANDLERS[name]

    def handler(args: dict[str, Any] | None = None, **_context) -> str:
        return _wrap(name, cap, fn, dict(args or {}))

    handler.__name__ = f"pc_handler_{name}"
    return handler
