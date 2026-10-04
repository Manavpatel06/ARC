<#
run_demo.ps1 — start the whole FLOCK demo on the world laptop (Windows PowerShell).

  .\run_demo.ps1                                   # judges scenario, real modules where they exist, stubs otherwise
  .\run_demo.ps1 -Scenario harness\scenarios\base_cutoff.json
  .\run_demo.ps1 -Spoof                            # add GHOST7 spoofer
  .\run_demo.ps1 -Stubs                            # force stub node/channel (if a real one is broken)
  .\run_demo.ps1 -Stop                             # stop everything this script started
  .\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json            # free flight + live traffic
  .\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json -Seed 11   # same, different (repeatable) traffic

Starts, in order: METAR fetch -> world (ws :8765, web :8080) -> radio channel -> one node per
FLOCK aircraft -> spoofer (optional) -> opens the log page. Each process gets its own window
titled with its role so you can see its output. PIDs go to .demo_pids for -Stop.
Scenarios with "traffic" (live_kdvt.json): the world also starts a background node for every AI
aircraft it spawns (logs in harness\out\nodes\), and data/live_traffic.py if "live_seed" is on.
Other laptops open:  http://<this-ip>:8080/index.html?role=cockpitA | cockpitB | god   and  /log.html
A second copy on other ports (testing next to a running demo):  -WorldPort 8795 -ChanPort 8796 -HttpPort 8097 -NoBrowser
If PowerShell blocks the script once:  Set-ExecutionPolicy -Scope Process Bypass
#>
param(
  [string]$Scenario = "harness\scenarios\judges.json",
  [double]$Loss = 0.1,
  [double]$Latency = 0.3,
  [int]$Seed = -1,
  [int]$WorldPort = 8765,
  [int]$ChanPort = 8766,
  [int]$HttpPort = 8080,
  [switch]$NoBrowser,
  [switch]$Spoof,
  [switch]$Stubs,
  [switch]$Stop
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"        # data/metar.py prints arrows; the Windows console codepage can't
$PidFile = Join-Path $PSScriptRoot $(if ($WorldPort -eq 8765) { ".demo_pids" } else { ".demo_pids_$WorldPort" })

if ($Stop) {
  if (Test-Path $PidFile) {
    # /T: the whole tree - the role window, the venv python launcher AND the real python it starts
    Get-Content $PidFile | Where-Object { $_ -match '^\d+$' } | ForEach-Object { cmd /c "taskkill /T /F /PID $_ >nul 2>&1" }
    Remove-Item $PidFile
  }
  Write-Host "FLOCK demo stopped."
  exit 0
}

$Py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$Ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match "Wi-?Fi|WLAN" -and $_.IPAddress -notmatch "^169\." } | Select-Object -First 1).IPAddress
if (-not $Ip) { $Ip = "localhost" }
$World = "ws://localhost:$WorldPort"
$Chan = "ws://localhost:$ChanPort"     # radio/channel.py WebSocket radio (Windows hotspots often block multicast)
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

$sc = Get-Content $Scenario -Raw | ConvertFrom-Json

# 1. world (+ web on 8080)
if (Test-Path "world\world_server.py") {
  $wArgs = @("world/world_server.py", "--scenario", $Scenario, "--port", $WorldPort, "--http", $HttpPort)
  if ($sc.traffic) {                     # live traffic: the world starts a node per AI aircraft it spawns
    if (-not $Stubs) { $wArgs += @("--traffic-nodes", $Chan, "--pidfile", (Split-Path $PidFile -Leaf)) }
    if ($Seed -ge 0) { $wArgs += @("--seed", $Seed) }
  }
  Start-Role "world" $wArgs
}
else { Start-Role "world(stub)" @("stubs/fake_world.py", "--scenario", $Scenario, "--port", $WorldPort); Start-Role "web" @("-m", "http.server", $HttpPort, "-d", "web") }
if (-not (Wait-Port $WorldPort)) { Write-Host "World did not open port $WorldPort - check its window." -ForegroundColor Red; exit 1 }

# 2. radio channel
if ((Test-Path "radio\channel.py") -and -not $Stubs) { Start-Role "channel" @("radio/channel.py", "--world", $World, "--ws-port", $ChanPort, "--loss", $Loss, "--latency", $Latency); Start-Sleep -Seconds 2 }
else {
  $chArgs = @("stubs/fake_channel.py", "--world", $World, "--loss", $Loss)
  if ($Spoof) { $chArgs += "--spoof" }            # stub channel injects GHOST7 itself
  Start-Role "channel(stub)" $chArgs
}

# 3. one node per FLOCK aircraft
$flock = $sc.aircraft | Where-Object { $_.flock -ne $false } | ForEach-Object { $_.id }
if ((Test-Path "node\node.py") -and -not $Stubs) {
  foreach ($id in $flock) { Start-Role "node $id" @("node/node.py", "--id", $id, "--world", $World, "--via-channel", $Chan) }
} else {
  $first = ($sc.aircraft | Where-Object { $_.human -eq $true } | Select-Object -First 1).id
  if (-not $first) { $first = $flock[0] }
  Start-Role "node $first(stub)" @("stubs/fake_node.py", "--id", $first, "--world", $World)
}

# 4. spoofer
if ($Spoof) {
  if (Test-Path "radio\spoofer.py") { Start-Role "spoofer" @("radio/spoofer.py", "--mode", "unsigned", "--via-channel", $Chan) }
  elseif ((Test-Path "radio\channel.py") -and -not $Stubs) { Write-Host "  (no radio/spoofer.py yet - use .\run_demo.ps1 -Stubs -Spoof)" -ForegroundColor Yellow }
}

# 5. live sky (real ADS-B) when the scenario seeds AI arrivals from it
if ($sc.traffic -and $sc.traffic.live_seed) { Start-Role "live sky" @("data/live_traffic.py", "--world", $World) }

if (-not $NoBrowser) { Start-Process "http://localhost:$HttpPort/log.html" }
Write-Host ""
Write-Host "Other laptops (same hotspot):" -ForegroundColor Green
Write-Host "  cockpit A  http://$($Ip):$HttpPort/index.html?role=cockpitA"
Write-Host "  cockpit B  http://$($Ip):$HttpPort/index.html?role=cockpitB"
Write-Host "  god view   http://$($Ip):$HttpPort/index.html?role=god"
Write-Host "  comms log  http://$($Ip):$HttpPort/log.html"
Write-Host "  nodes on another laptop:  python node/node.py --id <ID> --world ws://$($Ip):$WorldPort --via-channel ws://$($Ip):$ChanPort"
Write-Host "  live sky:   python data/live_traffic.py --world ws://localhost:$WorldPort"
if ($sc.traffic) { Write-Host "  live traffic: AI nodes log to harness\out\nodes\ - god view RESET DEMO puts the judges back at their starts" }
Write-Host "Stop everything:  .\run_demo.ps1 -Stop$(if ($WorldPort -ne 8765) { " -WorldPort $WorldPort" })"
Write-Host "Firewall (once, admin PowerShell):  New-NetFirewallRule -DisplayName 'FLOCK demo' -Direction Inbound -Protocol TCP -LocalPort 8765,8766,8080 -Action Allow"
