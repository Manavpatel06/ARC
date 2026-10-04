// web/log.js — Lane D comms log + explain panel. Plain ES module, no build step.
// Open: http://<world-ip>:8080/log.html   (Manas's world serves web/ on :8080)
//   or  python -m http.server 8080 -d web  then http://localhost:8080/log.html?world=ws://<ip>:8765
// Reads role=log from the world: every frame is {"type":"LOG","src","kind","t","payload"}.

const qs = new URLSearchParams(location.search);
const WORLD = qs.get("world") || `ws://${location.hostname || "localhost"}:8765`;
const MAX_FRAMES = 150000;      // kept for export
const MAX_ROWS = 2500;          // kept in the DOM

const LEVEL_COLOR = { SEQUENCE: "var(--teal)", TRAFFIC: "var(--amber)", RESOLVE: "var(--orange)", TAKEOVER: "var(--red)",
  RELEASE: "var(--green)", NO_SOLUTION: "var(--purple)", CLEAR: "var(--grey)" };
const MSG_COLOR = { STATE: "var(--grey)", HEARTBEAT: "#9aa1ac", INTENT: "var(--blue)", SEQ_PROPOSE: "var(--teal)",
  SEQ_ACCEPT: "var(--teal)", MANEUVER_COMMIT: "var(--orange)", SIGHTING: "var(--purple)", KEYS: "#9aa1ac" };
const TRUST_COLOR = { TRUSTED: "var(--green)", SUSPICIOUS: "var(--amber)", FAKE: "var(--red)", CAMERA_ONLY: "var(--blue)" };
const NEGOTIATION = new Set(["INTENT", "SEQ_PROPOSE", "SEQ_ACCEPT", "MANEUVER_COMMIT"]);

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmtT = (t) => { if (!t) return "—"; const d = new Date(t * 1000); return d.toTimeString().slice(0, 8) + "." + Math.floor((t % 1) * 10); };
const num = (v, d = 0) => (typeof v === "number" ? v.toFixed(d) : v ?? "—");

// ---------------- state ----------------
const frames = [];          // every LOG frame received (export)
const entries = [];         // displayable entries {i, f, cls, typ, sum, src, kind, msg, dropped, decision}
const aircraft = new Set();
const acOff = new Set();
const lastTrust = new Map();  // "N101>GHOST7" -> state
const firstHeard = new Map(); // radio id -> first time any packet from it was on the air (time-to-detect)
const fakes = new Map();      // target id -> {t, by, dt} first time any node called it FAKE
let lastDecisionBanner = 0, lastSpoofBanner = 0;

// Trust evidence in words a judge can read (radio/evidence.py + radio/client.py reasons).
function evidenceText(ev) {
  const s = String(ev);
  let m;
  if (s === "unsigned") return "no signature";
  if (s === "unknown_key") return "signed with an unregistered key";
  if (s === "signed") return "valid signature";
  if (s === "plausible") return "flight path plausible";
  if ((m = s.match(/^rf_rssi_mismatch:(.+)$/))) return `signal strength ${m[1]} off for where it claims to be`;
  if ((m = s.match(/^rf_doppler_mismatch:(.+)$/))) return `Doppler ${m[1]} off for how it claims to move`;
  if ((m = s.match(/^kinematics_violation:(.+)$/))) return ({ speed: "impossible speed", position_jump: "position jumped",
    accel: "impossible acceleration", turn_rate: "impossible turn rate", climb_rate: "impossible climb rate" }[m[1]] || `impossible ${m[1]}`);
  if ((m = s.match(/^refuted_by:(.+)$/))) return `${m[1]} should hear it and does not`;
  if ((m = s.match(/^corroborated:(\d+)$/))) return `confirmed by ${m[1]} aircraft`;
  if (s === "no_corroboration") return "no other aircraft hears it";
  if (s.startsWith("sighting:")) return `seen by ${s.slice(9)}`;
  return s;
}
function rejectText(reason) {
  const r = String(reason || "");
  if (/unsigned_impersonation/.test(r)) return "unsigned packet pretending to be a registered aircraft";
  if (/bad_sig|signature/.test(r)) return "signature does not match (packet altered)";
  if (/replay_seq|seq/.test(r)) return "old sequence number (replayed packet)";
  if (/time_window|time/.test(r)) return "timestamp too old (replayed packet)";
  if (/key/.test(r)) return "key not registered";
  return r;
}
let paused = false, selected = null, rateCount = 0;
const st = { delivered: 0, dropped: 0, lost: 0, range: 0, lat: [], slot: 0, rejected: 0 };

