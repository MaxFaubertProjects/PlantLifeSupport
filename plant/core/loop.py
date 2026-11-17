"""Decision pipeline — the heart of Plant Life Support.

A full check runs a few times a day at fixed times (``schedule.checks_per_day``
starting at ``schedule.first_check`` — default 08:00 and 20:00, see
plant/core/schedule.py). Each check:
  1. Sense    — read soil moisture + temperature (+ leaf temperature from
                the optional USB thermal camera)
  2. See      — capture a photo
  3. Describe — VLM: photo → plant description text
  4. Reason   — LLM: context → structured Decision JSON
  5. Validate — safety.py clamps / overrides the decision
  6. Act      — pulse the valve; set the light
  7. Record   — persist everything to SQLite
  8. Plan     — schedule light switches and waterings until the next check,
                with a moisture forecast (served at /api/schedule)

Between checks a light runner carries out the plan, and every 30 minutes
reads the soil (cheap — no camera, no models). If the soil is already below
the dry line it waters anyway (backup), through the same safety layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plant.hardware.interfaces import Hardware
from plant.storage.db import Database, open_db
from plant.ai.vision import VisionClient
from plant.ai.reasoning import ReasoningClient
from plant.core.safety import apply_safety
from plant.core import schedule as sched

log = logging.getLogger(__name__)


class ControlLoop:
    def __init__(self, cfg: dict, hardware: Hardware) -> None:
        self.cfg = cfg
        self.hardware = hardware
        self._db: Database = open_db(cfg)
        self._vision = VisionClient(cfg)
        self._reasoning = ReasoningClient(cfg)
        self.cycle_state: dict = {
            "stage": "idle",
            "stage_started_at": None,
            "cycle_started_at": None,
            "next_cycle_at": sched.next_check(cfg).astimezone(timezone.utc).isoformat(),
        }
        # The plan between checks, kept next to the database so a restart
        # carries on with it instead of forcing a new check.
        self._plan_path = Path(cfg["storage"]["db_path"]).with_name("schedule.json")
        self.plan: dict | None = self._load_plan()
        self._last_reading_at: datetime | None = None
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
        """Run checks at the scheduled times and the plan in between.

        On start-up a check only runs if the most recent scheduled one was
        missed (e.g. the Pi was off at 08:00) — restarting the service no
        longer forces a check.
        """
        sc = sched.schedule_cfg(self.cfg)
        log.info("ControlLoop: starting — %d checks/day from %s, mode=%s",
                 sc["checks_per_day"], sc["first_check"], self.cfg.get("mode"))
        loop = asyncio.get_event_loop()
        if self._missed_check():
            log.info("ControlLoop: the last scheduled check was missed — checking now")
            await self._run_check(loop)
        elif self.plan is None:
            self.replan(source="startup")
        while True:
            nxt = sched.next_check(self.cfg)
            self.cycle_state["next_cycle_at"] = nxt.astimezone(timezone.utc).isoformat()
            # Short naps so a change of check times in Settings takes effect,
            # and so the plan runner gets its turn.
            while datetime.now(timezone.utc) < nxt:
                try:
                    await loop.run_in_executor(None, self.run_plan_step)
                except Exception:
                    log.exception("Plan runner: error")
                await asyncio.sleep(20)
                new = sched.next_check(self.cfg)
                if new != nxt:
                    nxt = new
                    self.cycle_state["next_cycle_at"] = nxt.astimezone(timezone.utc).isoformat()
            await self._run_check(loop)

    async def _run_check(self, loop) -> None:
        try:
            await loop.run_in_executor(None, self._run_cycle)
        except Exception:
            log.exception("ControlLoop: unhandled error in check — will retry at the next one")

    def _missed_check(self) -> bool:
        latest = self._db.latest_cycle()
        if latest is None:
            return True
        try:
            last = datetime.fromisoformat(latest.ts)
        except ValueError:
            return True
        return last < sched.previous_check(self.cfg)

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
        thermal = self._read_thermal()

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
            thermal=thermal,
            air_c=None if temp_missing else temp.celsius,
            schedule_note=self._schedule_note(),
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
            last_water_ts=self._last_event_water(),
        )

        # The light follows the daily light window (Settings → Schedule);
        # a cold reading still forces it on for warmth.
        too_cold = float(self.cfg.get("temperature", {}).get("too_cold_celsius", 15.0))
        in_window = sched.light_planned_on(self.cfg, datetime.now(timezone.utc))
        cold = (not temp_missing) and temp.celsius < too_cold
        want_light = in_window or cold
        if want_light != safe_decision.light_on:
            why = ("inside the light window" if in_window else
                   "cold — on for warmth" if cold else "outside the light window")
            safe_decision = safe_decision.model_copy(update={
                "light_on": want_light,
                "reasoning": safe_decision.reasoning + f"\n[Schedule] light {'on' if want_light else 'off'} — {why}",
            })

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
            leaf_temp_c=thermal["spot_c"] if thermal else None,
            leaf_air_delta_c=thermal.get("spot_minus_air_c") if thermal else None,
        )

        log.info(
            "── Cycle complete (id=%d): water=%s, light=%s ─────────────────",
            cycle_id, safe_decision.water, safe_decision.light_on,
        )

        # 8. Plan until the next check
        try:
            self.replan(
                source="check",
                soil_pct=soil.moisture_pct if soil is not None else None,
                light_now=safe_decision.light_on,
                watered_now_secs=safe_decision.water_seconds if safe_decision.water else 0.0,
                ai_reasoning=decision.reasoning,
            )
        except Exception:
            log.exception("Plan: could not build the schedule")
        return cycle_id

    # ── Plan between checks ─────────────────────────────────────────────────

    def _thresholds_pct(self) -> tuple[float, float]:
        soil_cfg = self.cfg.get("soil", {})
        cal_dry = int(soil_cfg.get("cal_dry", 1023))
        cal_wet = int(soil_cfg.get("cal_wet", 0))
        span = (cal_dry - cal_wet) or 1
        pct = lambda adc: max(0.0, min(100.0, (cal_dry - adc) / span * 100))
        return pct(soil_cfg.get("dry_threshold", 700)), pct(soil_cfg.get("wet_threshold", 400))

    def _last_event_water(self) -> datetime | None:
        ts = self._db.last_water_event_ts()
        try:
            return datetime.fromisoformat(ts) if ts else None
        except ValueError:
            return None

    def _rate_points(self) -> list[dict]:
        """Moisture readings from checks and between checks, oldest first."""
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        pts: list[dict] = []
        for c in self._db.recent_cycles(limit=400):
            if c.ts < since:
                break
            try:
                ts = datetime.fromisoformat(c.ts)
            except ValueError:
                continue
            pts.append({"ts": ts, "soil_pct": c.soil_pct, "light_on": bool(c.final_light_on),
                        "water_secs": float(c.final_water_secs or 0) if c.final_water else 0.0})
        light = None
        for e in self._db.events_since(since):
            ts = datetime.fromisoformat(e["ts"])
            if e["kind"] in ("light_on", "light_off"):
                light = e["kind"] == "light_on"
            if e["kind"] == "water" and pts:
                # attach to the reading just before it
                prev = max((p for p in pts if p["ts"] <= ts), key=lambda p: p["ts"], default=None)
                if prev is not None:
                    prev["water_secs"] = prev.get("water_secs", 0) + float(e["seconds"] or 0)
            elif e["kind"] == "reading":
                pts.append({"ts": ts, "soil_pct": e["soil_pct"],
                            "light_on": e["light_on"] if e["light_on"] is not None else bool(light),
                            "water_secs": 0.0})
        pts.sort(key=lambda p: p["ts"])
        return pts

    def replan(self, *, source: str = "replan", soil_pct: float | None = None,
               light_now: bool | None = None, watered_now_secs: float = 0.0,
               ai_reasoning: str = "") -> dict:
        """Build (and save) the plan from now until the next check."""
        if soil_pct is None and source != "check":
            try:
                r = self.hardware.soil.read()
                soil_pct = r.moisture_pct if r is not None else None
            except Exception:
                soil_pct = None
        if light_now is None:
            light_now = self._model_light_on
        dry_pct, wet_pct = self._thresholds_pct()
        if not ai_reasoning and self.plan and source != "check":
            # keep the last check's explanation when only the timing changed
            ai_reasoning = self.plan.get("ai_reasoning", "")
        plan = sched.build_plan(
            self.cfg, now=datetime.now(timezone.utc), soil_pct=soil_pct,
            dry_pct=dry_pct, wet_pct=wet_pct, rates=sched.learn_rates(self._rate_points()),
            light_now=light_now, watered_now_secs=watered_now_secs,
            last_water_at=self._last_event_water(), ai_reasoning=ai_reasoning, source=source,
        )
        plan["ai_reasoning"] = ai_reasoning
        self.plan = plan
        self._save_plan()
        log.info("Plan (%s): %d actions until %s", source,
                 sum(1 for a in plan["actions"] if a["kind"] != "check"), plan["next_check_at"])
        return plan

    def run_plan_step(self) -> None:
        """Carry out due actions, and take the half-hourly reading (+ backup)."""
        if not self._cycle_lock.acquire(blocking=False):
            return                      # a check is running — it owns the hardware
        try:
            now = datetime.now(timezone.utc)
            plan = self.plan
            if plan:
                done = set(plan.setdefault("done", []))
                for i, a in enumerate(plan.get("actions", [])):
                    if a["kind"] == "check" or i in done:
                        continue
                    at = datetime.fromisoformat(a["at"])
                    if at > now:
                        continue
                    late = (now - at).total_seconds() / 60
                    self._do_action(a, late_minutes=late)
                    done.add(i)
                    plan["done"] = sorted(done)
                    self._save_plan()
            if (self._last_reading_at is None
                    or now - self._last_reading_at >= timedelta(minutes=sched.READING_MINUTES)):
                self._last_reading_at = now
                self._reading_and_backup()
        finally:
            self._cycle_lock.release()

    def _do_action(self, a: dict, late_minutes: float) -> None:
        kind = a["kind"]
        if kind in ("light_on", "light_off"):
            on = kind == "light_on"
            self.hardware.light.set(on)
            self.hardware.valve.off()        # relay-coil inrush guard, as in the check
            self._model_light_on = on
            self._db.insert_event(kind, light_on=on, source="plan", note=a.get("note"))
            log.info("Plan: light %s", "on" if on else "off")
        elif kind == "water":
            if late_minutes > 30:
                self._db.insert_event("skipped", seconds=a.get("seconds"), source="plan",
                                      note=f"watering skipped — {late_minutes:.0f} min late (backup covers it)")
                return
            self._water(float(a.get("seconds") or 0), source="plan", note=a.get("note"))

    def _water(self, seconds: float, *, source: str, note: str | None) -> bool:
        from plant.ai.schema import Decision
        soil = self.hardware.soil.read()
        proposed = Decision(water=True, water_seconds=min(seconds, 60.0), light_on=self._model_light_on,
                            reasoning=f"{source} watering between checks")
        safe = apply_safety(cfg=self.cfg, proposed=proposed, soil=soil,
                            recent_cycles=list(self._db.recent_cycles(limit=8)),
                            last_water_ts=self._last_event_water())
        if not safe.water:
            why = safe.reasoning.split("[Safety]")[-1].strip()
            self._db.insert_event("skipped", seconds=seconds,
                                  soil_pct=soil.moisture_pct if soil else None,
                                  source=source, note=f"watering skipped — {why}")
            log.info("Plan: %s watering skipped — %s", source, why)
            return False
        log.info("Plan: %s watering %.1fs", source, safe.water_seconds)
        self.hardware.valve.pulse(safe.water_seconds)
        self.hardware.valve.off()
        self._db.insert_event("water", seconds=safe.water_seconds,
                              soil_pct=soil.moisture_pct if soil else None,
                              soil_raw=soil.raw if soil else None, source=source, note=note)
        return True

    def _reading_and_backup(self) -> None:
        soil = self.hardware.soil.read()
        try:
            temp = self.hardware.temperature.read()
        except Exception:
            temp = None
        self._db.insert_event(
            "reading", soil_pct=soil.moisture_pct if soil else None,
            soil_raw=soil.raw if soil else None,
            temp_celsius=temp.celsius if temp is not None else None,
            light_on=self.hardware.light.is_on(), source="backup")
        if soil is None:
            return
        dry_adc = self.cfg.get("soil", {}).get("dry_threshold", 700)
        if soil.raw > dry_adc:
            max_pulse = float(self.cfg.get("watering", {}).get("max_pulse_seconds", 8))
            log.info("Backup: soil %.0f%% is below the dry line — watering", soil.moisture_pct)
            self._water(max_pulse, source="backup",
                        note=f"soil {soil.moisture_pct:.0f}% dropped below the dry line before the plan expected")

    def _schedule_note(self) -> str:
        sc = sched.schedule_cfg(self.cfg)
        nxt = sched.next_check(self.cfg)
        hrs = (nxt - datetime.now(timezone.utc)).total_seconds() / 3600
        return (f"This is one of {sc['checks_per_day']} full checks a day. The next one is at "
                f"{nxt.strftime('%H:%M')} (in about {hrs:.0f} h). Between checks a planner times the "
                f"light ({sc['light_on_at']} for {sc['light_hours']:g} h) and waters when the forecast reaches "
                "the dry line, with a backup soil check every 30 min. Decide what to do NOW; "
                "in your reasoning, mention anything the planner should know.")

    def _load_plan(self) -> dict | None:
        try:
            plan = json.loads(self._plan_path.read_text())
            # a plan whose next check has passed is stale
            if datetime.fromisoformat(plan["next_check_at"]) < datetime.now(timezone.utc) - timedelta(hours=1):
                return None
            return plan
        except Exception:
            return None

    def _save_plan(self) -> None:
        try:
            tmp = self._plan_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.plan))
            tmp.replace(self._plan_path)
        except Exception as exc:
            log.warning("Plan: could not save — %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _read_thermal(self) -> dict | None:
        """Stats from the thermal camera, or None if absent/uncalibrated/failing.

        Never raises: the thermal camera is an optional extra and must not be
        able to stop a watering cycle.
        """
        cam = getattr(self.hardware, "thermal", None)
        if cam is None or not getattr(cam, "available", False):
            return None
        try:
            reading = cam.read(max_age=30)
        except Exception as exc:
            log.warning("Sense: thermal camera read failed — %s", exc)
            return None
        if not reading.calibrated:
            log.info("Sense: thermal camera not calibrated — leaf temperature skipped")
            return None
        stats = reading.stats
        log.info("Sense: leaf=%.1f°C (spot), view %.1f–%.1f°C",
                 stats["spot_c"], stats["min_c"], stats["max_c"])
        return stats

    def _photo_path(self) -> str:
        photo_dir = Path(self.cfg["storage"]["photo_dir"])
        photo_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return str(photo_dir / f"plant_{ts}.jpg")

    @property
    def db(self) -> Database:
        return self._db
