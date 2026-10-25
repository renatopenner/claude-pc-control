#Requires -Version 5.1
<#
.SYNOPSIS
  Instala o plugin pc-control no Claude Code (mercado local ou GitHub) e libera as
  tools do plugin no settings.json.

.DESCRIPTION
  Passos:
    1. confere Python 3 (os três servidores MCP são scripts Python);
    2. acha a CLI do Claude Code;
    3. registra este repositório como marketplace e instala o plugin (escopo user);
    4. adiciona as regras de permissão mcp__plugin_pc-control_* no ~/.claude/settings.json;
    5. zera o ForegroundLockTimeout (janela nova não fica presa em segundo plano);
    6. cria a pasta de dados do plugin (%LOCALAPPDATA%\claude-pc-control).

.EXAMPLE
  .\setup-claude.ps1
  .\setup-claude.ps1 -FromGitHub
  .\setup-claude.ps1 -NoPermissions
#>
[CmdletBinding()]
param(
  [string]$MarketplaceSource = '',
  [switch]$FromGitHub,
  [switch]$NoPermissions,
  [switch]$NoFocusTweak
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Step($text) { Write-Host "== $text" -ForegroundColor Cyan }
function Write-Ok($text) { Write-Host "   ok  $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "   !   $text" -ForegroundColor Yellow }

# ---------------------------------------------------------------- Python ----
Write-Step 'Python 3'
$pyExe = $null
foreach ($cand in @('py', 'python', 'python3')) {
  $cmd = Get-Command $cand -ErrorAction SilentlyContinue
  if ($cmd) { $pyExe = $cmd.Source; break }
}
if ($pyExe) {
  Write-Ok "encontrado: $pyExe"
} else {
  Write-Warn2 'Python 3 não está no PATH — os servidores MCP não sobem sem ele.'
  Write-Warn2 'Instale de python.org marcando "Add python.exe to PATH" e rode de novo.'
}

# ---------------------------------------------------------------- Claude ----
Write-Step 'Claude Code CLI'
$claude = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $claude) {
  foreach ($guess in @(
      (Join-Path $env:APPDATA 'npm\claude.cmd'),
      (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe'),
      (Join-Path $env:USERPROFILE '.local\bin\claude.exe')
    )) {
    if (Test-Path $guess) { $claude = $guess; break }
  }
}
if (-not $claude) {
  Write-Warn2 'CLI do Claude Code não encontrada.'
  Write-Host ''
  Write-Host 'Instale primeiro:  npm install -g @anthropic-ai/claude-code' -ForegroundColor Yellow
  Write-Host 'Depois rode este script de novo, ou faça à mão:' -ForegroundColor Yellow
  Write-Host "  claude plugin marketplace add $here"
  Write-Host '  claude plugin install pc-control@pc-control-marketplace --scope user'
  exit 1
}
Write-Ok "encontrado: $claude"

# ------------------------------------------------------- marketplace/plugin ----
Write-Step 'Plugin'
if ($FromGitHub) {
  $source = 'renatopenner/claude-pc-control'
} elseif ($MarketplaceSource) {
  $source = $MarketplaceSource
} else {
  $source = $here
}
Write-Host "   fonte: $source"
& $claude plugin marketplace add $source
if ($LASTEXITCODE -ne 0) {
  Write-Warn2 'marketplace add falhou (talvez já registrado) — seguindo para o install.'
}
& $claude plugin install 'pc-control@pc-control-marketplace' --scope user
if ($LASTEXITCODE -ne 0) {
  Write-Warn2 'plugin install falhou. Rode `claude plugin list` e `claude plugin marketplace list` para ver o estado.'
} else {
  Write-Ok 'plugin instalado (escopo user)'
}

# ------------------------------------------------------------ permissões ----
if (-not $NoPermissions) {
  Write-Step 'Permissões (sem prompt por tool)'
  if ($pyExe) {
    $merge = Join-Path $here 'scripts\merge_settings.py'
    if (Test-Path $merge) {
      & $pyExe $merge
      if ($LASTEXITCODE -ne 0) { Write-Warn2 "merge_settings.py retornou $LASTEXITCODE" }
    } else {
      Write-Warn2 "não achei $merge"
    }
  } else {
    Write-Warn2 'sem Python não consigo mesclar o settings.json — edite à mão e inclua:'
    Write-Warn2 '  "permissions": { "allow": ["mcp__plugin_pc-control_pc__*", "mcp__plugin_pc-control_browser__*", "mcp__plugin_pc-control_vault__*"] }'
  }
}

# ------------------------------------------------------------ foco/janela ----
if (-not $NoFocusTweak) {
  Write-Step 'Foco de janela'
  try {
    Set-ItemProperty -Path 'HKCU:\Control Panel\Desktop' -Name 'ForegroundLockTimeout' -Value 0 -Type DWord -Force
    Write-Ok 'ForegroundLockTimeout = 0 (fallback em primeiro plano funciona na hora)'
  } catch {
    Write-Warn2 "não consegui ajustar o registro: $($_.Exception.Message)"
  }
}

# ------------------------------------------------------------- dados ----
Write-Step 'Pasta de dados'
$data = Join-Path $env:LOCALAPPDATA 'claude-pc-control'
New-Item -ItemType Directory -Force -Path $data | Out-Null
Write-Ok $data

Write-Host ''
Write-Host 'Pronto. Próximos passos:' -ForegroundColor Cyan
Write-Host '  1. abra o Claude Code numa pasta qualquer e rode  /plugin  (o pc-control deve estar habilitado)'
Write-Host '  2. /pc-status   -> estado do PC, do navegador e killswitch'
Write-Host '  3. /vault       -> cofre, credenciais do Windows e pastas de rede'
Write-Host '  4. para checar os servidores por fora:'
Write-Host "     $pyExe `"$here\tests\selftest_mcp.py`""
Write-Host ''
Write-Host 'Nada é gravado em nuvem: cofre = DPAPI local, perfil de navegador = %LOCALAPPDATA%\claude-pc-control\profiles'
