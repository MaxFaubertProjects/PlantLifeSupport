"""Full pipeline integration test in simulation mode.

Runs the real ControlLoop with mock hardware and a temp SQLite database.
No Ollama required — the AI clients fall back to rule-based decisions.
Verifies that the entire sense → describe → reason → validate → act → record
pipeline completes and persists sensible data.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from plant.config import load as load_config
from plant.hardware.factory import get_hardware
from plant.core.loop import ControlLoop


@pytest.fixture
def tmp_cfg(tmp_path):
    """Load real config but redirect data paths to a temp directory."""
    cfg = load_config()
    cfg["mode"] = "simulation"
    cfg["storage"]["db_path"] = str(tmp_path / "plant.db")
    cfg["storage"]["photo_dir"] = str(tmp_path / "photos")
    # Speed up: very short Ollama timeout so fallback triggers immediately
    cfg["ai"]["timeout_seconds"] = 2
    return cfg


@pytest.fixture
def loop(tmp_cfg):
    hw = get_hardware(tmp_cfg)
    return ControlLoop(cfg=tmp_cfg, hardware=hw)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestFullPipelineSim:
    def test_single_cycle_completes(self, loop):
        """A cycle runs without raising and returns a positive DB id."""
        cycle_id = loop.run_cycle()
        assert isinstance(cycle_id, int)
        assert cycle_id > 0

    def test_cycle_persisted_to_db(self, loop):
        """After one cycle, the DB contains exactly one record with valid data."""
        loop.run_cycle()
        latest = loop.db.latest_cycle()

        assert latest is not None
        assert latest.id == 1
        assert 0 <= latest.soil_pct <= 100
        assert -10 <= latest.temp_celsius <= 50
        assert latest.photo_path is not None
        assert Path(latest.photo_path).exists()

    def test_cycle_has_decision_fields(self, loop):
        """Decision fields are populated (even when using the rule fallback)."""
        loop.run_cycle()
        c = loop.db.latest_cycle()

        assert c.final_water is not None
        assert c.final_light_on is not None
        assert c.ai_reasoning is not None and len(c.ai_reasoning) > 10
        # With Ollama down, fallback must be True
        assert c.used_fallback is True

    def test_multiple_cycles_accumulate(self, loop):
        """Running three cycles stores three records."""
        for _ in range(3):
            loop.run_cycle()
        assert loop.db.cycle_count() == 3

    def test_soil_dries_between_cycles(self, loop):
        """Soil moisture decreases across consecutive cycles (drying model)."""
        loop.run_cycle()
        pct_1 = loop.db.latest_cycle().soil_pct

        loop.run_cycle()
        pct_2 = loop.db.latest_cycle().soil_pct

        # Mock soil dries by sim_dry_rate each cycle; pct goes DOWN as raw goes UP
        assert pct_2 <= pct_1, (
            f"Expected soil to dry: {pct_1:.1f}% → {pct_2:.1f}%"
        )

    def test_watering_raises_moisture(self, loop, tmp_cfg):
        """After a watering pulse, the next reading should show higher moisture."""
        from plant.hardware.mock import MockSoilSensor

        soil_sensor: MockSoilSensor = loop.hardware.soil  # type: ignore[assignment]
        initial_raw = soil_sensor._raw

        # Simulate a watering event directly
        soil_sensor.simulate_watering()
        watered_raw = soil_sensor._raw

        # Lower raw ADC = wetter soil = higher moisture %
        assert watered_raw < initial_raw

    def test_safety_never_exceeded(self, loop, tmp_cfg):
        """Final watering pulse is always within the configured max."""
        max_pulse = tmp_cfg["watering"]["max_pulse_seconds"]
        for _ in range(3):
            loop.run_cycle()

        for c in loop.db.recent_cycles():
            if c.final_water:
                assert c.final_water_secs <= max_pulse, (
                    f"Pulse {c.final_water_secs}s exceeded max {max_pulse}s"
                )

    def test_photo_file_created(self, loop):
        """Camera writes a file that exists after the cycle."""
        loop.run_cycle()
        c = loop.db.latest_cycle()
        assert c.photo_path is not None
        assert Path(c.photo_path).exists()
        assert Path(c.photo_path).stat().st_size > 0

    def test_recent_cycles_newest_first(self, loop):
        """recent_cycles() returns rows with descending IDs."""
        for _ in range(5):
            loop.run_cycle()
        cycles = list(loop.db.recent_cycles(limit=5))
        ids = [c.id for c in cycles]
        assert ids == sorted(ids, reverse=True)
