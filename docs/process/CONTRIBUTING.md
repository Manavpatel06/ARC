# How we work in this repo (read once, 3 minutes)

## Branches = lanes
| Lane | Owner | Branch | Folder you own |
|---|---|---|---|
| A — Sim world & views | Manas | `lane-a-world` | `world/`, `web/index.html` (cockpit + god views) |
| B — Node logic | Reya | `lane-b-node` | `node/`, `harness/montecarlo.py` |
| C — Radio, protocol, security | Mansi | `lane-c-radio` | `radio/` |
| D — Integration, data, log, camera, pitch | Manav | `lane-d-integration` | `data/`, `web/log.html`, `run_demo.*`, `docs/` |

Rules:
1. Work on your lane branch. Commit often (every working step), push at least every hour and **before each status report (1 PM, 7 PM)**.
2. Merge to `main` through a pull request. Anyone can approve; the owner of the folder you touched should look. Merge your own PR once it runs.
3. **Do not edit `schemas.py` or `docs/interface.md` alone.** Propose the change in the team chat, get a "yes" from the lanes it affects, then one person commits it to `main` directly with message `contract: <what changed>`. Everyone rebases after.
4. Only touch files outside your folder when the owner knows. Shared files: `requirements.txt` (add, never remove), `harness/scenarios/*.json` (add new scenarios freely).
5. `main` must always run: `python world/world_server.py --scenario harness/scenarios/judges.json` should start without a traceback. If you break main, fix it before anything else.

## Status lives in `BOARD.md`
Mark your row `[~]` when you start, `[x]` when the acceptance test passes, `[!]` + one line if blocked — in the same commit as the code. Then pick the next row; if your list is empty, take from the Pull Queue at the bottom of BOARD.md. Don't wait on another lane: every dependency has a stand-in in `stubs/` (see the table at the top of BOARD.md and the dependency map in PLAN.md).

## Daily loop
```
git checkout lane-x-name
git pull --rebase origin main        # pick up contract changes and others' merges
# ... work ...
git add -A && git commit -m "lane X: <what works now>"
git push -u origin lane-x-name
# open PR → main when a milestone passes its acceptance test
```

## Using your AI agent
Paste, in this order: `CONTEXT.md`, `docs/interface.md`, `lanes/<yours>.md`. Then tell it which acceptance test (1 PM / 4 PM / 7 PM) you are working toward. Keep `schemas.py` open in the agent's context so it imports the real models instead of inventing fields.

## Commit message prefixes
`world:` `node:` `radio:` `data:` `web:` `harness:` `contract:` `docs:` `demo:`