// ---------------- classify ----------------
function classify(f) {
  const p = f.payload || {};
  const e = { f, src: f.src || "?", kind: f.kind || "?", t: f.t || p.t, typ: "", sum: "", tag: "", color: "", dropped: false, decision: false, show: true };
  if (f.kind === "decision") {
    e.decision = true; e.typ = p.type;
    if (p.type === "ADVISORY") {
      e.tag = p.level; e.color = LEVEL_COLOR[p.level] || "var(--grey)";
      e.sum = `${p.text || ""}` + (p.target_id ? `  · vs ${p.target_id}` : "") + (p.ttc_s != null ? `  · ttc ${num(p.ttc_s, 1)} s` : "");
    } else if (p.type === "COMMAND") {
      e.tag = p.mode; e.color = p.mode === "TAKEOVER" ? "var(--red)" : "var(--green)";
      e.sum = p.mode === "TAKEOVER" ? `bank ${num(p.bank_cmd_deg)}° · vs ${num(p.vs_cmd_fpm)} fpm · hold ${num(p.hold_s)} s` : `release (${(p.reason && (p.reason.cause || p.reason.why)) || "—"})`;
      if (p.applied === false) e.sum += `  · NOT APPLIED${p.rejected_by_world ? " (" + p.rejected_by_world + ")" : ""}`;
      else if (p.applied === true) e.sum += "  · applied";
    } else if (p.type === "TRUST") {
      e.tag = "TRUST"; e.color = "var(--grey)"; e.decision = false;
      const changes = [];
      for (const tg of p.targets || []) {
        const k = `${p.ac_id}>${tg.id}`;
        if (lastTrust.get(k) !== tg.state) changes.push(tg);
        lastTrust.set(k, tg.state);
      }
      e.trustChange = changes.length > 0;
      const list = (changes.length ? changes : p.targets || []);
      e.sum = list.map((tg) => `${tg.id} ${tg.state} ${num(tg.score, 2)}${tg.evidence?.length ? " (" + tg.evidence.map(evidenceText).join("; ") + ")" : ""}`).join("  ·  ");
      if (changes.length) e.color = TRUST_COLOR[changes[0].state] || e.color;
      // spoofing: a target leaving TRUSTED (or a new one that never was) is the story judges should see
      e.spoof = changes.filter((tg) => tg.state === "SUSPICIOUS" || tg.state === "FAKE");
      if (e.spoof.length) { e.tag = e.spoof.some((tg) => tg.state === "FAKE") ? "FAKE" : "SPOOF?"; e.decision = true; }
      for (const tg of e.spoof) if (tg.state === "FAKE" && !fakes.has(tg.id)) {
        const t0 = firstHeard.get(tg.id), t = f.t || p.t;
        fakes.set(tg.id, { t, by: p.ac_id, dt: t0 != null && t != null ? t - t0 : null });
      }
    } else { e.tag = p.type || "?"; e.sum = JSON.stringify(p).slice(0, 140); }
  } else if (f.kind === "radio") {
    e.msg = p.msg; e.typ = p.msg || p.type || "RADIO"; e.tag = e.typ; e.color = MSG_COLOR[e.typ] || "var(--grey)";
    e.src = p.from || f.src;
    e.dropped = p.delivered === false;
    if (p.from && p.msg && !firstHeard.has(p.from)) firstHeard.set(p.from, p.t ?? f.t);
    if (p.rejected_by) { e.reject = true; e.tag = "REJECTED"; e.color = "var(--red)"; }
    const b = p.body || {};
    let s = "";
    if (p.msg === "STATE") s = `${b.leg && b.leg !== "UNKNOWN" ? b.leg + " " : ""}${num(b.alt_press_ft)} ft ${num(b.gs_kt)} kt trk ${num(b.track_deg)}°` + (b.intent ? ` · ${b.intent}` : "");
    else if (p.msg === "MANEUVER_COMMIT") s = `commits ${b.sense} ${num(b.bank_deg)}° vs ${b.target} hold ${num(b.hold_s)} s`;
    else if (p.msg === "INTENT") s = `${b.leg} → ${b.intent} (valid ${num(b.valid_for_s)} s)`;
    else if (p.msg === "SEQ_PROPOSE") s = `${b.runway} order ${(b.order || []).join(" → ")}` + (b.extend_s ? ` extend ${JSON.stringify(b.extend_s)}` : "");
    else if (p.msg === "SEQ_ACCEPT") s = `accepts proposal #${b.proposal_seq}`;
    else if (p.msg === "HEARTBEAT") s = "alive";
    else s = JSON.stringify(b).slice(0, 120);
    const to = p.to && p.to !== "*" ? ` → ${p.to}` : "";
    e.sum = `#${p.seq ?? "?"}${to}  ${s}` + (e.dropped ? `   DROPPED: ${p.reason || "?"}` : "") + (p.rng_m != null ? `  · ${(p.rng_m / 1852).toFixed(1)} NM` : "");
    if (e.reject) e.sum = `${p.rejected_by} REJECTED ${p.msg || "packet"} claiming to be ${p.from}: ${rejectText(p.reason)}`;
  } else {
    e.typ = p.type || f.kind; e.tag = e.typ; e.color = "var(--grey)";
    if (p.type === "HELLO") e.sum = `world up · scenario ${p.static?.scenario || p.scenario || ""} · fleet ${(p.fleet || (p.aircraft || []).map((a) => a.id) || []).join(" ")}`;
    else if (p.type === "STICK") { e.sum = `${p.ac_id}: pilot moved the stick — authority returns to the pilot`; e.color = "var(--green)"; e.decision = true; }
    else if (p.type === "SET_DA") e.sum = `density altitude set to ${num(p.ft)} ft`;
    else if (p.type === "LIVE_TRAFFIC") {
      const ac = p.aircraft || [], inp = ac.filter((a) => a.in_pattern);
      e.tag = "LIVE ADS-B"; e.color = "#0e7490"; e.src = "live";
      e.sum = `${ac.length} real aircraft within ${p.radius_nm} NM (${p.source}) · ${inp.length} in a KDVT pattern` +
        (inp.length ? ": " + inp.slice(0, 6).map((a) => `${a.callsign} ${a.leg} ${a.runway}`).join(", ") : "") + " · display + prediction only";
    }
    else if (p.type === "SCHEMA_ERROR") { e.sum = `schema error from ${p.from_role} (${p.msg_type}): ${(p.error || []).join(" ")}`; e.color = "var(--red)"; }
    else e.sum = JSON.stringify(p).slice(0, 160);
  }
  return e;
}

