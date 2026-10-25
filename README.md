# claude-pc-control

**Acesso total ao PC, ao navegador e às suas credenciais — em segundo plano — para o Claude Code.**

Plugin do Claude Code que sobe três servidores MCP (42 tools) com o mesmo motor
que já roda no Hermes e no Antigravity: UI Automation, mensagens Win32, `SendInput`,
COM, janelas/processos/serviços/energia, Chrome e Edge por CDP, cofre de senhas
com DPAPI, pastas de rede e elevação sem UAC.

```text
        pc_act  (escada automática, segundo plano primeiro)
           |
   UIA pattern → UIA legacy → UIA select → Win32 message → foreground
   (sem foco)    (sem foco)    (sem foco)   (sem foco)      (move o cursor)
           |
     resolved_by + foreground_used + verified   ← sem adivinhação
```

Cursor parado. Teclado do usuário preservado. Primeiro plano **só quando o app
não aceita nada menos** — e a resposta diz quando isso aconteceu.

## Por que este plugin existe

Todo agente de PC no Windows escolhe entre duas coisas ruins: "clicar por
coordenada" (quebra com DPI, tema, janela em outra posição) ou "trazer tudo para
a frente" (o usuário perde o foco no meio do que estava digitando). Aqui a regra
é outra: tentar as camadas invisíveis primeiro, na ordem mais barata, e só então
escalar — devolvendo sempre qual camada resolveu.

## Instalação

```powershell
git clone https://github.com/renatopenner/claude-pc-control.git C:\claude-pc-control
cd C:\claude-pc-control
.\setup-claude.ps1
```

O script confere Python 3, registra este repositório como marketplace, instala o
plugin (escopo `user`), libera as tools no `~/.claude/settings.json` e zera o
`ForegroundLockTimeout` (para o fallback em primeiro plano não ficar preso).

Manualmente, se preferir:

```powershell
claude plugin marketplace add C:\claude-pc-control
claude plugin install pc-control@pc-control-marketplace --scope user
python scripts\merge_settings.py
```

Depois abra o Claude Code e rode `/plugin` (o `pc-control` deve estar habilitado),
`/pc-status` e `/vault`.

Requisitos: Windows 10/11, Python 3.10+ no PATH, Chrome e/ou Edge instalados.
Sem `pip install`, sem build: os três servidores são stdlib puro.

## Comandos e skills

| Comando | O que faz |
|---|---|
| `/pc-status` | sessão, killswitch, monitores, últimas ações, abas do navegador |
| `/pc-shot [janela]` | print de janela escondida (`PrintWindow`) ou da tela |
| `/pc-off` / `/pc-on` | killswitch: bloqueia/religa todas as tools de PC |
| `/browser` | estado do Chrome/Edge e abas abertas |
| `/vault [\\\\servidor\\pasta]` | cofre, credenciais do Windows, conexões de rede |

Três skills entram no contexto sozinhas quando o assunto aparece:
`pc-control` (escada e pitfalls de UIA/Win32), `browser-control` (CDP, foco,
iframes, login) e `vault-network` (cofre, pastas de rede, elevação).

## As 42 tools

### `pc` — 17 tools de Windows

| Tool | O que faz |
|---|---|
| `pc_status` | sessão interativa/bloqueada, foco, monitores, DPI, elevação, killswitch |
| `pc_control` | killswitch (`enable`/`disable`), `grant`/`revoke` por capacidade, `audit` |
| `pc_windows` | janelas: hwnd, título, classe, PID, retângulo, foco |
| `pc_window` | `focus` `minimize` `maximize` `restore` `close` `topmost` `move` `resize` |
| `pc_elements` | árvore de controles Win32 (rápida): id, texto, editável, clicável |
| `pc_uia` | árvore de acessibilidade + `invoke` `set_value` `get_value` `toggle` `select` `expand` |
| `pc_act` | **a escada**: `click` `toggle` `type` `set` `key` `read` `minimize` … |
| `pc_win32` | mensagens cruas: `command` (WM_COMMAND por control id), `click`, `char`, `settext` |
| `pc_com` | automação COM por ProgID (Office, `Shell.Application`, WMI) |
| `pc_input` | `click` `type` `key` `hotkey` `drag` `scroll` em `mode=auto\|background\|foreground` |
| `pc_settext` | `WM_SETTEXT` em controle clássico |
| `pc_screenshot` | tela, monitor ou **janela escondida** (PrintWindow) → PNG |
| `pc_clipboard` | ler/gravar clipboard |
| `pc_process` | `list` `start` `stop` `kill` |
| `pc_service` | `status` `start` `stop` `restart` (elevação necessária para mudar) |
| `pc_power` | `lock` `sleep` `hibernate` `restart` `shutdown` `cancel` |
| `pc_audit` | últimas ações com resultado |

