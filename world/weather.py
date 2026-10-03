"""
world/weather.py — Lane A. World weather that acts on every aircraft (truth side only).

Nodes still learn weather the real way — from the METAR (data/metar.py) — not from this model.

  wind      mean wind FROM wind_from_deg at wind_kt (surface), +35 % and veering 15 deg by 3,000 ft AGL
  shear_kt  low-level wind shear: the wind weakens by up to shear_kt below 400 ft AGL (headwind loss on final)
  gust_kt   METAR-style peak gust ("250/15G25" -> wind 15, gust 25); 0 = steady
  turbulence 0 none, 1 light, 2 moderate, 3 severe: random bank / vertical / airspeed upsets per aircraft
  thermals  0..1: rising columns + gentle sink in between (Phoenix afternoon), weaker near the ground
  visibility_sm, ceiling_ft_agl   what the pilot sees (cockpit haze / cloud); no physical effect
  qnh_inhg  actual altimeter setting. Aircraft hold INDICATED altitude with the setting they have
            (Aircraft.baro_set_inhg); if QNH falls and they don't update, they fly lower than they think.
            Transponders report pressure altitude (29.92 datum), so FLOCK sees the true separation.
  da_ft     density altitude at the field (climb capability); None = keep the current value

Presets: PRESETS[name]. Per-aircraft gust/turbulence noise is seeded from the aircraft id, so offline
runs (world/find_conflict.py) stay reproducible.
"""
from __future__ import annotations
import math, random, zlib
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

KT = 0.514444
TURB_SIGMA = {   # level: (bank deg, vertical fpm, airspeed kt)
    0: (0.0, 0.0, 0.0), 1: (1.5, 120.0, 1.5), 2: (4.0, 350.0, 4.0), 3: (8.0, 800.0, 8.0)}
TURB_NAMES = ["none", "light", "moderate", "severe"]

@dataclass
class Weather:
    name: str = "metar"
    wind_from_deg: float = 250.0
    wind_kt: float = 8.0
    gust_kt: float = 0.0
    shear_kt: float = 0.0
    turbulence: int = 0
    thermals: float = 0.0
    visibility_sm: float = 10.0
    ceiling_ft_agl: Optional[float] = None
    qnh_inhg: float = 29.92
    da_ft: Optional[float] = None
    seed: int = 7
    _thermals: list = field(default_factory=list, repr=False)
    _t_ref: Optional[float] = field(default=None, repr=False)     # clock value when thermals were laid out

    # ---------- construction ----------
    @classmethod
    def from_metar(cls, wx: dict) -> "Weather":
        return cls(name="metar", wind_from_deg=float(wx.get("wind_dir_deg", 0) or 0), wind_kt=float(wx.get("wind_kt", 0) or 0),
                   gust_kt=float(wx.get("wind_gust_kt", 0) or 0), qnh_inhg=float(wx.get("altimeter_inhg", 29.92) or 29.92),
                   visibility_sm=float(wx.get("visibility_sm", 10) or 10), da_ft=None)

    def update(self, **kw) -> list[str]:
        """Apply a partial change (SET_WX). Returns the field names that changed."""
        names = {f.name for f in fields(self) if not f.name.startswith("_")}
        changed = []
        for k, v in kw.items():
            if k not in names or k == "name" and v is None:
                continue
            if k in ("turbulence",):
                v = int(max(0, min(3, int(v))))
            elif k in ("ceiling_ft_agl", "da_ft"):
                v = None if v in (None, "", "none") else float(v)
            elif k != "name":
                v = float(v)
            if getattr(self, k) != v:
                setattr(self, k, v)
                changed.append(k)
        if "thermals" in changed or "seed" in changed:
            self._thermals, self._t_ref = [], None
        return changed

    def public(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if not k.startswith("_")}
        d["turbulence_name"] = TURB_NAMES[self.turbulence]
        d["metar_style"] = self.metar_style()
        return d

    def metar_style(self) -> str:
        g = f"G{self.gust_kt:.0f}" if self.gust_kt > self.wind_kt else ""
        ceil = f" BKN{int(round(self.ceiling_ft_agl / 100)):03d}" if self.ceiling_ft_agl else ""
        return (f"{self.wind_from_deg:03.0f}{self.wind_kt:02.0f}{g}KT {self.visibility_sm:g}SM{ceil} "
                f"A{round(self.qnh_inhg * 100):04d}" + (f" TURB {TURB_NAMES[self.turbulence].upper()}" if self.turbulence else ""))

    # ---------- wind ----------
    def wind_vec(self, agl_ft: float) -> tuple[float, float]:
        """Mean wind (blowing TOWARD) east/north m/s at this height, incl. low-level shear."""
        h = max(0.0, min(agl_ft, 3000.0))
        spd = self.wind_kt * (1.0 + 0.35 * h / 3000.0)
        if self.shear_kt and agl_ft < 400:
            spd = max(0.0, spd - self.shear_kt * (1.0 - max(agl_ft, 0.0) / 400.0))
        to = math.radians(self.wind_from_deg + 15.0 * h / 3000.0 + 180.0)
        return spd * KT * math.sin(to), spd * KT * math.cos(to)

    # ---------- thermals ----------
    def _make_thermals(self):
        rng = random.Random(self.seed * 7919 + 13)
        self._thermals = []
        for _ in range(10):
            a, d = rng.uniform(0, 2 * math.pi), rng.uniform(300, 7000)
            self._thermals.append((d * math.sin(a), d * math.cos(a), rng.uniform(250, 600), rng.uniform(250, 600)))

    def thermal_list(self, t: float) -> list[tuple[float, float, float, float]]:
        """[(east m, north m, radius m, peak fpm)] drifting with the surface wind, wrapping inside
        +-8 km of the field so the area never runs out of thermals. t = any clock (world or sim)."""
        if self.thermals <= 0:
            return []
        if not self._thermals:
            self._make_thermals()
        if self._t_ref is None:
            self._t_ref = t
        we, wn = self.wind_vec(0)
        dt = t - self._t_ref
        wrap = lambda v: (v + 8000.0) % 16000.0 - 8000.0
        return [(wrap(x + we * dt), wrap(y + wn * dt), r, peak * self.thermals) for x, y, r, peak in self._thermals]

    def thermal_fpm(self, e: float, n: float, agl_ft: float, t: float) -> float:
        if self.thermals <= 0 or agl_ft < 1:
            return 0.0
        w = -60.0 * self.thermals                                   # gentle sink between thermals
        for x, y, r, peak in self.thermal_list(t):
            w += peak * math.exp(-((e - x) ** 2 + (n - y) ** 2) / (r * r))
        return w * min(1.0, agl_ft / 600.0)

