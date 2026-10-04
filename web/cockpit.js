// web/cockpit.js — Lane A. Cockpit view (?role=cockpitA | cockpitB | cockpit:<id>).
// Knows only what the world sends a cockpit: its own aircraft's state and its own node's
// ADVISORY / TRUST / COMMAND / STICK. No other aircraft's truth ever reaches this page.

import { connect, css, drawPlane, LEVEL_COLOR, MONO, NM, SANS, TRUST_COLOR } from "./net.js";
import { startInput } from "./input.js";
import { sayAdvisory, sayCallout, sayNow } from "./voice.js";
import { createCallouts, RA_SHOW_BELOW_FT, raDisplay } from "./callouts.js";
import { startHaptics } from "./haptics.js";
import { MAP_ATTRIBUTION, startMapLayer } from "./maplayer.js";
import { AIRCRAFT_MODEL, aircraftHeightM, EYE_M, gpuInfo, Interp, loadCesium, makeViewer, MODEL_SCALE, MODEL_WHEELS_M,
         offsetLL, surfaceM, TrafficTrack } from "./cesium3d.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;

export function startCockpit(role) {
  document.body.classList.add("is-cockpit");
  $("cockpit").hidden = false;
  const s = { own: null, acId: null, adv: null, advT: 0, trust: null, cmd: null, stickT: -Infinity, link: "connecting",
              fieldElevFt: 1478, interp: new Interp(), v3: null, static: null, map: mapState() };

  const link = connect(role, (m) => {
    switch (m.type) {
      case "HELLO":
        s.acId = m.ac_id;
        $("ck-id").textContent = m.ac_id;
        document.title = `FLOCK cockpit ${m.ac_id}`;
        if (m.static && m.static.airport) s.fieldElevFt = m.static.airport.elev_ft;
        if (m.static && m.static.wx) s.wx = m.static.wx;
        if (m.static) s.static = m.static;
        break;
      case "OWNSHIP": s.own = m; s.interp.push(m); break;
      case "WX": s.wx = m; break;
      case "ADVISORY":
        s.adv = m; s.advT = performance.now();
        sayAdvisory(m);
        break;
      case "TRUST": s.trust = m; trackTraffic(s, m); break;
      case "COMMAND": s.cmd = m; break;
      case "STICK": s.stickT = performance.now(); hap.bump(); break;
      case "AP_STATUS":
        if (!m.ok) { toast(`AP: ${m.reason}`, "warn", 6000); say("autopilot unavailable"); }
        else if (m.engaged) { toast(`AUTOPILOT ON · ${m.phase || ""}`, "ap"); say("autopilot engaged"); }
        else toast("AUTOPILOT OFF", "info");
        break;
      case "WORLD_EVENT":
        if (m.a !== s.acId) break;
        if (m.event === "RESET") {
          s.interp = new Interp(); s.adv = null; s.cmd = null; s.stickT = -Infinity;   // no slide from the old spot
          toast(`AIRCRAFT RESET · back at scenario start (${(m.leg || "").toLowerCase()})`, "ap", 5000);
          say("aircraft reset"); hap.bump();
          break;
        }
        if (m.event === "TOUCHDOWN") {
          if (m.surface === "TERRAIN_IMPACT") { toast(`TERRAIN IMPACT · ${m.ias_kt} kt ${m.vs_fpm} fpm`, "warn", 8000); sayNow("terrain impact"); hap.bump(); }
          else if (m.surface === "OFF_RUNWAY") { toast(`OFF-RUNWAY LANDING · ${m.vs_fpm} fpm`, "warn", 6000); say("off runway"); }
          else toast(m.hard ? `HARD LANDING ${m.vs_fpm} fpm` : `TOUCHDOWN ${m.vs_fpm} fpm`, m.hard ? "warn" : "info");
        }
        else if (m.event === "LIFTOFF") toast(`LIFTOFF ${m.ias_kt} kt`, "info");
        else if (m.event === "STOPPED") {
          if (m.runway) { toast(`STOPPED · RUNWAY ${m.runway} · throttle up + pull to take off, or AP`, "ap", 6000); say(`stopped, runway ${m.runway.split("").join(" ")}`); }
          else { toast("STOPPED OFF RUNWAY · hold L2 + R2 (or R) to reset", "warn", 8000); say("stopped off runway"); }
        }
        else if (m.event === "GO_AROUND") { toast(`GO AROUND · ${m.reason}`, "warn"); say("going around, runway occupied"); }
        else if (m.event === "HOLD_SHORT") { toast(`HOLDING SHORT · ${m.reason}`, "info"); say("holding short"); }
        else if (m.event === "AP_DISCONNECT") { toast("AUTOPILOT DISCONNECT · stick", "warn"); say("autopilot disconnect"); hap.bump(); }
        break;
    }
  }, (status, detail) => {
    s.link = status;
    $("ck-link").textContent = status === "open" ? "LINK" : status.toUpperCase();
    $("ck-link").dataset.state = status;
    if (status === "error") $("ck-link").title = detail;
  });

  const inp = startInput((v) => s.acId && link.send({ type: "INPUT", ac_id: s.acId, ...v }));

  // controller rumble from node data only (advisory level, takeover, TRUSTED target range)
  const hap = startHaptics(() => {
    let nearest = null;
    for (const t of (s.trust && s.trust.targets) || []) {
      if (t.state === "TRUSTED" && t.rel && t.rel.rng_m != null) nearest = nearest == null ? t.rel.rng_m : Math.min(nearest, t.rel.rng_m);
    }
    return { level: s.adv && s.adv.level, levelAt: s.advT, takeover: !!(s.own && s.own.cmd), nearestTrustedM: nearest,
             turb: s.own ? s.own.turb || 0 : 0, taws: s.own ? s.own.taws : null };
  });
  s.hap = hap;

  // reset own aircraft: hold L2 + R2 / R / on-screen RESET for 5 s (input.js) -> RESET to the world
  addEventListener("flock:reset", () => s.acId && link.send({ type: "RESET", ac_id: s.acId }));
  const rb = $("ck-reset-btn");
  const hold = (on) => (e) => { inp.resetButton = on; if (on) rb.setPointerCapture && e.pointerId != null && rb.setPointerCapture(e.pointerId); };
  rb.addEventListener("pointerdown", hold(true));
  for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) rb.addEventListener(ev, hold(false));

  // street map under the moving map (needs internet; ?basemap=0 turns it off)
  if (new URLSearchParams(location.search).get("basemap") !== "0") startMapLayer($("ck-mapbg")).then((ml) => { s.mapLayer = ml; });

  // moving map: click / Z / D-pad up-down = range, N / D-pad left-right = north-up / heading-up
  const cycleRange = (d) => { s.map.range = (s.map.range + d + MAP_RANGES_NM.length) % MAP_RANGES_NM.length; saveMap(s.map); };
  const flipOrient = () => { s.map.northUp = !s.map.northUp; saveMap(s.map); };
  $("ck-traffic").addEventListener("click", () => cycleRange(1));
  addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (e.code === "KeyZ") cycleRange(1);
    if (e.code === "KeyN") flipOrient();
  });
  addEventListener("flock:map-out", () => { if (s.map.range < MAP_RANGES_NM.length - 1) { s.map.range++; saveMap(s.map); } });
  addEventListener("flock:map-in", () => { if (s.map.range > 0) { s.map.range--; saveMap(s.map); } });
  addEventListener("flock:map-orient", flipOrient);

  // autopilot button: on-screen AP, key A, gamepad Cross / A -> toggle in the world
  const toggleAP = () => s.acId && link.send({ type: "AP", ac_id: s.acId, engage: null });
  $("ck-ap").addEventListener("click", toggleAP);
  addEventListener("flock:ap", toggleAP);
  addEventListener("keydown", (e) => { if (e.code === "KeyA" && !e.repeat) toggleAP(); });

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
    renderVisibility(s);
    renderTaws(s);
    heightCallouts(s);
    renderReset(inp);
    const chase = !!(s.v3 && s.v3.chase);
    drawPFD(pfd, s.own, !!s.v3, s.v3 && !chase ? s.v3.viewer.camera.frustum.fovy : null, !chase, s.wx);
    drawNavMap(tfc, s);
    renderBanner(s);
    renderBounds(s);
    renderTrust(s);
    renderStatus(s, inp);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// ---------- reset hold countdown ----------
