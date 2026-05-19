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
    soil: SoilReading,
    temp: TemperatureReading,
    fallback_reason: str = "rule-based fallback",
) -> Decision:
    """Return a safe Decision using threshold logic only.

    Logic:
    - Water if soil ADC raw > dry_threshold (soil is dry).
    - Don't water if soil ADC raw < wet_threshold (already moist).
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

    # Decide watering
    if temp.celsius > too_hot:
        water = False
        water_secs = 0
        reason_water = f"too hot ({temp.celsius:.1f}°C > {too_hot}°C) — skipping water"
    elif soil.raw < wet_threshold:
        water = False
        water_secs = 0
        reason_water = f"soil already moist (ADC {soil.raw} < {wet_threshold})"
    elif soil.raw > dry_threshold:
        water = True
        water_secs = max_pulse
        reason_water = f"soil dry (ADC {soil.raw} > {dry_threshold}) — watering {max_pulse}s"
    else:
        water = False
        water_secs = 0
        reason_water = f"soil in acceptable range (ADC {soil.raw})"

    # Decide light
    light_on = temp.celsius < too_cold
    reason_light = (
        f"cold ({temp.celsius:.1f}°C < {too_cold}°C) — light on for warmth"
        if light_on
        else "temperature OK — light follows normal schedule"
    )

    reasoning = (
        f"[{fallback_reason}] "
        f"Soil ADC={soil.raw} ({soil.moisture_pct:.0f}% moisture), "
        f"temp={temp.celsius:.1f}°C. "
        f"{reason_water}. {reason_light}."
    )

    log.info("RuleFallback: water=%s (%ds), light=%s", water, water_secs, light_on)
    return Decision(
        water=water,
        water_seconds=water_secs,
        light_on=light_on,
        reasoning=reasoning,
    )
