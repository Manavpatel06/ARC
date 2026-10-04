// web/livesky.js — Lane D. REAL aircraft (LIVE_TRAFFIC, data/live_traffic.py) in the cockpit:
// cyan targets on the nav-map radar and in the 3D out-the-window view, like ADS-B In traffic in a real
// GA cockpit. Display only: FLOCK nodes never see these aircraft and never maneuver for them
// (they carry no FLOCK radio), so they never raise an advisory.
//
// The feed polls every ~5 s; positions are dead-reckoned from each report (ground speed, track,
// vertical speed) so the targets move smoothly in between. Older than LIVE_HIDE_MS -> hidden.
import { css, drawPlane, NM, SANS } from "./net.js";
import { AIRCRAFT_MODEL, MODEL_SCALE } from "./cesium3d.js";

const D2R = Math.PI / 180;
const LIVE_STALE_MS = 20000, LIVE_HIDE_MS = 60000;
const DR_MAX_S = 30;                 // never dead-reckon further than this
const MODEL_WITHIN_M = 8 * NM;       // 3D: aircraft model (visible face to face) inside this range, label beyond
const MODEL_HDG_OFFSET = -90;        // Cesium_Air.glb nose points along +X (same as cockpit.js)

export function liveState() { return { m: null, at: 0 }; }
export function onLive(st, m) { st.m = m; st.at = performance.now(); }

/** Where each real aircraft is now (dead-reckoned), with range/bearing/relative altitude from own. */
export function livePositions(st, own) {
  if (!st.m || !own) return [];
  const ageMs = performance.now() - st.at;
  if (ageMs > LIVE_HIDE_MS) return [];
  const out = [];
  const kx0 = 111320 * Math.cos(own.lat * D2R);
  for (const a of st.m.aircraft || []) {
    if (a.lat == null || a.lon == null) continue;
    const dt = Math.min(DR_MAX_S, ageMs / 1000 + (a.age_s || 0));
    const v = a.on_ground ? 0 : (a.gs_kt || 0) * 0.514444, trk = (a.track_deg || 0) * D2R;
    const lat = a.lat + (v * dt * Math.cos(trk)) / 111320;
    const lon = a.lon + (v * dt * Math.sin(trk)) / (111320 * Math.cos(a.lat * D2R));
    const alt = (a.alt_msl_ft || 0) + (a.on_ground ? 0 : (a.vs_fpm || 0) * dt / 60);
    const e = (lon - own.lon) * kx0, n = (lat - own.lat) * 111320;
    out.push({ ...a, lat, lon, alt_ft: alt, rng_m: Math.hypot(e, n), brg_deg: (Math.atan2(e, n) / D2R + 360) % 360,
               dalt_ft: alt - (own.alt_msl_ft ?? alt), stale: ageMs > LIVE_STALE_MS });
  }
  return out;
}

/** Nav-map radar: cyan chevrons with a track line and "CALLSIGN 1.2 NM +05" labels, ADS-B style. */
export function drawLiveOnMap(ctx, list, g) {
  const { fromLL, cx, cy, R, up, k, label } = g;
  if (!list.length) return;
  const col = css("var(--live)");
  let shown = 0;
  for (const a of list) {
    const [x, y] = fromLL(a.lat, a.lon);
    if (Math.hypot(x - cx, y - cy) > R * 1.08) continue;            // outside the selected range: not drawn
    shown++;
    ctx.globalAlpha = a.stale ? 0.4 : 0.95;
    const rot = ((a.track_deg || 0) - up) * D2R, sz = 9 * k;
    ctx.fillStyle = col; ctx.strokeStyle = col; ctx.lineWidth = 1.6 * k;
    drawPlane(ctx, x, y, rot, sz, !a.on_ground);                    // same airplane symbol as FLOCK traffic, in cyan
    if (!a.on_ground && a.gs_kt > 30) {                             // track line ahead of the nose
      ctx.beginPath(); ctx.moveTo(x + Math.sin(rot) * sz * 1.2, y - Math.cos(rot) * sz * 1.2);
      ctx.lineTo(x + Math.sin(rot) * (sz * 1.2 + 18 * k), y - Math.cos(rot) * (sz * 1.2 + 18 * k)); ctx.stroke();
    }
    const d = Math.round((a.dalt_ft || 0) / 100);
    const arrow = a.vs_fpm > 300 ? "↑" : a.vs_fpm < -300 ? "↓" : "";
    const name = (a.callsign || a.id || "").trim().slice(0, 8);
    label(ctx, x + 9 * k, y + 4 * k, a.on_ground ? `${name} GND` :
          `${name} ${(a.rng_m / NM).toFixed(1)} NM ${d >= 0 ? "+" : "-"}${String(Math.abs(d)).padStart(2, "0")}${arrow}`, 10.5 * k, "left", col);
    ctx.globalAlpha = 1;
  }
  label(ctx, 10 * k, g.footerY, `ADS-B: ${shown} real aircraft (display only)`, 10.5 * k, "left", col);
}

/** 3D view: a model + callsign for each real airborne aircraft (relative to own height, like FLOCK targets). */
export function updateLive3D(v3, list, p, h) {
  const C = v3.Cesium, viewer = v3.viewer;
  v3.live = v3.live || new Map();
  const col = C.Color.fromCssColorString(css("var(--live)"));
  const seen = new Set();
  for (const a of list) {
    if (a.on_ground || a.rng_m > 15 * NM) continue;
    const id = `live:${a.id}`;
    seen.add(id);
    const pos = C.Cartesian3.fromDegrees(a.lon, a.lat, h + (a.dalt_ft || 0) * 0.3048);
    const near = a.rng_m < MODEL_WITHIN_M;
    let e = v3.live.get(id);
    if (!e || e.__near !== near) {
      if (e) viewer.entities.remove(e);
      e = viewer.entities.add({
        model: near ? { uri: AIRCRAFT_MODEL, scale: MODEL_SCALE, minimumPixelSize: 28, color: col,
                        colorBlendMode: C.ColorBlendMode.MIX, colorBlendAmount: 0.35 } : undefined,
        point: near ? undefined : { pixelSize: 8, color: col, outlineColor: C.Color.BLACK, outlineWidth: 2 },
        label: { text: "", font: `600 13px ${SANS}`, fillColor: col, outlineColor: C.Color.BLACK, outlineWidth: 3,
                 style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -20) },
      });
      e.__near = near;
      v3.live.set(id, e);
    }
    e.position = pos;
    if (near) {
      const hpr = new C.HeadingPitchRoll(C.Math.toRadians((a.track_deg || 0) + MODEL_HDG_OFFSET), 0, 0);
      e.orientation = C.Transforms.headingPitchRollQuaternion(pos, hpr);
    }
    const d = Math.round((a.dalt_ft || 0) / 100);
    e.label.text = `${(a.callsign || a.id || "").trim().slice(0, 8)} ${d >= 0 ? "+" : "-"}${String(Math.abs(d)).padStart(2, "0")}`;
  }
  for (const [id, e] of v3.live) if (!seen.has(id)) { viewer.entities.remove(e); v3.live.delete(id); }
}
