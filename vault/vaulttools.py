#!/usr/bin/env python3
"""Tools do cofre: credenciais model-blind, pastas de rede e elevação sem UAC.

Regra dura deste módulo: **nenhum segredo sai em resultado de tool**. O agente vê
handles, metadados e o efeito da ação (`ok`, `filled`, `exit_code`). O valor é
resolvido dentro do processo, usado na chamada nativa e descartado.

Por que assim: qualquer segredo que entre por argumento de tool vai para a
transcrição da conversa e para o histórico do cliente MCP. O cofre existe para
que isso nunca seja necessário — a senha é digitada uma vez pelo Renato (janela
segura ou `vault_cli.py`) e depois só o handle circula.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "browser"))

import vault_core as vc  # noqa: E402

ERRORS = {
    0: ("ok", ""),
    5: ("sem permissão", "a conta autenticou mas não tem direito na pasta"),
    53: ("caminho de rede não encontrado", "verifique o nome do servidor (DNS) e se o serviço está no ar"),
    67: ("nome do compartilhamento inválido", "confira o nome da pasta compartilhada"),
    85: ("já existe conexão com esse caminho", "use net_list para ver e net_unmount antes de repetir"),
    86: ("senha incorreta", "a credencial do cofre está errada/vencida"),
    1219: ("conflito de credenciais", "já há conexão nesse servidor com outro usuário; rode net_unmount e repita"),
    1326: ("falha de logon", "usuário ou senha recusados (ou conta bloqueada)"),
    1327: ("restrição de conta", "conta sem direito de logon neste tipo de acesso"),
    1909: ("conta bloqueada", "conta bloqueada por tentativas — espere ou peça desbloqueio"),
    2202: ("nome de usuário inválido", "use DOMINIO\\usuario"),
}


class ToolError(RuntimeError):
    pass


def ok(**kw) -> str:
    payload = {"ok": True}
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False, default=str)


def fail(msg: str, **kw) -> str:
    payload = {"ok": False, "error": str(msg)}
    payload.update(kw)
    return json.dumps(payload, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# pastas de rede — WNetAddConnection2W (senha em memória, nunca em argv)
# --------------------------------------------------------------------------- #
class _NETRESOURCEW(ctypes.Structure):
    _fields_ = [
        ("dwScope", wt.DWORD),
        ("dwType", wt.DWORD),
        ("dwDisplayType", wt.DWORD),
        ("dwUsage", wt.DWORD),
        ("lpLocalName", wt.LPWSTR),
        ("lpRemoteName", wt.LPWSTR),
        ("lpComment", wt.LPWSTR),
        ("lpProvider", wt.LPWSTR),
    ]


RESOURCETYPE_DISK = 0x1
CONNECT_TEMPORARY = 0x4


def _net_connect(remote: str, user: str | None, password: str | None, local: str | None) -> int:
    mpr = ctypes.WinDLL("mpr.dll")
    nr = _NETRESOURCEW()
    nr.dwType = RESOURCETYPE_DISK
    nr.lpRemoteName = remote
    nr.lpLocalName = local or None
    mpr.WNetAddConnection2W.argtypes = [
        ctypes.POINTER(_NETRESOURCEW),
        wt.LPCWSTR,
        wt.LPCWSTR,
        wt.DWORD,
    ]
    mpr.WNetAddConnection2W.restype = wt.DWORD
    return int(mpr.WNetAddConnection2W(ctypes.byref(nr), password, user, CONNECT_TEMPORARY))


def _net_disconnect(remote: str | None, local: str | None) -> int:
    mpr = ctypes.WinDLL("mpr.dll")
    mpr.WNetCancelConnection2W.argtypes = [wt.LPCWSTR, wt.DWORD, wt.BOOL]
    mpr.WNetCancelConnection2W.restype = wt.DWORD
    return int(mpr.WNetCancelConnection2W(local or remote, 0, True))


def _decode_console(data: bytes) -> str:
    """Saída de cmd.exe vem em OEM (cp850 no Windows pt-BR) ou UTF-8 se o
    wrapper fez chcp 65001. Tenta os dois antes de desistir."""
    for enc in ("utf-8", "cp850", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", "replace")


def _running(process: str) -> int:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {process}", "/NH"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("latin-1", "replace")
    return sum(1 for line in out.splitlines() if process.lower() in line.lower())


def _parse_net_use() -> list[dict]:
    out = subprocess.run(
        ["net", "use"],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("latin-1", "replace")
    rows: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("-") or ":" not in line:
            continue
        if line.lower().startswith(("status", "novas", "new", "comando", "the command")):
            continue
        parts = line.split()
        row = {"raw": line}
        if "\\\\" in line:
            idx = line.index("\\\\")
            remote = line[idx:].split()[0]
            row["remote"] = remote
            before = line[:idx].split()
            if before:
                token = before[0]
                if len(token) == 2 and token[1] == ":":
                    row["local"] = token
                elif token.startswith("\\\\"):
                    row["remote"] = token
        rows.append(row)
    return rows


def tool_net_list(args: dict) -> str:
    mapped = _parse_net_use()
    creds = [c for c in vc.cred_list() if c.get("target", "").startswith(("Domain:", "LegacyGeneric:", "TERMSRV/", "\\\\"))]
    return ok(mapped=mapped, stored_credentials=creds[:20], count=len(mapped))


def tool_net_mount(args: dict) -> str:
    ref = args.get("entry") or args.get("label")
    target = (args.get("target") or "").strip()
    user = (args.get("username") or "").strip()
    local = (args.get("drive") or "").strip().upper() or None
    if local and len(local) == 2 and local[1] == ":":
        pass
    elif local:
        raise ToolError("drive deve ser uma letra, ex.: Z:")

    secret = None
    handle = None
    if ref:
        item, secret = vc.resolve(ref, kind="network")
        handle = item["handle"]
        meta = item.get("meta") or {}
        target = target or (meta.get("target") or "").strip()
        user = user or (meta.get("username") or "").strip()
    if not target:
        raise ToolError("informe target=\\\\servidor\\pasta (ou entry=<handle do cofre>)")
    if not target.startswith("\\\\"):
        raise ToolError(f"target deve ser UNC (\\\\servidor\\pasta): {target!r}")

    code = _net_connect(target, user or None, secret, local)
    label, hint = ERRORS.get(code, (f"erro {code}", "consulte a mensagem do Windows para esse código"))
    if code not in (0, 85):
        return fail(
            f"não conectou em {target}: {label}",
            winerror=code,
            hint=hint,
            entry=handle,
            tried_user=(user or os.environ.get("USERNAME")),
        )

    # confirma de verdade: lista o conteúdo
    sample: list[str] = []
    verified = False
    try:
        with os.scandir(target) as it:
            for i, entry in enumerate(it):
                if i >= 8:
                    break
                sample.append(entry.name)
        verified = True
    except Exception as exc:  # noqa: BLE001
        verified = False
        hint = f"conectou mas não listou: {type(exc).__name__} {exc}"

    return ok(
        mounted=True,
        target=target,
        drive=local,
        entry=handle,
        user=user or os.environ.get("USERNAME"),
        verified=verified,
        sample=sample,
        foreground_used=False,
    )


def tool_net_unmount(args: dict) -> str:
    target = (args.get("target") or "").strip()
    drive = (args.get("drive") or "").strip().upper()
    if not target and not drive:
        ref = args.get("entry")
        if ref:
            item = vc.find(ref, kind="network")
            if not item:
                raise ToolError(f"entrada não encontrada: {ref!r}")
            target = (item.get("meta") or {}).get("target") or ""
    if not target and not drive:
        raise ToolError("informe target, drive ou entry")
    code = _net_disconnect(target or None, drive or None)
    if code not in (0, 2250):
        label, hint = ERRORS.get(code, (f"erro {code}", ""))
        return fail(f"não desconectou: {label}", winerror=code, hint=hint)
    return ok(unmounted=True, target=target or drive)


def tool_net_check(args: dict) -> str:
    target = (args.get("target") or "").strip()
    if not target.startswith("\\\\"):
        raise ToolError("informe target=\\\\servidor\\pasta")
    host = target.split("\\")[2] if len(target.split("\\")) > 2 else ""
    ping = subprocess.run(
        ["ping", "-n", "1", "-w", "1500", host],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    dns = subprocess.run(
        ["nslookup", host],
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode("latin-1", "replace")
    sample = None
    err = None
    try:
        with os.scandir(target) as it:
            sample = [e.name for _, e in zip(range(5), it)]
    except Exception as exc:  # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
    return ok(
        target=target,
        host=host,
        ping_ok=ping.returncode == 0,
        dns_ok="Address" in dns or "Endereço" in dns,
        accessible=err is None,
        sample=sample,
        error=err,
    )


# --------------------------------------------------------------------------- #
# elevação — token alto sem digitar senha
# --------------------------------------------------------------------------- #
def _is_elevated() -> bool:
    try:
        advapi = ctypes.windll.advapi32
        kernel = ctypes.windll.kernel32
        token = wt.HANDLE()
        if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            return False
        try:
            value = wt.DWORD()
            size = wt.DWORD()
            advapi.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size))
            return bool(value.value)
        finally:
            kernel.CloseHandle(token)
    except Exception:
        return False


def tool_elevate(args: dict) -> str:
    """Roda um comando com token elevado.

    Já elevado → executa direto. Não elevado → tarefa agendada com RunLevel
    Highest (InteractiveToken), que **não pede senha** quando a conta é
    administradora. Em último caso devolve o comando `runas` para o usuário
    aprovar o UAC na tela.
    """
    command = (args.get("command") or "").strip()
    if not command:
        raise ToolError("informe command (ex.: 'net share' ou 'powershell -c Get-SmbShare')")
    timeout = float(args.get("timeout_s") or 120)
    force_task = (args.get("mode") or "").lower() == "task"
    elevated = _is_elevated()

    if elevated and not force_task:
        t0 = time.time()
        proc = subprocess.run(
            ["cmd", "/c", command],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=timeout,
        )
        out = _decode_console(proc.stdout + proc.stderr)
        return ok(
            ran_as="current-token",
            elevated=True,
            exit_code=proc.returncode,
            output=out[-8000:],
            seconds=round(time.time() - t0, 2),
        )

    script = HERE / "elevate.ps1"
    if not script.is_file():
        raise ToolError(f"elevate.ps1 não encontrado em {HERE}")
    out_file = Path(tempfile.gettempdir()) / f"pcc-elev-{os.getpid()}-{int(time.time())}.log"
    proc = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Command",
            command,
            "-OutFile",
            str(out_file),
            "-TimeoutSec",
            str(int(timeout)),
        ],
        capture_output=True,
        timeout=timeout + 30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    raw = (proc.stdout or b"").decode("latin-1", "replace").strip()
    payload = {}
    for line in reversed(raw.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    output = ""
    try:
        output = _decode_console(out_file.read_bytes())[-8000:]
    except Exception:
        pass
    finally:
        try:
            out_file.unlink()
        except Exception:
            pass

    if not payload:
        return fail(
            "a elevação por tarefa agendada falhou",
            stderr=(proc.stderr or b"").decode("latin-1", "replace")[-1500:],
            hint="a conta atual não pode registrar tarefa com RunLevel Highest; peça ao usuário aprovar o UAC uma vez",
        )
    return ok(
        ran_as="scheduled-task-high",
        elevated=True,
        exit_code=payload.get("exit_code"),
        output=output,
        task=payload.get("task"),
        seconds=payload.get("seconds"),
    )


# --------------------------------------------------------------------------- #
# preencher login no navegador (segredo nunca volta)
# --------------------------------------------------------------------------- #
def tool_fill_login(args: dict) -> str:
    import cdp  # importado aqui: o servidor do cofre não exige navegador aberto

    ref = args.get("entry") or args.get("label")
    if not ref:
        raise ToolError("informe entry=<handle ou label do cofre>")
    item, password = vc.resolve(ref, kind="login")
    meta = item.get("meta") or {}
    want_origin = (meta.get("origin") or "").strip().lower()
    username = (meta.get("username") or "").strip()
    browser = (args.get("browser") or "chrome").lower()
    if browser not in ("chrome", "edge"):
        raise ToolError("browser deve ser chrome ou edge")

    port = None
    for cand in (9222, 9223):
        if cdp.socket_probe(cand):
            port = cand
            break
    if port is None:
        raise ToolError("nenhum navegador com porta de debug aberta — use browser_launch primeiro")

    pages = cdp.pages(port)
    if not pages:
        raise ToolError("navegador sem abas")
    target = pages[0]
    with cdp.connect(port, target) as page:
        origin = (page.eval("location.origin", await_promise=False) or "").lower()
        if want_origin and origin != want_origin:
            return fail(
                "origem da aba não bate com a do cofre — não preenchi nada",
                tab_origin=origin,
                entry_origin=want_origin,
            )
        js = r"""
