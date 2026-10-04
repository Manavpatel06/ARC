# ARC — starter repo

Peer-to-peer collision avoidance for general aviation. Devils Invent "Future-Ready Avionics" (Honeywell Aerospace), PS 2. Team: Manav · Manas · Reya · Mansi.

GitHub: https://github.com/Manavpatel06/ARC — **read `CONTRIBUTING.md` for branches and the contract rule before your first commit.**

> **Now: advisory-only traffic VERIFICATION** (`ARC_claude_code_prompt.md`). ARC checks that every aircraft on
> the traffic display really exists — TCAS, Mode S, 1030/1090 timing, signal strength, one-transmitter clusters,
> kinematics, replay — and never flies the aircraft or transmits. Start: `.\run_demo.ps1 -Scenario
> harness\scenarios\live_kdvt.json`; read `verify/README.md`, `docs/architecture.md`, `docs/demo_script.md`;
> results in `eval/out/report.md`. The original collision-avoidance stack below still runs with `-Legacy`.

## Who does what
| Lane | Owner | Branch | Folders | Brief for your AI agent | Task rows |
|---|---|---|---|---|---|
| A — Sim world & views | **Manas** | `lane-a-world` | `world/`, `web/index.html` | `lanes/A-world-manas.md` | `BOARD.md` → Lane A |
| B — Node logic & evidence | **Reya** | `lane-b-node` | `node/`, `harness/` | `lanes/B-node-reya.md` | `BOARD.md` → Lane B |
| C — Radio, protocol, security | **Mansi** | `lane-c-radio` | `radio/` | `lanes/C-radio-mansi.md` | `BOARD.md` → Lane C |
| D — Integration, data, log, pitch | **Manav** | `lane-d-integration` | `data/`, `web/log.html`, `run_demo.*`, `docs/` | `lanes/D-integration-manav.md` | `BOARD.md` → Lane D |

Finished your rows? Take from the **Pull Queue** at the bottom of `BOARD.md`. Blocked? Use the stub in `stubs/` (table at the top of `BOARD.md`) and keep going.

## Read in this order
0. `BOARD.md` — your task rows with times and status; the stub table so nobody waits on anybody.
1. `CONTEXT.md` — what we are building, hard rules, phases, lanes, fixed facts. Paste into every AI session.
2. `INTERFACE.md` + `schemas.py` — the contract between world, nodes, radio, data and views. Frozen; change by agreement only.
3. `PLAN.md` — hour-by-hour plan, status-report acceptance, go/no-go rules.
4. `lanes/<yours>.md` — your lane brief. Paste after CONTEXT.md and INTERFACE.md into your agent.
5. `CONTRIBUTING.md` — branch per lane, PR to main, how contract changes happen.
6. `docs/judge-round-1.md` — what the blind judges docked us for and which lane fixes each item.

## Setup (everyone, 10 minutes)
```
git clone https://github.com/Manavpatel06/ARC.git && cd ARC
git checkout -b lane-<x>-<name>   # see CONTRIBUTING.md
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
All laptops on the same phone hotspot. Note the world server laptop's IP; every process takes `--world ws://<ip>:8765`.

## Run the whole demo with one command
Windows (world laptop): `.\run_demo.ps1` (add `-Spoof` for GHOST7, `-Stubs` to force stand-ins, `-Stop` to end). macOS/Linux: `./run_demo.sh`. It prints the URLs for the other laptops.

## Run order by hand (what run_demo does)
1. `python world/world_server.py --scenario harness/scenarios/judges.json`
2. `python radio/channel.py --world ws://<ip>:8765 --loss 0.1 --latency 0.3`
3. `python node/node.py --id N101 --world ws://<ip>:8765` (one per ARC aircraft; `run_demo.sh` spawns all)
4. Open `web/index.html?role=cockpitA` / `cockpitB` / `god` / `log` on the four laptops.

## Shared building blocks (import, don't rewrite)
`schemas.py` (all messages, contract v1.1) · `pattern.py` (KDVT pattern: `place`, `legs`, ENU) · `data.runways.load()` · `data.metar.load()/climb_fpm()/wind_vector_ms()` · `data.terrain.elev_at_ft()`

## Start working in the next 5 minutes (no other lane needed)
```
python stubs/fake_world.py                 # terminal 1: stand-in world (add --scenario harness/scenarios/judges.json for all 8)
python stubs/fake_node.py --id N101 --speed 4   # terminal 2: stand-in node (ADVISORY ladder, TRUST, COMMAND)
python stubs/tail_log.py                   # terminal 3: see every frame; writes harness/out/*.jsonl
```
Lane B imports `from stubs.loopback_radio import RadioClient` until `radio/client.py` lands (same API).

## Phase 1 status checks
- 1:00 PM: skeleton alive (see PLAN.md row).
- 4:00 PM: loop closes.
- 7:00 PM: Phase 1 green; backup video; go/no-go on Phase 2 and 3.
