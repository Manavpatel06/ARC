// web/input.js — Lane A. Gamepad API (PlayStation/Xbox, standard mapping) + keyboard fallback.
// Produces {roll, pitch, throttle}; roll/pitch in [-1, 1] (+roll = right bank, +pitch = nose up / climb),
// throttle in [0, 1]. Nothing is sent until the pilot first moves a control (until then the world's
// autopilot flies the aircraft); after that INPUT goes out at 30 Hz.
//
// Gamepad: left stick X = roll, left stick Y = pitch (pull back = climb; ?invert=1 flips),
//          R2 / L2 = throttle up / down (held), right stick Y also nudges throttle,
//          Triangle / Y (standard button 3) = toggle chase camera (fires a "flock:chase" window event),
//          Cross / A (standard button 0) = autopilot on/off (fires "flock:ap"),
//          D-pad up / down = map range out / in, D-pad left / right = map north-up toggle.
// Buttons act on the press, not while held; a button already held when the page starts is ignored.
// Reset own aircraft: hold L2 + R2 together (or keyboard R, or the on-screen RESET button) for 5 s;
// fires "flock:reset" once, st.resetProgress (0..1) drives the countdown. Both triggers held = no throttle change.
// Keyboard: ←/→ roll, ↓ pull (climb) / ↑ push (descend), W/S throttle up/down.

const DEADZONE = 0.12;
const RESET_HOLD_S = 5;
const SEND_HZ = 30;

const dz = (v) => (Math.abs(v) < DEADZONE ? 0 : (v - Math.sign(v) * DEADZONE) / (1 - DEADZONE));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export function startInput(send, onState = () => {}) {
  const invert = new URLSearchParams(location.search).get("invert") === "1";
  const keys = new Set();
  const tapped = new Set();      // keys pressed since the last tick (a quick tap still counts once)
  const st = { roll: 0, pitch: 0, throttle: 0.5, source: "none", engaged: false, pad: null,
               resetButton: false, resetProgress: 0 };
  let resetHeld = 0, resetFired = false;
  let kRoll = 0, kPitch = 0;
  const wasDown = { 0: true, 3: true, 12: true, 13: true, 14: true, 15: true };   // ignore presses held at start

  addEventListener("keydown", (e) => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "KeyW", "KeyS", "KeyR"].includes(e.code)) {
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
    let roll = 0, pitch = 0, active = false, triggersHeld = false;

    if (pad) {
      roll = dz(pad.axes[0] || 0);
      pitch = dz(pad.axes[1] || 0) * (invert ? -1 : 1);          // stick toward you (+1) = nose up
      const up = pad.buttons[7] ? pad.buttons[7].value : 0;       // R2
      const down = pad.buttons[6] ? pad.buttons[6].value : 0;     // L2
      triggersHeld = up > 0.75 && down > 0.75;
      const rs = dz(pad.axes[3] || 0);
      const dThr = (up - down) * 0.5 - rs * 0.4;
      if (dThr) st.throttle = clamp(st.throttle + dThr * dt, 0, 1);
      active = roll !== 0 || pitch !== 0 || dThr !== 0;
      if (active) st.source = "gamepad";
      for (const [btn, evt] of [[3, "flock:chase"], [0, "flock:ap"], [12, "flock:map-out"], [13, "flock:map-in"],
                                [14, "flock:map-orient"], [15, "flock:map-orient"]]) {   // Triangle/Y, Cross/A, D-pad
        const down = !!(pad.buttons[btn] && pad.buttons[btn].pressed);
        if (down && !wasDown[btn]) dispatchEvent(new Event(evt));
        wasDown[btn] = down;
      }
    }

    const down = (c) => keys.has(c) || tapped.has(c);
    const kr = (down("ArrowRight") ? 1 : 0) - (down("ArrowLeft") ? 1 : 0);
    const kp = (down("ArrowDown") ? 1 : 0) - (down("ArrowUp") ? 1 : 0);
    // keyboard: full deflection while held (the world already limits bank rate to 15 deg/s),
    // quick ease-out after release so wings come level smoothly
    kRoll = kr ? kr : kRoll * Math.max(0, 1 - 8 * dt);
    kPitch = kp ? kp : kPitch * Math.max(0, 1 - 8 * dt);
    if (Math.abs(kRoll) < 0.02) kRoll = 0;
    if (Math.abs(kPitch) < 0.02) kPitch = 0;
    const kt = (down("KeyW") ? 1 : 0) - (down("KeyS") ? 1 : 0);
    tapped.clear();
    if (kt) st.throttle = clamp(st.throttle + kt * 0.5 * dt, 0, 1);
    if (kr || kp || kt || kRoll || kPitch) { roll = kRoll; pitch = kPitch; active = kr || kp || kt; st.source = "keyboard"; }

    // reset: hold for RESET_HOLD_S, fire once, then wait for release
    if (triggersHeld || keys.has("KeyR") || st.resetButton) {
      resetHeld += dt;
      if (!resetFired && resetHeld >= RESET_HOLD_S) { resetFired = true; dispatchEvent(new Event("flock:reset")); }
    } else { resetHeld = 0; resetFired = false; }
    st.resetProgress = resetFired ? 1 : Math.min(1, resetHeld / RESET_HOLD_S);

    st.roll = clamp(roll, -1, 1);
    st.pitch = clamp(pitch, -1, 1);
    if (active) st.engaged = true;
    if (st.engaged) send({ roll: +st.roll.toFixed(3), pitch: +st.pitch.toFixed(3), throttle: +st.throttle.toFixed(3) });
    onState(st);
  }, 1000 / SEND_HZ);

  return st;
}
