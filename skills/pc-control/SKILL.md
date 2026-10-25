---
name: pc-control
description: Use quando for controlar o Windows inteiro (janelas, campos, botões, processos, serviços, energia, clipboard, print) — em segundo plano por padrão, primeiro plano só quando o app exigir de fato.
---

# pc-control — controle do PC (segundo plano → primeiro plano)

As tools vêm do servidor MCP `pc` deste plugin. No Claude Code o nome completo é
`mcp__plugin_pc-control_pc__<tool>` (ex.: `mcp__plugin_pc-control_pc__pc_act`);
aqui elas aparecem pelo nome curto (`pc_act`). São 17:

`pc_status` `pc_control` `pc_windows` `pc_window` `pc_elements` `pc_uia`
`pc_act` `pc_com` `pc_win32` `pc_input` `pc_settext` `pc_screenshot`
`pc_clipboard` `pc_process` `pc_service` `pc_power` `pc_audit`

A regra é **segundo plano primeiro**. Toda resposta traz `resolved_by` /
`foreground_used` / `verified` (ou `mode`/`method` nas tools específicas): leia
antes de escalar e nunca repita uma ação já confirmada.

## Ladder de decisão

**Comece por `pc_act`.** Ele percorre a escada sozinho e diz onde parou — é uma
chamada em vez de quatro tentativas:

```
uia-pattern -> uia-legacy -> uia-select -> uia-child-self -> win32-message -> foreground
```

`pc_act action=click|toggle|type|set|key|read|minimize|maximize|restore|close|move|resize`
com `hwnd` + (`name` | `automation_id` | `type`) + `index`/`contains`.

Leia a resposta: `resolved_by` diz a camada que funcionou, `foreground_used=false`
significa que nada apareceu na tela nem mexeu no cursor, `verified` diz se o
estado mudou de fato. `verified=false` com `ok=true` = o app aceitou e ignorou;
confirme com `pc_screenshot target=window` antes de declarar sucesso.

Só desça manualmente quando precisar de controle fino:

1. `pc_uia action=dump hwnd=<janela>` — árvore acessível (name, automation_id,
   type, rect, patterns). Prefira elementos com pattern `invoke`/`value`/`toggle`.
2. `pc_uia action=invoke|set_value|toggle|select|expand` com `name` ou `automation_id`.
   Background de verdade: não move o cursor nem rouba foco.
3. `pc_win32 action=command control_id=<id>` quando o app é Win32 clássico e você
   já sabe o id (`pc_elements` mostra) — caminho curto para diálogos `#32770`.
4. Árvore vazia (app sem acessibilidade, canvas, jogo): `pc_input mode=foreground`
   — input real, move o cursor e rouba foco. Só quando o usuário não estiver no
   meio de algo.
5. Para app com interface de automação (Office, `Shell.Application`, WMI):
   `pc_com` — `attach`/`create` pelo ProgID, `members`, `invoke`, `get_value`,
   `set_value`, `default`. Único caminho que mexe no modelo do documento.

Ler conteúdo de campo de outro processo: `pc_uia action=get_value`
(ValuePattern/TextPattern) — `GetWindowText` não atravessa processos.
Ver sem trazer para a frente: `pc_screenshot target=window` (PrintWindow).

## Quando ir para primeiro plano (e como avisar)

Só quando: `pc_act` devolveu `resolved_by=foreground-*`, o app é DirectInput/
canvas, ou o usuário pediu explicitamente para ver na tela. Antes de roubar foco,
diga em uma linha o que vai acontecer ("vou trazer o Bloco de Notas para a
frente por ~5 s"). Depois de terminar, devolva o estado (minimize de volta, ou
`pc_window action=restore` da janela anterior) — o teclado é do usuário.

## Receitas

```
# abrir e preencher Notepad 11 sem roubar foco
pc_process action=start command=notepad.exe
pc_windows process=notepad            -> hwnd
pc_uia action=dump hwnd=<hwnd> limit=60
pc_uia action=set_value hwnd=<hwnd> type=Document value="texto"
pc_uia action=get_value hwnd=<hwnd> type=Document     # confirma

# botão clássico em segundo plano
pc_uia action=click hwnd=<hwnd> name="Selecionar"     # method=bm-click se não houver Invoke

# janela oculta / atrás de tudo
pc_screenshot target=window hwnd=<hwnd>

# mover janela para o segundo monitor e maximizar
pc_window hwnd=<hwnd> action=move x=2560 y=0 width=1280 height=720
pc_window hwnd=<hwnd> action=maximize
```

## Pitfalls que custam tempo

- **Notepad do Win11 é WinUI**: não tem filho `Edit` Win32 — `pc_elements` volta
  quase vazio. Use `pc_uia` (o `Document` expõe `value`/`text`). Notepad clássico
  e diálogos `#32770` respondem a `pc_elements` + `BM_CLICK`/`WM_SETTEXT`.
- **UIA custa ~0,5–1,2 s por chamada** (PowerShell + .NET). Faça `dump` uma vez,
  guarde `name`/`automation_id` e reuse; não faça dump dentro de loop.
- **`name` é exato por padrão**. Se falhar, a resposta traz `suggestions` — use-as
  ou passe `contains=true`.
- **Botão clássico sem Invoke cai em `win32-bm-click`, normalmente com
  `verified=false`** (BM_CLICK não muda nada que o probe enxergue). Não é falha:
  confirme o efeito real (campo preenchido, janela fechada) em vez do booleano.
- **`pc_win32` com `control_id` errado é silencioso**: WM_COMMAND de id
  inexistente retorna sucesso e não faz nada. Confirme pela janela/estado.
- **`pc_elements` é Win32 puro** (rápido, sem PowerShell) mas não enxerga apps
  XAML/modernos nem conteúdo de navegador.
- **Sessão bloqueada** (`pc_status.session.locked=true`): input não é entregue,
  ponto. Peça para desbloquear; processos/serviços/arquivos continuam.
- **Não feche janela alheia** com `pc_window action=close` nem use `pc_power` sem
  pedido explícito. `shutdown`/`restart` exigem `confirm="yes"` e têm `delay_s`
  (cancele com `action=cancel`).
- **Killswitch**: se `pc_control action=disable` estiver ativo, toda tool devolve
  erro explicando. Não tente contornar por Bash/PostMessage — peça para religar
  (`/pc-off` desliga, `/pc-on` religa).
- **Serviço** `start/stop` exige o processo do agente elevado; `status` não.

## Auditoria

`pc_audit` mostra as últimas ações com resultado. Use depois de uma sequência
longa antes de declarar sucesso. O comando `/pc-status` imprime status +
killswitch + últimas ações.
