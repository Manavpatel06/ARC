"""
world/tests/test_server.py — Lane A contract tests against a REAL world_server process (docs/interface.md v1.2).
Who receives what, what must never leak, and bad input that must not break the hub.
Run:  python -m pytest -q world/tests/test_server.py
"""
from __future__ import annotations
import asyncio, json, socket, subprocess, sys, time
from pathlib import Path

import pytest
import websockets

ROOT = Path(__file__).resolve().parents[2]

def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    port = free_port()
    log = tmp_path_factory.mktemp("world") / "world.log"      # a file, not a pipe: an unread pipe would block the server
    logf = open(log, "w")
    proc = subprocess.Popen([sys.executable, "-u", "world/world_server.py", "--scenario", "harness/scenarios/judges.json",
                             "--port", str(port), "--http", "0"], cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT, text=True)
    url = f"ws://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield url
    assert proc.poll() is None, "world server died during the tests: " + open(log).read()[-2000:]
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logf.close()

async def collect(url: str, role: str, secs: float, send=None, first_only=False) -> list[dict]:
    out = []
    async with websockets.connect(f"{url}/?role={role}") as ws:
        if send:
            await asyncio.sleep(0.3)
            for m in send:
                await ws.send(m if isinstance(m, str) else json.dumps(m))
        end = time.time() + secs
        while time.time() < end:
            try:
                out.append(json.loads(await asyncio.wait_for(ws.recv(), 0.2)))
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                break
            if first_only:
                break
    return out

def run(coro):
    return asyncio.run(coro)

def types(frames):
    return {f.get("type") for f in frames}

# ------------------------------------------------------------------ identity
def test_hello_first_and_cockpit_ab_mapping(world):
    a = run(collect(world, "cockpitA", 1, first_only=True))[0]
    b = run(collect(world, "cockpit:B", 1, first_only=True))[0]
    assert a["type"] == "HELLO" and a["ac_id"] == "N101" and a["role"] == "cockpit:N101"
    assert b["ac_id"] == "N102"
    assert {x["id"] for x in a["aircraft"]} >= {"N101", "N102"} and "lat" not in a["aircraft"][0]   # ids/flags only

def test_bad_roles_are_rejected(world):
    for role in ("cockpitZ", "node:N999", "hacker"):
        f = run(collect(world, role, 1.5))
        assert f and f[0]["type"] == "ERROR", role

# ------------------------------------------------------------------ what each role receives
def test_node_gets_only_own_ownship_and_env(world):
    f = run(collect(world, "node:N101", 2.2))
    own = [x for x in f if x["type"] == "OWNSHIP"]
    assert {x["ac_id"] for x in own} == {"N101"}
    assert 15 <= len(own) <= 26, f"OWNSHIP rate {len(own) / 2.2:.1f} Hz (want 10)"
    assert any(x["type"] == "ENV" and "da_field_ft" in x for x in f)
    assert not types(f) & {"TRUTH", "LIVE_TRAFFIC", "WX_FIELD", "WORLD_EVENT"}, types(f)

def test_cockpit_gets_only_own_state(world):
    f = run(collect(world, "cockpitB", 2.2))
    own = [x for x in f if x["type"] == "OWNSHIP"]
    assert {x["ac_id"] for x in own} == {"N102"} and len(own) >= 35, len(own)          # ~20 Hz
    assert not types(f) & {"TRUTH", "LIVE_TRAFFIC", "WX_FIELD"}, types(f)

def test_god_gets_truth_for_everyone(world):
    f = run(collect(world, "god", 1.5))
    truth = [x for x in f if x["type"] == "TRUTH"]
    assert truth and {a["ac_id"] for a in truth[-1]["aircraft"]} == {"N101", "N102", "N201", "N202", "N203", "N204", "N311", "N399"}

# ------------------------------------------------------------------ routing
async def _node_and_watchers(world, node_msgs, secs=2.0):
    async def node():
        async with websockets.connect(f"{world}/?role=node:N102") as ws:
            await asyncio.sleep(0.4)
            for m in node_msgs:
                await ws.send(json.dumps(m))
            await asyncio.sleep(secs)
    res = await asyncio.gather(node(), collect(world, "cockpit:N102", secs + 0.6), collect(world, "cockpit:N101", secs + 0.6),
                               collect(world, "god", secs + 0.6), collect(world, "log", secs + 0.6))
    return res[1:]