// ---------------- stats ----------------
function updateStats(e) {
  const p = e.f.payload || {};
  if (e.kind === "radio") {
    if (p.delivered === false) {
      st.dropped++;
      const r = String(p.reason || "");
      if (/slot|collision/i.test(r)) st.slot++;
      if (/sig|seq|replay|time|window|schema|key/i.test(r)) st.rejected++;
      if (/^loss/i.test(r)) st.lost++;
      if (/range/i.test(r)) st.range++;
    } else if (p.msg !== "HEARTBEAT" || p.delivered === true) st.delivered++;
    if (typeof p.latency_s === "number") { st.lat.push(p.latency_s); if (st.lat.length > 500) st.lat.shift(); }
  }
  if (e.typ === "HELLO") {
    const s = p.static || {};
    const wx = s.metar || p.metar;
    if (wx) $("sWx").textContent = `${wx.temp_c}°C ${num(wx.altimeter_inhg, 2)} ${String(wx.wind_dir_deg).padStart(3, "0")}/${wx.wind_kt}`;
    const da = s.da_field_ft ?? wx?.density_altitude_ft;
    if (da != null) $("sDa").textContent = `${Math.round(da).toLocaleString()} ft`;
    for (const id of p.fleet || (p.aircraft || []).map((a) => a.id)) addAircraft(id);
  }
  if (e.typ === "SET_DA") $("sDa").textContent = `${Math.round(p.ft).toLocaleString()} ft`;
  if (p.ac_id) addAircraft(p.ac_id);
  if (e.kind === "radio" && p.from) addAircraft(p.from);
}
function paintStats() {
  $("sDel").textContent = st.delivered; $("sDrop").textContent = st.dropped;
  const tot = st.delivered + st.lost;      // link loss only: out-of-range and rejected packets are not "loss"
  $("sLoss").textContent = tot ? `${((100 * st.lost) / tot).toFixed(1)}%` : "0%";
  $("sLat").textContent = st.lat.length ? `${Math.round((1000 * st.lat.reduce((a, b) => a + b, 0)) / st.lat.length)} ms` : "—";
  $("sSlot").textContent = st.slot; $("sRej").textContent = st.rejected;
  $("sAc").textContent = aircraft.size;
  const dts = [...fakes.values()].map((x) => x.dt).filter((x) => x != null);
  $("sFake").textContent = fakes.size ? `${fakes.size}${dts.length ? ` · ${(dts.reduce((a, b) => a + b, 0) / dts.length).toFixed(1)} s` : ""}` : "0";
}
setInterval(() => { $("sRate").textContent = rateCount; rateCount = 0; paintStats(); }, 1000);

