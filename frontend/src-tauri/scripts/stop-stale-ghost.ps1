# Stop a leftover Ghost backend that still holds Ghost's port: a process
# started from the Ghost folder, or one running `ghost serve`. Any other
# program on the port is left alone. Prints "stopped" for each one ended.
# Used by the desktop shell before it starts the backend (lib.rs) and by CI.
param([Parameter(Mandatory = $true)][string]$Dir, [Parameter(Mandatory = $true)][int]$Port)

$d = $Dir.TrimEnd('\') + '\'
$owners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty OwningProcess -Unique
foreach ($id in $owners) {
  $p = Get-CimInstance Win32_Process -Filter "ProcessId=$id"
  if (-not $p) { continue }
  $fromGhost = $p.ExecutablePath -and $p.ExecutablePath.StartsWith($d, [StringComparison]::OrdinalIgnoreCase)
  $runsGhost = $p.CommandLine -and (
    ($p.CommandLine.IndexOf($d, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
    ($p.CommandLine -match 'ghost(\.exe)?\W*\s+serve'))
  if ($fromGhost -or $runsGhost) {
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    'stopped'
  }
}