function renderReset(inp) {
  const el = $("ck-reset"), p = inp.resetProgress || 0;
  el.hidden = !(p > 0.04 && p < 1);
  if (el.hidden) return;
  el.querySelector("span").textContent = `RESET AIRCRAFT · keep holding ${(5 * (1 - p)).toFixed(1)} s`;
  el.querySelector("i").style.setProperty("--p", p.toFixed(3));
}

// ---------- terrain awareness (world/taws.py, own aircraft only) ----------
const TAWS_TEXT = { "PULL UP": ["PULL UP", "warning", "pull up, pull up"], "TERRAIN": ["TERRAIN", "warning", "terrain, terrain"],
                    "SINK RATE": ["SINK RATE", "caution", "sink rate"], "TOO LOW TERRAIN": ["TOO LOW · TERRAIN", "caution", "too low, terrain"] };
function renderTaws(s) {
  const el = $("ck-taws"), al = s.own && s.own.taws;
  if (!al || !TAWS_TEXT[al]) { el.hidden = true; s.tawsSaid = null; return; }
  const [text, level, speak] = TAWS_TEXT[al];
  if (el.textContent !== text) el.textContent = text;
  el.dataset.level = level; el.hidden = false;
  const now = performance.now();
  if (s.tawsSaid !== al || now - (s.tawsAt || 0) > (level === "warning" ? 2200 : 3500)) {   // repeat while it lasts
    sayNow(speak); s.tawsSaid = al; s.tawsAt = now;
  }
}

