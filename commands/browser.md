---
description: Estado do navegador e abas abertas (Chrome/Edge por CDP)
allowed-tools: mcp__plugin_pc-control_browser__browser_status, mcp__plugin_pc-control_browser__browser_tabs, mcp__plugin_pc-control_browser__browser_launch
---

1. `browser_status` — para Chrome e Edge: instalado, processo rodando, porta de
   debug aberta, versão, abas.
2. Onde houver porta aberta, `browser_tabs` e liste no máximo 15 abas
   (índice, título curto, url).
3. Onde o navegador estiver instalado mas sem porta aberta, diga apenas:
   "fechado para automação — `browser_launch profile=isolated` abre em segundo plano".

Não abra nem feche abas sem o usuário pedir. Se ele pedir para abrir algo, use
`browser_launch` (se necessário) + `browser_open`.
