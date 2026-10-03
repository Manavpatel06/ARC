# FLOCK — starter repo

Peer-to-peer collision avoidance for general aviation. Devils Invent "Future-Ready Avionics" (Honeywell Aerospace), PS 2. Team: Manav · Manas · Reya · Mansi.

GitHub: https://github.com/Manavpatel06/FLOCK — **read `CONTRIBUTING.md` for branches and the contract rule before your first commit.**

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
git clone https://github.com/Manavpatel06/FLOCK.git && cd FLOCK
git checkout -b lane-<x>-<name>   # see CONTRIBUTING.md
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```
All laptops on the same phone hotspot. Note the world server laptop's IP; every process takes `--world ws://<ip>:8765`.

## Run order (Lane D owns run_demo.sh)
1. `python world/world_server.py --scenario harness/scenarios/judges.json`
2. `python radio/channel.py --world ws://<ip>:8765 --loss 0.1 --latency 0.3`
3. `python node/node.py --id N101 --world ws://<ip>:8765` (one per FLOCK aircraft; `run_demo.sh` spawns all)
4. `python data/camera.py --world ws://<ip>:8765 --observer N311`
5. Open `web/index.html?role=cockpitA` / `cockpitB` / `god` / `log` on the four laptops.

## Start working in the next 5 minutes (no other lane needed)
```
python stubs/fake_world.py                 # terminal 1: stand-in world (OWNSHIP, COMMAND, LOG)
python stubs/fake_node.py --id N101 --speed 4   # terminal 2: stand-in node (ADVISORY ladder, TRUST, COMMAND)
python stubs/tail_log.py                   # terminal 3: see every frame; writes harness/out/*.jsonl
```
Lane B imports `from stubs.loopback_radio import RadioClient` until `radio/client.py` lands (same API).

## Phase 1 status checks
- 1:00 PM: skeleton alive (see PLAN.md row).
- 4:00 PM: loop closes.
- 7:00 PM: Phase 1 green; backup video; go/no-go on Phase 2 and 3.
