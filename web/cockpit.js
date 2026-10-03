// web/cockpit.js — Lane A. Cockpit view (?role=cockpitA | cockpitB | cockpit:<id>).
// Knows only what the world sends a cockpit: its own aircraft's state and its own node's
// ADVISORY / TRUST / COMMAND / STICK. No other aircraft's truth ever reaches this page.

import { connect, css, LEVEL_COLOR, MONO, NM, SANS, TRUST_COLOR } from "./net.js";
import { startInput } from "./input.js";
import { sayAdvisory } from "./voice.js";
import { AIRCRAFT_MODEL, gpuInfo, heightM, Interp, loadCesium, makeViewer, offsetLL } from "./cesium3d.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;

export function startCockpit(role) {
  document.body.classList.add("is-cockpit");
  $("cockpit").hidden = false;
  const s = { own: null, acId: null, adv: null, advT: 0, trust: null, cmd: null, stickT: -Infinity, link: "connecting",
              fieldElevFt: 1478, interp: new Interp(), v3: null };

  const link = connect(role, (m) => {
    switch (m.type) {
      case "HELLO":
        s.acId = m.ac_id;
        $("ck-id").textContent = m.ac_id;
        document.title = `FLOCK cockpit ${m.ac_id}`;
        if (m.static && m.static.airport) s.fieldElevFt = m.static.airport.elev_ft;
        break;
      case "OWNSHIP": s.own = m; s.interp.push(m); break;
      case "ADVISORY":
        s.adv = m; s.advT = performance.now();
        sayAdvisory(m);
        break;
      case "TRUST": s.trust = m; break;
      case "COMMAND": s.cmd = m; break;
      case "STICK": s.stickT = performance.now(); break;
    }
  }, (status, detail) => {
    s.link = status;
    $("ck-link").textContent = status === "open" ? "LINK" : status.toUpperCase();
    $("ck-link").dataset.state = status;
    if (status === "error") $("ck-link").title = detail;
  });

  const inp = startInput((v) => s.acId && link.send({ type: "INPUT", ac_id: s.acId, ...v }));

  // 3D unless ?view=2d. Without GPU acceleration 3D stutters (hundreds of ms per frame), so the
  // page stays 2D and says why; ?view=3d forces it anyway in lite mode.
  const view = new URLSearchParams(location.search).get("view");
  const gpu = gpuInfo();
  if (gpu.software) console.warn("[3d] software WebGL renderer:", gpu.renderer);
  if (view === "3d" || (view !== "2d" && !gpu.software)) start3D(s, gpu.software);
  else if (view !== "2d") $("ck-3d-note").textContent = "2D: browser has no GPU acceleration (see chrome://gpu) · &view=3d to force";

  const pfd = $("ck-pfd"), tfc = $("ck-traffic");
  const frame = () => {
    fit(pfd); fit(tfc);
    if (s.v3) update3D(s);
    const chase = !!(s.v3 && s.v3.chase);
    drawPFD(pfd, s.own, !!s.v3, s.v3 && !chase ? s.v3.viewer.camera.frustum.fovy : null, !chase);
    drawTraffic(tfc, s.own, s.trust, s.adv);
    renderBanner(s);
    renderBounds(s);
    renderTrust(s);
    renderStatus(s, inp);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// ---------- 3D out-the-window view (synthetic vision) ----------
// Camera = own aircraft (first person) or behind it (chase, key C). Traffic is drawn only where
// this aircraft's node says it is (TRUST rel), never from world truth.
const MODEL_HDG_OFFSET = -90;   // Cesium_Air.glb nose points along +X; heading 0 = north

async function start3D(s, lite = false) {
  const note = $("ck-3d-note");
  note.textContent = "loading 3D...";
  try {
    const Cesium = await loadCesium();
    const { viewer, terrain } = await makeViewer(Cesium, $("ck-3d"), { lite });
    viewer.scene.screenSpaceCameraController.enableInputs = false;   // the sim flies the camera
    s.v3 = { Cesium, viewer, terrain, chase: new URLSearchParams(location.search).get("cam") === "chase",
             own: null, targets: new Map() };
    window.__flock3d = s.v3;                                         // console debugging
    note.textContent = (lite ? "3D lite (no GPU acceleration) · " : "")
      + (terrain ? "3D: Cesium World Terrain · © Cesium ion · C = chase cam"
                 : "3D: flat · © OpenStreetMap contributors · C = chase cam · add ?ion=<token> for terrain");
    addEventListener("keydown", (e) => { if (e.code === "KeyC" && s.v3) s.v3.chase = !s.v3.chase; });
  } catch (e) {
    console.warn("[3d]", e);
    note.textContent = `2D only: ${e.message}`;
  }
}

function update3D(s) {
  const { Cesium: C, viewer, terrain } = s.v3;
  const p = s.interp.sample();
  if (!p) return;
  const h = heightM(p.alt_msl_ft, terrain, s.fieldElevFt);
  const pos = C.Cartesian3.fromDegrees(p.lon, p.lat, h);
  const fpa = Math.atan2(p.vs_fpm, Math.max(p.gs_kt, 30) * 101.27) / D2R;

  if (!s.v3.own) s.v3.own = viewer.entities.add({ model: { uri: AIRCRAFT_MODEL, minimumPixelSize: 48 } });
  const hpr = new C.HeadingPitchRoll(C.Math.toRadians(p.hdg_deg + MODEL_HDG_OFFSET), C.Math.toRadians(fpa), C.Math.toRadians(p.bank_deg));
  s.v3.own.position = pos;
  s.v3.own.orientation = C.Transforms.headingPitchRollQuaternion(pos, hpr);
  s.v3.own.show = s.v3.chase;

  if (s.v3.chase) {
    const [blat, blon] = offsetLL(p.lat, p.lon, p.track_deg + 180, 70);
    viewer.camera.setView({
      destination: C.Cartesian3.fromDegrees(blon, blat, h + 18),
      orientation: { heading: C.Math.toRadians(p.track_deg), pitch: C.Math.toRadians(-12), roll: 0 },
    });
  } else {
    viewer.camera.setView({
      destination: C.Cartesian3.fromDegrees(p.lon, p.lat, h + 1.5),
      orientation: { heading: C.Math.toRadians(p.hdg_deg), pitch: C.Math.toRadians(fpa), roll: C.Math.toRadians(p.bank_deg) },
    });
  }

  // node-reported traffic (TRUST rel) as 3D markers
  const seen = new Set();
  for (const t of (s.trust && s.trust.targets) || []) {
    if (!t.rel || t.state === "CAMERA_ONLY") continue;
    seen.add(t.id);
    const [lat, lon] = offsetLL(p.lat, p.lon, t.rel.brg_deg, t.rel.rng_m);
    const th = heightM(p.alt_msl_ft + (t.rel.dalt_ft || 0), terrain, s.fieldElevFt);
    const col = C.Color.fromCssColorString(css(TRUST_COLOR[t.state] || "#fff"));
    let e = s.v3.targets.get(t.id);
    if (!e) {
      e = viewer.entities.add({
        point: { pixelSize: 12, color: col, outlineColor: C.Color.BLACK, outlineWidth: 2 },
        label: { text: t.id, font: `600 14px ${SANS}`, fillColor: col, outlineColor: C.Color.BLACK, outlineWidth: 3,
                 style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -18) },
      });
      s.v3.targets.set(t.id, e);
    }
    e.position = C.Cartesian3.fromDegrees(lon, lat, th);
    e.point.color = t.state === "FAKE" ? C.Color.TRANSPARENT : col;     // FAKE: hollow
    e.point.outlineColor = t.state === "FAKE" ? col : C.Color.BLACK;
    e.label.fillColor = col;
  }
  for (const [id, e] of s.v3.targets) if (!seen.has(id)) { viewer.entities.remove(e); s.v3.targets.delete(id); }
}

function fit(cv) {
  const r = cv.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
}

// ---------- advisory banner ----------
function renderBanner(s) {
  const el = $("ck-adv"), now = performance.now();
  let level = "NONE", text = "NO ADVISORY", sub = "";
  if (now - s.stickT < 4000) {
    level = "RELEASE"; text = "YOUR AIRCRAFT"; sub = "stick input - FLOCK released control";
  } else if (s.adv && !(s.adv.level === "CLEAR" && now - s.advT > 6000)) {
    level = s.adv.level; text = s.adv.text;
    const bits = [];
    if (s.adv.target_id) bits.push(s.adv.target_id);
    if (s.adv.ttc_s != null) bits.push(`ttc ${Math.round(s.adv.ttc_s)} s`);
    const r = s.adv.reason || {};
    if (r.predicted_miss_ft != null) bits.push(`miss ${Math.round(r.predicted_miss_ft)} ft`);
    if (r.method) bits.push(`${r.method}${r.confidence != null ? ` ${(+r.confidence).toFixed(2)}` : ""}`);
    sub = bits.join(" · ");
  }
  if (el.dataset.level !== level) el.dataset.level = level;
  $("ck-adv-text").textContent = text;
  $("ck-adv-sub").textContent = sub;
}

// ---------- printed takeover bounds (judge fix #3) ----------
function renderBounds(s) {
  const el = $("ck-bounds");
  const c = s.own && s.own.cmd;
  el.hidden = !c;
  if (!c) return;
  const b = c.bounds || {};
  const bank = +c.bank_cmd_deg || 0;
  const side = bank < 0 ? "L" : bank > 0 ? "R" : "";
  $("ck-bounds-body").innerHTML = [
    ["Command", `bank ${side}${Math.abs(bank).toFixed(0)}° · vs ${Math.round(c.vs_cmd_fpm || 0)} fpm`],
    ["Max bank", `${b.max_bank_deg ?? 30}°`],
    ["Speed floor", `${b.min_ias_kt ?? 62} kt (1.3 Vs)`],
    ["Altitude floor", `${b.min_agl_ft ?? 300} ft AGL`],
    ["Authority", `≤ ${b.max_hold_s ?? 10} s · ${Math.max(0, c.left_s).toFixed(1)} s left`],
  ].map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("")
    + `<p>Any stick input returns control immediately.</p>`;
}

// ---------- trust badges ----------
function renderTrust(s) {
  const el = $("ck-trust");
  const targets = (s.trust && s.trust.targets) || [];
  const html = targets.map((t) =>
    `<li data-state="${t.state}"><b>${t.id}</b><span>${t.state.replace("_", " ")}</span><em>${(+t.score).toFixed(2)}</em></li>`).join("")
    || `<li class="muted">no targets from node</li>`;
  if (el.innerHTML !== html) el.innerHTML = html;
}

function renderStatus(s, inp) {
  const o = s.own;
  $("ck-mode").textContent = o ? o.mode || "" : "--";
  $("ck-mode").dataset.mode = o ? o.mode : "";
  $("ck-input").textContent = inp.engaged ? `${inp.source} · thr ${Math.round(inp.throttle * 100)}%` : inp.pad ? "gamepad ready - move a stick" : "keys: arrows + W/S";
  $("ck-thr").style.setProperty("--v", inp.throttle);
}

// ---------- primary flight display ----------
// svt = true: transparent overlay on the 3D view (no sky/ground fill). With the camera's vertical
// field of view the pitch ladder is conformal: the PFD horizon sits on the rendered horizon.
function drawPFD(cv, o, svt = false, fovy = null, att = true) {
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height, k = W / 640;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!svt) { ctx.fillStyle = css("var(--panel)"); ctx.fillRect(0, 0, W, H); }
  if (!o) { label(ctx, W / 2, H / 2, "waiting for OWNSHIP…", 18 * k, "center"); return; }

  const cx = W / 2, cy = svt ? H / 2 : H * 0.46, R = Math.min(W * 0.32, H * 0.38);
  const fpa = Math.atan2(o.vs_fpm, Math.max(o.gs_kt, 30) * 101.27) / D2R;   // flight-path angle, deg
  const ppd = fovy ? (H / 2) / Math.tan(fovy / 2) * D2R : R / 25;         // px per degree of pitch

  if (att) {   // attitude: hidden in chase view, where it would not line up with the scene
    // attitude sphere
    ctx.save();
    ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.clip();
    ctx.translate(cx, cy); ctx.rotate(-o.bank_deg * D2R); ctx.translate(0, fpa * ppd);
    if (!svt) {
      ctx.fillStyle = css("var(--sky)"); ctx.fillRect(-2 * R, -4 * R, 4 * R, 4 * R);
      ctx.fillStyle = css("var(--ground)"); ctx.fillRect(-2 * R, 0, 4 * R, 4 * R);
    }
    ctx.shadowColor = "rgba(0,0,0,.8)"; ctx.shadowBlur = svt ? 3 * k : 0;
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 * k;
    ctx.beginPath(); ctx.moveTo(-2 * R, 0); ctx.lineTo(2 * R, 0); ctx.stroke();
    ctx.lineWidth = 1.5 * k; ctx.fillStyle = "#fff"; ctx.font = `${11 * k}px ${MONO}`;
    for (let p = -20; p <= 20; p += 5) {
      if (!p) continue;
      const y = -p * ppd, half = (p % 10 === 0 ? 0.28 : 0.14) * R;
      ctx.beginPath(); ctx.moveTo(-half, y); ctx.lineTo(half, y); ctx.stroke();
      if (p % 10 === 0) { ctx.textAlign = "left"; ctx.fillText(Math.abs(p), half + 4 * k, y + 4 * k); }
    }
    ctx.restore();
    ctx.shadowBlur = 0;

    // bank scale + pointer
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2 * k;
    for (const a of [-60, -45, -30, -20, -10, 0, 10, 20, 30, 45, 60]) {
      const r0 = R + 4 * k, r1 = R + (a % 30 === 0 ? 16 : 9) * k, t = (a - 90) * D2R;
      ctx.beginPath(); ctx.moveTo(cx + r0 * Math.cos(t), cy + r0 * Math.sin(t));
      ctx.lineTo(cx + r1 * Math.cos(t), cy + r1 * Math.sin(t)); ctx.stroke();
    }
    ctx.save(); ctx.translate(cx, cy); ctx.rotate(-o.bank_deg * D2R);
    ctx.fillStyle = Math.abs(o.bank_deg) > 30 ? css("var(--lvl-resolve)") : "#fff";
    ctx.beginPath(); ctx.moveTo(0, -R + 2 * k); ctx.lineTo(-7 * k, -R + 14 * k); ctx.lineTo(7 * k, -R + 14 * k); ctx.fill();
    ctx.restore();

    // fixed aircraft symbol
    ctx.strokeStyle = css("var(--own)"); ctx.lineWidth = 4 * k; ctx.lineCap = "round";
    ctx.beginPath();
    ctx.moveTo(cx - R * 0.45, cy); ctx.lineTo(cx - R * 0.15, cy); ctx.lineTo(cx, cy + R * 0.08);
    ctx.lineTo(cx + R * 0.15, cy); ctx.lineTo(cx + R * 0.45, cy); ctx.stroke();

  }

  // speed / altitude readouts (tapes simplified to boxes + trend)
  const boxW = 96 * k, boxH = 40 * k;
  readout(ctx, cx - R - 30 * k - boxW, cy - boxH / 2, boxW, boxH, `${Math.round(o.ias_kt)}`, "KT IAS", k,
          o.ias_kt < 62 ? "var(--lvl-resolve)" : null);
  readout(ctx, cx + R + 30 * k, cy - boxH / 2, boxW + 14 * k, boxH, `${Math.round(o.alt_press_ft)}`, "FT ALT", k);
  label(ctx, cx + R + 30 * k, cy + boxH / 2 + 18 * k, `AGL ${Math.round(o.agl_ft)}`, 13 * k, "left",
        o.agl_ft < 300 ? css("var(--lvl-traffic)") : css("var(--text-2)"));
  label(ctx, cx + R + 30 * k, cy + boxH / 2 + 38 * k, `VS ${o.vs_fpm > 0 ? "+" : ""}${Math.round(o.vs_fpm / 10) * 10}`, 13 * k, "left", css("var(--text-2)"));
  label(ctx, cx - R - 30 * k - boxW, cy + boxH / 2 + 18 * k, `GS ${Math.round(o.gs_kt)}`, 13 * k, "left", css("var(--text-2)"));

  // heading strip
  const hy = H - 34 * k, span = 60, ppdH = (W * 0.7) / span;
  ctx.fillStyle = css("var(--panel-2)"); ctx.fillRect(W * 0.15, hy - 20 * k, W * 0.7, 40 * k);
  ctx.save(); ctx.beginPath(); ctx.rect(W * 0.15, hy - 20 * k, W * 0.7, 40 * k); ctx.clip();
  ctx.strokeStyle = css("var(--text-2)"); ctx.fillStyle = css("var(--text)"); ctx.lineWidth = 1.5 * k;
  ctx.font = `${12 * k}px ${MONO}`; ctx.textAlign = "center";
  const h0 = Math.floor((o.hdg_deg - span / 2) / 5) * 5;
  for (let h = h0; h <= o.hdg_deg + span / 2; h += 5) {
    const x = W / 2 + (h - o.hdg_deg) * ppdH;
    ctx.beginPath(); ctx.moveTo(x, hy - 20 * k); ctx.lineTo(x, hy - (h % 10 ? 14 : 10) * k); ctx.stroke();
    if (h % 30 === 0) {
      const hh = ((h % 360) + 360) % 360;
      ctx.fillText({ 0: "N", 90: "E", 180: "S", 270: "W" }[hh] ?? String(hh / 10).padStart(2, "0"), x, hy + 8 * k);
    }
  }
  ctx.restore();
  ctx.fillStyle = css("var(--own)"); ctx.font = `bold ${14 * k}px ${MONO}`; ctx.textAlign = "center";
  ctx.fillText(String(Math.round(o.hdg_deg) % 360).padStart(3, "0"), W / 2, hy - 26 * k);
}

