<#
run_demo.ps1 — start the whole FLOCK demo on the world laptop (Windows PowerShell).

  .\run_demo.ps1                                   # judges scenario, real modules where they exist, stubs otherwise
  .\run_demo.ps1 -Scenario harness\scenarios\base_cutoff.json
  .\run_demo.ps1 -Spoof                            # add GHOST7 spoofer
  .\run_demo.ps1 -Stubs                            # force stub node/channel (if a real one is broken)
  .\run_demo.ps1 -Stop                             # stop everything this script started

Starts, in order: METAR fetch -> world (ws :8765, web :8080) -> radio channel -> one node per
FLOCK aircraft -> spoofer (optional) -> opens the log page. Each process gets its own window
titled with its role so you can see its output. PIDs go to .demo_pids for -Stop.
Other laptops open:  http://<this-ip>:8080/index.html?role=cockpitA | cockpitB | god   and  /log.html
If PowerShell blocks the script once:  Set-ExecutionPolicy -Scope Process Bypass
#>
param(
  [string]$Scenario = "harness\scenarios\judges.json",
  [double]$Loss = 0.1,
  [double]$Latency = 0.3,
  [switch]$Spoof,
  [switch]$Stubs,
  [switch]$Stop
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$PidFile = Join-Path $PSScriptRoot ".demo_pids"

if ($Stop) {
  if (Test-Path $PidFile) {
    Get-Content $PidFile | ForEach-Object { try { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue } catch {} }
    Remove-Item $PidFile
  }
  Write-Host "FLOCK demo stopped."
  exit 0
}

$Py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$Ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match "Wi-?Fi|WLAN" -and $_.IPAddress -notmatch "^169\." } | Select-Object -First 1).IPAddress
if (-not $Ip) { $Ip = "localhost" }
$World = "ws://localhost:8765"
"" | Set-Content $PidFile

function Start-Role([string]$Title, [string[]]$PyArgs) {
  $cmd = "`$host.UI.RawUI.WindowTitle = 'FLOCK $Title'; & '$Py' $($PyArgs -join ' '); Write-Host ''; Write-Host '[$Title exited - press Enter]'; Read-Host"
  $p = Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -PassThru
  Add-Content $PidFile $p.Id
  Write-Host ("  started {0,-14} {1}" -f $Title, ($PyArgs -join ' '))
}
function Wait-Port([int]$Port, [int]$Seconds = 15) {
  for ($i = 0; $i -lt $Seconds * 4; $i++) {
    try { $c = New-Object Net.Sockets.TcpClient; $c.Connect("127.0.0.1", $Port); $c.Close(); return $true } catch { Start-Sleep -Milliseconds 250 }
  }
  return $false
}

Write-Host "FLOCK demo  scenario=$Scenario  ip=$Ip"
& $Py data/metar.py

# 1. world (+ web on 8080)
if (Test-Path "world\world_server.py") { Start-Role "world" @("world/world_server.py", "--scenario", $Scenario, "--http", "8080") }
else { Start-Role "world(stub)" @("stubs/fake_world.py", "--scenario", $Scenario); Start-Role "web" @("-m", "http.server", "8080", "-d", "web") }
if (-not (Wait-Port 8765)) { Write-Host "World did not open port 8765 - check its window." -ForegroundColor Red; exit 1 }

# 2. radio channel
if ((Test-Path "radio\channel.py") -and -not $Stubs) { Start-Role "channel" @("radio/channel.py", "--world", $World, "--loss", $Loss, "--latency", $Latency) }
else {
  $chArgs = @("stubs/fake_channel.py", "--world", $World, "--loss", $Loss)
  if ($Spoof) { $chArgs += "--spoof" }            # stub channel injects GHOST7 itself
  Start-Role "channel(stub)" $chArgs
}

# 3. one node per FLOCK aircraft
$sc = Get-Content $Scenario -Raw | ConvertFrom-Json
$flock = $sc.aircraft | Where-Object { $_.flock -ne $false } | ForEach-Object { $_.id }
if ((Test-Path "node\node.py") -and -not $Stubs) {
  foreach ($id in $flock) { Start-Role "node $id" @("node/node.py", "--id", $id, "--world", $World) }
} else {
  $first = ($sc.aircraft | Where-Object { $_.human -eq $true } | Select-Object -First 1).id
  if (-not $first) { $first = $flock[0] }
  Start-Role "node $first(stub)" @("stubs/fake_node.py", "--id", $first, "--world", $World)
}

# 4. spoofer
if ($Spoof) {
  if (Test-Path "radio\spoofer.py") { Start-Role "spoofer" @("radio/spoofer.py", "--world", $World) }
  elseif ((Test-Path "radio\channel.py") -and -not $Stubs) { Write-Host "  (no radio/spoofer.py yet - use .\run_demo.ps1 -Stubs -Spoof)" -ForegroundColor Yellow }
}

Start-Process "http://localhost:8080/log.html"
Write-Host ""
Write-Host "Other laptops (same hotspot):" -ForegroundColor Green
Write-Host "  cockpit A  http://$($Ip):8080/index.html?role=cockpitA"
Write-Host "  cockpit B  http://$($Ip):8080/index.html?role=cockpitB"
Write-Host "  god view   http://$($Ip):8080/index.html?role=god"
Write-Host "  comms log  http://$($Ip):8080/log.html"
Write-Host "Stop everything:  .\run_demo.ps1 -Stop"