// ---------------- aircraft chips ----------------
function addAircraft(id) {
  if (!id || aircraft.has(id) || id === "god" || id === "world" || id === "channel") return;
  aircraft.add(id);
  const c = document.createElement("span");
  c.className = "chip"; c.textContent = id; c.dataset.id = id;
  c.onclick = () => { acOff.has(id) ? acOff.delete(id) : acOff.add(id); c.classList.toggle("off"); rerender(); };
  const chips = [...$("acChips").children, c].sort((a, b) => a.dataset.id.localeCompare(b.dataset.id));
  $("acChips").replaceChildren(...chips);
}

// ---------------- filter + render ----------------
function visible(e) {
  if (e.kind === "decision" && !$("kDec").checked) return false;
  if (e.kind === "radio" && !$("kRadio").checked) return false;
  if ((e.kind === "world" || e.kind === "camera") && !$("kWorld").checked) return false;
  const rej = e.dropped && /sig|seq|replay|time|window|schema|key/i.test(String(e.f.payload?.reason || ""));
  if (e.msg === "STATE" && !$("mState").checked && !$("mDropOnly").checked && !rej) return false;
  if (e.msg === "HEARTBEAT" && !$("mHb").checked) return false;
  if (e.typ === "TRUST" && !e.trustChange && !$("mTrustAll").checked) return false;
  if ($("mDropOnly").checked && !e.dropped) return false;
  if ($("mSpoof").checked && !(e.spoof?.length || e.reject)) return false;
  const p = e.f.payload || {};
  const who = [e.src, p.ac_id, p.from, p.to, p.target_id, p.body?.target].filter(Boolean);
  if (who.length && who.every((w) => acOff.has(w))) return false;
  const q = $("search").value.trim().toLowerCase();
  if (q && !(e.sum + " " + e.typ + " " + e.src + " " + e.tag).toLowerCase().includes(q)) return false;
  return true;
}
function rowEl(e) {
  const tr = document.createElement("tr");
  if (e.decision) tr.className = "decision";
  if (e.dropped) tr.className += " drop";
  tr.innerHTML = `<td class="t">${fmtT(e.t)}</td><td class="src">${esc(e.src)}</td>` +
    `<td class="typ"><span class="tag" style="background:${e.color}">${esc(e.tag)}</span></td><td class="sum">${esc(e.sum)}</td>`;
  tr.onclick = () => select(e, tr);
  e.tr = tr;
  return tr;
}
const tbody = $("rows"), wrap = $("tablewrap");
function append(e) {
  const atBottom = wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 30;
  tbody.appendChild(rowEl(e));
  while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.firstChild);
  if (atBottom && !paused) wrap.scrollTop = wrap.scrollHeight;
}
function rerender() {
  const out = [];
  for (let i = entries.length - 1; i >= 0 && out.length < MAX_ROWS; i--) if (visible(entries[i])) out.push(entries[i]);
  tbody.replaceChildren(...out.reverse().map(rowEl));
  wrap.scrollTop = wrap.scrollHeight;
}
for (const id of ["kDec", "kRadio", "kWorld", "mState", "mHb", "mTrustAll", "mDropOnly", "mSpoof"]) $(id).onchange = rerender;
$("search").oninput = () => { clearTimeout(rerender.t); rerender.t = setTimeout(rerender, 150); };

