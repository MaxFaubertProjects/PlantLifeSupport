"""Check times and the plan that runs between checks.

The full check (photo → vision model → reasoning model) is expensive on the
Pi, so it only runs a few times a day (``schedule.checks_per_day``, default 2,
starting at ``schedule.first_check``, default 08:00, evenly spaced). Each
check ends by building a *plan* that covers the time until the next check:

  * light on/off at the edges of the daily light window
    (``schedule.light_on_at`` + ``schedule.light_hours``),
  * waterings timed for when the forecast says the soil will reach the dry
    line, sized to bring it back to the middle of the comfortable range,
  * a moisture forecast (with an uncertainty band) for the dashboard.

The forecast uses drying rates learned from the last 7 days of readings
(with the light on and off separately) and how much moisture one second of
watering adds. Until there is enough history it falls back to conservative
defaults and says so.

Everything here is pure: no hardware, no I/O. The control loop executes the
plan and the safety layer still vets every watering.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Iterable, Optional

# ── Config ──────────────────────────────────────────────────────────────────

DEFAULT_CHECKS_PER_DAY = 2
DEFAULT_FIRST_CHECK = "08:00"
DEFAULT_LIGHT_ON_AT = "07:00"
READING_MINUTES = 30          # soil reading + backup check between checks
STEP_MIN = 15                 # forecast resolution


def _hhmm(s: Any, default: str) -> dtime:
    try:
        h, m = str(s).strip().split(":")[:2]
        return dtime(int(h) % 24, int(m) % 60)
    except Exception:
        h, m = default.split(":")
        return dtime(int(h), int(m))


def schedule_cfg(cfg: dict) -> dict:
    """The effective schedule settings, with defaults filled in."""
    s = cfg.get("schedule") or {}
    light = cfg.get("light") or {}
    max_h = float(light.get("max_on_hours_per_day", 16))
    n = int(s.get("checks_per_day", DEFAULT_CHECKS_PER_DAY) or DEFAULT_CHECKS_PER_DAY)
    n = max(1, min(24, n))
    hours = float(s.get("light_hours", max_h))
    return {
        "checks_per_day": n,
        "first_check": _hhmm(s.get("first_check"), DEFAULT_FIRST_CHECK).strftime("%H:%M"),
        "light_on_at": _hhmm(s.get("light_on_at"), DEFAULT_LIGHT_ON_AT).strftime("%H:%M"),
        "light_hours": max(0.0, min(hours, max_h, 24.0)),
        "max_light_hours": max_h,
    }


def check_interval_minutes(cfg: dict) -> int:
    return round(24 * 60 / schedule_cfg(cfg)["checks_per_day"])


def _local(now: Optional[datetime]) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone()


def check_slots(cfg: dict, around: Optional[datetime] = None) -> list[datetime]:
    """Check times (local, tz-aware) from yesterday through tomorrow."""
    sc = schedule_cfg(cfg)
    now = _local(around)
    first = _hhmm(sc["first_check"], DEFAULT_FIRST_CHECK)
    step = timedelta(minutes=check_interval_minutes(cfg))
    out = []
    for day in (-1, 0, 1, 2):
        d = (now + timedelta(days=day)).date()
        t0 = datetime.combine(d, first, tzinfo=now.tzinfo)
        out += [t0 + k * step for k in range(sc["checks_per_day"])]
    return sorted(out)


def next_check(cfg: dict, now: Optional[datetime] = None) -> datetime:
    now = _local(now)
    return next(t for t in check_slots(cfg, now) if t > now)


def previous_check(cfg: dict, now: Optional[datetime] = None) -> datetime:
    now = _local(now)
    return [t for t in check_slots(cfg, now) if t <= now][-1]


# ── Light window ────────────────────────────────────────────────────────────

def light_windows(cfg: dict, around: datetime) -> list[tuple[datetime, datetime]]:
    sc = schedule_cfg(cfg)
    if sc["light_hours"] <= 0:
        return []
    now = _local(around)
    on = _hhmm(sc["light_on_at"], DEFAULT_LIGHT_ON_AT)
    out = []
    for day in (-1, 0, 1, 2):
        d = (now + timedelta(days=day)).date()
        start = datetime.combine(d, on, tzinfo=now.tzinfo)
        out.append((start, start + timedelta(hours=sc["light_hours"])))
    return out


def light_planned_on(cfg: dict, at: datetime) -> bool:
    return any(a <= at < b for a, b in light_windows(cfg, at))


# ── Learned rates ───────────────────────────────────────────────────────────

@dataclass
class Rates:
    dry_light_on: float      # % per hour while the light is on
    dry_light_off: float     # % per hour while it is off
    gain_per_sec: float      # % added per second of watering
    samples: int             # dry spells the drying rates come from
    learned: bool            # False → defaults

    def describe(self) -> list[str]:
        src = "" if self.learned else " (default — not enough history yet)"
        return [
            f"drying ≈ {self.dry_light_on:.1f} %/h light on, {self.dry_light_off:.1f} %/h off{src}",
            f"1 s of water ≈ +{self.gain_per_sec:.1f} %",
        ]


DEFAULT_RATES = Rates(1.0, 0.5, 3.0, 0, False)


def learn_rates(points: Iterable[dict], now: Optional[datetime] = None) -> Rates:
    """Learn drying and watering rates from moisture readings.

    ``points`` are dicts with ``ts`` (datetime), ``soil_pct``, ``light_on``
    and ``water_secs`` (seconds watered right after that reading, 0 if none),
    oldest first. Only the last 7 days count.
    """
    now = now or datetime.now(timezone.utc)
    pts = [p for p in points if p.get("soil_pct") is not None
           and p["ts"] >= now - timedelta(days=7)]
    on, off, gains = [], [], []
    for a, b in zip(pts, pts[1:]):
        hours = (b["ts"] - a["ts"]).total_seconds() / 3600
        if not 0.2 <= hours <= 13:
            continue
        delta = b["soil_pct"] - a["soil_pct"]
        if a.get("water_secs"):
            gains.append((delta + 0.7 * hours) / a["water_secs"])
        elif delta <= 0.5:
            (on if a.get("light_on") else off).append(max(0.0, -delta) / hours)
    if len(on) + len(off) < 4:
        r = DEFAULT_RATES
        dry_on, dry_off, n, learned = r.dry_light_on, r.dry_light_off, 0, False
    else:
        med = lambda xs: sorted(xs)[len(xs) // 2]
        dry_on = med(on) if on else med(off) * 1.6
        dry_off = med(off) if off else med(on) / 1.6
        n, learned = len(on) + len(off), True
    gain = sorted(gains)[len(gains) // 2] if len(gains) >= 2 else DEFAULT_RATES.gain_per_sec
    clamp = lambda v, lo, hi: max(lo, min(hi, v))
    return Rates(round(clamp(dry_on, 0.05, 8), 2), round(clamp(dry_off, 0.03, 6), 2),
                 round(clamp(gain, 0.3, 20), 2), n, learned)


# ── Plan ────────────────────────────────────────────────────────────────────

def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def build_plan(
    cfg: dict,
    *,
    now: datetime,
    soil_pct: Optional[float],
    dry_pct: float,
    wet_pct: float,
    rates: Rates,
    light_now: bool,
    watered_now_secs: float = 0.0,
    last_water_at: Optional[datetime] = None,
    ai_reasoning: str = "",
    source: str = "check",
) -> dict:
    """Plan actions and forecast moisture from ``now`` until the next check."""
    sc = schedule_cfg(cfg)
    watering = cfg.get("watering") or {}
    max_pulse = float(watering.get("max_pulse_seconds", 8))
    min_gap = timedelta(minutes=float(watering.get("min_interval_minutes", 90)))
    now = _local(now)
    until = next_check(cfg, now)

    actions: list[dict] = []
    # Light edges inside the horizon.
    for a, b in light_windows(cfg, now):
        if now < a < until:
            actions.append({"at": _iso(a), "kind": "light_on",
                            "note": f"{sc['light_hours']:g} h light window"})
        if now < b < until:
            actions.append({"at": _iso(b), "kind": "light_off", "note": "end of light window"})

    # Forecast and waterings.
    lo_line, hi_line = min(dry_pct, wet_pct), max(dry_pct, wet_pct)
    target = (lo_line + hi_line) / 2
    trigger = lo_line + min(3.0, (hi_line - lo_line) / 4)
    forecast: list[dict] = []
    level = soil_pct
    light = light_now
    t = now
    last_water = now if watered_now_secs > 0 else last_water_at
    band = 0.0 if rates.learned else 1.5
    while level is not None and t <= until:
        h = (t - now).total_seconds() / 3600
        spread = band + 0.8 + h * (0.25 if rates.learned else 0.6)
        forecast.append({"ts": _iso(t), "soil_pct": round(level, 1),
                         "lo": round(max(0.0, level - spread), 1),
                         "hi": round(min(100.0, level + spread * 0.7), 1)})
        nxt = min(t + timedelta(minutes=STEP_MIN), until) if t < until else t + timedelta(minutes=STEP_MIN)
        # light state over this step: planned edges override the current state
        for a in actions:
            at = datetime.fromisoformat(a["at"])
            if t < at <= nxt and a["kind"] in ("light_on", "light_off"):
                light = a["kind"] == "light_on"
        rate = rates.dry_light_on if light else rates.dry_light_off
        level = max(0.0, level - rate * STEP_MIN / 60)
        if level <= trigger and (last_water is None or nxt - last_water >= min_gap) and nxt < until:
            secs = max(0.5, min(max_pulse, (target - level) / rates.gain_per_sec))
            secs = round(secs * 2) / 2
            actions.append({"at": _iso(nxt), "kind": "water", "seconds": secs,
                            "note": f"forecast ≈ {level:.0f} % — back to ≈ {min(100, level + secs * rates.gain_per_sec):.0f} %"})
            level = min(100.0, level + secs * rates.gain_per_sec)
            last_water = nxt
        t = nxt

    actions.sort(key=lambda a: a["at"])
    waters = [a for a in actions if a["kind"] == "water"]
    hrs_to_next = (until - now).total_seconds() / 3600

    # Rationale — short, plain sentences built from the numbers.
    parts = []
    if soil_pct is None:
        parts.append("No soil reading, so no waterings are planned; the backup check keeps trying every "
                     f"{READING_MINUTES} min.")
    else:
        if watered_now_secs > 0:
            parts.append(f"Watered {watered_now_secs:g} s at this check.")
        if waters:
            w = waters[0]
            parts.append(f"Soil is {soil_pct:.0f} % and should get close to the dry line ({lo_line:.0f} %) "
                         f"around {datetime.fromisoformat(w['at']).astimezone().strftime('%H:%M')}, "
                         f"so {len(waters)} watering{'s are' if len(waters) > 1 else ' is'} planned before the next check.")
        else:
            end = forecast[-1]["soil_pct"] if forecast else soil_pct
            parts.append(f"Soil is {soil_pct:.0f} % and should still be ≈ {end:.0f} % at the next check "
                         f"({until.strftime('%H:%M')}), above the dry line ({lo_line:.0f} %) — no watering needed.")
    if sc["light_hours"] > 0:
        on = _hhmm(sc["light_on_at"], DEFAULT_LIGHT_ON_AT)
        off = (datetime.combine(now.date(), on) + timedelta(hours=sc["light_hours"])).strftime("%H:%M")
        parts.append(f"Light on {sc['light_on_at']}–{off} ({sc['light_hours']:g} h, cap {sc['max_light_hours']:g} h).")
    else:
        parts.append("Grow light off (0 h light window).")
    if ai_reasoning:
        import re
        text = ai_reasoning.strip().split("\n[")[0]
        text = re.sub(r"^\[[^\]]*\]\s*", "", text)      # "[Ollama unavailable: …]" etc.
        if text:
            parts.append(("Check: " if source == "check" else "Last check: ") + text)

    actions.append({"at": _iso(until), "kind": "check",
                    "note": f"next full check · then every {hrs_to_next if sc['checks_per_day'] == 1 else 24 / sc['checks_per_day']:g} h"})
    return {
        "generated_at": _iso(now),
        "source": source,
        "next_check_at": _iso(until),
        "rationale": " ".join(parts),
        "factors": rates.describe() + [f"backup check every {READING_MINUTES} min"],
        "actions": actions,
        "forecast": forecast,
        "rates": {"dry_light_on": rates.dry_light_on, "dry_light_off": rates.dry_light_off,
                  "gain_per_sec": rates.gain_per_sec, "learned": rates.learned, "samples": rates.samples},
        "done": [],
    }