function readout(ctx, x, y, w, h, value, unit, k, alert) {
  ctx.fillStyle = "#000"; ctx.fillRect(x, y, w, h);
  ctx.strokeStyle = alert ? css(alert) : css("var(--text-2)"); ctx.lineWidth = 2 * k; ctx.strokeRect(x, y, w, h);
  ctx.fillStyle = alert ? css(alert) : "#fff"; ctx.font = `bold ${24 * k}px ${MONO}`; ctx.textAlign = "right";
  ctx.fillText(value, x + w - 8 * k, y + h * 0.72);
  label(ctx, x, y - 6 * k, unit, 11 * k, "left", css("var(--text-2)"));
}

function label(ctx, x, y, text, size, align = "left", color) {
  ctx.fillStyle = color || css("var(--text)"); ctx.font = `${size}px ${SANS}`; ctx.textAlign = align;
  ctx.fillText(text, x, y);
}

// ---------- traffic display (heading-up, node-reported targets only) ----------
// A TRUST target is placed at t.rel = {brg_deg (TRUE), rng_m, dalt_ft, trk_deg?, vs_fpm?} (INTERFACE v1.1),
// computed by the node from the peer's STATE. Targets without rel stay in the badge list only.
function drawTraffic(cv, o, trust, adv) {
  const ctx = cv.getContext("2d"), W = cv.width, H = cv.height, k = Math.min(W, H) / 480;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#05080c"; ctx.fillRect(0, 0, W, H);
  const cx = W / 2, cy = H * 0.62, R = Math.min(W * 0.46, H * 0.56), rangeNm = 3;
  const pxPerM = R / (rangeNm * NM);

  ctx.strokeStyle = css("var(--grid)"); ctx.lineWidth = 1.2 * k; ctx.setLineDash([4 * k, 6 * k]);
  for (let r = 1; r <= rangeNm; r++) { ctx.beginPath(); ctx.arc(cx, cy, r * NM * pxPerM, 0, Math.PI * 2); ctx.stroke(); }
  ctx.setLineDash([]);
  label(ctx, cx + 4 * k, cy - R + 14 * k, `${rangeNm} NM · radio range`, 11 * k, "left", css("var(--text-2)"));
  label(ctx, 10 * k, 20 * k, o ? `HDG ${String(Math.round(o.hdg_deg) % 360).padStart(3, "0")} UP` : "", 12 * k, "left", css("var(--text-2)"));

  // own ship
  ctx.fillStyle = css("var(--own)");
  ctx.beginPath(); ctx.moveTo(cx, cy - 12 * k); ctx.lineTo(cx - 8 * k, cy + 9 * k); ctx.lineTo(cx, cy + 4 * k); ctx.lineTo(cx + 8 * k, cy + 9 * k); ctx.fill();

  const targets = (trust && trust.targets) || [];
  let unplaced = 0;
  for (const t of targets) {
    const rel = relOf(t, o);
    if (!rel) { unplaced++; continue; }
    const a = (rel.brg_deg - (o ? o.hdg_deg : 0)) * D2R;
    const r = Math.min(rel.rng_m * pxPerM, R + 10 * k);
    const x = cx + r * Math.sin(a), y = cy - r * Math.cos(a);
    const col = css(TRUST_COLOR[t.state] || "#fff");
    const hot = adv && adv.target_id === t.id && ["TRAFFIC", "RESOLVE", "TAKEOVER"].includes(adv.level);
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2 * k;
    if (t.state === "CAMERA_ONLY") {                 // bearing wedge, not a dot
      ctx.globalAlpha = 0.35; ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, a - Math.PI / 2 - 0.08, a - Math.PI / 2 + 0.08); ctx.fill(); ctx.globalAlpha = 1;
      continue;
    }
    const s = (hot ? 11 : 8) * k;
    ctx.beginPath(); ctx.moveTo(x, y - s); ctx.lineTo(x + s, y); ctx.lineTo(x, y + s); ctx.lineTo(x - s, y); ctx.closePath();
    if (t.state === "TRUSTED" || hot) ctx.fill(); else ctx.stroke();
    if (t.state === "FAKE") { ctx.beginPath(); ctx.moveTo(x - s, y - s); ctx.lineTo(x + s, y + s); ctx.moveTo(x + s, y - s); ctx.lineTo(x - s, y + s); ctx.stroke(); }
    if (rel.trk_deg != null) {                      // trend line: where the target is heading
      const ta = (rel.trk_deg - (o ? o.hdg_deg : 0)) * D2R;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + 22 * k * Math.sin(ta), y - 22 * k * Math.cos(ta)); ctx.stroke();
    }
    const d = Math.round((rel.dalt_ft || 0) / 100);
    const arrow = rel.vs_fpm > 300 ? "↑" : rel.vs_fpm < -300 ? "↓" : "";
    label(ctx, x + s + 3 * k, y + 4 * k, `${t.id} ${d >= 0 ? "+" : "-"}${String(Math.abs(d)).padStart(2, "0")}${arrow}`, 11 * k, "left", col);
  }
  if (unplaced) label(ctx, 10 * k, H - 12 * k, `${unplaced} target(s) without position from node`, 11 * k, "left", css("var(--text-2)"));
}

function relOf(t, o) {
  if (t.rel && t.rel.brg_deg != null && t.rel.rng_m != null) return t.rel;
  if (t.lat != null && t.lon != null && o) {
    const kx = 111320 * Math.cos(o.lat * D2R);
    const e = (t.lon - o.lon) * kx, n = (t.lat - o.lat) * 111320;
    return { brg_deg: (Math.atan2(e, n) / D2R + 360) % 360, rng_m: Math.hypot(e, n),
             dalt_ft: t.alt_ft != null ? t.alt_ft - o.alt_press_ft : null };
  }
  return null;
}
