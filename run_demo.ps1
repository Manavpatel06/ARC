<#
run_demo.ps1 — start the whole ARC demo on the world laptop (Windows PowerShell).

  .\run_demo.ps1                                   # judges scenario, real modules where they exist, stubs otherwise
  .\run_demo.ps1 -Scenario harness\scenarios\base_cutoff.json
  .\run_demo.ps1 -Spoof                            # add GHOST7 spoofer (unsigned)
  .\run_demo.ps1 -Spoof -SpoofMode impossible       # any radio/spoofer.py --mode: unsigned | unknown-key | impossible | impersonate | replay | ...
  .\run_demo.ps1 -Spoof -SpoofArgs "--sybil 3"      # extra spoofer flags passed through as-is
  .\run_demo.ps1 -Stubs                            # force stub node/channel (if a real one is broken)
  .\run_demo.ps1 -Stop                             # stop everything this script started
  .\run_demo.ps1 -Arc                             # FINAL DEMO: arc_head_on.json, both judges 5 NM apart nose to nose
  .\run_demo.ps1 -Arc -Scenario harness\scenarios\head_on_judges.json   # ARC: collision avoidance (radio nodes, takeover)
                                                   #   + verification units in the same run (god view attacks)
  .\run_demo.ps1 -Legacy                          # same as -Arc (old name)
  .\run_demo.ps1 -Arc -NoVerify                   # collision avoidance only, no verification units
  .\run_demo.ps1 -NoLive                           # without the real-ADS-B "live sky" window (on by default)
  .\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json            # free flight + live traffic
  .\run_demo.ps1 -Scenario harness\scenarios\live_kdvt.json -Seed 11   # same, different (repeatable) traffic

Starts, in order: METAR fetch -> world (ws :8765, web :8080, advisory only) -> one ARC onboard
verification unit per judge aircraft (verify/unit.py, receive only) -> opens the log page. Inject a spoofed
ghost from the god view (+ Ghost). -Arc (= -Legacy) also starts the radio channel + one collision-avoidance
node per aircraft (+ spoofer) and lets nodes take the controls; the verification units still run (unless
-NoVerify), so one run shows coordination, takeover and fakes flagged. Each process gets its own window
titled with its role so you can see its output. PIDs go to .demo_pids for -Stop.
Scenarios with "traffic" (live_kdvt.json): the world also starts a background node for every AI
aircraft it spawns (logs in harness\out\nodes\).
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
  [Alias("Arc")][switch]$Legacy,
  [switch]$NoVerify,
  [switch]$NoLive,
  [switch]$Spoof,
  [string]$SpoofMode = "unsigned",
  [string]$SpoofArgs = "",
  [switch]$Stubs,
  [switch]$Stop
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($Legacy -and -not $PSBoundParameters.ContainsKey("Scenario")) { $Scenario = "harness\scenarios\arc_head_on.json" }   # -Arc alone = the final demo
$env:PYTHONIOENCODING = "utf-8"        # data/metar.py prints arrows; the Windows console codepage can't
$PidFile = Join-Path $PSScriptRoot $(if ($WorldPort -eq 8765) { ".demo_pids" } else { ".demo_pids_$WorldPort" })