// ---------- weather out of the window: haze, dust, cloud (3D only) ----------
// Visibility is what the pilot sees; it hides traffic outside but not the FLOCK radar.
function renderVisibility(s) {
  const el = $("ck-haze"), w = s.wx, o = s.own;
  let alpha = 0, color = "rgba(170,178,190,1)";
  if (s.v3 && w && o) {
    const vis = Math.max(0.1, +w.visibility_sm || 10);
    alpha = 0.9 * Math.pow(Math.max(0, 1 - vis / 10), 2);
    if ((+w.wind_kt || 0) >= 20 && vis < 3) color = "rgba(160,118,72,1)";        // dust storm
    const base = +w.ceiling_ft_agl || 0;
    if (base && o.agl_ft > base - 150) {                                          // entering cloud
      alpha = Math.max(alpha, Math.min(0.95, (o.agl_ft - (base - 150)) / 150 * 0.95));
      color = "rgba(214,218,224,1)";
    }
    if (s.v3.viewer && s.v3.lastVis !== vis) {                                    // denser Cesium fog too
      s.v3.viewer.scene.fog.density = 2.0e-4 + 2.0e-3 * Math.max(0, 1 - vis / 10);
      s.v3.lastVis = vis;
    }
  }
  el.style.backgroundColor = color;
  el.style.opacity = alpha.toFixed(2);
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
      + (terrain ? "3D: Cesium World Terrain · © Cesium ion · C / Triangle / Y = chase cam"
                 : "3D: flat · © OpenStreetMap contributors · C / Triangle / Y = chase cam · add ?ion=<token> for terrain");
    addEventListener("keydown", (e) => { if (e.code === "KeyC" && s.v3) s.v3.chase = !s.v3.chase; });
    addEventListener("flock:chase", () => { if (s.v3) s.v3.chase = !s.v3.chase; });   // gamepad Triangle / Y
  } catch (e) {
    console.warn("[3d]", e);
    note.textContent = `2D only: ${e.message}`;
  }
}

function update3D(s) {
  const { Cesium: C, viewer, terrain } = s.v3;
  const p = s.interp.sample();
  if (!p) return;
  const h = aircraftHeightM(C, viewer, terrain, p.lat, p.lon, p.alt_msl_ft, p.agl_ft);   // wheels height
  s.v3.lastH = h;
  const pos = C.Cartesian3.fromDegrees(p.lon, p.lat, h + MODEL_WHEELS_M);                 // model origin
  const fpa = Math.atan2(p.vs_fpm, Math.max(p.gs_kt, 30) * 101.27) / D2R;

  if (!s.v3.own) s.v3.own = viewer.entities.add({ model: { uri: AIRCRAFT_MODEL, scale: MODEL_SCALE, minimumPixelSize: 48 } });
  const hpr = new C.HeadingPitchRoll(C.Math.toRadians(p.hdg_deg + MODEL_HDG_OFFSET), C.Math.toRadians(fpa), C.Math.toRadians(p.bank_deg));
  s.v3.own.position = pos;
  s.v3.own.orientation = C.Transforms.headingPitchRollQuaternion(pos, hpr);
  s.v3.own.show = s.v3.chase;

  if (s.v3.chase) {
    const [blat, blon] = offsetLL(p.lat, p.lon, p.track_deg + 180, 70);
    const camGround = surfaceM(C, viewer, terrain, blat, blon, p.alt_msl_ft - Math.max(0, p.agl_ft));
    viewer.camera.setView({
      destination: C.Cartesian3.fromDegrees(blon, blat, Math.max(h + 18, camGround + 5)),   // never under the ground behind
      orientation: { heading: C.Math.toRadians(p.track_deg), pitch: C.Math.toRadians(-12), roll: 0 },
    });
  } else {
    viewer.camera.setView({
      destination: C.Cartesian3.fromDegrees(p.lon, p.lat, h + EYE_M),
      orientation: { heading: C.Math.toRadians(p.hdg_deg), pitch: C.Math.toRadians(fpa), roll: C.Math.toRadians(p.bank_deg) },
    });
  }

  // node-reported traffic (TRUST rel) as real aircraft: same model as ours, flying its reported track,
  // pitched by its climb and banked by its turn; outline = trust colour, FAKE = see-through ghost
  const seen = new Set(), now = performance.now(), ground = p.alt_msl_ft - Math.max(0, p.agl_ft);
  for (const t of (s.trust && s.trust.targets) || []) {
    const tr = s.tracks && s.tracks.get(t.id);
    const q = tr && t.state !== "CAMERA_ONLY" ? tr.sample(now) : null;
    if (!q) continue;
    seen.add(t.id);
    const surf = surfaceM(C, viewer, terrain, q.lat, q.lon, ground);
    const th = Math.max(h + (q.alt_ft - p.alt_msl_ft) * 0.3048, surf);   // relative to own height, never underground
    const tpos = C.Cartesian3.fromDegrees(q.lon, q.lat, th + MODEL_WHEELS_M);
    const col = C.Color.fromCssColorString(css(TRUST_COLOR[t.state] || "#fff"));
    const fake = t.state === "FAKE";
    let e = s.v3.targets.get(t.id);
    if (!e) {
      e = viewer.entities.add({
        model: { uri: AIRCRAFT_MODEL, scale: MODEL_SCALE, minimumPixelSize: 56, silhouetteSize: 2.5,   // readable at 1-3 NM
                 colorBlendMode: C.ColorBlendMode.MIX, colorBlendAmount: 0.35 },
        label: { text: t.id, font: `600 14px ${SANS}`, outlineColor: C.Color.BLACK, outlineWidth: 3,
                 style: C.LabelStyle.FILL_AND_OUTLINE, pixelOffset: new C.Cartesian2(0, -30),
                 disableDepthTestDistance: Number.POSITIVE_INFINITY },
      });
      s.v3.targets.set(t.id, e);
    }
    e.position = tpos;
    e.orientation = C.Transforms.headingPitchRollQuaternion(tpos, new C.HeadingPitchRoll(
      C.Math.toRadians(q.trk_deg + MODEL_HDG_OFFSET), C.Math.toRadians(q.pitch_deg), C.Math.toRadians(q.bank_deg)));
    e.model.color = fake ? col.withAlpha(0.3) : col.withAlpha(1.0);
    e.model.silhouetteColor = col;
    const rng = t.rel ? t.rel.rng_m : null, d = Math.round(((t.rel && t.rel.dalt_ft) || 0) / 100);
    e.label.text = `${t.id}${rng != null ? ` · ${(rng / NM).toFixed(1)} NM ${d >= 0 ? "+" : "-"}${String(Math.abs(d)).padStart(2, "0")}` : ""}${fake ? " · FAKE" : ""}`;
    e.label.fillColor = col;
  }
  for (const [id, e] of s.v3.targets) if (!seen.has(id)) { viewer.entities.remove(e); s.v3.targets.delete(id); }
}