(function (user) {
  const pwd = document.querySelector('input[type=password]');
  if (!pwd) { return {found: false}; }
  const form = pwd.form || document;
  let userField = form.querySelector('input[type=email],input[name*=user i],input[id*=user i],input[name*=login i],input[autocomplete=username]');
  const setVal = (el, value) => {
    el.focus();
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
    if (desc && desc.set) { desc.set.call(el, value); } else { el.value = value; }
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  let filled = [];
  if (userField && user) { setVal(userField, user); filled.push('username'); }
  setVal(pwd, %(pwd)s);
  filled.push('password');
  return {found: true, filled: filled,
          user_field: !!userField,
          form_action: (pwd.form && pwd.form.action || '').slice(0, 200)};
})(%(user)s)
""" % {"pwd": json.dumps(password), "user": json.dumps(username)}
        res = page.eval(js)
        if not isinstance(res, dict) or not res.get("found"):
            return fail("a aba não tem campo de senha visível", url=page.url())
        submitted = False
        if args.get("submit"):
            page.eval(
                "(function(){const p=document.querySelector('input[type=password]');"
                "if(!p) return false; const f=p.form; if(f && f.requestSubmit){f.requestSubmit();return true;}"
                "p.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return true;})()"
            )
            submitted = True
        return ok(
            filled=res.get("filled"),
            submitted=submitted,
            entry=item["handle"],
            origin=origin,
            foreground_used=False,
            note="senha usada em memória; nada do valor foi devolvido",
        )


# --------------------------------------------------------------------------- #
# cofre — entrada de segredo nunca por argumento de tool
# --------------------------------------------------------------------------- #
def tool_vault_status(args: dict) -> str:
    st = vc.status()
    st["credential_manager"] = len(vc.cred_list())
    st["foreground_used"] = False
    return ok(**st)


def tool_vault_list(args: dict) -> str:
    kind = (args.get("kind") or "").strip().lower() or None
    if kind and kind not in vc.KINDS:
        raise ToolError(f"kind deve ser um de {', '.join(vc.KINDS)}")
    items = vc.list_items(kind)
    return ok(count=len(items), items=items)


def tool_vault_save(args: dict) -> str:
    """Guarda um segredo sem passar pelo chat.

    ``source=credential_manager`` importa do Gerenciador de Credenciais do
    Windows; ``source=dialog`` abre uma janela local onde o próprio usuário digita.
    Segredo por argumento é recusado de propósito.
    """
    if args.get("secret") or args.get("password"):
        return fail(
            "segredo por argumento é recusado: ele iria para a transcrição",
            hint="use source=dialog (janela segura no seu desktop) ou rode: python vault/vault_cli.py add",
        )
    kind = (args.get("kind") or "login").lower()
    label = (args.get("label") or "").strip()
    source = (args.get("source") or "dialog").lower()

    if source == "credential_manager":
        target = (args.get("credential_target") or "").strip()
        if not target:
            raise ToolError("informe credential_target (use source='list_credentials' para ver os destinos)")
        cred = vc.cred_read(target)
        if not cred:
            return fail(f"credencial não encontrada no Windows: {target}", hint="cmdkey /list mostra os destinos disponíveis")
        meta = {}
        if kind == "login":
            meta["origin"] = args.get("origin") or ""
            meta["username"] = cred.get("username") or ""
            if not meta["origin"]:
                return fail("faltou origin (site exato, ex.: https://sistema.banpara.b.br)")
        elif kind == "network":
            meta["target"] = args.get("target") or ""
            meta["username"] = cred.get("username") or ""
            if not meta["target"]:
                return fail("faltou target (\\\\servidor\\pasta)")
        item = vc.add_item(kind, label or os.path.basename(target), cred["secret"], **meta)
        return ok(saved=True, entry=item, source="credential_manager")

    if source == "dialog":
        script = HERE / "vault_dialog.py"
        proc = subprocess.run(
            [sys.executable, str(script), kind, label or "", args.get("username") or "", args.get("origin") or args.get("target") or ""],
            capture_output=True,
            timeout=180,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace").strip()
        try:
            payload = json.loads(out.splitlines()[-1]) if out else {}
        except ValueError:
            payload = {}
        if not payload.get("ok"):
            return fail(
                "nada foi salvo",
                detail=payload.get("error") or (proc.stderr or b"").decode("latin-1", "replace")[-400:],
                hint="a janela foi cancelada ou não abriu; alternativa: python vault/vault_cli.py add",
            )
        return ok(saved=True, entry=payload.get("entry"), source="dialog")

    if source == "list_credentials":
        return ok(credentials=vc.cred_list())

    raise ToolError("source deve ser dialog, credential_manager ou list_credentials")


def tool_vault_delete(args: dict) -> str:
    ref = args.get("entry") or args.get("label")
    if not ref:
        raise ToolError("informe entry")
    return ok(deleted=vc.remove(ref))


SCHEMAS: dict[str, dict] = {
    "vault_status": {
        "description": "Estado do cofre: arquivo, tipo de cifra (DPAPI), quantidade por tipo e credenciais do Windows disponíveis. Nunca mostra segredos.",
        "parameters": {"type": "object", "properties": {}},
    },
    "vault_list": {
        "description": "Lista entradas do cofre: handle, tipo (login/network/generic), label e metadados (usuário, origem, servidor). Nunca devolve o segredo.",
        "parameters": {
            "type": "object",
            "properties": {"kind": {"type": "string", "enum": ["login", "network", "generic"]}},
        },
    },
    "vault_save": {
        "description": "Guarda uma credencial sem que o segredo passe pelo chat: source=dialog abre janela nativa para o usuário digitar; source=credential_manager importa do Gerenciador de Credenciais do Windows; source=list_credentials lista os destinos importáveis. Segredo por argumento é recusado.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["login", "network", "generic"], "default": "login"},
                "label": {"type": "string"},
                "source": {"type": "string", "enum": ["dialog", "credential_manager", "list_credentials"], "default": "dialog"},
                "credential_target": {"type": "string"},
                "username": {"type": "string"},
                "origin": {"type": "string", "description": "login: origem exata, ex. https://site"},
                "target": {"type": "string", "description": "network: \\\\servidor\\pasta"},
            },
        },
    },
    "vault_delete": {
        "description": "Remove uma entrada do cofre pelo handle ou label.",
        "parameters": {
            "type": "object",
            "properties": {"entry": {"type": "string"}, "label": {"type": "string"}},
        },
    },
    "vault_fill_login": {
        "description": "Preenche usuário e senha na aba atual do Chrome/Edge usando uma entrada do cofre; exige origem idêntica à cadastrada. Devolve só quais campos foram preenchidos. submit=true envia o formulário.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry": {"type": "string"},
                "browser": {"type": "string", "enum": ["chrome", "edge"], "default": "chrome"},
                "submit": {"type": "boolean", "default": False},
            },
            "required": ["entry"],
        },
    },
    "net_mount": {
        "description": "Monta pasta de rede (\\\\servidor\\pasta) usando credencial do cofre ou do usuário atual, com a senha em memória (WNetAddConnection2W, nunca em linha de comando). Verifica listando o conteúdo. drive opcional para mapear letra.",
        "parameters": {
            "type": "object",
            "properties": {
                "entry": {"type": "string", "description": "handle/label de uma entrada network do cofre"},
                "target": {"type": "string"},
                "username": {"type": "string"},
                "drive": {"type": "string"},
            },
        },
    },
    "net_unmount": {
        "description": "Desconecta pasta de rede por target, drive ou entrada do cofre.",
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string"}, "drive": {"type": "string"}, "entry": {"type": "string"}},
        },
    },
    "net_list": {
        "description": "Lista conexões de rede ativas e credenciais armazenadas no Windows (sem valores).",
        "parameters": {"type": "object", "properties": {}},
    },
    "net_check": {
        "description": "Diagnostica acesso a uma pasta de rede: ping do host, DNS e se o conteúdo lista; devolve amostra de nomes.",
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    },
    "elevate_run": {
        "description": "Roda um comando com privilégio de administrador SEM digitar senha: execução direta se o processo já é elevado, senão via tarefa agendada RunLevel Highest. mode=task força o caminho da tarefa (teste).",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout_s": {"type": "number", "default": 120},
                "mode": {"type": "string", "enum": ["auto", "task"], "default": "auto"},
            },
            "required": ["command"],
        },
    },
}

HANDLERS = {
    "vault_status": tool_vault_status,
    "vault_list": tool_vault_list,
    "vault_save": tool_vault_save,
    "vault_delete": tool_vault_delete,
    "vault_fill_login": tool_fill_login,
    "net_mount": tool_net_mount,
    "net_unmount": tool_net_unmount,
    "net_list": tool_net_list,
    "net_check": tool_net_check,
    "elevate_run": tool_elevate,
}


def make_handler(name: str):
    fn = HANDLERS[name]

    def handler(args: dict) -> str:
        try:
            return fn(args or {})
        except vc.VaultError as exc:
            return fail(str(exc))
        except ToolError as exc:
            return fail(str(exc))
        except Exception as exc:  # noqa: BLE001
            return fail(f"{type(exc).__name__}: {exc}")

    return handler
