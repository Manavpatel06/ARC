// web/cesium3d.js — Lane A. CesiumJS loader + shared 3D helpers for the cockpit and god views.
//
// Cesium is pinned and loaded on demand from jsDelivr. If it cannot load (no internet on the
// hotspot) callers fall back to the 2D views.
// Ion token: pass ?ion=<token> once; it is kept in localStorage ("flock.ion") on that laptop.
//   with a token    -> Cesium World Terrain + ion imagery + OSM Buildings
//   without a token -> OpenStreetMap imagery on the flat ellipsoid (heights drawn AGL)

const CESIUM_VER = "1.121.0";
const BASE = `https://cdn.jsdelivr.net/npm/cesium@${CESIUM_VER}/Build/Cesium/`;
export const AIRCRAFT_MODEL = `https://cdn.jsdelivr.net/gh/CesiumGS/cesium@1.121/Apps/SampleData/models/CesiumAir/Cesium_Air.glb`;
const GEOID_N_M = -31.0;     // EGM96 geoid height at KDVT: ellipsoid height = MSL + N
const FT = 0.3048;

let loading = null;
export function loadCesium(timeoutMs = 30000) {   // generous: venue hotspots can be slow
  if (window.Cesium) return Promise.resolve(window.Cesium);
  if (loading) return loading;
  window.CESIUM_BASE_URL = BASE;
  const css = document.createElement("link");
  css.rel = "stylesheet"; css.href = `${BASE}Widgets/widgets.css`;
  document.head.appendChild(css);
  loading = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = `${BASE}Cesium.js`;
    s.onload = () => (window.Cesium ? resolve(window.Cesium) : reject(new Error("Cesium global missing")));
    s.onerror = () => reject(new Error("Cesium failed to load (offline?)"));
    setTimeout(() => reject(new Error("Cesium load timed out")), timeoutMs);
    document.head.appendChild(s);
  });
  return loading;
}

export function ionToken() {
  const q = new URLSearchParams(location.search).get("ion");
  try {
    if (q) localStorage.setItem("flock.ion", q);
    return q || localStorage.getItem("flock.ion") || "";
  } catch {
    return q || "";
  }
}

// WebGL renderer string if the browser exposes it; flags CPU-only rendering (no GPU acceleration):
// Windows "Microsoft Basic Render Driver", SwiftShader, llvmpipe. 3D is unusable there.
export function gpuInfo() {
  try {
    const gl = document.createElement("canvas").getContext("webgl");
    if (!gl) return { renderer: "no WebGL", software: true };
    const ext = gl.getExtension("WEBGL_debug_renderer_info");
    const renderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : "unknown";
    return { renderer, software: /Basic Render Driver|SwiftShader|llvmpipe|Software/i.test(renderer) };
  } catch {
    return { renderer: "unknown", software: false };
  }
}

// Create a bare viewer (no Cesium UI chrome). Returns {viewer, terrain:boolean}.
// lite = half resolution, no fog/atmosphere, coarser tiles: for weak or CPU-only graphics.
export async function makeViewer(Cesium, container, { buildings = true, lite = false } = {}) {
  const token = ionToken();
  const opts = {
    animation: false, timeline: false, baseLayerPicker: false, geocoder: false, homeButton: false,
    sceneModePicker: false, navigationHelpButton: false, fullscreenButton: false, infoBox: false,
    selectionIndicator: false, scene3DOnly: true, requestRenderMode: false,
  };
  if (token) {
    Cesium.Ion.defaultAccessToken = token;
    opts.terrain = Cesium.Terrain.fromWorldTerrain();
  } else {
    opts.baseLayer = new Cesium.ImageryLayer(new Cesium.OpenStreetMapImageryProvider({ url: "https://tile.openstreetmap.org/" }));
  }
  const viewer = new Cesium.Viewer(container, opts);
  viewer.scene.globe.depthTestAgainstTerrain = !!token;
  viewer.scene.skyAtmosphere.show = !lite;
  viewer.scene.fog.enabled = !lite;
  if (lite) {
    viewer.resolutionScale = 0.5;
    viewer.scene.globe.maximumScreenSpaceError = 4;
    viewer.scene.globe.showGroundAtmosphere = false;
  }
  if (token && buildings && !lite) {
    try { viewer.scene.primitives.add(await Cesium.createOsmBuildingsAsync()); } catch (e) { console.warn("[3d] OSM buildings", e); }
  }
  return { viewer, terrain: !!token };
}

// Height above the ellipsoid for an aircraft at alt_msl_ft.
export function heightM(altMslFt, terrain, fieldElevFt) {
  return terrain ? altMslFt * FT + GEOID_N_M : Math.max(0, (altMslFt - fieldElevFt) * FT);
}

// Smooth 20 Hz / 10 Hz state into 60 fps: keep the last two samples and render one sample late.
export class Interp {
  constructor() { this.prev = null; this.cur = null; }
  push(s) {
    const rx = performance.now();
    this.prev = this.cur ? this.cur : { ...s, rx };
    this.cur = { ...s, rx };
  }
  sample() {
    const { prev: a, cur: b } = this;
    if (!b) return null;
    const span = Math.max(1, b.rx - a.rx);
    const f = Math.min(1, Math.max(0, (performance.now() - b.rx) / span));
    const lerp = (x, y) => x + (y - x) * f;
    const lerpAng = (x, y) => x + ((((y - x) % 360) + 540) % 360 - 180) * f;
    return { ...b, lat: lerp(a.lat, b.lat), lon: lerp(a.lon, b.lon), alt_msl_ft: lerp(a.alt_msl_ft, b.alt_msl_ft),
             hdg_deg: lerpAng(a.hdg_deg, b.hdg_deg), track_deg: lerpAng(a.track_deg, b.track_deg),
             bank_deg: lerp(a.bank_deg, b.bank_deg), vs_fpm: lerp(a.vs_fpm, b.vs_fpm) };
  }
}

// Point at TRUE bearing/range from (lat, lon) — flat earth, fine inside 3 NM.
export function offsetLL(lat, lon, brgDeg, rngM) {
  const r = brgDeg * Math.PI / 180;
  return [lat + rngM * Math.cos(r) / 111320, lon + rngM * Math.sin(r) / (111320 * Math.cos(lat * Math.PI / 180))];
}