// ---------------- banner (big text for judges) ----------------
function spoofBanner(e) {
  if (performance.now() - lastDecisionBanner < 4000) return;      // a live advisory keeps the banner
  const p = e.f.payload || {};
  if (e.reject) {
    $("banner").innerHTML = `<span class="lvl" style="background:var(--red)">${esc(p.rejected_by)} · REJECTED</span>` +
      `<span class="txt">Packet claiming to be ${esc(p.from)} thrown away</span><span class="why">${esc(rejectText(p.reason))}</span>`;
    return;
  }
  lastSpoofBanner = performance.now();
  const tg = e.spoof[0], fk = fakes.get(tg.id);
  const fast = tg.state === "FAKE" && fk && fk.by === p.ac_id && fk.dt != null ? ` · caught ${fk.dt.toFixed(1)} s after it first transmitted` : "";
  const what = tg.state === "FAKE" ? `${tg.id} is FAKE: shown, never acted on` : `${tg.id} is SUSPICIOUS: warnings only, no maneuver`;
  $("banner").innerHTML = `<span class="lvl" style="background:${TRUST_COLOR[tg.state]}">${esc(p.ac_id)} · TRUST</span>` +
    `<span class="txt">${esc(what)}</span><span class="why">${esc((tg.evidence || []).map(evidenceText).join(" · ") + fast)}</span>`;
}
function banner(e) {
  const p = e.f.payload || {};
  if (e.spoof?.length || e.reject) return spoofBanner(e);
  if (!(p.type === "ADVISORY" || (p.type === "COMMAND") || p.type === "STICK")) return;
  if (p.type === "ADVISORY" && (p.level === "SEQUENCE" || p.level === "CLEAR") && performance.now() - lastSpoofBanner < 8000) return;   // let judges read the spoof verdict
  if (!(p.type === "ADVISORY" && (p.level === "CLEAR" || p.level === "SEQUENCE"))) lastDecisionBanner = performance.now();
  if (p.type === "ADVISORY" && p.level === "CLEAR") { /* keep showing, but grey */ }
  const r = p.reason || {};
  let why = [];
  if (r.predicted_miss_ft != null) why.push(`predicted miss ${num(r.predicted_miss_ft)} ft`);
  if (p.ttc_s != null) why.push(`ttc ${num(p.ttc_s, 1)} s`);
  if (r.method) why.push(`${r.method}${r.confidence != null ? " " + num(r.confidence, 2) : ""}`);
  if (r.chosen) why.push(`chose ${r.chosen}`);
  if (r.rejected) why.push("rejected " + Object.entries(r.rejected).map(([k, v]) => `${k}: ${v}`).join(", "));
  const label = p.type === "ADVISORY" ? p.level : p.type === "COMMAND" ? p.mode : "STICK";
  const text = p.type === "ADVISORY" ? p.text : p.type === "COMMAND" ? e.sum : "Pilot took control";
  $("banner").innerHTML = `<span class="lvl" style="background:${e.color}">${esc(e.src)} · ${esc(label)}</span>` +
    `<span class="txt">${esc(text)}</span><span class="why">${esc(why.join(" · "))}</span>`;
}

