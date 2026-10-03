// web/god.js — Lane A. God view (?role=god): the whole pattern from above, for the projector.
// Truth for every aircraft (TRUTH, 10 Hz) + every node's ADVISORY / TRUST / COMMAND / PREDICTION.
// Mouse wheel = zoom, drag = pan, double-click = reset.

import { connect, css, LEVEL_COLOR, Local, NM, TRUST_COLOR, MONO, SANS } from "./net.js";

const $ = (id) => document.getElementById(id);
const D2R = Math.PI / 180;
const TRAIL_S = 60, ADV_TTL_MS = 12000, PRED_TTL_MS = 4000;
const LAYER_R_M = { 1: 900, 2: 650, 3: 450, 4: 300 };   // ring radius by layer (visual only)

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
    trust: new Map(), crystal: new Map(), t: 0, da: null,
    view: { cx: 0, cy: 0, scale: 0.08, fitted: false },
  };

  const link = connect("god", (m) => {
    const now = performance.now();
    switch (m.type) {
      case "HELLO":
        if (m.static) {
          s.stat = m.static;
          s.local = new Local(m.static.airport.lat, m.static.airport.lon);
          s.da = m.static.da_field_ft;
          renderWeather(s);
          $("god-da").value = Math.round(s.da);
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
      case "ADVISORY": s.adv.set(m.ac_id, { m, at: now }); break;
      case "PREDICTION": s.pred.set(m.ac_id, { m, at: now }); break;
      case "TRUST": s.trust.set(m.ac_id, m); break;
      case "CRYSTAL": s.crystal.set(m.ac_id, { m, at: now }); break;
      case "ENV": s.da = m.da_field_ft; renderDA(s); break;
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
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
}

// ---------- side panel ----------
function renderWeather(s) {
  const w = s.stat.metar || {};
  $("god-metar").textContent = w.raw || (w.station
    ? `${w.station} ${w.observed || ""}  ${String(w.wind_dir_deg ?? "---").padStart(3, "0")}@${w.wind_kt ?? "-"}kt  ${w.temp_c ?? "-"}°C  A${w.altimeter_inhg ?? "-"}`
    : "no METAR");
  $("god-scn").textContent = `${s.stat.scenario} · x${s.stat.time_scale}`;
}

function renderDA(s) {
  if (s.da == null) return;
  if (document.activeElement !== $("god-da")) $("god-da").value = Math.round(s.da);
  $("god-da-val").textContent = `${Math.round(s.da).toLocaleString()} ft`;
  $("god-climb").textContent = `${Math.round(climbFpm(s.da))} fpm`;
}

let lastTable = "";
function renderTable(s) {
  const now = performance.now();
  const rows = [...s.ac.values()].sort((a, b) => a.ac_id.localeCompare(b.ac_id)).map((a) => {
    const adv = s.adv.get(a.ac_id);
    const lvl = adv && now - adv.at < ADV_TTL_MS ? adv.m.level : "";
    const kind = a.human ? "human" : a.flock ? "ai" : "noflock";
    return `<tr data-kind="${kind}" data-mode="${a.mode}"><td>${a.ac_id}</td><td>${a.leg ?? ""}</td>`
      + `<td>${Math.round(a.alt_msl_ft)}</td><td>${Math.round(a.ias_kt)}</td><td>${a.mode}</td>`
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

function draw(cv, s) {
  const r = cv.getBoundingClientRect(), dpr = devicePixelRatio || 1;
  const W = Math.round(r.width * dpr), H = Math.round(r.height * dpr);
  if (cv.width !== W || cv.height !== H) { cv.width = W; cv.height = H; s.view.fitted = false; }
  const ctx = cv.getContext("2d"), v = s.view, k = dpr;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = css("var(--map-bg)"); ctx.fillRect(0, 0, W, H);
  if (!s.stat) { text(ctx, W / 2, H / 2, "waiting for world…", 16 * k, "center", css("var(--text-2)")); return; }
  if (!v.fitted) fitView(cv, s);
  const P = (e, n) => [W / 2 + (e - v.cx) * v.scale, H / 2 - (n - v.cy) * v.scale];
  const LL = (lat, lon) => P(...s.local.toEnu(lat, lon));
  const now = performance.now();

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
  for (const p of Object.values(s.stat.patterns)) {
    for (const [name, seg] of Object.entries(p.legs)) {
      if (name === "STRAIGHT_IN") continue;
      const [x0, y0] = LL(...seg[0]), [x1, y1] = LL(...seg[1]);
      ctx.strokeStyle = css("var(--pattern)"); ctx.lineWidth = 1.5 * k; ctx.setLineDash([8 * k, 6 * k]);
      ctx.beginPath(); ctx.moveTo(x0, y0); ctx.lineTo(x1, y1); ctx.stroke(); ctx.setLineDash([]);
      if (v.scale > 0.04) text(ctx, (x0 + x1) / 2, (y0 + y1) / 2 - 6 * k, `${p.runway} ${name.toLowerCase()}`, 10 * k, "center", css("var(--pattern)"));
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

  // predictions
  for (const [id, { m, at }] of s.pred) {
    if (now - at > PRED_TTL_MS || !m.path || !m.path.length) continue;
    ctx.strokeStyle = css("var(--pred)"); ctx.lineWidth = 1.5 * k; ctx.globalAlpha = 0.85;
    ctx.beginPath();
    m.path.forEach((p, i) => { const [x, y] = LL(p.lat, p.lon); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
    ctx.globalAlpha = 0.05; ctx.fillStyle = css("var(--pred)");
    for (const p of m.path) if (p.sigma_m) { const [x, y] = LL(p.lat, p.lon); ctx.beginPath(); ctx.arc(x, y, p.sigma_m * v.scale, 0, Math.PI * 2); ctx.fill(); }
    ctx.globalAlpha = 1;
  }

  // trails
  for (const [id, tr] of s.trails) {
    ctx.strokeStyle = css("var(--trail)"); ctx.lineWidth = 1.2 * k;
    ctx.beginPath();
    tr.forEach(([e, n], i) => { const [x, y] = P(e, n); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.stroke();
  }

  // conflict lines + layer rings (from each node's latest advisory)
  for (const [id, { m, at }] of s.adv) {
    const a = s.ac.get(id);
    if (!a || now - at > ADV_TTL_MS || m.level === "CLEAR") continue;
    const [x, y] = LL(a.lat, a.lon);
    const col = css(LEVEL_COLOR[m.level] || "#fff");
    ctx.strokeStyle = col; ctx.lineWidth = 2.5 * k;
    ctx.beginPath(); ctx.arc(x, y, Math.max(14 * k, (LAYER_R_M[m.layer] || 400) * v.scale), 0, Math.PI * 2); ctx.stroke();
    const tgt = m.target_id && s.ac.get(m.target_id);
    if (tgt) {
      const [tx, ty] = LL(tgt.lat, tgt.lon);
      ctx.setLineDash([5 * k, 5 * k]); ctx.lineWidth = 1.5 * k;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(tx, ty); ctx.stroke(); ctx.setLineDash([]);
      if (m.ttc_s != null) text(ctx, (x + tx) / 2, (y + ty) / 2 - 4 * k, `${m.level} ${Math.round(m.ttc_s)}s`, 11 * k, "center", col);
    }
  }

  // Escape Crystal (Phase 3, schemas.Crystal): reachable end-points, green = safe, red = blocked
  for (const [id, { m, at }] of s.crystal) {
    if (now - at > 3000 || !m.points) continue;
    for (const p of m.points) {
      const [x, y] = LL(p.lat, p.lon);
      ctx.fillStyle = css(p.safe ? "var(--trust-ok)" : "var(--trust-fake)"); ctx.globalAlpha = 0.7;
      ctx.beginPath(); ctx.arc(x, y, 3 * k, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
    const a = s.ac.get(id);
    if (a && m.mfi != null) { const [x, y] = LL(a.lat, a.lon); text(ctx, x + 13 * k, y + 24 * k, `MFI ${m.mfi.toFixed(2)}`, 11 * k, "left", css("var(--trust-ok)")); }
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
    const alt = String(Math.round(a.alt_msl_ft / 100)).padStart(3, "0");
    const vs = a.vs_fpm > 150 ? "↑" : a.vs_fpm < -150 ? "↓" : "";
    text(ctx, x + 13 * k, y - 4 * k, `${a.ac_id}${a.mode === "COMMAND" ? " FLOCK" : a.mode === "HUMAN" ? " ✋" : ""}`, 12 * k, "left", col, true);
    text(ctx, x + 13 * k, y + 10 * k, `${alt}${vs} ${Math.round(a.gs_kt)}kt ${a.leg ? a.leg.slice(0, 2) : ""}`, 11 * k, "left", css("var(--text-2)"));
  }

  // scale bar
  const barM = v.scale > 0.05 ? NM : 2 * NM, bx = 16 * k, by = H - 16 * k;
  ctx.strokeStyle = css("var(--text-2)"); ctx.lineWidth = 2 * k;
  ctx.beginPath(); ctx.moveTo(bx, by); ctx.lineTo(bx + barM * v.scale, by); ctx.stroke();
  text(ctx, bx, by - 6 * k, `${barM / NM} NM`, 11 * k, "left", css("var(--text-2)"));
}

function text(ctx, x, y, s, size, align, color, bold) {
  ctx.font = `${bold ? "600 " : ""}${size}px ${SANS}`; ctx.textAlign = align; ctx.fillStyle = color;
  ctx.fillText(s, x, y);
}
