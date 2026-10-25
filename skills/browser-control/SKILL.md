---
name: browser-control
description: Use quando precisar agir no Chrome ou no Edge (abrir aba, ler página, clicar, digitar, print) sem roubar o foco do usuário — via CDP, em segundo plano.
---

# browser-control — Chrome e Edge por CDP, em segundo plano

Tools do servidor MCP `browser` deste plugin. Nome completo no Claude Code:
`mcp__plugin_pc-control_browser__<tool>`; aqui pelo nome curto. São 15:

`browser_status` `browser_launch` `browser_tabs` `browser_open` `browser_navigate`
`browser_read` `browser_eval` `browser_click` `browser_type` `browser_key`
`browser_scroll` `browser_wait` `browser_screenshot` `browser_close` `browser_activate`

Tudo fala o Chrome DevTools Protocol com a porta de debug do próprio navegador.
Consequência prática: **nada move o cursor nem rouba foco**. `browser_activate` é
a única exceção e devolve `foreground_used=true`.

## Ordem de trabalho

1. `browser_status` — instalado? processo vivo? porta de debug aberta? quantas abas?
2. Sem porta aberta → `browser_launch browser=chrome|edge profile=isolated`.
   O perfil dedicado abre **fora da tela e sem ativar**; faça login uma vez e a
   sessão fica salva em `%LOCALAPPDATA%\claude-pc-control\profiles\<navegador>`.
   `profile=user` usa o seu perfil real e exige o navegador fechado (uma instância
   já aberta ignora `--remote-debugging-port`).
3. `browser_tabs` → escolha a aba por `tab` (índice 1-based, id, prefixo do id ou
   texto do título). Sem `tab`, vale a última aba usada.
4. Ler barato: `browser_eval` (JS direto, devolve JSON) ou `browser_read`
   (text|html|value|links). Prefira isso a screenshot: custa menos contexto.
5. Agir: `browser_click` (por CSS ou por texto visível), `browser_type`
   (`submit=true` manda Enter), `browser_key`, `browser_scroll`, `browser_wait`.
6. Confirmar com `browser_read`/`browser_eval` no elemento afetado. Print só
   quando o visual importa — `browser_screenshot` grava PNG e devolve o caminho.

## Segundo plano de verdade

- `browser_launch` usa porta `0`: o navegador escolhe uma porta livre e grava em
  `<user-data-dir>\DevToolsActivePort`. Assim a instância do agente nunca briga
  com o seu Chrome da porta 9222.
- A janela nasce em `--window-position=-32000,-32000` e o foco é devolvido à
  janela anterior (`focus_restored=true` na resposta). Se o usuário não deve nem
  ver o processo, use `headless=true`.
- `browser_click`/`browser_type` agem no renderer: funcionam com a aba atrás de
  outras e com a janela minimizada. `real=true` dispara eventos de mouse reais na
  aba (ainda em segundo plano) — use quando o site ignora `element.click()`.

## Receitas

```
browser_status
browser_launch browser=edge profile=isolated
browser_open url=https://exemplo.com
browser_read mode=text max_chars=4000
browser_type selector="#busca" text="nota fiscal" submit=true
browser_wait selector=".resultado" timeout_s=10
browser_eval expression="Array.from(document.querySelectorAll('h2')).map(h=>h.innerText)"
browser_screenshot full_page=true
browser_close            # fecha a aba usada; o navegador continua vivo
```

## Pitfalls

- **Página interna** (`chrome://`, `edge://`, Web Store, PDF viewer) não expõe
  DOM: `browser_eval` falha ou volta vazio. Não insista — use o PC
  (`pc_screenshot target=window`) ou o caminho de download.
- **`data:` URL** costuma ser bloqueada em navegação de topo; prefira `file:///`
  ou http(s).
- **iframe**: `browser_click`/`browser_read` só enxergam o frame principal. Para
  iframe, use `browser_eval` com `document.querySelector('iframe').contentDocument`
  quando for same-origin; cross-origin só com input real na tela.
- **Login/2FA/pagamento**: nunca digite senha, código ou cartão. Se a página pedir,
  pare e devolva a tarefa ao usuário (`browser_activate` para ele assumir).
- **`browser_type`** usa `Input.insertText` depois de focar e limpar via setter
  nativo — funciona em React/Vue. Se o site usa máscara, digite em partes e
  confirme o `value_after`.
- **Screenshot de aba em segundo plano** pode sair com a página "congelada" se o
  site usa `requestAnimationFrame` — chame `browser_eval expression="new Promise(r=>requestAnimationFrame(()=>r(1)))"` antes.
- **`profile=isolated` sem login**: muitas páginas pedem login. Faça o login uma
  vez com `browser_activate`, depois volte ao modo invisível.
- **Não feche o navegador do usuário.** `browser_close` fecha uma aba; para
  encerrar o processo use `pc_process` — e só com pedido explícito.
