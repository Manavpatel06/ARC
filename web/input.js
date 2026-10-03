// web/input.js — Lane A. Gamepad API (PlayStation/Xbox, standard mapping) + keyboard fallback.
// Produces {roll, pitch, throttle}; roll/pitch in [-1, 1] (+roll = right bank, +pitch = nose up / climb),
// throttle in [0, 1]. Nothing is sent until the pilot first moves a control (until then the world's
// autopilot flies the aircraft); after that INPUT goes out at 30 Hz.
//
// Gamepad: left stick X = roll, left stick Y = pitch (pull back = climb; ?invert=1 flips),
//          R2 / L2 = throttle up / down (held), right stick Y also nudges throttle.
// Keyboard: ←/→ roll, ↓ pull (climb) / ↑ push (descend), W/S throttle up/down.

const DEADZONE = 0.12;
const SEND_HZ = 30;

const dz = (v) => (Math.abs(v) < DEADZONE ? 0 : (v - Math.sign(v) * DEADZONE) / (1 - DEADZONE));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function startInput(send, onState = () => {}) {
  const invert = new URLSearchParams(location.search).get("invert") === "1";
  const keys = new Set();
  const tapped = new Set();      // keys pressed since the last tick (a quick tap still counts once)
  const st = { roll: 0, pitch: 0, throttle: 0.5, source: "none", engaged: false, pad: null };
  let kRoll = 0, kPitch = 0;

  addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "KeyW", "KeyS"].includes(e.code)) {
      keys.add(e.code); tapped.add(e.code); e.preventDefault();
    }
  });
  addEventListener("keyup", (e) => keys.delete(e.code));
  addEventListener("blur", () => keys.clear());

  let last = performance.now();
  setInterval(() => {
    const now = performance.now();
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;

    const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
    const pad = pads[0] || null;
    st.pad = pad ? pad.id : null;
    let roll = 0, pitch = 0, active = false;

    if (pad) {
      roll = dz(pad.axes[0] || 0);
      pitch = dz(pad.axes[1] || 0) * (invert ? -1 : 1);          // stick toward you (+1) = nose up
      const up = pad.buttons[7] ? pad.buttons[7].value : 0;       // R2
      const down = pad.buttons[6] ? pad.buttons[6].value : 0;     // L2
      const rs = dz(pad.axes[3] || 0);
      const dThr = (up - down) * 0.5 - rs * 0.4;
      if (dThr) st.throttle = clamp(st.throttle + dThr * dt, 0, 1);
      active = roll !== 0 || pitch !== 0 || dThr !== 0;
      if (active) st.source = "gamepad";
    }

    // keyboard: ramp toward full deflection so a tap is not a slam
    const down = (c) => keys.has(c) || tapped.has(c);
    const kr = (down("ArrowRight") ? 1 : 0) - (down("ArrowLeft") ? 1 : 0);
    const kp = (down("ArrowDown") ? 1 : 0) - (down("ArrowUp") ? 1 : 0);
    // keyboard deflection ramps in while held and eases out after release
    kRoll = kr ? clamp(kRoll + kr * 2.5 * dt, -1, 1) : kRoll * Math.max(0, 1 - 4 * dt);
    kPitch = kp ? clamp(kPitch + kp * 2.5 * dt, -1, 1) : kPitch * Math.max(0, 1 - 4 * dt);
    if (Math.abs(kRoll) < 0.02) kRoll = 0;
    if (Math.abs(kPitch) < 0.02) kPitch = 0;
    const kt = (down("KeyW") ? 1 : 0) - (down("KeyS") ? 1 : 0);
    tapped.clear();
    if (kt) st.throttle = clamp(st.throttle + kt * 0.5 * dt, 0, 1);
    if (kr || kp || kt || kRoll || kPitch) { roll = kRoll; pitch = kPitch; active = kr || kp || kt; st.source = "keyboard"; }

    st.roll = clamp(roll, -1, 1);
    st.pitch = clamp(pitch, -1, 1);
    if (active) st.engaged = true;
    if (st.engaged) send({ roll: +st.roll.toFixed(3), pitch: +st.pitch.toFixed(3), throttle: +st.throttle.toFixed(3) });
    onState(st);
  }, 1000 / SEND_HZ);

  return st;
}