// Each TRUST frame: where the node says every target is, made absolute from our position right now,
// into a TrafficTrack per target (the 3D view glides between these).
function trackTraffic(s, m) {
  const o = s.own;
  s.tracks = s.tracks || new Map();
  if (!o) return;
  const now = performance.now(), ids = new Set();
  for (const t of m.targets || []) {
    if (!t.rel || t.rel.brg_deg == null || t.rel.rng_m == null) continue;
    ids.add(t.id);
    const [lat, lon] = offsetLL(o.lat, o.lon, t.rel.brg_deg, t.rel.rng_m);
    let tr = s.tracks.get(t.id);
    if (!tr) { tr = new TrafficTrack(); s.tracks.set(t.id, tr); }
    tr.push({ lat, lon, alt_ft: o.alt_msl_ft + (t.rel.dalt_ft || 0), trk_deg: t.rel.trk_deg, vs_fpm: t.rel.vs_fpm }, now);
  }
  for (const id of [...s.tracks.keys()]) if (!ids.has(id)) s.tracks.delete(id);
}

// ---------- short notices (AP, touchdown, liftoff) ----------
let toastTimer = 0;
function toast(text, kind = "info", ms = 3500) {
  const el = $("ck-toast");
  el.textContent = text; el.dataset.kind = kind; el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms);
}
function say(text) { sayAdvisory({ level: "INFO", layer: 0, speak: text }); }

// Radio-altimeter callouts on descent (callouts.js); silent while a terrain warning is talking.
function heightCallouts(s) {
  const o = s.own;
  if (!o) return;
  s.callouts = s.callouts || createCallouts();
  const words = s.callouts(o.agl_ft, o.vs_fpm, !!o.on_ground);
  if (words && !o.taws) sayCallout(words);
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
  const targets = [...((s.trust && s.trust.targets) || [])]
    .sort((a, b) => ((a.rel && a.rel.rng_m) ?? 1e9) - ((b.rel && b.rel.rng_m) ?? 1e9));
  const html = targets.map((t) => {
    const nm = t.rel && t.rel.rng_m != null ? `${(t.rel.rng_m / NM).toFixed(1)} NM` : "—";
    return `<li data-state="${t.state}"><b>${t.id}</b><i>${nm}</i><span>${t.state.replace("_", " ")}</span><em>${(+t.score).toFixed(2)}</em></li>`;
  }).join("") || `<li class="muted">no traffic heard on the FLOCK radio</li>`;
  if (el.innerHTML !== html) el.innerHTML = html;
}