### `browser` — 15 tools de Chrome/Edge (CDP)

`browser_status` `browser_launch` `browser_tabs` `browser_open` `browser_navigate`
`browser_read` `browser_eval` `browser_click` `browser_type` `browser_key`
`browser_scroll` `browser_wait` `browser_screenshot` `browser_close` `browser_activate`

- Tudo por Chrome DevTools Protocol — **nada move o cursor nem rouba foco**.
- `browser_launch` abre a janela **fora da tela, minimizada e sem ativar**, e
  devolve o foco à janela anterior (`focus_restored`). `headless=true` para nem
  existir janela.
- Porta de debug `0`: o navegador escolhe uma porta livre e grava em
  `DevToolsActivePort` — nunca briga com o seu Chrome da 9222.
- `profile=isolated` (perfil dedicado, faça login uma vez) ou `profile=user`
  (seu perfil real, exige o navegador fechado).

### `vault` — 10 tools de credencial, rede e elevação

`vault_status` `vault_list` `vault_save` `vault_delete` `vault_fill_login`
`net_mount` `net_unmount` `net_list` `net_check` `elevate_run`

## Cofre de senhas (model-blind, DPAPI)

Mesmo desenho do cofre do Hermes Agent: o modelo vê **handles e metadados**, o
valor secreto é resolvido dentro do processo e nunca entra em resultado de tool,
log ou transcrição.

```text
lo-1a2b3c4d  login    Sistema Banpará     username=banpara\rsoares, origin=https://...
ne-9f8e7d6c  network  Pasta GESER         target=\\servidor\gesser, username=banpara\rsoares
```

Diferença em relação ao cofre do Hermes: não há arquivo de chave. Cada campo é
cifrado com **DPAPI** (`CryptProtectData`, escopo do usuário Windows) — copiar o
`vault.json` para outra máquina ou outro usuário do Windows não abre nada. O
arquivo ainda leva ACL restrita ao usuário atual.

**A senha nunca é digitada no chat.** Cadastrar:

```powershell
python vault\vault_cli.py add                    # login (site + usuário + senha via getpass)
python vault\vault_cli.py add-net                # pasta de rede (\\servidor\pasta)
python vault\vault_cli.py import --target "Domain:target=TERMSRV/servidor"
python vault\vault_cli.py list
```

Ou, de dentro do Claude Code, `vault_save source=dialog` — abre uma janela nativa
onde **você** digita; o plugin devolve só o handle. `vault_save` com `secret=`
é recusado por projeto: segredo em argumento de tool vira histórico.

Uso: `vault_fill_login entry="Sistema Banpará"` preenche a aba atual (a origem
tem que ser idêntica à cadastrada) e devolve apenas `filled`/`submitted`.

## Pastas de rede ("entrar no diretório de rede")

```text
net_check  target="\\servidor\pasta"      # ping, DNS e listagem antes de tentar
net_mount  entry="Pasta GESER"            # credencial do cofre (kind=network)
net_mount  target="\\servidor\pasta" drive="Z:"
net_unmount target="\\servidor\pasta"
net_list
```

A senha vai em memória para `WNetAddConnection2W` — não aparece em `net use`, em
argv, no histórico do PowerShell nem no WMI. `net_mount` só devolve
`verified=true` depois de **listar** o conteúdo. Os erros vêm traduzidos com
dica (1326 logon, 1219 conflito de credenciais, 53/67 caminho, 86 senha vencida).