# Leftovers from an earlier run (closed windows, a crashed script, the old -Stop) keep ports 8765/8766/8080 bound
# and the next start dies with WinError 10048. These two clean them up.
function Stop-PortOwners([int[]]$Ports) {
  foreach ($port in $Ports) {
    Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.OwningProcess -gt 4) { cmd /c "taskkill /T /F /PID $($_.OwningProcess) >nul 2>&1" }
    }
  }
}
function Stop-StaleArc {      # every python running an ARC role (nodes hold no port, so ports alone miss them)
  Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'world[\\/]world_server\.py|radio[\\/]channel\.py|node[\\/]node\.py|stubs[\\/]fake_|data[\\/]live_traffic\.py|radio[\\/]spoofer\.py|verify[\\/]unit\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Test-PortBusy([int]$Port) {
  return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

if ($Stop) {
  if (Test-Path $PidFile) {
    # /T: the whole tree - the role window, the venv python launcher AND the real python it starts
    Get-Content $PidFile | Where-Object { $_ -match '^\d+$' } | ForEach-Object { cmd /c "taskkill /T /F /PID $_ >nul 2>&1" }
    Remove-Item $PidFile
  }
  if ($WorldPort -eq 8765) { Stop-StaleArc }
  Stop-PortOwners @($WorldPort, $ChanPort, $HttpPort)
  Write-Host "ARC demo stopped."
  exit 0
}

# start clean: if an earlier run is still holding our ports, stop it first
$busy = @($WorldPort, $ChanPort, $HttpPort) | Where-Object { Test-PortBusy $_ }
if ($busy) {
  Write-Host "Ports $($busy -join ', ') still in use from an earlier run - stopping it first." -ForegroundColor Yellow
  if (Test-Path $PidFile) { Get-Content $PidFile | Where-Object { $_ -match '^\d+$' } | ForEach-Object { cmd /c "taskkill /T /F /PID $_ >nul 2>&1" } }
  if ($WorldPort -eq 8765) { Stop-StaleArc }
  Stop-PortOwners $busy
  Start-Sleep -Seconds 2
  $still = @($WorldPort, $ChanPort, $HttpPort) | Where-Object { Test-PortBusy $_ }
  if ($still) {
    foreach ($port in $still) {
      $o = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
      $n = (Get-Process -Id $o.OwningProcess -ErrorAction SilentlyContinue).ProcessName
      Write-Host "Port $port is held by '$n' (PID $($o.OwningProcess)). Close it, or run as admin, then start again." -ForegroundColor Red
    }
    exit 1
  }
}

$Py = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
$Ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match "Wi-?Fi|WLAN" -and $_.IPAddress -notmatch "^169\." } | Select-Object -First 1).IPAddress
if (-not $Ip) { $Ip = "localhost" }
$World = "ws://localhost:$WorldPort"
$Chan = "ws://localhost:$ChanPort"     # radio/channel.py WebSocket radio (Windows hotspots often block multicast)
"" | Set-Content $PidFile

