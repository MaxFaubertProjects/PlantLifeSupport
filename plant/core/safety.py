"""Deterministic safety layer.

The AI *proposes* a Decision; safety.py *disposes* of it.
All hard limits are enforced here, independently of what the LLM returned.

Checks (in order):
1. Clamp watering duration to max_pulse_seconds.
2. Enforce minimum interval between waterings (min_interval_minutes).
3. No watering if soil is already moist (wet_threshold sanity check).
4. Clamp light to max_on_hours_per_day (tracked via DB history).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from plant.ai.schema import Decision
from plant.hardware.interfaces import SoilReading

log = logging.getLogger(__name__)


def apply_safety(
    cfg: dict,
    proposed: Decision,
    soil: SoilReading,
    recent_cycles: list,   # Sequence[Cycle] — avoids circular import
) -> Decision:
    """Return a (possibly modified) Decision that passes all safety checks.

    Modifications are logged.  The returned decision is the one that
    gets actuated and stored.

    Args:
        cfg: Top-level config dict.
        proposed: Raw Decision from the AI or rule-based fallback.
        soil: Current soil reading (for sanity check).
        recent_cycles: Recent Cycle objects from the DB (newest first).

    Returns:
        A safety-clamped Decision ready to act on.
    """
    watering = cfg["watering"]
    soil_cfg = cfg["soil"]
    light_cfg = cfg["light"]

    water = proposed.water
    water_seconds = proposed.water_seconds
    light_on = proposed.light_on
    notes: list[str] = []

    # 1. Clamp watering duration
    max_pulse = watering["max_pulse_seconds"]
    if water and water_seconds > max_pulse:
        notes.append(f"pulse clamped {water_seconds}s → {max_pulse}s")
        water_seconds = max_pulse

    # 2. Minimum interval between waterings
    if water:
        min_interval = timedelta(minutes=watering["min_interval_minutes"])
        last_water_ts = _last_watering_ts(recent_cycles)
        if last_water_ts is not None:
            elapsed = datetime.now(timezone.utc) - last_water_ts
            if elapsed < min_interval:
                remaining = (min_interval - elapsed).seconds // 60
                notes.append(
                    f"watering suppressed — last watered {elapsed.seconds // 60}m ago "
                    f"(min interval {watering['min_interval_minutes']}m, {remaining}m remaining)"
                )
                water = False
                water_seconds = 0

    # 3. No watering if soil is already moist
    wet_threshold = soil_cfg["wet_threshold"]
    if water and soil.raw < wet_threshold:
        notes.append(
            f"watering suppressed — soil moist (ADC {soil.raw} < {wet_threshold})"
        )
        water = False
        water_seconds = 0

    # 4. Light cap (max on-hours per day)
    if light_on:
        on_hours = _light_on_hours_today(recent_cycles)
        max_hours = light_cfg["max_on_hours_per_day"]
        if on_hours >= max_hours:
            notes.append(
                f"light capped — already {on_hours:.1f}h on today "
                f"(max {max_hours}h)"
            )
            light_on = False

    if notes:
        safety_note = " | ".join(notes)
        log.info("Safety overrides: %s", safety_note)
        reasoning = proposed.reasoning + f"\n[Safety] {safety_note}"
    else:
        reasoning = proposed.reasoning

    if not water:
        water_seconds = 0

    return Decision(
        water=water,
        water_seconds=water_seconds,
        light_on=light_on,
        reasoning=reasoning,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _last_watering_ts(recent_cycles: list) -> datetime | None:
    """Return the UTC timestamp of the most recent cycle where we actually watered."""
    for c in recent_cycles:
        if c.final_water:
            try:
                return datetime.fromisoformat(c.ts)
            except ValueError:
                pass
    return None


def _light_on_hours_today(recent_cycles: list) -> float:
    """Estimate how many hours the light was on in the last 24 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    hours = 0.0
    for c in recent_cycles:
        try:
            ts = datetime.fromisoformat(c.ts)
        except ValueError:
            continue
        if ts < cutoff:
            break   # cycles are newest-first; stop outside the 24h window
        if c.final_light_on:
            hours += 1.0   # each cycle ≈ 1 hour of potential on-time
    return hours
