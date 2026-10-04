# ARC — Status report template (1 PM and 7 PM)

Copy this file to `docs/status-<time>.md`, fill the `<...>`, keep it to one page. Quote numbers from the acceptance scripts, not from memory.

**One line:** <what ARC is, one sentence, same words as last time>

## Working right now (demo on request)
- **World / views** (Manas): <what runs, on which laptop>
- **Contract / data / log** (Manav): <what runs>
- **Node logic** (Reya): <paste the PASS lines from `python harness/accept_b.py`>
- **Radio / trust** (Mansi): <paste the PASS lines from `python radio/accept_c.py`>

## Numbers we can stand behind
| Claim | Number | Where it comes from |
|---|---|---|
| NMAC rate, no avoidance / straight-line baseline / ARC | <..> | `python harness/montecarlo.py` (simulation, say so) |
| Warning lead time | <..> s | same |
| Nuisance alerts on benign encounters | <..> % | same |
| Per-tick compute | <..> ms | `accept_b.py` |

## Not working / not claimed
- <anything red, with one line why; things cut stay cut>

## Next gate
<the next acceptance row from BOARD.md, with its time>

## Risks we are managing
- <Wi-Fi / hotspot status and last test time>
- <scope: what was cut and why>
