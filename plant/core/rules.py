"""Rule-based fallback — keeps the plant alive when the AI is unavailable.

Pure deterministic logic: no Ollama, no network, no external dependencies.
Called by ReasoningClient when the LLM is down or returns bad output,
and can also be called directly for testing.
"""

from __future__ import annotations

import logging

from plant.ai.schema import Decision
from plant.hardware.interfaces import SoilReading, TemperatureReading

log = logging.getLogger(__name__)


def rule_based_decision(
    cfg: dict,
    soil: SoilReading | None,
    temp: TemperatureReading,
    fallback_reason: str = "rule-based fallback",
) -> Decision:
    """Return a safe Decision using threshold logic only.

    Logic:
    - Water if soil ADC raw > dry_threshold (soil is dry).
    - Don't water if soil ADC raw < wet_threshold (already moist).
    - Never water if soil is None (no sensor — moisture unverifiable).
    - Turn light on if temperature < too_cold_celsius.
    - Never water if temperature > too_hot_celsius.
    """
    soil_cfg = cfg["soil"]
    temp_cfg = cfg["temperature"]
    watering_cfg = cfg["watering"]

    dry_threshold = soil_cfg["dry_threshold"]
    wet_threshold = soil_cfg["wet_threshold"]
    max_pulse = watering_cfg["max_pulse_seconds"]
    too_cold = temp_cfg["too_cold_celsius"]
    too_hot = temp_cfg["too_hot_celsius"]

    # Decide watering. water_secs is a float — max_pulse can be sub-second
    # (e.g. during pump-rate calibration) and must not get truncated.
    if soil is None:
        water = False
        water_secs = 0.0
        reason_water = "no soil sensor — cannot verify moisture, skipping water"
    elif temp.celsius > too_hot:
        water = False
        water_secs = 0.0
        reason_water = f"too hot ({temp.celsius:.1f}°C > {too_hot}°C) — skipping water"
    elif soil.raw < wet_threshold:
        water = False
        water_secs = 0.0
        reason_water = f"soil already moist (ADC {soil.raw} < {wet_threshold})"
    elif soil.raw > dry_threshold:
        water = True
        water_secs = float(max_pulse)
        reason_water = f"soil dry (ADC {soil.raw} > {dry_threshold}) — watering {water_secs:.1f}s"
    else:
        water = False
        water_secs = 0.0
        reason_water = f"soil in acceptable range (ADC {soil.raw})"

    # Decide light
    light_on = temp.celsius < too_cold
    reason_light = (
        f"cold ({temp.celsius:.1f}°C < {too_cold}°C) — light on for warmth"
        if light_on
        else "temperature OK — light follows normal schedule"
    )

    soil_summary = (
        f"Soil ADC={soil.raw} ({soil.moisture_pct:.0f}% moisture)"
        if soil is not None
        else "Soil: no sensor connected"
    )
    reasoning = (
        f"[{fallback_reason}] "
        f"{soil_summary}, temp={temp.celsius:.1f}°C. "
        f"{reason_water}. {reason_light}."
    )

    log.info("RuleFallback: water=%s (%.1fs), light=%s", water, water_secs, light_on)
    return Decision(
        water=water,
        water_seconds=water_secs,
        light_on=light_on,
        reasoning=reasoning,
    )
