`index.html?role=cockpitA|cockpitB|god` — Lane A (Manas). `log.html?role=log` — Lane D (Manav). Plain ES modules, no build step. Served by the world server on :8080 (ES modules do not load from `file://`):

```
python world/world_server.py --scenario harness/scenarios/judges.json
http://<ip>:8080/index.html?role=god
http://<ip>:8080/index.html?role=cockpitA          # cockpitB; &ac=N102 to pick an aircraft
```

Cockpit URL options:
- `&view=2d` — 2D PFD only (no Cesium). The page also stays 2D by itself if Cesium cannot load, or if the browser has no GPU acceleration (software WebGL: "Microsoft Basic Render Driver" / SwiftShader — check `chrome://gpu`; Chrome falls back to it after a GPU-process crash until restarted).
- `&view=3d` — force 3D even without GPU acceleration (lite mode: half resolution, no fog/atmosphere/buildings).
- `&cam=chase` — start in chase view; key **C** or gamepad **Triangle / Y** toggles first-person / chase.
- `&ion=<Cesium ion token>` — once per laptop (kept in localStorage): Cesium World Terrain + imagery + OSM Buildings. Without it: OpenStreetMap imagery on flat ground (heights drawn AGL).
- `&invert=1` — flip gamepad pitch. `&world=ws://<ip>:8765` — world on a different host.

3D needs internet for Cesium (jsDelivr, pinned 1.121.0) and map tiles.

Cockpit controls:
- Gamepad: left stick = bank / pitch, R2 / L2 = throttle, **Triangle / Y** = chase cam, **Cross / A** = autopilot on/off.
- Keyboard: arrows = bank / pitch, W / S = throttle, **C** = chase cam, **A** = autopilot on/off. On-screen **AP** button too.
- Autopilot (ap_equipped aircraft only): in the air it levels, joins the pattern (the leg it is lined up with, else a
  downwind entry at pattern altitude), flies the circuit and lands to a full stop. Pressed again while stopped on the
  runway: takes off and flies another circuit to a stop. Any stick movement disconnects it.
- On the ground: roll steers the nosewheel, throttle up to accelerate, pull back above 55 kt to lift off; throttle to
  idle brakes. Only one cockpit page per aircraft — two pages fight over the controls.