def test_advisory_goes_to_own_cockpit_god_and_log_only_and_id_is_enforced(world):
    adv = {"type": "ADVISORY", "ac_id": "N101", "t": 0, "layer": 2, "level": "TRAFFIC", "text": "SPOOFED ID"}  # node N102 lies
    ck102, ck101, god, log = run(_node_and_watchers(world, [adv]))
    assert any(x["type"] == "ADVISORY" and x["ac_id"] == "N102" for x in ck102), "remapped to the sender's own aircraft"
    assert not any(x["type"] == "ADVISORY" for x in ck101), "other cockpit must not see it"
    assert any(x["type"] == "ADVISORY" for x in god)
    assert any(x["type"] == "LOG" and x["payload"].get("type") == "ADVISORY" for x in log)

def test_command_always_refused_arc_is_advisory_only(world):
    cmd = {"type": "COMMAND", "ac_id": "N102", "t": 0, "mode": "TAKEOVER", "bank_cmd_deg": -20, "hold_s": 5}
    ck102, _, god, _ = run(_node_and_watchers(world, [cmd]))
    c = [x for x in god if x["type"] == "COMMAND"]
    assert c and c[0]["applied"] is False and c[0].get("rejected_by_world") == "advisory_only"
    last = [x for x in god if x["type"] == "TRUTH"][-1]
    assert all(a["mode"] != "COMMAND" for a in last["aircraft"])

# ------------------------------------------------------------------ ARC onboard unit (avionics role)
def test_avionics_gets_only_own_sensor_data_and_no_ground_truth(world):
    frames = run(collect(world, "avionics:N101", 2.0))
    assert frames[0]["type"] == "HELLO" and "static" not in frames[0]
    sens = [f for f in frames if f["type"] == "SENSORS"]
    assert len(sens) >= 10 and all(f["ac_id"] == "N101" for f in sens)          # ~10 Hz
    msgs = [m for f in sens for m in f["msgs"]]
    kinds = {m["kind"] for m in msgs}
    assert {"ADSB_POSITION", "ADSB_VELOCITY", "OWNSHIP_STATE", "TCAS_TRACK"} <= kinds, kinds
    assert all("label" not in m for m in msgs)                                   # truth never reaches the unit
    assert not {"TRUTH", "GROUND_TRUTH", "ADVISORY", "TRUST"} & types(frames)
    from world.sensors import icao_of
    assert icao_of("N101") not in {m.get("icao") for m in msgs}                   # never hears itself
    assert run(collect(world, "avionics:N201", 1.0))[0]["type"] == "ERROR"        # AI aircraft carry no ARC unit

def test_verify_goes_to_own_cockpit_god_and_log_only(world):
    v = {"type": "VERIFY", "ac_id": "N999", "t": 0, "targets": [
        {"icao": "a1b2c3", "id": "N204", "trust": 97, "state": "VERIFIED", "reasons": ["TCAS confirms it"], "checks": {}}]}
    async def go():
        async def unit():
            async with websockets.connect(f"{world}/?role=avionics:N101") as ws:
                await asyncio.sleep(0.4); await ws.send(json.dumps(v)); await asyncio.sleep(1.0)
        return await asyncio.gather(unit(), collect(world, "cockpitA", 1.8), collect(world, "cockpitB", 1.8),
                                    collect(world, "god", 1.8), collect(world, "log", 1.8))
    _, a, b, god, log = run(go())
    va = [x for x in a if x["type"] == "VERIFY"]
    assert va and va[0]["ac_id"] == "N101" and va[0]["targets"][0]["state"] == "VERIFIED"   # id enforced
    assert "VERIFY" not in types(b)
    assert any(x["type"] == "VERIFY" for x in god)
    assert any(x["type"] == "LOG" and x["payload"].get("type") == "VERIFY" for x in log)

def test_ghost_attack_from_god_shows_in_ground_truth_and_the_victims_adsb(world):
    async def go():
        god = asyncio.create_task(collect(world, "god", 3.0, send=[{"type": "SET_ATTACK", "attack": "ghost", "on": True}]))
        av = asyncio.create_task(collect(world, "avionics:N101", 3.0))
        return await god, await av
    god, av = run(go())
    on = [x for x in god if x.get("event") == "ATTACK_ON"]
    assert on and on[0]["attack"]["kind"] == "ghost"
    icao = on[0]["attack"]["icao"]
    gt = [x for x in god if x["type"] == "GROUND_TRUTH"][-1]
    assert any(e["icao"] == icao and e["label"] == "ghost" for e in gt["emitters"])
    msgs = [m for f in av if f["type"] == "SENSORS" for m in f["msgs"]]
    assert any(m["kind"] == "ADSB_POSITION" and m["icao"] == icao for m in msgs)          # it broadcasts ADS-B ...
    assert not any(m["kind"] in ("TCAS_TRACK", "MODES_REPLY") and m["icao"] == icao for m in msgs)  # ... and nothing else
    off = run(collect(world, "god", 1.5, send=[{"type": "SET_ATTACK", "attack": "ghost", "on": False}]))
    assert any(x.get("event") == "ATTACK_OFF" for x in off)