function renderStatus(s, inp) {
  const o = s.own;
  $("ck-mode").textContent = o ? (o.ap && o.mode === "AUTOPILOT" ? `AP · ${o.ap_phase || ""}` : o.mode || "") : "--";
  $("ck-mode").dataset.mode = o ? o.mode : "";
  $("ck-ap").dataset.on = o && o.ap ? "1" : "";
  const hs = s.hap && s.hap.status;
  $("ck-haptics").textContent = !hs || !hs.enabled ? "rumble off" : hs.supported ? "rumble ✓" : hs.pad ? "no rumble on this pad" : "";
  $("ck-input").textContent = inp.engaged ? `${inp.source} · thr ${Math.round(inp.throttle * 100)}%` : inp.pad ? "gamepad ready - move a stick" : "keys: arrows + W/S";
  $("ck-thr").style.setProperty("--v", inp.throttle);
}

// ---------- primary flight display ----------
// svt = true: transparent overlay on the 3D view (no sky/ground fill). With the camera's vertical
// field of view the pitch ladder is conformal: the PFD horizon sits on the rendered horizon.
function drawPFD(cv, o, svt = false, fovy = null, att = true, wx = null) {
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
  // altimeter: indicated altitude with this aircraft's setting (amber if it differs from the reported QNH)
  const ind = o.alt_ind_ft != null ? o.alt_ind_ft : o.alt_press_ft;
  readout(ctx, cx + R + 30 * k, cy - boxH / 2, boxW + 14 * k, boxH, `${Math.round(ind)}`, "FT ALT", k);
  if (o.baro_set_inhg != null) {
    const qnh = wx && wx.qnh_inhg != null ? +wx.qnh_inhg : null;
    const stale = qnh != null && Math.abs(qnh - o.baro_set_inhg) > 0.005;
    const col = stale ? css("var(--lvl-traffic)") : css("var(--text-2)");
    label(ctx, cx + R + 30 * k, cy + boxH / 2 + 58 * k, `BARO ${(+o.baro_set_inhg).toFixed(2)}`, 12 * k, "left", col);
    if (stale) label(ctx, cx + R + 30 * k, cy + boxH / 2 + 74 * k, `ATIS ${qnh.toFixed(2)}`, 12 * k, "left", col);
  }
  if (wx && wx.metar_style) label(ctx, 10 * k, 20 * k, wx.metar_style, 12 * k, "left", css("var(--text-2)"));
  // on the ground: where we are and how to stop (brakes only work on a runway)
  if (o.on_ground && o.surface) {
    const off = o.surface === "OFF";
    const msg = off ? "OFF RUNWAY · rough ground · reset: hold L2 + R2"
      : o.ias_kt > 2 ? `RUNWAY ${o.surface} · idle or hold ◯ / B to brake` : `RUNWAY ${o.surface} · stopped`;
    label(ctx, W / 2, H - 70 * k, msg, 14 * k, "center", off ? css("var(--lvl-resolve)") : css("var(--lvl-release)"));
  }
  label(ctx, cx + R + 30 * k, cy + boxH / 2 + 18 * k, `AGL ${Math.round(o.agl_ft)}`, 13 * k, "left",
        o.agl_ft < 300 ? css("var(--lvl-traffic)") : css("var(--text-2)"));
  // radio altimeter: height above the ground right below the aircraft (shown under 2,500 ft, airborne)
  if (!o.on_ground && o.agl_ft < RA_SHOW_BELOW_FT) {
    const ra = raDisplay(o.agl_ft), low = o.agl_ft < 200;
    const col = low ? css("var(--lvl-traffic)") : css("var(--lvl-release)");
    const bw = 150 * k, bh = 46 * k, bx = cx - bw / 2, by = cy + R * 0.52;
    ctx.fillStyle = "#000"; ctx.fillRect(bx, by, bw, bh);                 // opaque: pitch ladder must not show through
    ctx.strokeStyle = col; ctx.lineWidth = 2 * k; ctx.strokeRect(bx, by, bw, bh);
    ctx.fillStyle = col; ctx.textAlign = "left"; ctx.font = `600 ${12 * k}px ${MONO}`;
    ctx.fillText("RA", bx + 8 * k, by + bh * 0.62);
    ctx.textAlign = "right"; ctx.font = `bold ${28 * k}px ${MONO}`;
    ctx.fillText(String(ra), bx + bw - 10 * k, by + bh * 0.74);
    if (Math.abs(o.vs_fpm) > 100) {                              // trend: where we will be in 6 s
      const fut = Math.max(0, o.agl_ft + o.vs_fpm / 10);
      ctx.fillStyle = "#000"; ctx.fillRect(bx, by + bh + 2 * k, bw, 20 * k);   // readable over sky/ground/3D
      label(ctx, cx, by + bh + 16 * k, `${o.vs_fpm < 0 ? "↓" : "↑"} ${raDisplay(fut)} ft in 6 s`, 12 * k, "center", col);
    }
  }
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

// ---------- moving map + traffic (right panel) ----------
// Own aircraft in the middle, charts (runways, pattern legs from HELLO static), where the aircraft is
// headed (60 s path that curves with the current bank), the leg the autopilot is flying, and traffic.
// Traffic comes ONLY from this aircraft's node (TRUST rel), never from world truth.
const MAP_RANGES_NM = [0.75, 1.5, 3, 6, 12];        // default 1.5 NM: close in, so the path ahead is clear

function mapState() {
  let st = { range: 1, northUp: false };
  try { st = { ...st, ...JSON.parse(localStorage.getItem("flock.cockpit.map2") || "{}") }; } catch { /* private mode */ }
  st.range = Math.max(0, Math.min(MAP_RANGES_NM.length - 1, st.range | 0));
  return st;
}
function saveMap(m) { try { localStorage.setItem("flock.cockpit.map2", JSON.stringify(m)); } catch { /* ignore */ } }

function drawNavMap(cv, s) {
  const o = s.own, m = s.map, ctx = cv.getContext("2d"), W = cv.width, H = cv.height, k = Math.min(W, H) / 480;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!s.mapLayer) { ctx.fillStyle = "#05080c"; ctx.fillRect(0, 0, W, H); }
  if (!o) { label(ctx, W / 2, H / 2, "waiting for OWNSHIP…", 14 * k, "center", css("var(--text-2)")); return; }
  const rangeNm = MAP_RANGES_NM[m.range];
  const cx = W / 2, cy = m.northUp ? H / 2 : H * 0.70;            // heading-up: more room ahead
  const R = Math.min(W * 0.46, m.northUp ? H * 0.46 : H * 0.64);
  const pxPerM = R / (rangeNm * NM);
  const up = m.northUp ? 0 : o.hdg_deg;
  if (s.mapLayer) {                                             // street map underneath, same scale / rotation
    const dpr = W / Math.max(1, cv.clientWidth);
    s.mapLayer.update(o.lat, o.lon, dpr / pxPerM, up, cx / dpr, cy / dpr, W / dpr, H / dpr);
    ctx.fillStyle = "rgba(5, 8, 12, 0.08)"; ctx.fillRect(0, 0, W, H);   // faint veil keeps overlays readable
  }
  const kx = 111320 * Math.cos(o.lat * D2R);
  const fromEN = (e, n) => {                                  // metres east/north of own -> screen
    const r = Math.hypot(e, n) * pxPerM, a = Math.atan2(e, n) - up * D2R;
    return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
  };
  const fromLL = (lat, lon) => fromEN((lon - o.lon) * kx, (lat - o.lat) * 111320);

  ctx.save();
  ctx.beginPath(); ctx.rect(0, 0, W, H); ctx.clip();

  // range rings
  ctx.strokeStyle = css("var(--grid)"); ctx.lineWidth = 1.2 * k; ctx.setLineDash([4 * k, 6 * k]);
  for (const f of [0.5, 1]) { ctx.beginPath(); ctx.arc(cx, cy, R * f, 0, Math.PI * 2); ctx.stroke(); }
  ctx.setLineDash([]);
  label(ctx, cx + R * 0.71 + 4 * k, cy - R * 0.71, `${rangeNm} NM`, 11 * k, "left", css("var(--text-2)"));
  label(ctx, cx + R * 0.35 + 4 * k, cy - R * 0.35, `${rangeNm / 2}`, 10 * k, "left", css("var(--text-2)"));
  if (rangeNm > 3) {                                          // radio range of the FLOCK link
    ctx.strokeStyle = css("var(--grid-strong)"); ctx.setLineDash([2 * k, 6 * k]);
    ctx.beginPath(); ctx.arc(cx, cy, 3 * NM * pxPerM, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
  }

  // charts: pattern legs (the leg the autopilot is flying highlighted), runways
  const st = s.static || {};
  for (const p of Object.values(st.patterns || {})) {
    for (const [name, seg] of Object.entries(p.legs)) {
      if (name === "STRAIGHT_IN") continue;
      const active = o.ap_leg === name && o.ap_rwy === p.runway;
      const [x0, y0] = fromLL(...seg[0]), [x1, y1] = fromLL(...seg[1]);
      ctx.strokeStyle = active ? css("var(--lvl-release)") : css("var(--pattern)");
      ctx.lineWidth = (active ? 3 : 1.4) * k; ctx.setLineDash(active ? [] : [7 * k, 6 * k]);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
      if (active) label(ctx, (x0 + x1) / 2, (y0 + y1) / 2 - 6 * k, `AP ${p.runway} ${name.toLowerCase()}`, 11 * k, "center", css("var(--lvl-release)"));
    }
  }
  const ends = Object.entries((st.airport && st.airport.ends) || {});
  ctx.strokeStyle = css("var(--runway)"); ctx.lineCap = "butt";
  for (const [, e] of ends) {
    const [x0, y0] = fromLL(e.lat, e.lon), [x1, y1] = fromLL(e.far_lat, e.far_lon);
    ctx.lineWidth = Math.max(4 * k, (e.width_ft || 75) * 0.3048 * pxPerM);
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  }
  if (rangeNm <= 3) for (const [id, e] of ends) {             // runway idents only when zoomed in (no clutter)
    const [x0, y0] = fromLL(e.lat, e.lon), [x1, y1] = fromLL(e.far_lat, e.far_lon);
    const d = Math.hypot(x1 - x0, y1 - y0) || 1;
    label(ctx, x0 - (x1 - x0) / d * 13 * k, y0 - (y1 - y0) / d * 13 * k + 4 * k, id, 11 * k, "center", css("var(--text)"));
  }

  // where the aircraft is headed: up to 60 s ahead at the current ground speed, curving with the current
  // bank, drawn until it leaves the map; marks every 10 s when zoomed in, every 30 s when zoomed out
  if (!o.on_ground && o.gs_kt > 20) {
    const v = o.gs_kt * 0.514444, tas = Math.max(o.ias_kt, 40) * 0.514444;
    const rate = (9.80665 * Math.tan(o.bank_deg * D2R) / tas) / D2R;     // turn rate now, deg/s
    const every = rangeNm <= 1.5 ? 10 : 30;
    let e = 0, n = 0, trk = o.track_deg;
    const pts = [[cx, cy]], marks = [];
    for (let t = 1; t <= 60; t++) {
      trk += rate; e += v * Math.sin(trk * D2R); n += v * Math.cos(trk * D2R);
      const [x, y] = fromEN(e, n);
      pts.push([x, y]);
      if (t % every === 0) marks.push([t, x, y]);
      if (Math.hypot(x - cx, y - cy) > R * 1.25) break;
    }
    const path = () => { ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke(); };
    ctx.lineCap = "round"; ctx.lineJoin = "round";
    ctx.strokeStyle = "rgba(0,0,0,.75)"; ctx.lineWidth = 6 * k; path();           // dark outline for contrast on the map
    ctx.strokeStyle = css("var(--own)"); ctx.lineWidth = 3 * k; path();
    ctx.fillStyle = css("var(--own)");
    for (const [t, x, y] of marks) {
      ctx.beginPath(); ctx.arc(x, y, 4 * k, 0, Math.PI * 2); ctx.fill();
      label(ctx, x + 7 * k, y + 4 * k, `${t}s`, 11 * k, "left", css("var(--own)"));
    }
  }

  // traffic (node-reported only)
  const targets = (s.trust && s.trust.targets) || [];
  let unplaced = 0, nearest = null;
  for (const t of targets) {
    const rel = relOf(t, o);
    if (!rel) { unplaced++; continue; }
    const te = rel.rng_m * Math.sin(rel.brg_deg * D2R), tn = rel.rng_m * Math.cos(rel.brg_deg * D2R);
    let [x, y] = fromEN(te, tn);
    const dx = x - cx, dy = y - cy, dr = Math.hypot(dx, dy);
    if (dr > R + 10 * k) { x = cx + dx / dr * (R + 10 * k); y = cy + dy / dr * (R + 10 * k); }   // pin to the edge
    const col = css(TRUST_COLOR[t.state] || "#fff");
    const hot = s.adv && s.adv.target_id === t.id && ["TRAFFIC", "RESOLVE", "TAKEOVER"].includes(s.adv.level);
    if (t.state !== "CAMERA_ONLY") {                        // distance line from own aircraft
      ctx.strokeStyle = col; ctx.lineWidth = (hot ? 2.5 : 1.2) * k; ctx.globalAlpha = hot ? 0.95 : 0.45;
      ctx.setLineDash(hot ? [] : [5 * k, 5 * k]);
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(x, y); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
    }
    if (t.state !== "FAKE" && t.state !== "CAMERA_ONLY" && (!nearest || rel.rng_m < nearest.rel.rng_m)) nearest = { t, rel };
    ctx.strokeStyle = col; ctx.fillStyle = col; ctx.lineWidth = 2 * k;
    if (t.state === "CAMERA_ONLY") {                         // bearing wedge, not a dot
      const a = (rel.brg_deg - up) * D2R;
      ctx.globalAlpha = 0.35; ctx.beginPath(); ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, R, a - Math.PI / 2 - 0.08, a - Math.PI / 2 + 0.08); ctx.fill(); ctx.globalAlpha = 1;
      continue;
    }
    const sz = (hot ? 13 : 10) * k;
    if (rel.trk_deg != null) {                              // an airplane pointing where it is going
      drawPlane(ctx, x, y, (rel.trk_deg - up) * D2R, sz, t.state === "TRUSTED" || hot);
      const ta = (rel.trk_deg - up) * D2R;                  // short trend line ahead of the nose
      ctx.globalAlpha = 0.6; ctx.beginPath();
      ctx.moveTo(x + 1.4 * sz * Math.sin(ta), y - 1.4 * sz * Math.cos(ta));
      ctx.lineTo(x + 3.2 * sz * Math.sin(ta), y - 3.2 * sz * Math.cos(ta)); ctx.stroke(); ctx.globalAlpha = 1;
    } else {                                                // no track reported: diamond
      ctx.beginPath(); ctx.moveTo(x, y - sz); ctx.lineTo(x + sz, y); ctx.lineTo(x, y + sz); ctx.lineTo(x - sz, y); ctx.closePath();
      if (t.state === "TRUSTED" || hot) ctx.fill(); else ctx.stroke();
    }
    if (t.state === "FAKE") { ctx.beginPath(); ctx.moveTo(x - sz, y - sz); ctx.lineTo(x + sz, y + sz); ctx.moveTo(x + sz, y - sz); ctx.lineTo(x - sz, y + sz); ctx.stroke(); }
    const d = Math.round((rel.dalt_ft || 0) / 100);
    const arrow = rel.vs_fpm > 300 ? "↑" : rel.vs_fpm < -300 ? "↓" : "";
    label(ctx, x + sz + 3 * k, y + 4 * k, `${t.id} ${(rel.rng_m / NM).toFixed(1)} NM ${d >= 0 ? "+" : "-"}${String(Math.abs(d)).padStart(2, "0")}${arrow}`, 11 * k, "left", col);
  }

  // own aircraft
  ctx.fillStyle = css("var(--own)");
  drawPlane(ctx, cx, cy, (o.hdg_deg - up) * D2R, 12 * k, true);
  ctx.restore();

  // header: orientation, north pointer, range, track / GS (dark backing so it reads over the street map)
  ctx.fillStyle = "rgba(5, 8, 12, 0.72)";
  ctx.fillRect(0, 0, 210 * k, nearest ? 60 * k : 44 * k); ctx.fillRect(W - 44 * k, 0, 44 * k, 48 * k); ctx.fillRect(W - 220 * k, H - 40 * k, 220 * k, 40 * k);
  if (nearest) {
    const d = Math.round((nearest.rel.dalt_ft || 0) / 100);
    const clock = ((Math.round((nearest.rel.brg_deg - o.hdg_deg) / 30) % 12) + 12) % 12 || 12;
    label(ctx, 10 * k, 52 * k, `NEAREST ${nearest.t.id} ${(nearest.rel.rng_m / NM).toFixed(1)} NM · ${clock} o'clock · ${d >= 0 ? "+" : "-"}${Math.abs(d) * 100} ft`,
          12 * k, "left", css(TRUST_COLOR[nearest.t.state] || "#fff"));
  }
  const hdr = `${m.northUp ? "NORTH UP" : `HDG ${String(Math.round(o.hdg_deg) % 360).padStart(3, "0")} UP`} · ${rangeNm} NM`;
  label(ctx, 10 * k, 20 * k, hdr, 12 * k, "left", css("var(--text-2)"));
  label(ctx, 10 * k, 36 * k, `TRK ${String(Math.round(o.track_deg) % 360).padStart(3, "0")} · GS ${Math.round(o.gs_kt)} kt · AGL ${o.on_ground ? "GND" : raDisplay(o.agl_ft)}`,
        12 * k, "left", css("var(--own)"));
  const nx = W - 26 * k, ny = 28 * k, na = -up * D2R;
  ctx.strokeStyle = css("var(--text-2)"); ctx.lineWidth = 2 * k;
  ctx.beginPath(); ctx.moveTo(nx - 9 * k * Math.sin(na), ny + 9 * k * Math.cos(na)); ctx.lineTo(nx + 9 * k * Math.sin(na), ny - 9 * k * Math.cos(na)); ctx.stroke();
  label(ctx, nx + 13 * k * Math.sin(na), ny - 13 * k * Math.cos(na) + 4 * k, "N", 11 * k, "center", css("var(--text)"));
  label(ctx, W - 10 * k, H - 12 * k, "click / Z: range · N: north-up", 10 * k, "right", css("var(--text-2)"));
  if (s.mapLayer) label(ctx, W - 10 * k, H - 26 * k, MAP_ATTRIBUTION, 9 * k, "right", css("var(--text-2)"));
  if (unplaced) label(ctx, 10 * k, H - 12 * k, `${unplaced} target(s) without position`, 11 * k, "left", css("var(--text-2)"));
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