function Start-Role([string]$Title, [string[]]$PyArgs) {
  $cmd = "`$host.UI.RawUI.WindowTitle = 'ARC $Title'; & '$Py' $($PyArgs -join ' '); Write-Host ''; Write-Host '[$Title exited - press Enter]'; Read-Host"
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

Write-Host "ARC demo  scenario=$Scenario  ip=$Ip"
& $Py data/metar.py

$sc = Get-Content $Scenario -Raw | ConvertFrom-Json

# 1. world (+ web on 8080)
if (Test-Path "world\world_server.py") {
  $wArgs = @("world/world_server.py", "--scenario", $Scenario, "--port", $WorldPort, "--http", $HttpPort)
  if ($Legacy) { $wArgs += "--allow-takeover" }      # old collision-avoidance demo: nodes may fly the aircraft
  if ($sc.traffic) {
    if ($Legacy -and -not $Stubs) { $wArgs += @("--traffic-nodes", $Chan, "--pidfile", (Split-Path $PidFile -Leaf)) }
    if ($Seed -ge 0) { $wArgs += @("--seed", $Seed) }
  }
  Start-Role "world" $wArgs
}
else { Start-Role "world(stub)" @("stubs/fake_world.py", "--scenario", $Scenario, "--port", $WorldPort); Start-Role "web" @("-m", "http.server", $HttpPort, "-d", "web") }
if (-not (Wait-Port $WorldPort)) { Write-Host "World did not open port $WorldPort - check its window." -ForegroundColor Red; exit 1 }

$judges = $sc.aircraft | Where-Object { $_.human -eq $true -and $_.arc -ne $false } | ForEach-Object { $_.id }
if (-not $NoVerify) {
  # 2. onboard verification unit per judge aircraft: receive only, advisory only (verify/unit.py)
  foreach ($id in $judges) { Start-Role "verify $id" @("verify/unit.py", "--id", $id, "--world", $World) }
}
if ($Legacy) {
  # 2. radio channel (legacy collision-avoidance demo)
  if ((Test-Path "radio\channel.py") -and -not $Stubs) { Start-Role "channel" @("radio/channel.py", "--world", $World, "--ws-port", $ChanPort, "--loss", $Loss, "--latency", $Latency); Start-Sleep -Seconds 2 }
  else {
    $chArgs = @("stubs/fake_channel.py", "--world", $World, "--loss", $Loss)
    if ($Spoof) { $chArgs += "--spoof" }            # stub channel injects GHOST7 itself
    Start-Role "channel(stub)" $chArgs
  }

  # 3. one node per ARC aircraft
  $arcIds = $sc.aircraft | Where-Object { $_.arc -ne $false } | ForEach-Object { $_.id }
  if ((Test-Path "node\node.py") -and -not $Stubs) {
    foreach ($id in $arcIds) { Start-Role "node $id" @("node/node.py", "--id", $id, "--world", $World, "--via-channel", $Chan) }
  } else {
    $first = ($sc.aircraft | Where-Object { $_.human -eq $true } | Select-Object -First 1).id
    if (-not $first) { $first = $arcIds[0] }
    Start-Role "node $first(stub)" @("stubs/fake_node.py", "--id", $first, "--world", $World)
  }

  # 4. spoofer
  if ($Spoof) {
    if (Test-Path "radio\spoofer.py") {
      $spArgs = @("radio/spoofer.py", "--mode", $SpoofMode, "--via-channel", $Chan)
      if ($SpoofArgs) { $spArgs += ($SpoofArgs -split '\s+' | Where-Object { $_ }) }
      Start-Role "spoofer" $spArgs
    }
    elseif ((Test-Path "radio\channel.py") -and -not $Stubs) { Write-Host "  (no radio/spoofer.py yet - use .\run_demo.ps1 -Stubs -Spoof)" -ForegroundColor Yellow }
  }
}

# 5. live sky: REAL ADS-B aircraft around KDVT on the god view + log (display only, never sent to nodes).
#    Always on unless -NoLive. Frames are recorded to harness\out\live_*.jsonl; with no internet it replays the newest one.
if (-not $NoLive) { Start-Role "live sky" @("data/live_traffic.py", "--world", $World, "--record") }

if (-not $NoBrowser) { Start-Process "http://localhost:$HttpPort/log.html" }
Write-Host ""
Write-Host "Other laptops (same hotspot):" -ForegroundColor Green
Write-Host "  cockpit A  http://$($Ip):$HttpPort/index.html?role=cockpitA"
Write-Host "  cockpit B  http://$($Ip):$HttpPort/index.html?role=cockpitB"
Write-Host "  god view   http://$($Ip):$HttpPort/index.html?role=god"
Write-Host "  comms log  http://$($Ip):$HttpPort/log.html"
if ($Legacy) { Write-Host "  ARC: collision avoidance + takeover$(if (-not $NoVerify) { ' + verification units (god view attacks)' })"; Write-Host "  nodes on another laptop:  python node/node.py --id <ID> --world ws://$($Ip):$WorldPort --via-channel ws://$($Ip):$ChanPort" }
else { Write-Host "  ARC verification: one onboard unit per judge (verify/unit.py) - god view + Ghost injects a spoofed aircraft" }
Write-Host "  live sky:   python data/live_traffic.py --world ws://localhost:$WorldPort"
if ($sc.traffic) { Write-Host "  live traffic: god view RESET DEMO puts the judges back at their starts" }
Write-Host "Stop everything:  .\run_demo.ps1 -Stop$(if ($WorldPort -ne 8765) { " -WorldPort $WorldPort" })"
Write-Host "Firewall (once, admin PowerShell):  New-NetFirewallRule -DisplayName 'ARC demo' -Direction Inbound -Protocol TCP -LocalPort 8765,8766,8080 -Action Allow"
