"""
verify/ - FLOCK onboard traffic VERIFICATION (advisory only, receive only).

Answers one question per aircraft on the traffic display: does it really exist where it claims to be?
It cross-checks each target's ADS-B claims against independent evidence the aircraft already receives
(own TCAS, Mode S replies to ground radar, physics) and fuses the results into a trust score with
plain-English reasons. It never commands a maneuver, never talks to the autopilot, never transmits.

  schema.py      normalized sensor message + VERIFY output (pydantic)
  tracks.py      per-aircraft track manager, keyed by ICAO 24-bit address
  checks/        one module per check: check(track, ctx, cfg) -> CheckResult (score None = no data)
  fusion.py      weighted log-odds + smoothing -> trust 0-100, VERIFIED / UNVERIFIED / SUSPECT
  guardrails.py  the rules that may never be broken (TCAS-confirmed is never SUSPECT, ...)
  unit.py        the onboard unit process (world role avionics:<id>) and the pure VerifyUnit class
  config.json    every threshold and weight
"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)
