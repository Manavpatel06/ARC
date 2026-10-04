# ARC live run — 4 laptops in sync (runbook)

One world, one radio channel, every aircraft's ARC node, two judge cockpits, one god view, one comms log.
Everything talks to ONE world server, so every screen shows the same sky at the same moment.

## 0. Before you start (once)
- Everyone: `git fetch && git checkout integration && git pull`, then `.venv\Scripts\activate` (Windows) and `pip install -r requirements.txt`.
- World laptop (Manav): Windows **Mobile hotspot ON** (name ARC). The other three join ARC Wi-Fi.
  World laptop address on its own hotspot is always **192.168.137.1**.
- World laptop firewall, admin PowerShell, once:
  `New-NetFirewallRule -DisplayName "ARC" -Direction Inbound -Protocol TCP -LocalPort 8765,8766,8080 -Action Allow`

## 1. Who runs what
| Laptop | Person | Runs | Opens in the browser |
|---|---|---|---|
| 1 World + projector | Manav | `.\run_demo.ps1 -Scenario harness\scenarios\head_on_judges.json` (world, channel, every ARC node) | `http://localhost:8080/index.html?role=god` on the projector |
| 2 Cockpit A + controller 1 | Manas | nothing | `http://192.168.137.1:8080/index.html?role=cockpitA` → Start, plug in PS controller |
| 3 Cockpit B + controller 2 | Reya | nothing | `http://192.168.137.1:8080/index.html?role=cockpitB` → Start, plug in PS controller |
| 4 Comms log | Mansi | optional terminal view: `python radio/watch.py --world ws://192.168.137.1:8765 --no-state` | `http://192.168.137.1:8080/log.html` |

Live sky (real aircraft around KDVT) on laptop 1, separate window: `python data/live_traffic.py --world ws://localhost:8765`

## 2. Sync check (60 seconds, before every run)
1. Laptops 2-4: `python stubs/ping_world.py --world ws://192.168.137.1:8765` → `TRUTH frames in 5 s: ≥ 40 (OK)`.
2. God view shows the same aircraft list as each cockpit's HELLO (cockpit A = N101, B = N102 in head_on_judges / judges).
3. Move controller 1: the N101 symbol turns on the god view within a second; the log shows nothing red.
4. Log page header: link loss ≈ the configured 10 %, mean latency ≈ 300 ms.

## 3. The runs that make Phase 1 GREEN
| # | Scenario (`-Scenario`) | Who flies | Pass when |
|---|---|---|---|
| 1 | `head_on_judges.json` | nobody touches the sticks | SEQUENCE → TRAFFIC → RESOLVE → TAKEOVER (both) → RELEASE → CLEAR in the log; god view truth "0 NMAC" |
| 2 | `head_on_judges.json` | both judges fly, ignore advisories | takeover happens on the AP aircraft; moving the stick gives control back within 1 s ("STICK" in log) |
| 3 | `three_on_final.json` | nobody | three-way sequencing + commits in the log; 0 NMAC (without ARC it is a 48 ft near miss) |
| 4 | `base_vs_straight_in.json` | nobody | N101 climbs / go-around, N399 continues (right-of-way), 0 NMAC (was a 433 ft NMAC before GO_AROUND) |
| 5 | `live_kdvt.json` (unscripted: judges start 4 NM north and south, 5-8 AI aircraft that run ARC, live weather) | both judges fly freely for 3-4 min | no crashes, views stay in sync, every alert has a reason in the explain panel, 0 NMAC. God view RESET DEMO restarts it; `-Seed 11` gives different traffic |
Record runs 1, 3 and 5 with OBS / Win+G on laptop 1 (god view + log side by side): that is the backup video.

## 4. If something drops
- A cockpit freezes: refresh the page (it reconnects; the world keeps flying).
- Log page empty: check its URL uses 192.168.137.1, not localhost.
- Nodes not alerting: is the `channel` window on laptop 1 alive? `.\run_demo.ps1 -Stop`, then start again.
- Everything dies: `.\run_demo.ps1 -Stop` → start again (≈ 20 s). Worst case: play the backup video.

## Known limits (say them if asked)
- AI aircraft do not follow advisories; only the AP-equipped aircraft is moved (by a bounded takeover). Judges' aircraft follow their pilots.
- Head-on robustness (Mansi, radio/ROBUSTNESS.md): median ~850 ft from 0 % to 50 % loss, but ~1 run in 5 still ends inside 500 ft; takeover/commit ordering being fixed.
- Live traffic: AI aircraft far from the pattern are often UNKNOWN leg -> ARC falls back to straight-line prediction for them (by design, like TCAS).