def test_live_traffic_reaches_god_and_log_never_nodes_or_cockpits(world):
    lt = {"type": "LIVE_TRAFFIC", "t": time.time(), "source": "test", "radius_nm": 25,
          "aircraft": [{"id": "abc123", "callsign": "TEST1", "lat": 33.7, "lon": -112.08, "alt_msl_ft": 3000}]}
    async def go():
        async def data():
            async with websockets.connect(f"{world}/?role=data") as ws:
                await asyncio.sleep(0.5); await ws.send(json.dumps(lt)); await asyncio.sleep(1.5)
        return await asyncio.gather(data(), collect(world, "god", 2.2), collect(world, "log", 2.2),
                                    collect(world, "node:N101", 2.2), collect(world, "cockpitA", 2.2))
    _, god, log, node, ck = run(go())
    assert any(x["type"] == "LIVE_TRAFFIC" for x in god)
    assert any(x["type"] == "LOG" and x["payload"].get("type") == "LIVE_TRAFFIC" for x in log)
    assert "LIVE_TRAFFIC" not in types(node) | types(ck)

def test_set_da_reaches_every_node(world):
    async def go():
        async def god():
            async with websockets.connect(f"{world}/?role=god") as ws:
                await asyncio.sleep(0.6); await ws.send(json.dumps({"type": "SET_DA", "ft": 7777})); await asyncio.sleep(1)
        return await asyncio.gather(god(), collect(world, "node:N201", 2.0), collect(world, "node:N311", 2.0))
    _, n1, n2 = run(go())
    for n in (n1, n2):
        assert 7777 in [x.get("da_field_ft") for x in n if x["type"] == "ENV"]

# ------------------------------------------------------------------ bad input must not break the hub
def test_garbage_and_bad_values_do_not_break_the_hub(world):
    junk = ["not json", "[1,2,3]", "null", json.dumps({"type": "INPUT", "roll": "abc"}),
            json.dumps({"type": "SET_WX", "visibility_sm": "abc", "turbulence": 99, "qnh_inhg": None}),
            json.dumps({"type": "SET_WX", "preset": "no_such_preset"}),
            json.dumps({"type": "SET_DA", "ft": "lots"}), json.dumps({"type": 42})]
    for role in ("god", "cockpitA", "node:N204"):
        run(collect(world, role, 0.8, send=junk))
    f = run(collect(world, "god", 1.0))                          # hub still serving
    assert any(x["type"] == "TRUTH" for x in f)

def test_input_cannot_fly_someone_elses_aircraft(world):
    async def go():
        async def pilot():
            async with websockets.connect(f"{world}/?role=cockpitB") as ws:
                await asyncio.sleep(0.3)
                for _ in range(40):
                    await ws.send(json.dumps({"type": "INPUT", "ac_id": "N101", "roll": 1.0, "pitch": 0, "throttle": 0.5}))
                    await asyncio.sleep(1 / 30)
        return await asyncio.gather(pilot(), collect(world, "god", 2.0))
    _, god = run(go())
    last = [x for x in god if x["type"] == "TRUTH"][-1]
    modes = {a["ac_id"]: a["mode"] for a in last["aircraft"]}
    assert modes["N102"] == "HUMAN" and modes["N101"] != "HUMAN", modes

# ------------------------------------------------------------------ live traffic (live_kdvt.json)
@pytest.fixture(scope="module")
def live_world(tmp_path_factory):
    port = free_port()
    log = tmp_path_factory.mktemp("live") / "world.log"
    logf = open(log, "w")
    proc = subprocess.Popen([sys.executable, "-u", "world/world_server.py", "--scenario", "harness/scenarios/live_kdvt.json",
                             "--port", str(port), "--http", "0", "--seed", "3"], cwd=ROOT, stdout=logf,
                            stderr=subprocess.STDOUT, text=True)
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    yield f"ws://127.0.0.1:{port}"
    assert proc.poll() is None, "live world died: " + open(log).read()[-2000:]
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
    logf.close()

def test_live_traffic_in_truth_and_static(live_world):
    god = run(collect(live_world, "god", 2.0))
    st = god[0]["static"]
    assert st["live_traffic"] and st["flow"] in ("07", "25") and st["judges"] == ["N101", "N102"]
    assert len(st["patterns"]) == 2                                    # both active patterns drawn
    ai = {a["ac_id"] for f in god if f["type"] == "TRUTH" for a in f["aircraft"] if not a["human"]}
    assert len(ai) >= 4, ai

