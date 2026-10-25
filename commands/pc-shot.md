---
description: Print de uma janela (mesmo escondida) ou da tela — salva PNG e mostra o caminho
argument-hint: "[título da janela | tela | monitor]"
allowed-tools: mcp__plugin_pc-control_pc__pc_windows, mcp__plugin_pc-control_pc__pc_screenshot
---

Alvo pedido: `$ARGUMENTS` (vazio = tela inteira).

1. Se o alvo for um nome/título, ache o `hwnd` com `pc_windows` (casamento parcial
   no título ou no processo) e chame `pc_screenshot target=window hwnd=<hwnd>` —
   isso usa PrintWindow, então funciona com a janela **atrás de outras ou minimizada**.
2. Se o alvo for "tela" ou vazio, `pc_screenshot target=screen`.
3. Se o alvo for um número de monitor, `pc_screenshot target=monitor monitor=<n>`.

Devolva o caminho do PNG gerado e uma linha dizendo o que aparece nele. Não
traga a janela para a frente.
