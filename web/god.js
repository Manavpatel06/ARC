// web/god.js — Lane A. God view (?role=god): the whole pattern from above, for the projector.
// Truth for every aircraft (TRUTH, 10 Hz) + every node's ADVISORY / TRUST / COMMAND / PREDICTION.
// Mouse wheel = zoom, drag = pan, double-click = reset. Layer toggles in the side panel.
//
// Overlays (A9):
//   predictions   node PREDICTION paths. Own path solid; straight-line fallback or confidence < 0.6
//                 drawn amber dashed (the predictor's failure mode is visible). Peer predictions
//                 (target_id set) thin dashed. Optional sigma discs.
//   conflicts     for each live advisory with ttc: both aircraft's predicted positions at the
//                 conflict time (node prediction if any, else straight-line from truth), joined,
//                 labelled with predicted miss and seconds to go.
//   layer rings   ring per aircraft coloured by its node's level; TAKEOVER pulses, NO_SOLUTION dashed.
//   takeover      box + countdown while the world applies a COMMAND; "STICK" flash on handback.
//   live sky      LIVE_TRAFFIC (contract v1.2, data/live_traffic.py): real ADS-B aircraft around KDVT as small
//                 cyan chevrons with callsign / altitude / pattern leg; display only, fades when stale.
//   separation    world truth WORLD_EVENT (world/separation.py): red line between a pair while it is
//                 inside the NMAC box, burst marker where NMAC / COLLISION happened, counter.
// All ages use the world clock (frame t), so --time-scale runs keep consistent fades.

import { connect, css, LEVEL_COLOR, Local, NM, SANS } from "./net.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;
const KT = 0.514444;
const TRAIL_S = 60, ADV_TTL_S = 12, PRED_TTL_S = 4, STICK_FLASH_S = 4, EVENTS_MAX = 12, SEP_MARK_S = 20;
const LAYER_R_M = { 1: 900, 2: 650, 3: 450, 4: 300 };   // ring radius by layer (visual only)
const CONFLICT_LEVELS = new Set(["SEQUENCE", "TRAFFIC", "RESOLVE", "TAKEOVER", "NO_SOLUTION"]);
const LAYERS = { pred: "Predictions", sigma: "Uncertainty", conflict: "Conflicts", trails: "Trails", pattern: "Pattern", labels: "Labels", weather: "Thermals", live: "Live sky" };
const LIVE_STALE_MS = 20000, LIVE_HIDE_MS = 90000;
// world weather controls (SET_WX fields): [field, label, min, max, step, format]
const WX_SLIDERS = [
  ["wind_from_deg", "Wind from", 0, 359, 5, (v) => `${String(Math.round(v)).padStart(3, "0")}°`],
  ["wind_kt", "Wind", 0, 40, 1, (v) => `${v} kt`],
  ["gust_kt", "Gusts to", 0, 50, 1, (v) => (+v ? `${v} kt` : "none")],
  ["shear_kt", "Shear <400 ft", 0, 20, 1, (v) => (+v ? `-${v} kt` : "none")],
  ["thermals", "Thermals", 0, 1, 0.1, (v) => (+v ? `${Math.round(v * 100)} %` : "none")],
  ["visibility_sm", "Visibility", 0.25, 10, 0.25, (v) => `${v} SM`],
  ["ceiling_ft_agl", "Cloud base", 0, 5000, 100, (v) => (+v ? `${v} ft AGL` : "none")],
  ["qnh_inhg", "QNH", 28.9, 30.6, 0.01, (v) => `${(+v).toFixed(2)}`],
];

export function climbFpm(da) {   // same table as data/metar.py climb_fpm
  const pts = [[0, 730], [5000, 500], [8000, 300], [12000, 100]];
  da = Math.max(0, da);
  for (let i = 1; i < pts.length; i++) if (da <= pts[i][0]) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i];
    return y0 + (y1 - y0) * (da - x0) / (x1 - x0);
  }
  return 100;
}

