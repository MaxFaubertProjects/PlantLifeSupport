"""Tests for the safety layer and rule-based fallback.

These tests are deliberately self-contained — no Ollama, no hardware,
no file I/O.  They run on any machine with the venv installed.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

from plant.ai.schema import Decision
from plant.core.safety import apply_safety
from plant.core.rules import rule_based_decision
from plant.hardware.interfaces import SoilReading, TemperatureReading


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def cfg():
    return {
        "soil": {"dry_threshold": 700, "wet_threshold": 400},
        "temperature": {"too_cold_celsius": 15.0, "too_hot_celsius": 32.0},
        "watering": {"max_pulse_seconds": 8, "min_interval_minutes": 90},
        "light": {"max_on_hours_per_day": 16},
    }


def make_soil(raw: int) -> SoilReading:
    return SoilReading(raw=raw, moisture_pct=round((1023 - raw) / 1023 * 100, 1))


def make_temp(c: float) -> TemperatureReading:
    return TemperatureReading(celsius=c)


def make_cycle(*, final_water: bool, final_water_secs: float = 5.0,
               final_light_on: bool = False,
               ts: str | None = None) -> MagicMock:
    c = MagicMock()
    c.final_water = final_water
    c.final_water_secs = final_water_secs
    c.final_light_on = final_light_on
    c.ts = ts or datetime.now(timezone.utc).isoformat()
    return c


# ── Decision schema ───────────────────────────────────────────────────────────

class TestDecisionSchema:
    def test_valid_decision(self):
        d = Decision(water=True, water_seconds=5, light_on=False,
                     reasoning="soil is dry")
        assert d.water is True
        assert d.water_seconds == 5

    def test_water_seconds_forced_to_zero_when_not_watering(self):
        d = Decision(water=False, water_seconds=8, light_on=False,
                     reasoning="already moist")
        assert d.water_seconds == 0

    def test_water_seconds_out_of_range(self):
        with pytest.raises(Exception):
            Decision(water=True, water_seconds=999, light_on=False,
                     reasoning="too long")

    def test_reasoning_too_short(self):
        with pytest.raises(Exception):
            Decision(water=False, water_seconds=0, light_on=False, reasoning="ok")

    def test_fallback_factory(self):
        d = Decision.fallback("test")
        assert d.water is False
        assert d.water_seconds == 0
        assert "Rule-based fallback" in d.reasoning


# ── Safety: pulse clamping ────────────────────────────────────────────────────

class TestSafetyPulseClamping:
    def test_pulse_clamped_to_max(self, cfg):
        proposed = Decision(water=True, water_seconds=30, light_on=False,
                            reasoning="the AI wants 30 seconds")
        result = apply_safety(cfg, proposed, make_soil(800), [])
        assert result.water_seconds == cfg["watering"]["max_pulse_seconds"]
        assert "clamped" in result.reasoning

    def test_pulse_within_limit_unchanged(self, cfg):
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="reasonable request")
        result = apply_safety(cfg, proposed, make_soil(800), [])
        assert result.water_seconds == 5

    def test_no_water_zero_seconds(self, cfg):
        proposed = Decision(water=False, water_seconds=0, light_on=False,
                            reasoning="soil is fine")
        result = apply_safety(cfg, proposed, make_soil(300), [])
        assert result.water is False
        assert result.water_seconds == 0


# ── Safety: minimum watering interval ────────────────────────────────────────

class TestSafetyMinInterval:
    def test_suppresses_watering_if_too_recent(self, cfg):
        recent_ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        recent = [make_cycle(final_water=True, ts=recent_ts)]
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="wants to water again")
        result = apply_safety(cfg, proposed, make_soil(800), recent)
        assert result.water is False
        assert "suppressed" in result.reasoning

    def test_allows_watering_after_interval(self, cfg):
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
        recent = [make_cycle(final_water=True, ts=old_ts)]
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="enough time has passed")
        result = apply_safety(cfg, proposed, make_soil(800), recent)
        assert result.water is True

    def test_no_history_allows_watering(self, cfg):
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="first watering ever")
        result = apply_safety(cfg, proposed, make_soil(800), [])
        assert result.water is True


# ── Safety: wet soil override ─────────────────────────────────────────────────

class TestSafetyWetSoilOverride:
    def test_suppresses_watering_when_moist(self, cfg):
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="AI wants to water wet soil")
        moist_soil = make_soil(300)  # below wet_threshold of 400
        result = apply_safety(cfg, proposed, moist_soil, [])
        assert result.water is False
        assert "moist" in result.reasoning

    def test_allows_watering_when_dry(self, cfg):
        proposed = Decision(water=True, water_seconds=5, light_on=False,
                            reasoning="soil is dry")
        dry_soil = make_soil(750)  # above dry_threshold of 700
        result = apply_safety(cfg, proposed, dry_soil, [])
        assert result.water is True


# ── Safety: light cap ────────────────────────────────────────────────────────

class TestSafetyLightCap:
    def _make_light_history(self, hours: int, base_offset_h: int = 0):
        """Create N cycles today where the light was on."""
        now = datetime.now(timezone.utc)
        cycles = []
        for i in range(hours):
            ts = (now - timedelta(hours=base_offset_h + i)).isoformat()
            cycles.append(make_cycle(final_water=False, final_light_on=True, ts=ts))
        return cycles

    def test_light_capped_at_max(self, cfg):
        # 16 hours already on today
        history = self._make_light_history(16)
        proposed = Decision(water=False, water_seconds=0, light_on=True,
                            reasoning="AI wants light on, but cap hit")
        result = apply_safety(cfg, proposed, make_soil(600), history)
        assert result.light_on is False
        assert "capped" in result.reasoning

    def test_light_allowed_under_cap(self, cfg):
        history = self._make_light_history(8)  # only 8h today
        proposed = Decision(water=False, water_seconds=0, light_on=True,
                            reasoning="light within daily budget")
        result = apply_safety(cfg, proposed, make_soil(600), history)
        assert result.light_on is True


# ── Rule-based fallback ───────────────────────────────────────────────────────

class TestRuleBasedFallback:
    def test_waters_when_dry(self, cfg):
        d = rule_based_decision(cfg, make_soil(750), make_temp(22.0))
        assert d.water is True
        assert d.water_seconds == cfg["watering"]["max_pulse_seconds"]

    def test_skips_water_when_moist(self, cfg):
        d = rule_based_decision(cfg, make_soil(300), make_temp(22.0))
        assert d.water is False

    def test_skips_water_when_acceptable(self, cfg):
        d = rule_based_decision(cfg, make_soil(550), make_temp(22.0))
        assert d.water is False

    def test_skips_water_when_too_hot(self, cfg):
        d = rule_based_decision(cfg, make_soil(800), make_temp(35.0))
        assert d.water is False
        assert "hot" in d.reasoning

    def test_light_on_when_too_cold(self, cfg):
        d = rule_based_decision(cfg, make_soil(500), make_temp(10.0))
        assert d.light_on is True
        assert "cold" in d.reasoning

    def test_light_off_at_normal_temp(self, cfg):
        d = rule_based_decision(cfg, make_soil(500), make_temp(22.0))
        assert d.light_on is False

    def test_fallback_marker_in_reasoning(self, cfg):
        d = rule_based_decision(cfg, make_soil(600), make_temp(20.0),
                                fallback_reason="Ollama timed out")
        assert "Ollama timed out" in d.reasoning
