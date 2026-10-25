param(
  [Parameter(Mandatory = $true)][string]$Action,
  [long]$Hwnd = 0,
  [int]$Limit = 200,
  [int]$Depth = 8,
  [string]$Name = '',
  [string]$AutomationId = '',
  [string]$Type = '',
  [int]$Index = 0,
  [string]$Value = '',
  [int]$X = 0,
  [int]$Y = 0,
  [int]$W = 0,
  [int]$H = 0,
  [string]$Direction = 'down',
  [switch]$Contains,
  [switch]$Self
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { $OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Emit($obj) {
  $obj | ConvertTo-Json -Depth 8 -Compress
}

try {
  Add-Type -AssemblyName UIAutomationClient
  Add-Type -AssemblyName UIAutomationTypes

  $AE = [System.Windows.Automation.AutomationElement]
  $TS = [System.Windows.Automation.TreeScope]

  if ($Hwnd -ne 0) {
    $root = $AE::FromHandle([IntPtr]$Hwnd)
  } else {
    $root = $AE::RootElement
  }
  if ($null -eq $root) { Emit @{ ok = $false; error = "elemento raiz nao encontrado para hwnd=$Hwnd" }; exit 0 }

  $PATTERNS = @(
    @{ id = 'invoke'; cls = [System.Windows.Automation.InvokePattern] },
    @{ id = 'value'; cls = [System.Windows.Automation.ValuePattern] },
    @{ id = 'toggle'; cls = [System.Windows.Automation.TogglePattern] },
    @{ id = 'selectionitem'; cls = [System.Windows.Automation.SelectionItemPattern] },
    @{ id = 'expandcollapse'; cls = [System.Windows.Automation.ExpandCollapsePattern] },
    @{ id = 'scrollitem'; cls = [System.Windows.Automation.ScrollItemPattern] },
    @{ id = 'rangevalue'; cls = [System.Windows.Automation.RangeValuePattern] },
    @{ id = 'selection'; cls = [System.Windows.Automation.SelectionPattern] },
    @{ id = 'text'; cls = [System.Windows.Automation.TextPattern] },
    @{ id = 'transform'; cls = [System.Windows.Automation.TransformPattern] },
    @{ id = 'window'; cls = [System.Windows.Automation.WindowPattern] },
    @{ id = 'scroll'; cls = [System.Windows.Automation.ScrollPattern] },
    @{ id = 'griditem'; cls = [System.Windows.Automation.GridItemPattern] }
  )

  function Get-Patterns($el) {
    $out = @()
    foreach ($p in $PATTERNS) {
      $obj = $null
      try {
        if ($el.TryGetCurrentPattern($p.cls::Pattern, [ref]$obj)) { $out += $p.id }
      } catch { }
    }
    return $out
  }

  function Get-Info($el, $level) {
    $c = $el.Current
    $r = $c.BoundingRectangle
    $ct = ''
    try { $ct = $c.ControlType.ProgrammaticName -replace '^ControlType\.', '' } catch { }
    $obj = [ordered]@{
      name          = $c.Name
      automation_id = $c.AutomationId
      type          = $ct
      class         = $c.ClassName
      rect          = @([int]$r.X, [int]$r.Y, [int]($r.X + $r.Width), [int]($r.Y + $r.Height))
      enabled       = $c.IsEnabled
      offscreen     = $c.IsOffscreen
      focusable     = $c.IsKeyboardFocusable
      focused       = $c.HasKeyboardFocus
      depth         = $level
      patterns      = @(Get-Patterns $el)
    }
    return $obj
  }

  function Get-Value($el) {
    $obj = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$obj)) {
      return $obj.Current.Value
    }
    $tp = $null
    if ($el.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$tp)) {
      return $tp.DocumentRange.GetText(2000)
    }
    return $null
  }

  # ---------------- dump ----------------
  if ($Action -eq 'dump') {
    $walker = [System.Windows.Automation.TreeScope]::Descendants
    $all = $root.FindAll($walker, [System.Windows.Automation.Condition]::TrueCondition)
    $items = @()
    $count = 0
    foreach ($el in $all) {
      if ($count -ge $Limit) { break }
      try {
        $items += Get-Info $el 0
        $count++
      } catch { }
    }
    Emit @{ ok = $true; hwnd = $Hwnd; count = $items.Count; elements = $items }
    exit 0
  }

  # ---------------- match ----------------
  function Matches($el) {
    $c = $el.Current
    if ($Name -ne '') {
      $n = [string]$c.Name
      if ($Contains) { if ($n.IndexOf($Name, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false } }
      else { if ($n -ne $Name) { return $false } }
    }
    if ($AutomationId -ne '' -and [string]$c.AutomationId -ne $AutomationId) { return $false }
    if ($Type -ne '') {
      $ct = ([string]$c.ControlType.ProgrammaticName) -replace '^ControlType\.', ''
      if ($ct -ne $Type) { return $false }
    }
    return $true
  }

  if ($Action -eq 'find' -or $Action -eq 'do') {
    if ($Hwnd -eq 0 -and $Name -eq '' -and $AutomationId -eq '' -and $Type -eq '') {
      Emit @{ ok = $false; error = 'informe hwnd e/ou name/automation_id/type' }; exit 0
    }
    $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
    $hits = @()
    foreach ($el in $all) {
      try { if (Matches $el) { $hits += $el } } catch { }
      if ($hits.Count -gt ($Index + 8)) { break }
    }
    if ($hits.Count -le $Index) {
      Emit @{ ok = $false; error = "nenhum elemento casou (hits=$($hits.Count), index=$Index)"; name = $Name; automation_id = $AutomationId; type = $Type }
      exit 0
    }
    $target = $hits[$Index]
    $info = Get-Info $target 0

    if ($Action -eq 'find') {
      Emit @{ ok = $true; hits = $hits.Count; element = $info; value = (Get-Value $target) }
      exit 0
    }
    Emit @{ ok = $true; hits = $hits.Count; element = $info; value = (Get-Value $target) }
    exit 0
  }

  # ---------------- single action on matched element ----------------
  if ($Self) {
    $target = $root
    $info = Get-Info $root 0
    $hits = @($root)
  } else {
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  $hits = @()
  foreach ($el in $all) {
    try { if (Matches $el) { $hits += $el } } catch { }
    if ($hits.Count -gt ($Index + 8)) { break }
  }
  if ($hits.Count -le $Index) {
    Emit @{ ok = $false; error = "nenhum elemento casou (hits=$($hits.Count), index=$Index)"; name = $Name; automation_id = $AutomationId; type = $Type }
    exit 0
  }
  if (-not $Self) {
    $target = $hits[$Index]
    $info = Get-Info $target 0
  }
  }
  $done = $false
  $detail = ''

  switch ($Action) {
    'invoke' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); $done = $true; $detail = 'InvokePattern' }
      else {
        $si = $null
        if ($target.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$si)) { $si.Select(); $done = $true; $detail = 'SelectionItemPattern.Select' }
      }
    }
    'set_value' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { $p.SetValue($Value); $done = $true; $detail = 'ValuePattern.SetValue' }
    }
    'toggle' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); $done = $true; $detail = 'TogglePattern.Toggle' }
    }
    'select' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); $done = $true; $detail = 'SelectionItemPattern.Select' }
      else {
        $ip = $null
        if ($target.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip)) { $ip.Invoke(); $done = $true; $detail = 'InvokePattern' }
      }
    }
    'expand' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); $done = $true; $detail = 'ExpandCollapse.Expand' }
    }
    'collapse' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Collapse(); $done = $true; $detail = 'ExpandCollapse.Collapse' }
    }
    'scroll_into_view' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern, [ref]$p)) { $p.ScrollIntoView(); $done = $true; $detail = 'ScrollItem.ScrollIntoView' }
    }
    'focus' {
      $target.SetFocus(); $done = $true; $detail = 'SetFocus'
    }
    'get_value' {
      $v = Get-Value $target
      $done = $null -ne $v
      $detail = if ($done) { 'read' } else { 'sem ValuePattern/TextPattern' }
      Emit @{ ok = $done; element = $info; value = $v; detail = $detail }
      exit 0
    }
    'legacy' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$p)) {
        if ($p.Current.DefaultAction -or $true) { $p.DoDefaultAction(); $done = $true; $detail = 'LegacyIAccessible.DoDefaultAction' }
      }
    }
    'legacy_value' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern, [ref]$p)) {
        $p.SetValue($Value); $done = $true; $detail = 'LegacyIAccessible.SetValue'
      }
    }
    'range' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$p)) {
        $p.SetValue([double]$Value); $done = $true; $detail = 'RangeValue.SetValue'
      }
    }
    'move' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$p)) {
        $p.Move($X, $Y); $done = $true; $detail = 'Transform.Move'
      }
    }
    'resize' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.TransformPattern]::Pattern, [ref]$p)) {
        $p.Resize($W, $H); $done = $true; $detail = 'Transform.Resize'
      }
    }
    'scroll' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern, [ref]$p)) {
        $h = if ($Direction -eq 'left') { [System.Windows.Automation.ScrollAmount]::LargeDecrement }
             elseif ($Direction -eq 'right') { [System.Windows.Automation.ScrollAmount]::LargeIncrement }
             else { [System.Windows.Automation.ScrollAmount]::NoAmount }
        $v = if ($Direction -eq 'up') { [System.Windows.Automation.ScrollAmount]::LargeDecrement }
             elseif ($Direction -eq 'down') { [System.Windows.Automation.ScrollAmount]::LargeIncrement }
             else { [System.Windows.Automation.ScrollAmount]::NoAmount }
        $p.Scroll($h, $v); $done = $true; $detail = 'ScrollPattern.Scroll'
      }
    }
    'window_close' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$p)) {
        $p.Close(); $done = $true; $detail = 'WindowPattern.Close'
      }
    }
    'window_state' {
      $p = $null
      if ($target.TryGetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern, [ref]$p)) {
        $state = switch ($Direction) {
          'maximize' { [System.Windows.Automation.WindowVisualState]::Maximized }
          'minimize' { [System.Windows.Automation.WindowVisualState]::Minimized }
          default { [System.Windows.Automation.WindowVisualState]::Normal }
        }
        $p.SetWindowVisualState($state); $done = $true; $detail = 'WindowPattern.SetWindowVisualState'
      }
    }
    default { Emit @{ ok = $false; error = "acao desconhecida '$Action'" }; exit 0 }
  }

  Start-Sleep -Milliseconds 60
  $after = Get-Info $target 0
  Emit ([ordered]@{ ok = $done; detail = $detail; element = $info; after = $after; value = (Get-Value $target) })
} catch {
  Emit @{ ok = $false; error = $_.Exception.Message; action = $Action }
}
