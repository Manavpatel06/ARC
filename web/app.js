// web/app.js — Lane A. One page, role from ?role= : cockpitA | cockpitB | cockpit:<id> | god | log.

import { connect } from "./net.js";
import { startCockpit } from "./cockpit.js";
import { startGod } from "./god.js";
import { unlock } from "./voice.js";

// INTERFACE v1.1: ?role=cockpitA -> cockpit:A (world resolves A/B to 1st/2nd human aircraft);
// &ac=N102 overrides -> cockpit:N102.
const q = new URLSearchParams(location.search);
let role = q.get("role") || "god";
if (role.startsWith("cockpit")) role = `cockpit:${q.get("ac") || role.replace(/^cockpit:?/, "") || "A"}`;
const $ = (id) => document.getElementById(id);

$("start-role").textContent = role.startsWith("cockpit:") ? `Cockpit ${role.slice(8)}` : role;

let started = false;
function start() {
  if (started) return;
  started = true;
  unlock();
  $("start").remove();
  if (role.startsWith("cockpit")) startCockpit(role);
  else if (role === "log") startLog();
  else startGod();
}
$("start-btn").addEventListener("click", start);
addEventListener("keydown", start, { once: true });
// a controller button press also starts (no gesture needed for gamepad polling)
const padPoll = setInterval(() => {
  const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
  if (pads.some((p) => p.buttons.some((b) => b.pressed))) { clearInterval(padPoll); start(); }
  if (started) clearInterval(padPoll);
}, 100);
// god / log need no audio: start straight away
if (role === "god" || role === "log") start();

function startLog() {
  $("log").hidden = false;
  const tbody = $("log-rows");
  connect("log", (m) => {
    if (m.type !== "LOG") return;
    const p = m.payload || {};
    const desc = p.type === "ADVISORY" ? `ADVISORY ${p.level} ${p.text}`
      : p.type === "COMMAND" ? `COMMAND ${p.mode} bank ${p.bank_cmd_deg ?? ""} applied=${p.applied}`
      : p.type === "TRUST" ? `TRUST ${(p.targets || []).map((t) => `${t.id}=${t.state}`).join(", ")}`
      : JSON.stringify(p).slice(0, 160);
    const tr = document.createElement("tr");
    tr.dataset.kind = m.kind;
    for (const v of [new Date(m.t * 1000).toLocaleTimeString(), m.kind, m.src, desc]) {
      const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
    }
    tbody.prepend(tr);
    while (tbody.children.length > 500) tbody.lastChild.remove();
  }, (st) => { $("log-link").textContent = st; });
}
