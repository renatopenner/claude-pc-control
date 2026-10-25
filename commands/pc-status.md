---
description: Estado do controle do PC e do navegador (sessão, killswitch, abas, últimas ações)
allowed-tools: mcp__plugin_pc-control_pc__pc_status, mcp__plugin_pc-control_pc__pc_audit, mcp__plugin_pc-control_pc__pc_windows, mcp__plugin_pc-control_browser__browser_status, mcp__plugin_pc-control_browser__browser_tabs
---

Mostre o estado atual do plugin, em no máximo 12 linhas:

1. `pc_status` — sessão interativa? bloqueada? monitor/DPI? elevação? killswitch ligado?
2. `pc_audit` com `limit=5` — as últimas 5 ações e o resultado de cada uma.
3. `browser_status` — para Chrome e Edge: instalado, porta de debug aberta, número de abas.
4. Se alguma porta de debug estiver aberta, `browser_tabs` do navegador com mais abas (máximo 8 abas na saída).

Responda em português, em tabela curta ou lista de bullets. Não abra, feche nem
altere nada. Se o killswitch estiver desligado, diga em uma linha que
`/pc-on` religa.
