---
name: vault-network
description: Use quando precisar de credencial guardada, pasta de rede (\\\\servidor\\pasta) ou privilégio de administrador — cofre DPAPI model-blind, montagem por WNetAddConnection2W e elevação sem UAC.
---

# vault-network — cofre, pastas de rede e elevação

Tools do servidor MCP `vault` (`mcp__plugin_pc-control_vault__<tool>` no Claude
Code). São 10:

`vault_status` `vault_list` `vault_save` `vault_delete` `vault_fill_login`
`net_mount` `net_unmount` `net_list` `net_check` `elevate_run`

## Regra de ouro: segredo não passa pelo chat

O valor secreto **nunca** entra em argumento de tool. Se entrar, fica na
transcrição, no log do cliente e no histórico do servidor MCP — e aí não é mais
segredo. Por isso:

- `vault_save source=dialog` → abre uma janela no desktop do usuário; **ele**
  digita a senha, o plugin cifra e devolve só o handle.
- `vault_save source=credential_manager` → importa do Gerenciador de Credenciais
  do Windows (`credential_target="Domain:target=TERMSRV/servidor"`); também sem
  digitar nada. `source=list_credentials` lista os destinos disponíveis.
- `vault_cli.py` (terminal do usuário, `getpass`) → alternativa quando a janela
  não está disponível.
- `vault_save` com `secret=` é **recusado por projeto**. Não insista nem tente
  contornar com Bash; explique o caminho certo.

Depois de cadastrado, o handle (`ne-1a2b3c4d`, `lo-...`) circula livremente — ele
não abre nada sozinho, só funciona neste usuário Windows nesta máquina (DPAPI).

## Pastas de rede (o caso "entrar em diretório de rede")

```
net_check  target="\\\\servidor\\pasta"        # ping/DNS/acesso antes de tentar
net_mount  entry="Pasta GESER"                # credencial do cofre (kind=network)
net_mount  target="\\\\servidor\\pasta" drive="Z:"
net_unmount target="\\\\servidor\\pasta"
net_list
```

- A senha vai em memória para `WNetAddConnection2W` — **não** aparece em
  `net use`, em argv, no histórico do PowerShell nem no WMI.
- `net_mount` só devolve `verified=true` depois de **listar** o conteúdo; sem
  isso a conexão não conta como boa.
- Erros vêm traduzidos com dica: 1326 falha de logon, 1219 conflito de
  credenciais (já há conexão com outro usuário — `net_unmount` e repita), 53/67
  caminho errado, 86 senha vencida.
- Se a credencial do cofre estiver vencida: `vault_save` de novo com o mesmo
  label depois de `vault_delete`, não fique tentando com a antiga (conta
  bloqueia no domínio).

## Elevação sem digitar senha de administrador

```
elevate_run command="net share"                  # token alto, sem UAC
elevate_run command="sc query lanmanserver"
elevate_run command="powershell -c Get-SmbShare" mode=task
```

Como funciona: se o processo do agente já está elevado, roda direto. Se não
está (caso normal: UAC filtra o token do administrador), registra uma **tarefa
agendada com RunLevel=Highest / InteractiveToken** do próprio usuário, executa e
apaga a tarefa — o comando nasce com integridade Alta (`S-1-16-12288`) e **não
aparece prompt de UAC nem pede senha**. Confirme com
`whoami /groups | findstr /c:"S-1-16-12288"`.

Limites reais:
- a conta precisa ser administradora; conta padrão não eleva por mágica;
- política de domínio pode proibir tarefa com RunLevel Highest (`Access denied`),
  aí só resta o usuário aprovar o UAC na tela;
- elevação **não** herda a sessão visual: apps que exigem desktop interativo
  elevado (ex.: janela do gerenciador de dispositivos) podem não aparecer;
- `pc_service start/stop` e `pc_power` continuam precisando de token alto — rode
  via `elevate_run` quando o processo do agente não for elevado.

## Preencher login no navegador

```
vault_fill_login entry="Sistema Banpará"          # origem precisa bater exatamente
vault_fill_login entry="Sistema Banpará" submit=true
```

Preenche usuário e senha na aba atual via CDP (sem foco, sem cursor) e devolve
apenas `filled`, `submitted`, `origin`. A origem da aba tem que ser **idêntica** à
cadastrada (`https://sistema.exemplo.br`) — diferente, ele não preenche nada.

O que continua proibido, sem exceção: 2FA, código de verificação, cartão e
qualquer "senha" de tela que não seja um login cadastrado. Nesses casos, pare e
devolva ao usuário (`browser_activate` para ele assumir).
