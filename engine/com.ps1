param(
  [Parameter(Mandatory = $true)][string]$Action,
  [Parameter(Mandatory = $true)][string]$ProgId,
  [string]$Member = '',
  [string]$ArgsJson = '[]',
  [string]$ValueJson = 'null',
  [int]$Limit = 200,
  [switch]$Create
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Emit($obj) { $obj | ConvertTo-Json -Depth 4 -Compress }

function Wrap($v) {
  if ($null -eq $v) { return $null }
  if ($v -is [string] -or $v -is [ValueType]) { return $v }
  try { return "$v" } catch { return $null }
}

try {
  $obj = $null
  $created = $false
  if (-not $Create) {
    try { $obj = [System.Runtime.InteropServices.Marshal]::GetActiveObject($ProgId) } catch { $obj = $null }
  }
  if ($null -eq $obj) {
    $obj = New-Object -ComObject $ProgId
    $created = $true
  }

  if ($Action -eq 'attach' -or $Action -eq 'create') {
    $name = $null
    try { $name = "$($obj.Name)" } catch {}
    Emit @{ ok = $true; progid = $ProgId; created = $created; instance = $name }
    exit 0
  }

  if ($Action -eq 'members') {
    $items = @()
    foreach ($m in ($obj | Get-Member | Select-Object -First $Limit)) {
      $items += @{ name = $m.Name; kind = "$($m.MemberType)" }
    }
    Emit @{ ok = $true; progid = $ProgId; count = $items.Count; members = $items }
    exit 0
  }

  # navega caminho com pontos: Workbooks.Item(1) nao; Properties.Settings
  function Resolve-Owner($root, [string]$path) {
    if ([string]::IsNullOrWhiteSpace($path)) { return $root }
    $cur = $root
    foreach ($seg in $path.Split('.')) {
      if ($seg -eq '') { continue }
      $cur = $cur.$seg
      if ($null -eq $cur) { throw "caminho nulo em '$seg'" }
    }
    return $cur
  }

  $ownerPath = ''
  $leaf = $Member
  if ($Member.Contains('.')) {
    $idx = $Member.LastIndexOf('.')
    $ownerPath = $Member.Substring(0, $idx)
    $leaf = $Member.Substring($idx + 1)
  }
  $owner = Resolve-Owner $obj $ownerPath

  switch ($Action) {
    'get' {
      $v = $owner.$leaf
      Emit @{ ok = $true; member = $Member; value = (Wrap $v); type = $(try { "$($v.GetType().Name)" } catch { $null }) }
    }
    'set' {
      $val = $ValueJson | ConvertFrom-Json
      $owner.$leaf = $val
      $after = $owner.$leaf
      Emit @{ ok = $true; member = $Member; value = (Wrap $after) }
    }
    'call' {
      $arr = @()
      $raw = $ArgsJson | ConvertFrom-Json
      if ($null -ne $raw) { $arr = @($raw) }
      $res = $owner.GetType().InvokeMember($leaf, [System.Reflection.BindingFlags]::InvokeMethod,
                                           $null, $owner, $arr)
      Emit @{ ok = $true; member = $Member; args = $arr.Count; result = (Wrap $res);
              type = $(try { "$($res.GetType().Name)" } catch { $null }) }
    }
    default { Emit @{ ok = $false; error = "acao desconhecida '$Action'" } }
  }
} catch {
  Emit @{ ok = $false; error = $_.Exception.Message; action = $Action; progid = $ProgId; member = $Member }
}
