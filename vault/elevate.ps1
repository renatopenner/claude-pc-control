param(
  [Parameter(Mandatory=$true)][string]$Command,
  [Parameter(Mandatory=$true)][string]$OutFile,
  [int]$TimeoutSec = 120
)
# Roda um comando com token elevado SEM pedir senha: registra uma tarefa
# agendada com RunLevel=Highest (InteractiveToken) do próprio usuário, executa,
# espera o fim e apaga a tarefa. Devolve uma linha JSON no stdout.
$ErrorActionPreference = 'Stop'
$taskName = "pcc-elev-$PID-$([guid]::NewGuid().ToString('N').Substring(0,8))"
$cmdFile = "$OutFile.cmd"
$t0 = Get-Date

try {
  # o comando vai num .cmd para não depender do escaping da linha de argumentos
  [IO.File]::WriteAllText($cmdFile, "@echo off`r`nchcp 65001 >nul`r`n$Command`r`n", [Text.UTF8Encoding]::new($false))

  $svc = New-Object -ComObject Schedule.Service
  $svc.Connect()
  $root = $svc.GetFolder('\')
  $td = $svc.NewTask(0)
  $td.RegistrationInfo.Description = 'claude-pc-control elevate'
  $td.Principal.LogonType = 3          # TASK_LOGON_INTERACTIVE_TOKEN
  $td.Principal.RunLevel = 1           # TASK_RUNLEVEL_HIGHEST
  $td.Settings.AllowDemandStart = $true
  $td.Settings.DisallowStartIfOnBatteries = $false
  $td.Settings.StopIfGoingOnBatteries = $false
  $td.Settings.ExecutionTimeLimit = 'PT10M'
  $act = $td.Actions.Create(0)         # TASK_ACTION_EXEC
  $act.Path = "$env:ComSpec"
  $act.Arguments = "/c `"`"$cmdFile`" > `"$OutFile`" 2>&1`""
  $root.RegisterTaskDefinition($taskName, $td, 6, $null, $null, 3) | Out-Null

  $task = $root.GetTask($taskName)
  $task.Run($null) | Out-Null

  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  $lastLen = -1
  while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
    $state = $task.State                 # 3 = Ready, 4 = Running
    if (Test-Path $OutFile) {
      $len = (Get-Item $OutFile).Length
      if ($state -ne 4 -and $len -eq $lastLen) { break }
      $lastLen = $len
    } elseif ($state -ne 4) {
      Start-Sleep -Milliseconds 700
      break
    }
  }
  $exit = 0
  try { $exit = $task.LastTaskResult } catch { $exit = 0 }
  $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 2)
  "{`"ok`":true,`"exit_code`":$exit,`"task`":`"$taskName`",`"seconds`":$secs}"
}
catch {
  $msg = $_.Exception.Message -replace '"', "'" -replace "`r?`n", ' '
  "{`"ok`":false,`"error`":`"$msg`"}"
}
finally {
  try { (New-Object -ComObject Schedule.Service).GetFolder('\').DeleteTask($taskName, 0) } catch {}
  try { Remove-Item $cmdFile -Force -ErrorAction SilentlyContinue } catch {}
}
