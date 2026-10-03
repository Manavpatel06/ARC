// web/haptics.js — Lane A. Controller rumble that rises with the FLOCK alert level.
//
// Driven ONLY by what this aircraft's node reports (ADVISORY level, TAKEOVER command, TRUST rel
// ranges) — never by world truth — so FAKE / SUSPICIOUS targets never shake the stick.
//
//   SEQUENCE        one soft tick when it first appears
//   TRAFFIC         light pulse every 2 s
//   RESOLVE         strong double pulse every 1 s (NO_SOLUTION too)
//   TAKEOVER        continuous shake (stick-shaker) while FLOCK has control
//   proximity       TRUSTED target inside 1 NM: pulses speed up and strengthen as range closes
//   bump()          one short knock: control handed back (STICK / AP disconnect)
//
// Chrome Gamepad API "dual-rumble" (Xbox, DualShock 4 on Windows; DualSense varies). ?haptics=0 disables.

const NM = 1852;
const ADV_TTL_MS = 12000;

export function haptic(strong, weak, ms) { return { strong, weak, ms }; }

// Pure: decide which effects to start now. `mem` keeps timers between calls.
// st = { level, levelAt, takeover, nearestTrustedM }
export function plan(st, now, mem) {
  const out = [];
  const live = st.level && !["CLEAR", "RELEASE"].includes(st.level) && now - st.levelAt < ADV_TTL_MS;
  const level = live ? st.level : null;

  if (level !== mem.level) {                                   // level changed
    mem.level = level;
    mem.next = now;                                            // fire the new pattern immediately
    if (level === "SEQUENCE") out.push(haptic(0.0, 0.35, 90));
  }
  if (st.takeover) {                                           // stick shaker, back-to-back bursts
    if (now >= (mem.shake || 0)) { out.push(haptic(0.9, 0.5, 300)); mem.shake = now + 250; }
    return out;
  }
  if (now >= mem.next) {
    if (level === "TRAFFIC") { out.push(haptic(0.2, 0.45, 150)); mem.next = now + 2000; }
    else if (level === "RESOLVE" || level === "NO_SOLUTION" || level === "TAKEOVER") {
      out.push(haptic(0.65, 0.6, 120), { ...haptic(0.65, 0.6, 120), delay: 240 });
      mem.next = now + 1000;
    } else mem.next = now + 250;
  }
  // proximity of the nearest TRUSTED target (only when no faster pattern is running)
  if (st.nearestTrustedM != null && st.nearestTrustedM < NM && level !== "RESOLVE" && level !== "TAKEOVER") {
    const f = 1 - Math.max(0, st.nearestTrustedM) / NM;       // 0 at 1 NM -> 1 at contact
    if (now >= (mem.prox || 0)) {
      out.push(haptic(0.1 + 0.4 * f, 0.15 + 0.45 * f, 90));
      mem.prox = now + (1500 - 1200 * f);                      // 1.5 s at 1 NM -> 0.3 s close in
    }
  }
  return out;
}

export function startHaptics(getState) {
  const enabled = new URLSearchParams(location.search).get("haptics") !== "0";
  const mem = { level: null, next: 0 };
  const status = { enabled, supported: false, pad: null, last: "" };

  const actuator = () => {
    const pads = navigator.getGamepads ? [...navigator.getGamepads()].filter(Boolean) : [];
    const pad = pads[0];
    status.pad = pad ? pad.id : null;
    const a = pad && (pad.vibrationActuator || (pad.hapticActuators && pad.hapticActuators[0]));
    status.supported = !!(a && a.playEffect);
    return status.supported ? a : null;
  };
  const play = (e) => {
    const a = actuator();
    if (!enabled || !a) return;
    const go = () => a.playEffect("dual-rumble", { startDelay: 0, duration: e.ms, strongMagnitude: e.strong, weakMagnitude: e.weak })
      .catch(() => {});
    e.delay ? setTimeout(go, e.delay) : go();
    status.last = `${e.strong.toFixed(2)}/${e.weak.toFixed(2)}`;
  };

  setInterval(() => {
    actuator();
    for (const e of plan(getState(), performance.now(), mem)) play(e);
  }, 50);

  return {
    status,
    bump() { play(haptic(0.7, 0.3, 200)); },
  };
}
