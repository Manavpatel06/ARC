// web/callouts.js — Lane A. Radio-altimeter callouts on descent ("one thousand ... fifty, forty, thirty, twenty,
// ten"), from the own aircraft's height above ground (OWNSHIP agl_ft). Each callout fires once as the aircraft
// descends through it and re-arms after climbing 50 ft back above it (go-around, touch-and-go).

export const CALLOUTS = [
  [1000, "one thousand"], [500, "five hundred"], [400, "four hundred"], [300, "three hundred"], [200, "two hundred"],
  [100, "one hundred"], [50, "fifty"], [40, "forty"], [30, "thirty"], [20, "twenty"], [10, "ten"],
];
export const RA_SHOW_BELOW_FT = 2500;

// Returns update(aglFt, vsFpm, onGround) -> text to say or null. Pure (no timers), so it is unit-testable.
export function createCallouts() {
  const armed = new Set(CALLOUTS.map(([h]) => h));
  let prev = null;
  return function update(agl, vs, onGround) {
    let say = null;
    if (prev != null && !onGround && vs < 0) {
      for (const [h, words] of CALLOUTS) {
        if (armed.has(h) && prev > h && agl <= h) { armed.delete(h); say = words; }   // lowest crossed wins
      }
    }
    for (const [h] of CALLOUTS) if (agl > h + 50) armed.add(h);
    prev = agl;
    return say;
  };
}

// Radio-altitude display value: 10 ft steps up high, 5 ft below 200 ft, 1 ft below 50 ft (like a real RA).
export function raDisplay(agl) {
  const a = Math.max(0, agl);
  return a < 50 ? Math.round(a) : a < 200 ? Math.round(a / 5) * 5 : Math.round(a / 10) * 10;
}
