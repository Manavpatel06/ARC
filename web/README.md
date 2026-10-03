`index.html?role=cockpitA|cockpitB|god` — Lane A (Manas). `log.html?role=log` — Lane D (Manav). Plain ES modules, no build step. Served by the world server on :8080 (ES modules do not load from `file://`):

```
python world/world_server.py --scenario harness/scenarios/judges.json
http://<ip>:8080/index.html?role=god
http://<ip>:8080/index.html?role=cockpitA          # cockpitB; &ac=N102 to pick an aircraft
```

Cockpit URL options:
- `&view=2d` — 2D PFD only (no Cesium). The page also falls back to 2D by itself if Cesium cannot load.
- `&cam=chase` — start in chase view; key **C** toggles first-person / chase.
- `&ion=<Cesium ion token>` — once per laptop (kept in localStorage): Cesium World Terrain + imagery + OSM Buildings. Without it: OpenStreetMap imagery on flat ground (heights drawn AGL).
- `&invert=1` — flip gamepad pitch. `&world=ws://<ip>:8765` — world on a different host.

3D needs internet for Cesium (jsDelivr, pinned 1.121.0) and map tiles.
