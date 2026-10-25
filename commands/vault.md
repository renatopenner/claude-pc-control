---
description: Cofre, credenciais e pastas de rede (status, entradas, montagens, credenciais do Windows)
allowed-tools: mcp__plugin_pc-control_vault__vault_status, mcp__plugin_pc-control_vault__vault_list, mcp__plugin_pc-control_vault__net_list, mcp__plugin_pc-control_vault__net_check
---

Mostre em no máximo 12 linhas:

1. `vault_status` — quantas entradas, por tipo (login/network/generic), arquivo do cofre e credenciais do Windows disponíveis.
2. `vault_list` — handle, tipo, label e metadados. Nunca há segredo; não invente um.
3. `net_list` — conexões ativas (drive → UNC) e até 8 credenciais de rede armazenadas no Windows.
4. Se `$ARGUMENTS` trouxer um caminho UNC, rode `net_check target=$ARGUMENTS` e diga se o conteúdo lista.

Se faltar credencial para algo que o usuário quer acessar, ofereça os caminhos:
`vault_save source=dialog` (janela para ele digitar) ou
`python vault/vault_cli.py add-net`. Nunca peça a senha no chat.