// ---------------- explain panel ----------------
function kv(obj, keys) {
  return `<div class="kv">${(keys || Object.keys(obj)).filter((k) => obj[k] !== undefined && typeof obj[k] !== "object")
    .map((k) => `<div>${esc(k)}</div><div>${esc(typeof obj[k] === "number" ? +obj[k].toFixed(3) : obj[k])}</div>`).join("")}</div>`;
}
function transcript(e) {
  const p = e.f.payload || {};
  const a = p.ac_id || e.src, b = p.target_id || p.reason?.target || p.body?.target;
  const t0 = e.t || 0;
  const seen = new Set();
  const lines = [];
  for (const x of entries) {
    if (x.kind !== "radio" || !NEGOTIATION.has(x.msg)) continue;
    const q = x.f.payload;
    if (Math.abs((x.t || 0) - t0) > 30) continue;
    const ends = [q.from, q.to, q.body?.target];
    if (!ends.includes(a) && !(b && ends.includes(b))) continue;
    const k = `${q.from}#${q.seq}`;
    if (seen.has(k)) continue; seen.add(k);
    lines.push(`<div><span style="color:${x.color}">${fmtT(x.t)} ${esc(q.from)} ${esc(q.msg)}</span> ${esc(x.sum)}</div>`);
  }
  return lines.length ? `<div class="tx">${lines.join("")}</div>` : `<div class="empty">No INTENT / SEQ / COMMIT messages between ${esc(a)}${b ? " and " + esc(b) : ""} within ±30 s.</div>`;
}
function select(e, tr) {
  if (selected?.tr) selected.tr.classList.remove("sel");
  selected = e; tr?.classList.add("sel");
  const p = e.f.payload || {};
  const r = p.reason || {};
  let h = `<h2><span class="tag" style="background:${e.color}">${esc(e.tag)}</span> ${esc(e.src)} <span style="color:var(--muted);font-weight:400">${fmtT(e.t)}</span></h2>`;
  h += `<div>${esc(e.sum)}</div>`;
  if (p.type === "ADVISORY") {
    h += `<h3>Advisory</h3>` + kv({ level: p.level, layer: p.layer, text: p.text, target: p.target_id, ttc_s: p.ttc_s, speak: p.speak });
  }
  if (p.type === "COMMAND") {
    h += `<h3>Command</h3>` + kv({ mode: p.mode, bank_cmd_deg: p.bank_cmd_deg, vs_cmd_fpm: p.vs_cmd_fpm, hold_s: p.hold_s,
      applied: p.applied, rejected_by_world: p.rejected_by_world });
    if (p.bounds) h += `<h3>Printed bounds (authority monitor)</h3>` + kv(p.bounds);
  }
  if (p.type === "ADVISORY" || p.type === "COMMAND") {
    h += `<h3>Why</h3>` + (Object.keys(r).length ? kv(r) : `<div class="empty">No reason given — every decision must carry one.</div>`);
    if (r.rejected && typeof r.rejected === "object") {
      h += `<h3>Maneuvers considered</h3><table class="mtab"><tr><th>maneuver</th><th>result</th></tr>`;
      if (r.chosen) h += `<tr><td>${esc(r.chosen)}</td><td class="ok">chosen</td></tr>`;
      for (const [k, v] of Object.entries(r.rejected)) h += `<tr><td>${esc(k)}</td><td class="bad">${esc(v)}</td></tr>`;
      h += `</table>`;
    }
    for (const [k, v] of Object.entries(r)) if (v && typeof v === "object" && k !== "rejected") h += `<h3>${esc(k)}</h3><pre>${esc(JSON.stringify(v, null, 1))}</pre>`;
    h += `<h3>Negotiation transcript (±30 s)</h3>` + transcript(e);
  }
  if (p.type === "TRUST") {
    h += `<h3>Trust picture of ${esc(p.ac_id)}</h3><table class="mtab"><tr><th>target</th><th>state</th><th>score</th><th>evidence</th><th>position</th></tr>`;
    for (const tg of p.targets || []) {
      const rel = tg.rel ? `${num(tg.rel.brg_deg)}° ${(tg.rel.rng_m / 1852).toFixed(2)} NM ${tg.rel.dalt_ft >= 0 ? "+" : ""}${num(tg.rel.dalt_ft)} ft` : "—";
      h += `<tr><td>${esc(tg.id)}</td><td style="color:${TRUST_COLOR[tg.state]};font-weight:700">${esc(tg.state)}</td><td>${num(tg.score, 2)}</td><td>${esc((tg.evidence || []).map(evidenceText).join("; "))}</td><td>${esc(rel)}</td></tr>`;
    }
    h += `</table><div class="empty" style="margin-top:6px">Only TRUSTED targets may trigger RESOLVE or TAKEOVER. SUSPICIOUS = warnings only. FAKE = shown, never acted on.</div>`;
  }
  if (e.kind === "radio") {
    h += `<h3>Delivery</h3>` + kv({ from: p.from, to: p.to, seq: p.seq, delivered: p.delivered, reason: p.reason, range_nm: p.rng_m != null ? +(p.rng_m / 1852).toFixed(2) : undefined, latency_s: p.latency_s, signed: p.sig ? "yes" : "NO" });
    h += `<h3>Body</h3>` + kv(p.body || {});
    if (NEGOTIATION.has(p.msg)) h += `<h3>Negotiation transcript (±30 s)</h3>` + transcript(e);
  }
  h += `<h3>Raw frame</h3><pre>${esc(JSON.stringify(e.f, null, 1))}</pre>`;
  $("detail").innerHTML = h;
}

