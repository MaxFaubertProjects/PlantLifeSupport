"""Hourly decision pipeline — the heart of Plant Life Support.

Each cycle:
  1. Sense    — read soil moisture + temperature
  2. See      — capture a photo
  3. Describe — VLM: photo → plant description text
  4. Reason   — LLM: context → structured Decision JSON
  5. Validate — safety.py clamps / overrides the decision
  6. Act      — pulse the valve; set the light
  7. Record   — persist everything to SQLite
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from plant.hardware.interfaces import Hardware
from plant.storage.db import Database, open_db
from plant.ai.vision import VisionClient
from plant.ai.reasoning import ReasoningClient
from plant.core.safety import apply_safety

log = logging.getLogger(__name__)


class ControlLoop:
    def __init__(self, cfg: dict, hardware: Hardware) -> None:
        self.cfg = cfg
        self.hardware = hardware
        self._interval = cfg.get("loop_interval_minutes", 60) * 60
        self._db: Database = open_db(cfg)
        self._vision = VisionClient(cfg)
        self._reasoning = ReasoningClient(cfg)

    # ── Public API ────────────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Run the pipeline immediately, then repeat every loop_interval."""
        log.info(
            "ControlLoop: starting — interval=%ds, mode=%s",
            self._interval, self.cfg.get("mode"),
        )
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._run_cycle)
            except Exception:
                log.exception("ControlLoop: unhandled error in cycle — will retry next interval")
            await asyncio.sleep(self._interval)

    def run_cycle(self) -> int:
        """Run one cycle synchronously and return the DB cycle id.

        Useful for tests and manual triggers from the web API.
        """
        return self._run_cycle()

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _run_cycle(self) -> int:
        log.info("── Cycle start ─────────────────────────────────────────────")

        # 1. Sense
        soil = self.hardware.soil.read()
        temp = self.hardware.temperature.read()
        log.info("Sense: soil=%d ADC (%.1f%%), temp=%.1f°C",
                 soil.raw, soil.moisture_pct, temp.celsius)

        # 2. See
        photo_path = self._photo_path()
        saved_path = self.hardware.camera.capture(photo_path)

        # 3. Describe
        description = self._vision.describe(saved_path)
        log.info("Describe: %s", description[:120])

        # 4. Reason
        recent = self._db.recent_cycles(limit=8)
        decision, used_fallback = self._reasoning.decide(
            soil=soil,
            temp=temp,
            plant_description=description,
            recent_cycles=list(recent),
        )

        # 5. Validate
        safe_decision = apply_safety(
            cfg=self.cfg,
            proposed=decision,
            soil=soil,
            recent_cycles=list(recent),
        )

        # 6. Act
        if safe_decision.water:
            log.info("Act: watering for %.1fs", safe_decision.water_seconds)
            self.hardware.valve.pulse(safe_decision.water_seconds)
        else:
            log.info("Act: no watering this cycle")

        self.hardware.light.set(safe_decision.light_on)

        # 7. Record
        cycle_id = self._db.insert_cycle(
            soil_raw=soil.raw,
            soil_pct=soil.moisture_pct,
            temp_celsius=temp.celsius,
            photo_path=saved_path,
            plant_description=description,
            ai_water=decision.water,
            ai_water_seconds=decision.water_seconds,
            ai_light_on=decision.light_on,
            ai_reasoning=decision.reasoning,
            used_fallback=used_fallback,
            final_water=safe_decision.water,
            final_water_secs=float(safe_decision.water_seconds),
            final_light_on=safe_decision.light_on,
        )

        log.info(
            "── Cycle complete (id=%d): water=%s, light=%s ─────────────────",
            cycle_id, safe_decision.water, safe_decision.light_on,
        )
        return cycle_id

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _photo_path(self) -> str:
        photo_dir = Path(self.cfg["storage"]["photo_dir"])
        photo_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return str(photo_dir / f"plant_{ts}.jpg")

    @property
    def db(self) -> Database:
        return self._db
