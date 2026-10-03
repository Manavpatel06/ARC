// web/voice.js — Lane A. Spoken advisories via speechSynthesis.
// Browsers only allow speech after a user gesture; the start overlay calls unlock().

let unlocked = false;
let lastKey = "";

export function unlock() {
  if (unlocked || !("speechSynthesis" in window)) return;
  unlocked = true;
  const u = new SpeechSynthesisUtterance(" ");
  u.volume = 0;
  speechSynthesis.speak(u);
}

// Speak an ADVISORY once. Higher layers interrupt whatever is being said.
export function sayAdvisory(adv) {
  if (!unlocked || !adv.speak) return;
  const key = `${adv.level}|${adv.speak}`;
  if (key === lastKey) return;
  lastKey = key;
  if ((adv.layer || 0) >= 3 || adv.level === "RELEASE") speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(adv.speak);
  u.rate = adv.level === "TAKEOVER" || adv.level === "RESOLVE" ? 1.15 : 1.0;
  u.pitch = 1.0;
  speechSynthesis.speak(u);
}

export const voiceReady = () => unlocked;