export function startGod() {
  document.body.classList.add("is-god");
  $("god").hidden = false;
  const s = {
    stat: null, local: null, ac: new Map(), trails: new Map(), adv: new Map(), pred: new Map(),
    trust: new Map(), crystal: new Map(), cmd: new Map(), stick: new Map(), events: [],
    nmacOpen: new Map(), sepMarks: [], sepCounts: { NMAC: 0, COLLISION: 0 }, wx: null, thermals: [], live: null,
    t: 0, da: null, show: loadToggles(),
    view: { cx: 0, cy: 0, scale: 0.08, fitted: false },
  };
  buildToggles(s);

  const link = connect("god", (m) => {
    switch (m.type) {
      case "HELLO":
        if (m.static) {
          s.stat = m.static;
          s.local = new Local(m.static.airport.lat, m.static.airport.lon);
          s.da = m.static.da_field_ft;
          s.wx = m.static.wx || null;
          buildWxControls(s, link, m.static.presets || []);
          renderWeather(s);
          renderDA(s);
        }
        break;
      case "TRUTH":
        s.t = m.t;
        for (const a of m.aircraft) {
          s.ac.set(a.ac_id, a);
          if (!s.local) continue;
          const tr = s.trails.get(a.ac_id) || [];
          tr.push([...s.local.toEnu(a.lat, a.lon), m.t]);
          while (tr.length && m.t - tr[0][2] > TRAIL_S) tr.shift();
          s.trails.set(a.ac_id, tr);
        }
        break;
      case "ADVISORY":
        s.adv.set(m.ac_id, m);
        pushEvent(s, m.t, m.ac_id, m.level, m.text);
        break;
      case "PREDICTION":
        s.pred.set(`${m.ac_id}>${m.target_id || m.ac_id}`, m);
        break;
      case "COMMAND":
        if (m.mode === "TAKEOVER" && m.applied) s.cmd.set(m.ac_id, { ...m, until: m.t + Math.min(+m.hold_s || 10, 10) });
        if (m.mode === "RELEASE") s.cmd.delete(m.ac_id);
        pushEvent(s, m.t, m.ac_id, m.mode === "TAKEOVER" ? "TAKEOVER" : "RELEASE",
          m.mode === "TAKEOVER" ? `COMMAND bank ${m.bank_cmd_deg}° ${m.applied ? "applied" : `rejected (${m.rejected_by_world || "world"})`}` : "COMMAND release");
        break;
      case "STICK":
        s.stick.set(m.ac_id, m.t);
        s.cmd.delete(m.ac_id);
        pushEvent(s, m.t, m.ac_id, "RELEASE", "STICK - pilot took control");
        break;
      case "WORLD_EVENT": onWorldEvent(s, m); break;
      case "AP_STATUS":
        pushEvent(s, m.t, m.ac_id, m.engaged ? "RELEASE" : "CLEAR", m.ok ? `autopilot ${m.engaged ? `ON · ${m.phase}` : "OFF"}` : `AP refused: ${m.reason}`);
        break;
      case "TRUST": s.trust.set(m.ac_id, m); break;
      case "CRYSTAL": s.crystal.set(m.ac_id, m); break;
      case "ENV": s.da = m.da_field_ft; renderDA(s); break;
      case "WX": s.wx = m; s.da = m.da_field_ft; renderWeather(s); renderDA(s); break;
      case "WX_FIELD": s.thermals = m.thermals || []; break;
      case "LIVE_TRAFFIC": s.live = { m, at: performance.now() }; break;
    }
  }, (status) => {
    $("god-link").textContent = status === "open" ? "LIVE" : status.toUpperCase();
    $("god-link").dataset.state = status;
  });

  // density-altitude slider -> SET_DA
  const slider = $("god-da");
  let daTimer = 0;
  slider.addEventListener("input", () => {
    $("god-da-val").textContent = `${(+slider.value).toLocaleString()} ft`;
    $("god-climb").textContent = `${Math.round(climbFpm(+slider.value))} fpm`;
    clearTimeout(daTimer);
    daTimer = setTimeout(() => link.send({ type: "SET_DA", ft: +slider.value }), 120);
  });

  const cv = $("god-map");
  panZoom(cv, s.view);
  const frame = () => {
    draw(cv, s);
    renderTable(s);
    renderEvents(s);
    renderLive(s);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// ---------- live sky (real ADS-B, display only) ----------
function renderLive(s) {
  const el = $("god-live");
  if (!s.live) { el.textContent = "Live sky: no feed (python data/live_traffic.py)"; el.dataset.state = ""; return; }
  const age = (performance.now() - s.live.at) / 1000, ac = s.live.m.aircraft || [];
  const inPat = ac.filter((a) => a.in_pattern).length;
  el.textContent = `Live sky (${s.live.m.source || "ADS-B"}): ${ac.length} aircraft · ${inPat} in the pattern · ${age.toFixed(0)} s ago`;
  el.dataset.state = age * 1000 > LIVE_STALE_MS ? "stale" : "live";
}

// ---------- ground-truth separation (world/separation.py) ----------
function onWorldEvent(s, m) {
  const key = `${m.a}|${m.b}`;
  if (m.event === "NMAC" || m.event === "COLLISION") {
    s.nmacOpen.set(key, m);
    s.sepMarks.push(m);
    if (m.counts) s.sepCounts = m.counts;
    pushEvent(s, m.t, `${m.a}/${m.b}`, m.event, `${m.event} ${m.h_ft} ft / ${m.v_ft} ft · ${(m.legs || []).join("/").toLowerCase()}`);
  } else if (m.event === "STOPPED") {
    pushEvent(s, m.t, m.a, m.runway ? "CLEAR" : "TRAFFIC", m.runway ? `stopped on runway ${m.runway}` : "stopped OFF runway");
    return;
  } else if (m.event === "RESET") {
    pushEvent(s, m.t, m.a, "CLEAR", `reset to scenario start (was ${m.was})`);
    s.trails.delete(m.a);
    return;
  } else if (m.event === "TAWS") {
    if (m.alert) pushEvent(s, m.t, m.a, m.alert === "PULL UP" || m.alert === "TERRAIN" ? "NMAC" : "TRAFFIC",
                           `TAWS ${m.alert} · ${m.agl_ft} ft AGL, ${m.vs_fpm} fpm`);
    return;
  } else if (m.event === "TOUCHDOWN" || m.event === "LIFTOFF" || m.event === "AP_DISCONNECT") {
    const where = m.surface && m.surface !== "RUNWAY" ? ` ${m.surface.replace("_", " ").toLowerCase()}` : "";
    const txt = m.event === "TOUCHDOWN" ? `${m.surface === "TERRAIN_IMPACT" ? "TERRAIN IMPACT" : m.hard ? "HARD LANDING" : "touchdown"}${where && m.surface !== "TERRAIN_IMPACT" ? where : ""} ${m.vs_fpm} fpm`
      : m.event === "LIFTOFF" ? `liftoff ${m.ias_kt} kt` : "autopilot disconnect (stick)";
    pushEvent(s, m.t, m.a, m.hard ? "NMAC" : "CLEAR", txt);
    return;
  } else if (m.event === "NMAC_END") {
    s.nmacOpen.delete(key);
    pushEvent(s, m.t, `${m.a}/${m.b}`, m.collided ? "COLLISION" : "NMAC",
      `closest ${m.min_h_ft} ft / ${m.min_v_ft} ft${m.collided ? " · collided" : ""}`);
  }
  const c = s.sepCounts;
  $("god-sep").textContent = `truth: ${c.NMAC} NMAC · ${c.COLLISION} collision${c.COLLISION === 1 ? "" : "s"}`;
  $("god-sep").dataset.hot = c.NMAC + c.COLLISION > 0 ? "1" : "";
}

// ---------- side panel ----------
function loadToggles() {
  const def = Object.fromEntries(Object.keys(LAYERS).map((k) => [k, true]));
  try { return { ...def, ...JSON.parse(localStorage.getItem("flock.god.layers") || "{}") }; } catch { return def; }
}

function buildToggles(s) {
  const box = $("god-layers");
  box.innerHTML = Object.entries(LAYERS).map(([k, label]) =>
    `<label><input type="checkbox" data-layer="${k}" ${s.show[k] ? "checked" : ""}> ${label}</label>`).join("");
  box.addEventListener("change", (e) => {
    const k = e.target.dataset.layer;
    if (!k) return;
    s.show[k] = e.target.checked;
    try { localStorage.setItem("flock.god.layers", JSON.stringify(s.show)); } catch { /* private mode */ }
  });
}

function renderWeather(s) {
  const w = s.stat.metar || {};
  $("god-metar").textContent = w.raw || (w.station
    ? `${w.station} ${w.observed || ""}  ${String(w.wind_dir_deg ?? "---").padStart(3, "0")}@${w.wind_kt ?? "-"}kt  ${w.temp_c ?? "-"}°C  A${w.altimeter_inhg ?? "-"}`
    : "no METAR");
  $("god-scn").textContent = `${s.stat.scenario} · x${s.stat.time_scale}`;
  const x = s.wx;
  if (!x) return;
  $("god-wx-line").textContent = `world: ${x.metar_style}`;
  const stale = x.stale_altimeters || 0;
  $("god-wx-note").textContent = (x.note || (x.name === "custom" ? "custom weather" : ""))
    + (stale ? ` · ${stale} aircraft on an old altimeter setting` : "");
  // reflect the state in the controls unless the user is dragging one
  const box = $("god-wx");
  if (!box.firstChild) return;
  const sel = box.querySelector("select[data-f=preset]");
  if (sel && document.activeElement !== sel) sel.value = [...sel.options].some((o) => o.value === x.name) ? x.name : "custom";
  const tsel = box.querySelector("select[data-f=turbulence]");
  if (tsel && document.activeElement !== tsel) tsel.value = String(x.turbulence);
  for (const [f, , , , , fmt] of WX_SLIDERS) {
    const el = box.querySelector(`input[data-f=${f}]`);
    const v = f === "ceiling_ft_agl" ? (x[f] || 0) : x[f];
    if (el && document.activeElement !== el && v != null) el.value = v;
    if (el) box.querySelector(`output[data-f=${f}]`).textContent = fmt(el.value);
  }
  const up = box.querySelector("button[data-f=update_altimeters]");
  if (up) up.dataset.hot = stale ? "1" : "";
}

// World weather controls -> SET_WX (presets, sliders, turbulence, update altimeters)
function buildWxControls(s, link, presets) {
  const box = $("god-wx");
  if (box.firstChild) return;
  const opt = (v, label) => `<option value="${v}">${label}</option>`;
  box.innerHTML =
    `<label>Preset</label><select class="wide" data-f="preset">${presets.map((p) => opt(p, p.replaceAll("_", " "))).join("")}${opt("custom", "custom")}</select>`
    + `<label>Turbulence</label><select class="wide" data-f="turbulence">${["none", "light", "moderate", "severe"].map((n, i) => opt(i, n)).join("")}</select>`
    + WX_SLIDERS.map(([f, label, mn, mx, st]) =>
      `<label>${label}</label><input type="range" data-f="${f}" min="${mn}" max="${mx}" step="${st}"><output data-f="${f}"></output>`).join("")
    + `<span></span><button class="wide" type="button" data-f="update_altimeters" title="ATIS update: every aircraft dials in the current QNH">Update all altimeters to QNH</button>`;
  const timers = {};
  const send = (msg, key) => { clearTimeout(timers[key]); timers[key] = setTimeout(() => link.send({ type: "SET_WX", ...msg }), 150); };
  box.addEventListener("input", (e) => {
    const f = e.target.dataset.f;
    if (!f || e.target.tagName !== "INPUT") return;
    const sl = WX_SLIDERS.find((x) => x[0] === f);
    box.querySelector(`output[data-f=${f}]`).textContent = sl[5](e.target.value);
    const v = f === "ceiling_ft_agl" && +e.target.value === 0 ? null : +e.target.value;
    send({ [f]: v }, f);
  });
  box.addEventListener("change", (e) => {
    const f = e.target.dataset.f;
    if (f === "preset" && e.target.value !== "custom") send({ preset: e.target.value }, "preset");
    if (f === "turbulence") send({ turbulence: +e.target.value }, "turbulence");
  });
  box.addEventListener("click", (e) => {
    if (e.target.dataset.f === "update_altimeters") link.send({ type: "SET_WX", update_altimeters: true });
  });
}

function renderDA(s) {
  if (s.da == null) return;
  if (document.activeElement !== $("god-da")) $("god-da").value = Math.round(s.da);
  $("god-da-val").textContent = `${Math.round(s.da).toLocaleString()} ft`;
  $("god-climb").textContent = `${Math.round(climbFpm(s.da))} fpm`;
}

function pushEvent(s, t, ac, level, text) {
  // nodes may repeat an advisory every tick: log a change, not a repeat
  const prev = s.events.find((e) => e.ac === ac);
  if (prev && prev.level === level && prev.text === text && t - prev.t < 15) return;
  s.events.unshift({ t, ac, level, text });
  if (s.events.length > EVENTS_MAX) s.events.length = EVENTS_MAX;
  s.eventsDirty = true;
}

function renderEvents(s) {
  if (!s.eventsDirty) return;
  s.eventsDirty = false;
  const esc = (x) => String(x ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  $("god-events").innerHTML = s.events.map((e) =>
    `<li data-level="${esc(e.level)}"><time>${new Date(e.t * 1000).toLocaleTimeString([], { hour12: false })}</time>`
    + `<b>${esc(e.ac)}</b><span>${esc(e.text)}</span></li>`).join("") || `<li class="muted">no decisions yet</li>`;
}

let lastTable = "";
function renderTable(s) {
  const rows = [...s.ac.values()].sort((a, b) => a.ac_id.localeCompare(b.ac_id)).map((a) => {
    const adv = s.adv.get(a.ac_id);
    const lvl = adv && s.t - adv.t < ADV_TTL_S && adv.level !== "CLEAR" ? adv.level : "";
    const kind = a.human ? "human" : a.flock ? "ai" : "noflock";
    const vs = Math.round(a.vs_fpm / 50) * 50;
    return `<tr data-kind="${kind}" data-mode="${a.mode}"><td>${a.ac_id}</td><td>${a.leg ?? ""}</td>`
      + `<td>${Math.round(a.alt_msl_ft)}</td><td>${vs > 0 ? "+" : ""}${vs}</td><td>${Math.round(a.ias_kt)}</td>`
      + `<td>${a.ap_phase ? `AP ${a.ap_phase.toLowerCase()}` : a.mode}</td>`
      + `<td data-level="${lvl}">${lvl}</td></tr>`;
  }).join("");
  if (rows !== lastTable) { $("god-table").innerHTML = rows; lastTable = rows; }
}

// ---------- map ----------
function panZoom(cv, v) {
  let drag = null;
  cv.addEventListener("wheel", (e) => {
    e.preventDefault();
    const r = cv.getBoundingClientRect(), dpr = devicePixelRatio || 1;
    const mx = (e.clientX - r.left) * dpr, my = (e.clientY - r.top) * dpr;
    const before = toWorld(cv, v, mx, my);
    v.scale *= Math.exp(-e.deltaY * 0.0015);
    v.scale = Math.max(0.005, Math.min(2, v.scale));
    const after = toWorld(cv, v, mx, my);
    v.cx += before[0] - after[0]; v.cy += before[1] - after[1];
  }, { passive: false });
  cv.addEventListener("pointerdown", (e) => { drag = [e.clientX, e.clientY]; cv.setPointerCapture(e.pointerId); });
  cv.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dpr = devicePixelRatio || 1;
    v.cx -= (e.clientX - drag[0]) * dpr / v.scale; v.cy += (e.clientY - drag[1]) * dpr / v.scale;
    drag = [e.clientX, e.clientY];
  });
  cv.addEventListener("pointerup", () => { drag = null; });
  cv.addEventListener("dblclick", () => { v.fitted = false; });
}

function toWorld(cv, v, x, y) { return [v.cx + (x - cv.width / 2) / v.scale, v.cy - (y - cv.height / 2) / v.scale]; }

function fitView(cv, s) {
  const pts = [];
  for (const p of Object.values(s.stat.patterns)) for (const seg of Object.values(p.legs)) for (const ll of seg) pts.push(s.local.toEnu(...ll));
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  s.view.cx = (x0 + x1) / 2; s.view.cy = (y0 + y1) / 2;
  s.view.scale = Math.min(cv.width / (x1 - x0 + 1500), cv.height / (y1 - y0 + 1500));
  s.view.fitted = true;
}

// Where aircraft `id` is predicted to be at world time T: a node prediction of it (its own, or the
// advising node's prediction of it as a peer), else straight-line from truth. Returns {lat, lon, src}.
function predictedAt(s, id, T, adviser) {
  const cands = [s.pred.get(`${id}>${id}`), adviser ? s.pred.get(`${adviser}>${id}`) : null];
  for (const m of cands) {
    if (!m || !m.path || m.path.length < 2 || s.t - m.t > PRED_TTL_S) continue;
    const dt = T - m.t, path = m.path;
    if (dt < path[0].t || dt > path[path.length - 1].t) continue;
    for (let i = 1; i < path.length; i++) {
      if (dt <= path[i].t) {
        const a = path[i - 1], b = path[i], f = (dt - a.t) / Math.max(1e-6, b.t - a.t);
        return { lat: a.lat + (b.lat - a.lat) * f, lon: a.lon + (b.lon - a.lon) * f, src: m.method };
      }
    }
  }
  const a = s.ac.get(id);
  if (!a) return null;
  const d = a.gs_kt * KT * Math.max(0, T - s.t), r = a.track_deg * D2R;
  return { lat: a.lat + d * Math.cos(r) / 111320, lon: a.lon + d * Math.sin(r) / (111320 * Math.cos(a.lat * D2R)), src: "truth straight-line" };
}

function draw(cv, s) {
  const r = cv.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  const W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; s.view.fitted = false; }
  const ctx = cv.getContext("2d"), v = s.view, k = dpr, show = s.show;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = css("var(--map-bg)"); ctx.fillRect(0, 0, W, H);
  if (!s.stat) { text(ctx, W / 2, H / 2, "waiting for world…", 16 * k, "center", css("var(--text-2)")); return; }
  if (!v.fitted) fitView(cv, s);
  const P = (e, n) => [W / 2 + (e - v.cx) * v.scale, H / 2 - (n - v.cy) * v.scale];
  const LL = (lat, lon) => P(...s.local.toEnu(lat, lon));
  const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 180);

  // 1 km grid
  ctx.strokeStyle = css("var(--grid)"); ctx.lineWidth = 1;
  const [wx0, wy1] = toWorld(cv, v, 0, 0), [wx1, wy0] = toWorld(cv, v, W, H);
  const step = v.scale > 0.05 ? 1000 : 5000;
  ctx.beginPath();
  for (let x = Math.floor(wx0 / step) * step; x < wx1; x += step) { const [px] = P(x, 0); ctx.moveTo(px, 0); ctx.lineTo(px, H); }
  for (let y = Math.floor(wy0 / step) * step; y < wy1; y += step) { const [, py] = P(0, y); ctx.moveTo(0, py); ctx.lineTo(W, py); }
  ctx.stroke();

  // 3 NM radio range ring around the field
  ctx.strokeStyle = css("var(--grid-strong)"); ctx.setLineDash([6 * k, 8 * k]);
  ctx.beginPath(); const [ax, ay] = P(0, 0); ctx.arc(ax, ay, 3 * NM * v.scale, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);

  // pattern legs
  if (show.pattern) for (const p of Object.values(s.stat.patterns)) {
    for (const [name, seg] of Object.entries(p.legs)) {
      if (name === "STRAIGHT_IN") continue;
      const [x0, y0] = LL(...seg[0]), [x1, y1] = LL(...seg[1]);
      ctx.strokeStyle = css("var(--pattern)"); ctx.lineWidth = 1.5 * k; ctx.setLineDash([8 * k, 6 * k]);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
      if (show.labels && v.scale > 0.04) text(ctx, (x0 + x1) / 2, (y0 + y1) / 2 - 6 * k, `${p.runway} ${name.toLowerCase()}`, 10 * k, "center", css("var(--pattern)"));
    }
  }
  // runways (data/runways.py: ends{id: lat, lon, far_lat, far_lon, width_ft})
  const ends = Object.entries(s.stat.airport.ends || {});
  ctx.strokeStyle = css("var(--runway)"); ctx.lineCap = "butt";
  for (const [, e] of ends) {
    const [x0, y0] = LL(e.lat, e.lon), [x1, y1] = LL(e.far_lat, e.far_lon);
    ctx.lineWidth = Math.max(4 * k, (e.width_ft || 75) * 0.3048 * v.scale);
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  }
  for (const [id, e] of ends) {               // label each end, pushed off the runway end
    const [x0, y0] = LL(e.lat, e.lon), [x1, y1] = LL(e.far_lat, e.far_lon);
    const d = Math.hypot(x1 - x0, y1 - y0) || 1, off = 14 * k;
    text(ctx, x0 - (x1 - x0) / d * off, y0 - (y1 - y0) / d * off + 4 * k, id, 12 * k, "center", css("var(--text)"));
  }

  // thermals (world weather): rising columns drifting with the wind
  if (show.weather) for (const [e, n, r, pk] of s.thermals) {
    const [x, y] = P(e, n);
    ctx.globalAlpha = 0.18; ctx.fillStyle = css("var(--lvl-traffic)");
    ctx.beginPath(); ctx.arc(x, y, Math.max(4 * k, r * v.scale), 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha = 1;
    if (show.labels && v.scale > 0.06) text(ctx, x, y + 4 * k, `+${pk} fpm`, 10 * k, "center", css("var(--lvl-traffic)"));
  }

  // live sky: real ADS-B traffic (cyan, small), display only
  if (show.live && s.live) {
    const age = performance.now() - s.live.at;
    if (age < LIVE_HIDE_MS) {
      const col = css("var(--live)");
      ctx.globalAlpha = age > LIVE_STALE_MS ? 0.35 : 0.95;
      for (const a of s.live.m.aircraft || []) {
        const [x, y] = LL(a.lat, a.lon), sz = 5.5 * k;
        ctx.save(); ctx.translate(x, y); ctx.rotate((a.track_deg || 0) * D2R);
        ctx.fillStyle = col; ctx.strokeStyle = "#000"; ctx.lineWidth = 1 * k;
        ctx.beginPath(); ctx.moveTo(0, -sz * 1.4); ctx.lineTo(sz, sz); ctx.lineTo(0, sz * 0.4); ctx.lineTo(-sz, sz); ctx.closePath();
        a.on_ground ? ctx.stroke() : ctx.fill();
        ctx.restore();
        if (show.labels && v.scale > 0.03) {
          const alt = a.on_ground ? "GND" : String(Math.round(a.alt_msl_ft / 100)).padStart(3, "0");
          const leg = a.in_pattern && a.leg ? ` ${a.leg.slice(0, 2)}` : "";
          text(ctx, x + 8 * k, y + 3 * k, `${(a.callsign || a.id || "").trim()} ${alt}${leg}`, 9.5 * k, "left", col);
        }
      }
      ctx.globalAlpha = 1;
    }
  }

  // trails
  if (show.trails) for (const [, tr] of s.trails) {
    ctx.strokeStyle = css("var(--trail)"); ctx.lineWidth = 1.2 * k;
    ctx.beginPath();
    tr.forEach(([e, n], i) => { const [x, y] = P(e, n); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }

  // predictions
  if (show.pred) for (const [key, m] of s.pred) {
    if (s.t - m.t > PRED_TTL_S || !m.path || !m.path.length) continue;
    const peer = m.target_id && m.target_id !== m.ac_id;
    const fallback = /straight/i.test(m.method || "") || (m.confidence ?? 1) < 0.6;
    const col = css(fallback ? "var(--lvl-traffic)" : "var(--pred)");
    const pts = m.path.map((p) => LL(p.lat, p.lon));
    if (show.sigma) {
      ctx.globalAlpha = 0.06; ctx.fillStyle = col;
      m.path.forEach((p, i) => { if (p.sigma_m) { ctx.beginPath(); ctx.arc(pts[i][0], pts[i][1], p.sigma_m * v.scale, 0, Math.PI * 2); ctx.fill(); } });
      ctx.globalAlpha = 1;
    }
    ctx.strokeStyle = col; ctx.lineWidth = (peer ? 1.2 : 2) * k; ctx.globalAlpha = peer ? 0.6 : 0.9;
    ctx.setLineDash(peer ? [2 * k, 5 * k] : fallback ? [7 * k, 5 * k] : []);
    ctx.beginPath(); pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y))); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    if (!peer) {                                      // tick every 10 s + method/confidence at the end
      ctx.fillStyle = col;
      m.path.forEach((p, i) => { if (p.t > 0 && p.t % 10 === 0) { ctx.beginPath(); ctx.arc(pts[i][0], pts[i][1], 2.2 * k, 0, Math.PI * 2); ctx.fill(); } });
      if (show.labels) {
        const [ex, ey] = pts[pts.length - 1];
        const conf = m.confidence != null ? ` ${(+m.confidence).toFixed(2)}` : "";
        text(ctx, ex + 6 * k, ey - 4 * k, `${m.method}${conf}${m.leg ? ` · ${m.leg.toLowerCase()}` : ""}`, 10 * k, "left", col);
      }
    }
  }

  // layer rings, conflict markers (from each node's latest advisory)
  for (const [id, m] of s.adv) {
    const a = s.ac.get(id);
    if (!a || s.t - m.t > ADV_TTL_S || m.level === "CLEAR" || m.level === "RELEASE") continue;
    const [x, y] = LL(a.lat, a.lon);
    const col = css(LEVEL_COLOR[m.level] || "#fff");
    const R = Math.max(14 * k, (LAYER_R_M[m.layer] || 400) * v.scale) * (m.level === "TAKEOVER" ? 1 + 0.12 * pulse : 1);
    ctx.strokeStyle = col; ctx.lineWidth = 2.5 * k;
    ctx.setLineDash(m.level === "NO_SOLUTION" ? [6 * k, 4 * k] : []);
    ctx.beginPath(); ctx.arc(x, y, R, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    if (show.labels) text(ctx, x, y - R - 4 * k, m.level, 10 * k, "center", col, true);

    if (!show.conflict || !CONFLICT_LEVELS.has(m.level) || m.ttc_s == null || !m.target_id) continue;
    const T = m.t + m.ttc_s, left = T - s.t;
    if (left < -2) continue;
    const pa = predictedAt(s, id, T, id), pb = predictedAt(s, m.target_id, T, id);
    if (!pa || !pb) continue;
    const [px, py] = LL(pa.lat, pa.lon), [qx, qy] = LL(pb.lat, pb.lon);
    const tgt = s.ac.get(m.target_id);
    ctx.strokeStyle = col; ctx.lineWidth = 1.2 * k; ctx.globalAlpha = 0.5; ctx.setLineDash([3 * k, 4 * k]);
    ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(px, py);
    if (tgt) { const [tx, ty] = LL(tgt.lat, tgt.lon); ctx.moveTo(tx, ty); ctx.lineTo(qx, qy); }
    ctx.stroke(); ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.lineWidth = 2.5 * k;
    ctx.beginPath(); ctx.moveTo(px, py); ctx.lineTo(qx, qy); ctx.stroke();
    for (const [cx, cy] of [[px, py], [qx, qy]]) cross(ctx, cx, cy, 7 * k);
    const miss = m.reason && m.reason.predicted_miss_ft != null ? `miss ${Math.round(m.reason.predicted_miss_ft)} ft · ` : "";
    // label beside the conflict segment (offset along its normal, away from the aircraft labels)
    const dx = qx - px, dy = qy - py, dl = Math.hypot(dx, dy) || 1;
    const nx = -dy / dl, ny = dx / dl, side = ny > 0 ? 1 : -1;
    text(ctx, (px + qx) / 2 + nx * 22 * k * side, (py + qy) / 2 + ny * 22 * k * side + 4 * k,
         `${id}↔${m.target_id}  ${miss}${Math.max(0, Math.round(left))} s`, 12 * k, "center", col, true);
  }

  // ground-truth separation: open NMAC pairs joined in red, event markers where they happened
  for (const [, e] of s.nmacOpen) {
    const a = s.ac.get(e.a), b = s.ac.get(e.b);
    if (!a || !b) continue;
    const [x0, y0] = LL(a.lat, a.lon), [x1, y1] = LL(b.lat, b.lon);
    ctx.strokeStyle = css("var(--trust-fake)"); ctx.lineWidth = (3 + 2 * pulse) * k;
    ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke();
  }
  s.sepMarks = s.sepMarks.filter((e) => s.t - e.t < SEP_MARK_S);
  for (const e of s.sepMarks) {
    const [x, y] = LL(e.lat, e.lon), col = css("var(--trust-fake)");
    const age = (s.t - e.t) / SEP_MARK_S, R = (e.event === "COLLISION" ? 26 : 18) * k * (1 + age);
    ctx.globalAlpha = 1 - age; ctx.strokeStyle = col; ctx.lineWidth = 2.5 * k;
    for (let i = 0; i < 8; i++) {                       // burst
      const th = i * Math.PI / 4;
      ctx.beginPath(); ctx.moveTo(x + Math.cos(th) * R * 0.45, y + Math.sin(th) * R * 0.45);
      ctx.lineTo(x + Math.cos(th) * R, y + Math.sin(th) * R); ctx.stroke();
    }
    text(ctx, x, y - R - 6 * k, `${e.event} ${e.a}/${e.b} · ${e.h_ft} ft / ${e.v_ft} ft`, 12 * k, "center", col, true);
    ctx.globalAlpha = 1;
  }

  // Escape Crystal (Phase 3, schemas.Crystal): reachable end-points, green = safe, red = blocked
  for (const [id, m] of s.crystal) {
    if (s.t - m.t > 3 || !m.points) continue;
    for (const p of m.points) {
      const [x, y] = LL(p.lat, p.lon);
      ctx.fillStyle = css(p.safe ? "var(--trust-ok)" : "var(--trust-fake)"); ctx.globalAlpha = 0.7;
      ctx.beginPath(); ctx.arc(x, y, 3 * k, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    const a = s.ac.get(id);
    if (a && m.mfi != null) { const [x, y] = LL(a.lat, a.lon); text(ctx, x + 13 * k, y + 38 * k, `MFI ${m.mfi.toFixed(2)}`, 11 * k, "left", css("var(--trust-ok)")); }
  }

  // aircraft
  for (const a of s.ac.values()) {
    const [x, y] = LL(a.lat, a.lon);
    const col = css(a.mode === "COMMAND" ? "var(--lvl-takeover)" : a.human ? "var(--own)" : a.flock ? "var(--ai)" : "var(--noflock)");
    ctx.save(); ctx.translate(x, y); ctx.rotate(a.track_deg * D2R);
    ctx.fillStyle = col; ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5 * k;
    const sz = 9 * k;
    ctx.beginPath(); ctx.moveTo(0, -sz * 1.4); ctx.lineTo(sz, sz); ctx.lineTo(0, sz * 0.45); ctx.lineTo(-sz, sz); ctx.closePath();
    a.flock ? ctx.fill() : ctx.stroke();
    if (!a.flock) { ctx.strokeStyle = col; ctx.stroke(); }
    ctx.restore();

    // takeover box + countdown
    const c = s.cmd.get(a.ac_id);
    if (a.mode === "COMMAND" && c) {
      const b = 18 * k;
      ctx.strokeStyle = css("var(--lvl-takeover)"); ctx.lineWidth = 2 * k;
      ctx.strokeRect(x - b, y - b, 2 * b, 2 * b);
      const bank = +c.bank_cmd_deg || 0;
      text(ctx, x, y + b + 14 * k, `FLOCK ${bank < 0 ? "L" : "R"}${Math.abs(bank).toFixed(0)} · ${Math.max(0, c.until - s.t).toFixed(1)} s`, 11 * k, "center", css("var(--lvl-takeover)"), true);
    }
    const st = s.stick.get(a.ac_id);
    if (st != null && s.t - st < STICK_FLASH_S) {
      ctx.globalAlpha = 1 - (s.t - st) / STICK_FLASH_S;
      text(ctx, x, y + 34 * k, "STICK · pilot has control", 11 * k, "center", css("var(--lvl-release)"), true);
      ctx.globalAlpha = 1;
    }

    if (!show.labels) continue;
    const alt = String(Math.round(a.alt_msl_ft / 100)).padStart(3, "0");
    const vs = a.vs_fpm > 150 ? "↑" : a.vs_fpm < -150 ? "↓" : "";
    text(ctx, x + 13 * k, y - 4 * k, `${a.ac_id}${a.mode === "HUMAN" ? " ✋" : ""}`, 12 * k, "left", col, true);
    text(ctx, x + 13 * k, y + 10 * k, `${alt}${vs} ${Math.round(a.gs_kt)}kt ${a.leg ? a.leg.slice(0, 2) : ""}`, 11 * k, "left", css("var(--text-2)"));
  }

  drawWind(ctx, s, W, k);

  // scale bar
  const barM = v.scale > 0.05 ? NM : 2 * NM, bx = 16 * k, by = H - 16 * k;
  ctx.strokeStyle = css("var(--text-2)"); ctx.lineWidth = 2 * k;
  ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + barM * v.scale, by); ctx.stroke();
  text(ctx, bx, by - 6 * k, `${barM / NM} NM`, 11 * k, "left", css("var(--text-2)"));
}

// Wind arrow (points where the wind blows TO) + DA, top-left of the map.
function drawWind(ctx, s, W, k) {
  const m = s.stat.metar || {};
  const w = s.wx ? { wind_dir_deg: s.wx.wind_from_deg, wind_kt: s.wx.wind_kt, gust: s.wx.gust_kt } : m;
  const cx = 44 * k, cy = 48 * k, R = 22 * k;
  ctx.strokeStyle = css("var(--grid-strong)"); ctx.lineWidth = 1.5 * k;
  ctx.beginPath(); ctx.arc(cx, cy, R, 0, Math.PI * 2); ctx.stroke();
  text(ctx, cx, cy - R - 4 * k, "N", 10 * k, "center", css("var(--text-2)"));
  if (w.wind_kt) {
    const to = ((w.wind_dir_deg || 0) + 180) * D2R, L = R * 0.85;
    const ex = cx + L * Math.sin(to), ey = cy - L * Math.cos(to), sx = cx - L * Math.sin(to), sy = cy + L * Math.cos(to);
    ctx.strokeStyle = css("var(--text)"); ctx.fillStyle = css("var(--text)"); ctx.lineWidth = 2 * k;
    ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(ex, ey);
    ctx.lineTo(ex - 7 * k * Math.sin(to - 0.5), ey + 7 * k * Math.cos(to - 0.5));
    ctx.lineTo(ex - 7 * k * Math.sin(to + 0.5), ey + 7 * k * Math.cos(to + 0.5)); ctx.fill();
  }
  const wind = w.wind_kt ? `${String(Math.round(w.wind_dir_deg ?? 0)).padStart(3, "0")}° ${Math.round(w.wind_kt)} kt${w.gust > w.wind_kt ? ` G${Math.round(w.gust)}` : ""}` : "calm";
  text(ctx, cx + R + 10 * k, cy - 2 * k, `wind ${wind}`, 12 * k, "left", css("var(--text)"), true);
  if (s.da != null) text(ctx, cx + R + 10 * k, cy + 14 * k, `DA ${Math.round(s.da).toLocaleString()} ft · climb ${Math.round(climbFpm(s.da))} fpm`, 11 * k, "left", css("var(--text-2)"));
  if (s.wx) {
    const bits = [];
    if (s.wx.turbulence) bits.push(`${s.wx.turbulence_name} turbulence`);
    if (s.wx.shear_kt) bits.push(`shear -${s.wx.shear_kt} kt`);
    if (s.wx.thermals) bits.push("thermals");
    bits.push(`vis ${s.wx.visibility_sm} SM`);
    if (s.wx.ceiling_ft_agl) bits.push(`base ${s.wx.ceiling_ft_agl} ft`);
    bits.push(`QNH ${(+s.wx.qnh_inhg).toFixed(2)}`);
    text(ctx, cx + R + 10 * k, cy + 29 * k, bits.join(" · "), 11 * k, "left", css(s.wx.turbulence >= 2 ? "var(--lvl-traffic)" : "var(--text-2)"));
  }
}

function cross(ctx, x, y, r) {
  ctx.beginPath(); ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r); ctx.moveTo(x + r, y - r); ctx.lineTo(x - r, y + r); ctx.stroke();
}

// Text with a dark halo so labels stay legible over lines, discs and each other.
function text(ctx, x, y, s, size, align, color, bold) {
  ctx.font = `${bold ? "600 " : ""}${size}px ${SANS}`; ctx.textAlign = align;
  ctx.lineJoin = "round"; ctx.lineWidth = Math.max(2, size / 3.5); ctx.strokeStyle = css("var(--map-bg)");
  ctx.strokeText(s, x, y);
  ctx.fillStyle = color; ctx.fillText(s, x, y);
}