// ---------------- ingest ----------------
function ingest(f, live = true) {
  if (!f || typeof f !== "object") return;
  if (f.type !== "LOG") f = { type: "LOG", src: f.ac_id || f.from || "world", kind: f.type === "HELLO" ? "world" : "world", t: f.t, payload: f };
  frames.push(f); if (frames.length > MAX_FRAMES) frames.shift();
  rateCount++;
  const e = classify(f);
  updateStats(e);
  entries.push(e); if (entries.length > MAX_FRAMES) entries.shift();
  if (e.decision || e.typ === "STICK" || e.reject) banner(e);
  if (!paused && visible(e) && live) append(e);
}

let ws, retry;
function connect() {
  try { ws = new WebSocket(`${WORLD}?role=log`); } catch (err) { schedule(); return; }
  ws.onopen = () => { $("conn").classList.add("on"); $("conn").title = WORLD; };
  ws.onclose = () => { $("conn").classList.remove("on"); schedule(); };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => { try { ingest(JSON.parse(ev.data)); } catch (err) { console.warn("bad frame", err); } };
}
function schedule() { clearTimeout(retry); retry = setTimeout(connect, 2000); }
connect();

// ---------------- buttons ----------------
$("bPause").onclick = () => { paused = !paused; $("bPause").classList.toggle("on", paused); $("bPause").textContent = paused ? "Resume" : "Pause"; if (!paused) rerender(); };
document.addEventListener("keydown", (ev) => { if (ev.code === "Space" && ev.target.tagName !== "INPUT") { ev.preventDefault(); $("bPause").click(); } });
$("bClear").onclick = () => { frames.length = 0; entries.length = 0; tbody.replaceChildren(); Object.assign(st, { delivered: 0, dropped: 0, lost: 0, range: 0, lat: [], slot: 0, rejected: 0 }); paintStats(); };
$("bExport").onclick = () => {
  const blob = new Blob([frames.map((f) => JSON.stringify(f)).join("\n") + "\n"], { type: "application/x-ndjson" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `flock_log_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.jsonl`;
  a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};
$("bLoad").onclick = () => $("fLoad").click();
$("fLoad").onchange = async () => {
  const file = $("fLoad").files[0]; if (!file) return;
  const text = await file.text();
  for (const line of text.split("\n")) { if (line.trim()) { try { ingest(JSON.parse(line), false); } catch {} } }
  rerender();
};

window.flockLog = { frames, entries, st };   // for debugging in the console
