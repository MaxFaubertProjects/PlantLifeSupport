"""Pydantic model for the structured AI decision.

The reasoning LLM is instructed to return exactly this JSON.
Pydantic validates every field before the decision reaches safety.py.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, BeforeValidator
from typing import Annotated


def _coerce_float(v):
    # Be lenient — small models sometimes emit floats ("3.0"), ints (3), or
    # strings ("3"). Stored as float because the safety max_pulse_seconds can
    # be sub-second (e.g. 0.5s during calibration) and we don't want fractional
    # durations getting silently truncated to zero.
    if isinstance(v, bool):
        return float(int(v))
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return float(v.strip())
    return v


class Decision(BaseModel):
    """Structured output from the reasoning LLM."""

    water: bool = Field(
        description="True if the plant should be watered this cycle."
    )
    water_seconds: Annotated[float, BeforeValidator(_coerce_float)] = Field(
        ge=0,
        le=60,
        description="Requested watering duration in seconds (0 if water=False).",
    )
    light_on: bool = Field(
        description="True if the grow light should be on after this cycle."
    )
    reasoning: str = Field(
        min_length=10,
        description="Plain-English explanation of the decision (1–3 sentences).",
    )

    @field_validator("water_seconds")
    @classmethod
    def water_seconds_zero_when_not_watering(cls, v: float, info) -> float:
        # Coerce to 0 if AI says water=False but gave a nonzero duration.
        if info.data.get("water") is False and v != 0:
            return 0.0
        return v

    @classmethod
    def fallback(cls, reason: str) -> "Decision":
        """Return a safe no-op decision with a fallback note."""
        return cls(
            water=False,
            water_seconds=0.0,
            light_on=False,
            reasoning=f"[Rule-based fallback] {reason}",
        )
