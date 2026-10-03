// web/maplayer.js — Lane A. Street-map background (Leaflet + OpenStreetMap tiles, darkened in CSS) under the
// cockpit's moving map. Leaflet cannot rotate, so the map lives in an oversized inner box that is
// centred on the own aircraft, rotated for heading-up with CSS and zoomed to exactly the canvas scale.
// Loaded on demand and pinned; if it cannot load (no internet) the moving map simply has no background.

const LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/";
// OpenStreetMap standard tiles (free, attribution required, no API key; CARTO now asks browsers for a key).
// Dark look comes from a CSS filter on the tile pane (style.css .ck-leaflet).
const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
export const MAP_ATTRIBUTION = "© OpenStreetMap contributors";

let loading = null;
function loadLeaflet(timeoutMs = 20000) {
  if (window.L) return Promise.resolve(window.L);
  if (loading) return loading;
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = `${LEAFLET}leaflet.css`;
  document.head.appendChild(css);
  loading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `${LEAFLET}leaflet.js`;
    s.onload = () => (window.L ? resolve(window.L) : reject(new Error("Leaflet global missing")));
    s.onerror = () => reject(new Error("Leaflet failed to load (offline?)"));
    setTimeout(() => reject(new Error("Leaflet load timed out")), timeoutMs);
    document.head.appendChild(s);
  });
  return loading;
}

// Returns a controller with update(...) or null if Leaflet is unavailable.
export async function startMapLayer(container) {
  let L;
  try { L = await loadLeaflet(); } catch (e) { console.warn("[map]", e.message); return null; }
  const inner = document.createElement("div");
  inner.className = "ck-leaflet";
  container.appendChild(inner);
  const map = L.map(inner, {
    zoomControl: false, attributionControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false,
    boxZoom: false, keyboard: false, touchZoom: false, zoomSnap: 0, zoomAnimation: false, fadeAnimation: false,
    markerZoomAnimation: false, inertia: false,
  });
  L.tileLayer(TILES, { maxZoom: 19, minZoom: 3, keepBuffer: 2 }).addTo(map);   // no retina doubling: OSM tile policy
  map.setView([33.69, -112.08], 13);
  let size = 0, lastKey = "", zoomNow = 13;

  return {
    // lat/lon of own aircraft, metres per CSS pixel, map rotation (deg, heading-up = own heading),
    // where the aircraft sits in the panel (CSS px) and the panel size (CSS px)
    update(lat, lon, mPerPx, upDeg, cx, cy, w, h) {
      const rr = Math.max(Math.hypot(cx, cy), Math.hypot(w - cx, cy), Math.hypot(cx, h - cy), Math.hypot(w - cx, h - cy));
      const want = Math.ceil(rr * 2 + 64);
      if (want !== size) {
        size = want;
        inner.style.width = inner.style.height = `${size}px`;
        map.invalidateSize({ animate: false, pan: false });
      }
      inner.style.left = `${cx - size / 2}px`;
      inner.style.top = `${cy - size / 2}px`;
      inner.style.transform = `rotate(${-upDeg}deg)`;
      // Any zoom change makes Leaflet reset (drop + reload every tile), so only re-zoom when the scale really
      // changed (range switch); otherwise just pan, which keeps the loaded tiles.
      const zWant = Math.log2(156543.03392 * Math.cos(lat * Math.PI / 180) / Math.max(mPerPx, 0.01));
      if (Math.abs(zWant - zoomNow) > 0.01) zoomNow = Math.round(zWant * 100) / 100;
      const key = `${lat.toFixed(6)},${lon.toFixed(6)},${zoomNow}`;
      if (key !== lastKey) { map.setView([lat, lon], zoomNow, { animate: false }); lastKey = key; }
    },
  };
}