## Administrador sem digitar senha

```text
elevate_run command="net share"
elevate_run command="sc query lanmanserver"
```

Se o processo já está elevado, roda direto. Se não (o normal: o UAC filtra o
token do administrador), registra uma **tarefa agendada `RunLevel=Highest`**
(`InteractiveToken`) do próprio usuário, executa e apaga — o comando nasce com
integridade Alta (`S-1-16-12288`), **sem prompt de UAC e sem senha**. Verificado
neste PC: `_is_elevated()==False` no agente, tarefa rodando com `S-1-16-12288`.

Limites honestos: a conta precisa ser administradora; política de domínio pode
proibir tarefa `Highest`; apps que exigem desktop interativo elevado podem não
aparecer.

## Limites reais (não são bugs)

1. **Sessão bloqueada (Win+L)**: o secure desktop não entrega input. `pc_status.session.locked=true`.
2. **Session 0** (serviço/SSH): sem desktop interativo, mesma história.
3. **UAC prompt**: nenhum agente clica nele — por isso a elevação por tarefa.
4. **Anti-cheat/DirectInput**: ignoram input sintético, até em primeiro plano.
5. **Texto de outro processo**: `GetWindowText` não cruza processos — use `pc_uia get_value`.
6. **Chromium/Electron**: `PostMessage` costuma ser ignorado; UIA e foreground funcionam.
7. **Páginas internas do navegador** (`chrome://`, `edge://`, PDF viewer) não expõem DOM.
8. **iframe cross-origin**: só com input real; `browser_eval` não atravessa.

## Segurança

- Sem elevação permanente, sem driver, sem injeção em outros processos, sem
  socket de rede aberto. Só API documentada: UI Automation, mensagens Win32,
  `SendInput`, `PrintWindow`, COM, CDP em `127.0.0.1`.
- Killswitch em processo: `pc_control action=disable` (ou `/pc-off`) bloqueia
  tudo na hora; toda ação é auditada em `pc_audit`.
- O agente **não** digita 2FA, código de verificação ou cartão — nem com cofre.
- Servidor MCP só escuta `127.0.0.1`; o cofre cifra por usuário do Windows.

## Estrutura

```text
.claude-plugin/plugin.json        manifesto do plugin
.claude-plugin/marketplace.json   este repo como marketplace
.mcp.json                         os três servidores MCP (pc, browser, vault)
commands/                         /pc-status /pc-shot /pc-off /pc-on /browser /vault
skills/                           pc-control, browser-control, vault-network
engine/                           motor do Windows (sincronizado de hermes-pc-control)
browser/                          cdp.py (WebSocket stdlib) + browsertools.py
vault/                            vault_core.py (DPAPI), vaulttools.py, elevate.ps1, CLI
scripts/merge_settings.py         permissões no ~/.claude/settings.json
tests/                            selftest_mcp.py, selftest_browser.py, selftest_vault.py
setup-claude.ps1                  instalador
sync-engine.ps1                   atualiza engine/ a partir do hermes-pc-control
```

## Testes

```powershell
python tests\selftest_mcp.py                 # os 3 servidores, handshake + tools/list + 1 chamada
python tests\selftest_browser.py --browser edge --launch
python tests\selftest_vault.py               # DPAPI, elevação, share temporário, vazamento
```

Resultado neste PC (Windows 11, Chrome 155, Edge):

- `selftest_mcp`: 3/3 servidores, 17 + 15 + 10 = **42 tools**;
- `selftest_browser` (Edge e Chrome): **13/13**, incluindo "foco da janela não mudou";
- `selftest_vault`: **14/14**, incluindo integridade Alta via tarefa e
  "nenhuma resposta contém o segredo".

## Motor compartilhado

`engine/` é uma cópia versionada do motor de
[hermes-pc-control](https://github.com/renatopenner/hermes-pc-control) (mesmo
código que roda no Hermes e no Antigravity). Para atualizar:

```powershell
.\sync-engine.ps1                 # copia do plugin do Hermes
.\sync-engine.ps1 -Check          # só mostra se divergiu
```

## Licença

MIT — ver [LICENSE](LICENSE).