def test_ai_pilot_hears_its_node_and_logs_the_decision(live_world):
    truth = [f for f in run(collect(live_world, "god", 1.0)) if f["type"] == "TRUTH"][-1]
    ai = sorted(a["ac_id"] for a in truth["aircraft"] if not a["human"] and not a["on_ground"])[0]
    adv = {"type": "ADVISORY", "ac_id": ai, "t": truth["t"], "layer": 2, "level": "RESOLVE",
           "text": "TURN RIGHT 20 - N101 TURNING RIGHT", "reason": {"chosen": "R20"}}

    async def both():
        logs = asyncio.create_task(collect(live_world, "log", 2.5))
        await asyncio.sleep(0.5)
        await collect(live_world, f"node:{ai}", 0.5, send=[adv])
        return await logs
    dec = [f["payload"] for f in run(both()) if f.get("kind") == "decision" and f["payload"].get("ai_pilot") == ai]
    assert dec and dec[0]["action"] == ["TURN", 20.0] and isinstance(dec[0]["follow"], bool), dec

def test_reset_demo_only_from_god_and_puts_judges_at_their_starts(live_world):
    ignored = run(collect(live_world, "cockpitA", 1.0, send=[{"type": "RESET_DEMO"}]))
    assert not [f for f in ignored if f.get("event") in ("RESET", "RESET_DEMO")]
    god = run(collect(live_world, "god", 1.5, send=[{"type": "RESET_DEMO"}]))
    resets = [f for f in god if f.get("event") == "RESET"]
    demo = [f for f in god if f.get("event") == "RESET_DEMO"]
    assert {f["a"] for f in resets} == {"N101", "N102"} and demo and demo[0]["aircraft"] == ["N101", "N102"]
    lat = {f["a"]: f["lat"] for f in resets}
    assert lat["N101"] > 33.75 and lat["N102"] < 33.63                   # 4 NM north / 4 NM south of KDVT
    truth = [f for f in god if f["type"] == "TRUTH"][-1]
    j = {a["ac_id"]: a for a in truth["aircraft"] if a["human"]}
    assert all(a["mode"] == "AUTOPILOT" and abs(a["alt_msl_ft"] - 2500) < 40 for a in j.values()), j

def test_signed_verify_link_pins_the_units_key_and_drops_forgeries(world, tmp_path):
    from verify.eventlog import load_key, sign_frame
    good = load_key("UNIT_A", key_dir=str(tmp_path)); evil = load_key("EVIL", key_dir=str(tmp_path))
    v = lambda st: {"type": "VERIFY", "ac_id": "N102", "t": 0, "targets": [
        {"icao": "a1b2c3", "id": "N204", "trust": 5, "state": st, "reasons": ["x"], "checks": {}}]}
    first = sign_frame(v("SUSPECT"), good)
    forged = dict(sign_frame(v("SUSPECT"), good), targets=[dict(v("SUSPECT")["targets"][0], state="VERIFIED")])
    rekeyed = sign_frame(v("VERIFIED"), evil)
    async def go():
        async def unit():
            async with websockets.connect(f"{world}/?role=avionics:N102") as ws:
                await asyncio.sleep(0.4)
                for f in (first, forged, rekeyed, {k: x for k, x in v("VERIFIED").items()}):
                    await ws.send(json.dumps(f)); await asyncio.sleep(0.2)
                await asyncio.sleep(0.6)
        return await asyncio.gather(unit(), collect(world, "cockpitB", 2.2), collect(world, "log", 2.2))
    _, ck, log = run(go())
    got = [x for x in ck if x["type"] == "VERIFY"]
    assert len(got) == 1 and got[0]["targets"][0]["state"] == "SUSPECT" and got[0]["link"] == "signed"
    assert "sig" not in got[0] and "pub" not in got[0]
    why = sorted(x["payload"]["why"] for x in log if x.get("payload", {}).get("type") == "VERIFY_REJECTED")
    assert why == ["bad signature", "key changed", "unsigned"], why

def test_god_sets_playback_speed(world):
    god = run(collect(world, "god", 1.0, send=[{"type": "SET_SPEED", "time_scale": 2}]))
    assert any(x.get("event") == "SPEED" and x["time_scale"] == 2 for x in god)
    run(collect(world, "god", 0.6, send=[{"type": "SET_SPEED", "time_scale": 1}]))
