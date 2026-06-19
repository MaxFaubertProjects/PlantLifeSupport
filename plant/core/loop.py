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
import threading
from datetime import datetime, timedelta, timezone
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
        self.cycle_state: dict = {
            "stage": "idle",
            "stage_started_at": None,
            "cycle_started_at": None,
            # First-cycle estimate; the loop refines this after each cycle completes.
            "next_cycle_at": (
                datetime.now(timezone.utc) + timedelta(seconds=self._interval)
            ).isoformat(),
        }
        self._cycle_lock = threading.Lock()
        # Track the light state the model last prescribed so that the capture
        # flash always restores to the model's intent, not to whatever the
        # hardware happens to be at (e.g. a manual dashboard toggle).
        # Initialise from the most-recent DB cycle so a restart picks up the
        # correct idle state immediately.
        _seed = list(self._db.recent_cycles(limit=1))
        self._model_light_on: bool = bool(_seed[0].final_light_on) if _seed else False

    def _set_stage(self, stage: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.cycle_state["stage"] = stage
        self.cycle_state["stage_started_at"] = now
        if stage == "starting":
            self.cycle_state["cycle_started_at"] = now

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
            self.cycle_state["next_cycle_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=self._interval)
            ).isoformat()
            await asyncio.sleep(self._interval)

    def run_cycle(self) -> int | None:
        """Run one cycle synchronously and return the DB cycle id.

        Returns None if a cycle is already in progress (no overlap allowed).
        Useful for tests and manual triggers from the web API.
        """
        return self._run_cycle()

    # ── Pipeline ──────────────────────────────────────────────────────────────

    def _run_cycle(self) -> int | None:
        if not self._cycle_lock.acquire(blocking=False):
            log.warning("ControlLoop: cycle already in progress — skipping new trigger")
            return None
        try:
            log.info("── Cycle start ─────────────────────────────────────────────")
            self._set_stage("starting")
            try:
                return self._run_cycle_inner()
            finally:
                self._set_stage("idle")
        finally:
            self._cycle_lock.release()

    # Neutral temperature used for decision-making when the DS18B20 probe is
    # missing or its wire is intermittent. Chosen to sit comfortably between
    # too_cold and too_hot so a missing probe neither forces the warmth-light
    # on nor suppresses watering — soil moisture stays in charge. The real
    # (None) value is still what gets recorded to the DB.
    _NEUTRAL_TEMP_C = 22.0

    def _run_cycle_inner(self) -> int:
        # 1. Sense
        self._set_stage("sensing")
        soil = self.hardware.soil.read()   # may be None — no sensor installed
        from plant.hardware.interfaces import TemperatureReading
        temp_reading = self.hardware.temperature.read()  # may be None — flaky/no probe
        temp_missing = temp_reading is None
        # Downstream reasoning/rules/prompts take a non-optional temperature.
        # When the probe is unavailable, feed them a neutral stand-in but keep
        # the honest None for the DB record.
        temp = temp_reading if temp_reading is not None else TemperatureReading(
            celsius=self._NEUTRAL_TEMP_C
        )
        temp_log = "temp=unavailable (no probe)" if temp_missing else f"temp={temp.celsius:.1f}°C"
        if soil is not None:
            log.info("Sense: soil=%d ADC (%.1f%%), %s",
                     soil.raw, soil.moisture_pct, temp_log)
        else:
            log.info("Sense: soil=unavailable (no sensor), %s", temp_log)

        # 2. See
        self._set_stage("capturing")
        photo_path = self._photo_path()
        # Briefly turn the grow light on while the camera captures so the
        # photo is well-lit for the vision model. Restore the model's
        # prescribed state afterwards — not the live hardware state, which
        # might have been overridden manually via the dashboard.
        light_was_on = self._model_light_on
        self.hardware.light.set(True)
        # Defensive: the shared 5V rail dips briefly when the light relay's
        # coil energizes, and that transient has been observed to briefly
        # trigger CH1 (the valve relay) on this HAT. Re-assert valve OFF
        # immediately after activating the light to clear any spurious state.
        self.hardware.valve.off()

        # Wait for the bulb to reach steady-state brightness before opening
        # the camera. Without this delay, auto-exposure starts converging
        # against a still-warming light and ends up over-exposed once the
        # bulb fully catches up. ~1s gets us out of the LED-warmup region.
        import time as _time
        pre_delay = float(self.cfg.get("camera", {}).get("pre_capture_light_sec", 1.0))
        if pre_delay > 0:
            _time.sleep(pre_delay)

        try:
            saved_path = self.hardware.camera.capture(photo_path)
        finally:
            self.hardware.light.set(light_was_on)
            self.hardware.valve.off()   # again, after the light state restore

        # 3. Describe
        self._set_stage("describing")
        import time as _time
        _t0 = _time.monotonic()
        description = self._vision.describe(saved_path)
        vision_ms = int((_time.monotonic() - _t0) * 1000)
        log.info("Describe: %s [%.1fs]", description[:120], vision_ms / 1000)

        # 4. Reason
        self._set_stage("reasoning")
        recent = self._db.recent_cycles(limit=8)
        _t0 = _time.monotonic()
        decision, used_fallback = self._reasoning.decide(
            soil=soil,
            temp=temp,
            plant_description=description,
            recent_cycles=list(recent),
        )
        reasoning_ms = int((_time.monotonic() - _t0) * 1000)
        log.info("Reason: %s [%.1fs]",
                 "fallback" if used_fallback else "llm",
                 reasoning_ms / 1000)

        # 5. Validate
        safe_decision = apply_safety(
            cfg=self.cfg,
            proposed=decision,
            soil=soil,
            recent_cycles=list(recent),
        )

        # 6. Act
        self._set_stage("acting")
        if safe_decision.water:
            log.info("Act: watering for %.1fs", safe_decision.water_seconds)
            self.hardware.valve.pulse(safe_decision.water_seconds)
        else:
            log.info("Act: no watering this cycle")

        self.hardware.light.set(safe_decision.light_on)
        # Record what the model prescribed so the next cycle's capture flash
        # knows exactly what to restore the light to during idle.
        self._model_light_on = safe_decision.light_on
        # Same defensive clear as the capture step: the light coil's inrush
        # has been seen to spuriously latch the valve relay on this HAT.
        # Re-assert valve OFF after any light state change.
        self.hardware.valve.off()

        # 7. Record
        cycle_id = self._db.insert_cycle(
            soil_raw=soil.raw if soil is not None else None,
            soil_pct=soil.moisture_pct if soil is not None else None,
            temp_celsius=None if temp_missing else temp.celsius,
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
            vision_ms=vision_ms,
            reasoning_ms=reasoning_ms,
            warnings=safe_decision.warnings,
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
