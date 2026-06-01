"""Prompt templates for the two-stage AI pipeline.

Vision prompt  → ask the VLM to describe the plant in plain text.
Reasoning prompt → ask the LLM to produce a structured Decision JSON.
"""

from __future__ import annotations

from plant.hardware.interfaces import SoilReading, TemperatureReading

# ── Vision ────────────────────────────────────────────────────────────────────

VISION_PROMPT_DEFAULT = """\
You are a plant-health assistant. Describe what you see in this photo of a plant.
Focus on:
- Leaf colour (green, yellow, brown patches, wilting?)
- Stem and overall posture (upright, drooping?)
- Visible soil surface (dry, moist, cracked?)
- Any signs of pests, disease, or physical damage

Be concise — 2–4 sentences. Do not give watering advice; just describe what you observe.\
"""

# keep old name as alias so any code that imported VISION_PROMPT still works
VISION_PROMPT = VISION_PROMPT_DEFAULT

# ── Reasoning ─────────────────────────────────────────────────────────────────

REASONING_SYSTEM_DEFAULT = """\
You are an expert autonomous plant caretaker. You receive sensor data and a visual
description of a plant, then decide whether to water it and whether to leave the
grow light on. You must reply with valid JSON matching this exact schema:

{
  "water": <true|false>,
  "water_seconds": <number 0–60, may be fractional like 0.5>,
  "light_on": <true|false>,
  "reasoning": "<1–3 sentence plain-English explanation>"
}

Rules:
- water_seconds must be 0 when water is false.
- Be conservative with watering — overwatering causes root rot.
- Reply with JSON only. No markdown fences, no extra keys.\
"""

# keep old private name as alias
_REASONING_SYSTEM = REASONING_SYSTEM_DEFAULT


def get_vision_prompt(cfg: dict) -> str:
    """Return the active vision prompt — config override if set, else the default."""
    override = cfg.get("ai", {}).get("prompts", {}).get("vision")
    return override if override else VISION_PROMPT_DEFAULT


def get_reasoning_system(cfg: dict) -> str:
    """Return the active reasoning system prompt — config override if set, else the default."""
    override = cfg.get("ai", {}).get("prompts", {}).get("reasoning_system")
    return override if override else REASONING_SYSTEM_DEFAULT


def build_reasoning_prompt(
    cfg: dict,
    soil: SoilReading | None,
    temp: TemperatureReading,
    plant_description: str,
    recent_cycles_summary: str,
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) for the reasoning model.

    Args:
        cfg: Top-level config dict.
        soil: Current soil reading, or None if no soil sensor is installed.
            When None, the prompt tells the model to rely on the photo and
            temperature instead, and watering is treated as suppressed.
        temp: Current temperature reading.
        plant_description: Plain-text output from the VLM.
        recent_cycles_summary: Short text describing the last few cycles
            (e.g. "Watered 3h ago. Light was off.").

    Returns:
        A (system, user) tuple ready to pass to the Ollama chat API.
    """
    plant = cfg["plant"]
    soil_cfg = cfg.get("soil", {})
    temp_cfg = cfg.get("temperature", {})
    watering = cfg["watering"]
    light = cfg["light"]

    # Convert soil ADC thresholds to moisture % for human-readable prompting.
    dry_adc = soil_cfg.get("dry_threshold", 700)
    wet_adc = soil_cfg.get("wet_threshold", 400)
    dry_pct = round((1023 - dry_adc) / 1023 * 100, 1)
    wet_pct = round((1023 - wet_adc) / 1023 * 100, 1)
    too_cold = temp_cfg.get("too_cold_celsius", 15.0)
    too_hot  = temp_cfg.get("too_hot_celsius", 32.0)

    if soil is not None:
        soil_reading_line = (
            f"  Soil moisture: {soil.moisture_pct:.1f}% "
            f"(raw ADC {soil.raw}/1023; higher raw = drier)"
        )
        soil_guardrails = (
            f'  • Soil considered "dry — watering allowed" below {dry_pct}% moisture\n'
            f'  • Soil considered "already wet — watering overridden OFF" above {wet_pct}% moisture\n'
        )
        judgement_hint = (
            "Use the visual observation and species notes to make a judgement call — "
            "moisture % alone doesn't tell the full story (a wilting plant in 40% "
            "moisture may still need water; a perky plant in 25% may not)."
        )
    else:
        soil_reading_line = (
            "  Soil moisture: NO SENSOR CONNECTED — there is no moisture reading\n"
            "  this cycle. You must judge the plant from the photo and temperature alone."
        )
        soil_guardrails = (
            "  • Soil moisture sensor is offline — any request to water will be\n"
            "    automatically suppressed, because moisture cannot be verified and\n"
            "    overwatering causes root rot. Still report your honest judgement in\n"
            '    "water" and explain it in "reasoning".\n'
        )
        judgement_hint = (
            "There is no soil sensor this cycle, so lean entirely on the visual "
            "observation and species notes: look for drooping, curling, or dry/cracked "
            "soil in the photo. State clearly in your reasoning that you are working "
            "without a moisture reading."
        )

    user = f"""\
Plant: {plant['name']}
Species notes: {plant['species_notes'].strip()}

Current sensor readings:
{soil_reading_line}
  Temperature: {temp.celsius:.1f} °C

Visual observation:
  {plant_description.strip()}

Recent history:
  {recent_cycles_summary.strip()}

Safety guardrails (these clamp your decision — you can reason within them freely):
{soil_guardrails}\
  • Temperature too cold below {too_cold} °C → light is forced on for warmth
  • Temperature too hot above {too_hot} °C → watering is suppressed (heat-stress risk)
  • Max watering pulse: {watering['max_pulse_seconds']}s per cycle
  • Min interval between waterings: {watering['min_interval_minutes']} minutes
  • Max light on per day: {light['max_on_hours_per_day']} hours

You are autonomous within these guardrails. {judgement_hint}

Decide: should the plant be watered this cycle? Should the grow light be on?
Reply with JSON only.\
"""
    return get_reasoning_system(cfg), user


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