class Turbulence:
    """Per-aircraft gust + turbulence state (Ornstein-Uhlenbeck noise, seeded from the aircraft id)."""
    def __init__(self, ac_id: str):
        self.rng = random.Random(zlib.crc32(ac_id.encode()))
        self.bank = self.w = self.u = 0.0         # deg, fpm, kt
        self.gust = 0.0                           # kt above the mean wind
        self.gust_target = 0.0
        self.next_gust_t = 0.0

    def _ou(self, x: float, sigma: float, tau: float, dt: float) -> float:
        return x - x / tau * dt + sigma * math.sqrt(2.0 * dt / tau) * self.rng.gauss(0.0, 1.0)

    def step(self, wx: Weather, dt: float, t: float, agl_ft: float) -> tuple[float, float, float, float]:
        """Returns (bank_upset deg, vertical air fpm, airspeed upset kt, gust kt)."""
        sb, sw, su = TURB_SIGMA[wx.turbulence]
        near_ground = min(1.0, max(agl_ft, 0.0) / 50.0)            # damp vertical jolts in the flare
        self.bank = self._ou(self.bank, sb, 1.5, dt)
        self.w = self._ou(self.w, sw, 1.0, dt)
        self.u = self._ou(self.u, su, 2.0, dt)
        spread = max(0.0, wx.gust_kt - wx.wind_kt)
        if spread and t >= self.next_gust_t:
            self.gust_target = self.rng.uniform(0.0, spread) if self.rng.random() < 0.6 else 0.0
            self.next_gust_t = t + self.rng.uniform(2.0, 6.0)
        elif not spread:
            self.gust_target = 0.0
        self.gust += (self.gust_target - self.gust) * min(1.0, dt / 1.0)
        return self.bank, self.w * near_ground, self.u, self.gust

PRESETS: dict[str, dict] = {
    "metar": {},   # filled from the METAR at load time
    "calm_morning": dict(wind_from_deg=250, wind_kt=5, gust_kt=0, shear_kt=0, turbulence=0, thermals=0.0,
                         visibility_sm=10, ceiling_ft_agl=None, qnh_inhg=30.02, da_ft=2500),
    "hot_gusty_afternoon": dict(wind_from_deg=250, wind_kt=15, gust_kt=25, shear_kt=5, turbulence=2, thermals=0.8,
                                visibility_sm=6, ceiling_ft_agl=None, qnh_inhg=29.82, da_ft=6500),
    "haboob": dict(wind_from_deg=160, wind_kt=25, gust_kt=35, shear_kt=10, turbulence=3, thermals=0.3,
                   visibility_sm=1, ceiling_ft_agl=None, qnh_inhg=29.72, da_ft=5500),
    "low_ceiling": dict(wind_from_deg=270, wind_kt=10, gust_kt=0, shear_kt=0, turbulence=1, thermals=0.0,
                        visibility_sm=4, ceiling_ft_agl=1100, qnh_inhg=29.92, da_ft=3500),
    "pressure_drop": dict(wind_from_deg=250, wind_kt=8, gust_kt=0, shear_kt=0, turbulence=0, thermals=0.0,
                          visibility_sm=10, ceiling_ft_agl=None, qnh_inhg=29.42, da_ft=None),
}
PRESET_NOTES = {
    "metar": "live / cached METAR: steady wind, no turbulence",
    "calm_morning": "250/05, smooth, 10 SM, DA 2,500 ft",
    "hot_gusty_afternoon": "250/15G25, moderate turbulence, thermals, 6 SM haze, DA 6,500 ft",
    "haboob": "160/25G35 dust storm, severe turbulence, 1 SM",
    "low_ceiling": "270/10, cloud base 1,100 ft AGL (just above pattern altitude), 4 SM",
    "pressure_drop": "QNH falls 0.50 inHg: aircraft that don't update fly ~500 ft lower than they think",
}

def preset(name: str, metar: dict) -> Weather:
    if name not in PRESETS:
        raise KeyError(f"unknown weather preset {name!r}; choose from {sorted(PRESETS)}")
    wx = Weather.from_metar(metar)
    wx.update(**PRESETS[name])
    wx.name = name
    return wx
