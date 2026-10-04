// web/net.js — Lane A. WebSocket link to the world hub with auto-reconnect.
// World URL: ?world=ws://<ip>:8765, else ws://<page host>:8765 (the world server serves web/ on :8080).

export function worldUrl() {
  const q = new URLSearchParams(location.search);
  if (q.get("world")) return q.get("world");
  const host = location.hostname || "localhost";
  return `ws://${host}:8765`;
}

export function connect(role, onMessage, onStatus = () => {}) {
  let ws = null;
  let closedByUs = false;
  let retry = 500;
  const link = {
    send(obj) {
      if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
    },
    get open() { return !!ws && ws.readyState === WebSocket.OPEN; },
    close() { closedByUs = true; ws && ws.close(); },
  };
  const open = () => {
    const url = `${worldUrl()}/?role=${encodeURIComponent(role)}`;
    onStatus("connecting", url);
    ws = new WebSocket(url);
    ws.onopen = () => { retry = 500; onStatus("open", url); };
    ws.onmessage = (ev) => {
      let m;
      try { m = JSON.parse(ev.data); } catch { return; }
      if (m.type === "ERROR") onStatus("error", m.error);
      onMessage(m);
    };
    ws.onclose = (ev) => {
      if (closedByUs) return;
      if (ev.code === 4000) { onStatus("error", ev.reason); return; }   // bad role: don't hammer
      onStatus("closed", `retry in ${retry} ms`);
      setTimeout(open, retry);
      retry = Math.min(retry * 2, 5000);
    };
  };
  open();
  return link;
}

// Flat-earth ENU frame around the airport reference (same maths as world/traffic.py Local).
export class Local {
  constructor(lat0, lon0) {
    this.lat0 = lat0; this.lon0 = lon0;
    this.kx = 111320 * Math.cos(lat0 * Math.PI / 180);
  }
  toEnu(lat, lon) { return [(lon - this.lon0) * this.kx, (lat - this.lat0) * 111320]; }
}

export const NM = 1852;
export const LEVEL_COLOR = {
  SEQUENCE: "var(--lvl-seq)", TRAFFIC: "var(--lvl-traffic)", RESOLVE: "var(--lvl-resolve)",
  TAKEOVER: "var(--lvl-takeover)", RELEASE: "var(--lvl-release)", NO_SOLUTION: "var(--lvl-resolve)",
  CLEAR: "var(--lvl-clear)",
};
export const TRUST_COLOR = {
  TRUSTED: "var(--trust-ok)", SUSPICIOUS: "var(--trust-sus)", FAKE: "var(--trust-fake)",
  CAMERA_ONLY: "var(--trust-cam)",
};

// Resolve a CSS var for canvas drawing.
const cssCache = {};
export function css(v) {
  if (!v.startsWith("var(")) return v;
  const name = v.slice(4, -1);
  if (!(name in cssCache)) cssCache[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#fff";
  return cssCache[name];
}

// Top-view airplane silhouette (high wing, tailplane), nose along `rot` radians clockwise from screen-up,
// `size` ~ half the wingspan in px. fill = solid, else outline. Shared by the cockpit map and the god view.
const PLANE_R = [[0, -1.3], [0.13, -1.15], [0.16, -0.35], [1.2, -0.2], [1.2, 0.02], [0.16, 0.1],
                 [0.1, 0.78], [0.48, 0.92], [0.48, 1.08], [0, 1.02]];
export function drawPlane(ctx, x, y, rot, size, fill = true) {
  ctx.save();
  ctx.translate(x, y); ctx.rotate(rot);
  ctx.beginPath();
  PLANE_R.forEach(([px, py], i) => (i ? ctx.lineTo(px * size, py * size) : ctx.moveTo(px * size, py * size)));
  for (let i = PLANE_R.length - 2; i > 0; i--) ctx.lineTo(-PLANE_R[i][0] * size, PLANE_R[i][1] * size);
  ctx.closePath();
  if (fill) { ctx.fill(); ctx.lineWidth = Math.max(1, size * 0.12); ctx.strokeStyle = "rgba(0,0,0,.7)"; ctx.stroke(); }
  else ctx.stroke();
  ctx.restore();
}

// Canvas cannot resolve CSS variables in ctx.font; use literal stacks.
export const SANS = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif';
export const MONO = 'ui-monospace, "Cascadia Mono", Consolas, Menlo, monospace';
