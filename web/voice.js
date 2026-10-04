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

// Height callout ("one hundred", "fifty"...): spoken right away, never interrupts what is already being said
// apart from an older callout (keeps the sequence current), no de-duplication.
let lastCallout = null;
export function sayCallout(text) {
  if (!unlocked || !text) return;
  if (lastCallout && speechSynthesis.speaking) speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.3;
  lastCallout = u;
  u.onend = () => { if (lastCallout === u) lastCallout = null; };
  speechSynthesis.speak(u);
}

// Urgent callout (terrain): always spoken, interrupts anything else, no de-duplication.
export function sayNow(text) {
  if (!unlocked || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 1.2;
  speechSynthesis.speak(u);
}

export const voiceReady = () => unlocked;
