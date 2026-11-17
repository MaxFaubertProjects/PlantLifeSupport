"""Twice-daily checks and the plan that runs between them."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from plant.config import load as load_config
from plant.core import schedule as sched
from plant.core.loop import ControlLoop
from plant.hardware.factory import get_hardware


def _local(h, m=0):
    return datetime(2026, 11, 17, h, m).astimezone()


CFG = {"schedule": {"checks_per_day": 2, "first_check": "08:00", "light_on_at": "07:00"},
       "light": {"max_on_hours_per_day": 12},
       "watering": {"max_pulse_seconds": 8, "min_interval_minutes": 90}}


class TestCheckTimes:
    def test_twice_a_day(self):
        assert sched.next_check(CFG, _local(8, 1)) == _local(20)
        assert sched.previous_check(CFG, _local(8, 1)) == _local(8)
        assert sched.next_check(CFG, _local(21)) == _local(8) + timedelta(days=1)

    def test_custom_frequency(self):
        cfg = {"schedule": {"checks_per_day": 3, "first_check": "06:30"}}
        assert sched.next_check(cfg, _local(7)) == _local(14, 30)
        assert sched.check_interval_minutes(cfg) == 480

    def test_light_hours_capped(self):
        cfg = {"schedule": {"light_hours": 20}, "light": {"max_on_hours_per_day": 12}}
        assert sched.schedule_cfg(cfg)["light_hours"] == 12


class TestPlan:
    RATES = sched.Rates(2.0, 1.0, 4.0, 10, True)

    def test_waters_before_dry_line_and_ends_at_next_check(self):
        plan = sched.build_plan(CFG, now=_local(8), soil_pct=40, dry_pct=30, wet_pct=60,
                                rates=self.RATES, light_now=True)
        waters = [a for a in plan["actions"] if a["kind"] == "water"]
        assert waters and all(0 < a["seconds"] <= 8 for a in waters)
        assert plan["forecast"][-1]["ts"] == plan["next_check_at"]
        assert min(f["soil_pct"] for f in plan["forecast"]) >= 30 - 1
        kinds = [a["kind"] for a in plan["actions"]]
        assert "light_off" in kinds and kinds[-1] == "check"

    def test_no_water_when_moist(self):
        plan = sched.build_plan(CFG, now=_local(8), soil_pct=58, dry_pct=30, wet_pct=60,
                                rates=self.RATES, light_now=True)
        assert not [a for a in plan["actions"] if a["kind"] == "water"]

    def test_min_interval_between_planned_waterings(self):
        fast = sched.Rates(8.0, 6.0, 1.0, 10, True)
        plan = sched.build_plan(CFG, now=_local(8), soil_pct=35, dry_pct=30, wet_pct=60,
                                rates=fast, light_now=True)
        ts = [datetime.fromisoformat(a["at"]) for a in plan["actions"] if a["kind"] == "water"]
        assert all(b - a >= timedelta(minutes=90) for a, b in zip(ts, ts[1:]))


@pytest.fixture
def loop(tmp_path):
    cfg = load_config()
    cfg["mode"] = "simulation"
    cfg["storage"]["db_path"] = str(tmp_path / "plant.db")
    cfg["storage"]["photo_dir"] = str(tmp_path / "photos")
    cfg["ai"]["timeout_seconds"] = 2
    cfg["ai"]["ollama_base_url"] = "http://127.0.0.1:9"
    return ControlLoop(cfg=cfg, hardware=get_hardware(cfg))


class TestRunner:
    def test_check_builds_and_saves_plan(self, loop):
        loop.run_cycle()
        assert loop.plan and loop.plan["source"] == "check"
        assert (loop._plan_path).exists()
        assert not loop._missed_check()

    def test_due_actions_run_once(self, loop):
        now = datetime.now(timezone.utc)
        loop.hardware.light.set(False)
        loop.plan = {"next_check_at": (now + timedelta(hours=5)).isoformat(), "done": [], "actions": [
            {"at": (now - timedelta(minutes=1)).isoformat(), "kind": "light_on"},
            {"at": (now - timedelta(hours=2)).isoformat(), "kind": "water", "seconds": 2},
            {"at": (now + timedelta(hours=1)).isoformat(), "kind": "light_off"},
        ]}
        loop.run_plan_step()
        loop.run_plan_step()
        ev = loop.db.events_since((now - timedelta(hours=1)).isoformat())
        kinds = [e["kind"] for e in ev]
        assert kinds.count("light_on") == 1 and loop.hardware.light.is_on()
        assert "light_off" not in kinds
        # a watering more than 30 min late is skipped, not done
        assert "water" not in [e["kind"] for e in ev if e["source"] == "plan"]
        assert any(e["kind"] == "skipped" for e in ev)
        assert kinds.count("reading") == 1

    def test_backup_waters_when_dry(self, loop):
        from plant.hardware.interfaces import SoilReading
        dry = loop.cfg["soil"]["dry_threshold"] + 20
        loop.hardware.soil.read = lambda: SoilReading(raw=dry, moisture_pct=5.0)
        loop.plan = None
        loop.run_plan_step()
        ev = loop.db.events_since((datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat())
        assert [e for e in ev if e["kind"] == "water" and e["source"] == "backup"]
