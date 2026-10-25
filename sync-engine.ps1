#Requires -Version 5.1
<#
.SYNOPSIS
  Sincroniza o motor do pc-control (17 tools de Windows) a partir do repositório
  hermes-pc-control, para o plugin do Claude Code não manter duas cópias divergentes.

.DESCRIPTION
  O motor vive em github.com/renatopenner/hermes-pc-control e é usado pelo Hermes,
  pelo Antigravity e por este plugin. Este script copia os arquivos do motor para
  .\engine\ e mostra o diff de versão, para o commit do plugin registrar de onde veio.

.EXAMPLE
  .\sync-engine.ps1
  .\sync-engine.ps1 -Source 'C:\Users\renat\AppData\Local\hermes\plugins\pc-control'
  .\sync-engine.ps1 -Source '.' -Check
#>
[CmdletBinding()]
param(
  [string]$Source = (Join-Path $env:LOCALAPPDATA 'hermes\plugins\pc-control'),
  [switch]$Check
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $here 'engine'
$files = @('mcp_server.py', 'pccore.py', 'pctools.py', 'uia.ps1', 'com.ps1', 'launch.cmd')

if (-not (Test-Path $Source)) {
  throw "motor não encontrado em $Source — clone github.com/renatopenner/hermes-pc-control e passe -Source"
}

$changed = @()
foreach ($f in $files) {
  $src = Join-Path $Source $f
  if (-not (Test-Path $src)) { Write-Warning "ausente na origem: $f"; continue }
  $dst = Join-Path $dest $f
  $same = (Test-Path $dst) -and ((Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash)
  if (-not $same) { $changed += $f }
}

if ($changed.Count -eq 0) {
  Write-Host 'motor já está sincronizado' -ForegroundColor Green
  exit 0
}

Write-Host "arquivos diferentes: $($changed -join ', ')"
if ($Check) {
  Write-Host 'modo -Check: nada foi copiado' -ForegroundColor Yellow
  exit 2
}

New-Item -ItemType Directory -Force -Path $dest | Out-Null
foreach ($f in $changed) {
  Copy-Item (Join-Path $Source $f) (Join-Path $dest $f) -Force
  Write-Host "  copiado $f"
}

# .cmd sempre CRLF (cmd.exe)
foreach ($cmd in Get-ChildItem $dest -Filter *.cmd) {
  $text = [IO.File]::ReadAllText($cmd.FullName) -replace "`r`n", "`n"
  [IO.File]::WriteAllText($cmd.FullName, ($text -replace "`n", "`r`n"), [Text.UTF8Encoding]::new($false))
}

$commit = ''
try {
  $commit = (git -C $Source rev-parse --short HEAD) 2>$null
} catch {}
Write-Host ''
Write-Host "motor sincronizado de $Source (commit $commit)" -ForegroundColor Green
Write-Host 'Rode os testes antes de commitar:  python tests\selftest_mcp.py --pc'
