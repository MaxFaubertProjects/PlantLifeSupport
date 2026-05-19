"""Prompt templates for the two-stage AI pipeline.

Vision prompt  → ask the VLM to describe the plant in plain text.
Reasoning prompt → ask the LLM to produce a structured Decision JSON.
"""

from __future__ import annotations

from plant.hardware.interfaces import SoilReading, TemperatureReading

# ── Vision ────────────────────────────────────────────────────────────────────

VISION_PROMPT = """\
You are a plant-health assistant. Describe what you see in this photo of a plant.
Focus on:
- Leaf colour (green, yellow, brown patches, wilting?)
- Stem and overall posture (upright, drooping?)
- Visible soil surface (dry, moist, cracked?)
- Any signs of pests, disease, or physical damage

Be concise — 2–4 sentences. Do not give watering advice; just describe what you observe.\
"""

# ── Reasoning ─────────────────────────────────────────────────────────────────

_REASONING_SYSTEM = """\
You are an expert autonomous plant caretaker. You receive sensor data and a visual
description of a plant, then decide whether to water it and whether to leave the
grow light on. You must reply with valid JSON matching this exact schema:

{
  "water": <true|false>,
  "water_seconds": <integer 0–60>,
  "light_on": <true|false>,
  "reasoning": "<1–3 sentence plain-English explanation>"
}

Rules:
- water_seconds must be 0 when water is false.
- Be conservative with watering — overwatering causes root rot.
- Reply with JSON only. No markdown fences, no extra keys.\
"""


def build_reasoning_prompt(
    cfg: dict,
    soil: SoilReading,
    temp: TemperatureReading,
    plant_description: str,
    recent_cycles_summary: str,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the reasoning model.

    Args:
        cfg: Top-level config dict.
        soil: Current soil reading.
        temp: Current temperature reading.
        plant_description: Plain-text output from the VLM.
        recent_cycles_summary: Short text describing the last few cycles
            (e.g. "Watered 3h ago. Light was off.").

    Returns:
        A (system, user) tuple ready to pass to the Ollama chat API.
    """
    plant = cfg["plant"]
    watering = cfg["watering"]
    light = cfg["light"]

    user = f"""\
Plant: {plant['name']}
Species notes: {plant['species_notes'].strip()}

Current sensor readings:
  Soil moisture: {soil.moisture_pct:.1f}% (raw ADC {soil.raw}/1023; higher raw = drier)
  Temperature: {temp.celsius:.1f} °C

Visual observation:
  {plant_description.strip()}

Recent history:
  {recent_cycles_summary.strip()}

Safety limits (enforced in hardware — do not exceed):
  Max watering pulse: {watering['max_pulse_seconds']}s
  Min interval between waterings: {watering['min_interval_minutes']} minutes
  Max light on per day: {light['max_on_hours_per_day']} hours

Decide: should the plant be watered this cycle? Should the grow light be on?
Reply with JSON only.\
"""
    return _REASONING_SYSTEM, user


def summarise_recent_cycles(cycles) -> str:
    """Build a short human-readable summary of recent cycles for the prompt.

    Args:
        cycles: Sequence of Cycle objects (newest first) from the database.
                Pass an empty list if no history is available yet.
    """
    if not cycles:
        return "No previous cycles recorded."

    lines = []
    for c in cycles[:4]:  # last 4 cycles = ~4 hours
        parts = [f"{c.ts[:16]}Z"]
        if c.final_water:
            parts.append(f"watered {c.final_water_secs:.0f}s")
        else:
            parts.append("not watered")
        if c.final_light_on is not None:
            parts.append("light ON" if c.final_light_on else "light OFF")
        if c.soil_pct is not None:
            parts.append(f"moisture {c.soil_pct:.0f}%")
        lines.append("  • " + ", ".join(parts))
    return "\n".join(lines)
