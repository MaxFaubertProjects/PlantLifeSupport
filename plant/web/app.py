"""FastAPI web application — dashboard API + manual overrides.

Endpoints
─────────
GET  /                     → dashboard HTML
GET  /photos/{filename}    → serve plant photos
GET  /api/status           → latest cycle + current hardware state
GET  /api/cycles           → recent cycles for charts + reasoning log
POST /api/water            → trigger a manual watering pulse
POST /api/light            → set light on/off
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# Request models must live at module level so FastAPI can inspect them correctly
class WaterRequest(BaseModel):
    seconds: float = 3.0


class LightRequest(BaseModel):
    on: bool


class PlantConfigSection(BaseModel):
    name: Optional[str] = None
    species: Optional[str] = None
    species_notes: Optional[str] = None


class SoilRanges(BaseModel):
    # The UI thinks in moisture percent (0–100, higher = wetter);
    # we convert to ADC (0–1023, higher = drier) before storing.
    dry_pct: Optional[float] = None
    wet_pct: Optional[float] = None


class TemperatureRanges(BaseModel):
    too_cold_celsius: Optional[float] = None
    too_hot_celsius: Optional[float] = None


class WateringRanges(BaseModel):
    max_pulse_seconds: Optional[float] = None
    min_interval_minutes: Optional[int] = None


class LightRanges(BaseModel):
    max_on_hours_per_day: Optional[float] = None


class PromptsUpdateRequest(BaseModel):
    vision: Optional[str] = None
    reasoning_system: Optional[str] = None


class ConfigUpdateRequest(BaseModel):
    loop_interval_minutes: Optional[int] = None
    plant: Optional[PlantConfigSection] = None
    soil: Optional[SoilRanges] = None
    temperature: Optional[TemperatureRanges] = None
    watering: Optional[WateringRanges] = None
    light: Optional[LightRanges] = None


def _adc_to_pct(adc: int, cal_dry: int = 1023, cal_wet: int = 0) -> float:
    """Convert a raw ADC reading to moisture % using calibration endpoints.

    ``cal_dry`` is the raw ADC reading in completely dry air;
    ``cal_wet`` is the raw reading fully submerged in water.
    Defaults (1023 / 0) reproduce the old full-range behaviour so this
    is backward-compatible with configs that have no cal values.
    """
    span = cal_dry - cal_wet
    if span <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (cal_dry - adc) / span * 100)), 1)


def _pct_to_adc(pct: float, cal_dry: int = 1023, cal_wet: int = 0) -> int:
    """Convert a moisture % back to a raw ADC threshold value."""
    adc = cal_dry - (pct / 100) * (cal_dry - cal_wet)
    return max(0, min(1023, round(adc)))

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
PHOTO_DIR_FALLBACK = Path("data/photos")


def create_app(cfg: dict, hardware, control_loop) -> FastAPI:
    app = FastAPI(title="Plant Life Support", version="0.1.0")

    # ── Static files ──────────────────────────────────────────────────────────
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ── Dashboard HTML ────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        html = STATIC_DIR / "index.html"
        if html.exists():
            return html.read_text()
        return HTMLResponse("<h1>Dashboard not found — build static files first.</h1>")

    # ── Photo serving ─────────────────────────────────────────────────────────

    @app.get("/photos/{filename}")
    async def serve_photo(filename: str):
        photo_dir = Path(cfg["storage"]["photo_dir"])
        path = photo_dir / filename
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="Photo not found")
        # Security: prevent path traversal
        path.resolve().relative_to(photo_dir.resolve())
        return FileResponse(str(path))

    # ── API: live snapshot ────────────────────────────────────────────────────

    @app.get("/api/snapshot.jpg")
    async def api_snapshot():
        """Fresh camera JPEG for the dashboard's live-ish view.

        Lower resolution than the cycle's official capture, no grow-light
        flash. Camera access is locked, so if a cycle is in its capture
        step this will wait a few seconds.
        """
        snap = getattr(hardware.camera, "snapshot", None)
        if snap is None:
            raise HTTPException(status_code=501, detail="snapshot not supported")
        try:
            jpeg = await asyncio.get_event_loop().run_in_executor(None, snap)
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"snapshot failed: {exc}")
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={
                # Aggressively prevent caching so the dashboard always
                # sees the latest frame.
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    # ── API: status ───────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        """Return the latest cycle data and current hardware state."""
        from datetime import datetime, timezone
        db = control_loop.db
        latest = db.latest_cycle()
        light_on = hardware.light.is_on()
        # Live valve state — True while a pulse is in flight (or while a
        # manual override holds it open). Used by the dashboard's water
        # button to toggle between "give a drink" and "stop pump".
        pump_on = getattr(hardware.valve, "is_open", lambda: False)()

        cycle_state = dict(control_loop.cycle_state)
        nxt = cycle_state.get("next_cycle_at")
        if nxt:
            eta = (datetime.fromisoformat(nxt) - datetime.now(timezone.utc)).total_seconds()
            cycle_state["next_cycle_eta_sec"] = max(0, int(eta))
        else:
            cycle_state["next_cycle_eta_sec"] = None

        plant_cfg = cfg.get("plant", {})
        plant = {
            "name": plant_cfg.get("name", "Phil"),
            "species": plant_cfg.get("species", plant_cfg.get("name", "Plant")),
        }

        soil_cfg  = cfg.get("soil", {})
        temp_cfg  = cfg.get("temperature", {})
        water_cfg = cfg.get("watering", {})
        light_cfg = cfg.get("light", {})
        cal_dry = int(soil_cfg.get("cal_dry", 1023))
        cal_wet = int(soil_cfg.get("cal_wet", 0))
        thresholds = {
            "dry_pct":              _adc_to_pct(soil_cfg.get("dry_threshold", 700), cal_dry, cal_wet),
            "wet_pct":              _adc_to_pct(soil_cfg.get("wet_threshold", 400), cal_dry, cal_wet),
            "too_cold_celsius":     temp_cfg.get("too_cold_celsius", 15.0),
            "too_hot_celsius":      temp_cfg.get("too_hot_celsius", 32.0),
            "max_pulse_seconds":    water_cfg.get("max_pulse_seconds", 8),
            "min_interval_minutes": water_cfg.get("min_interval_minutes", 90),
            "max_on_hours_per_day": light_cfg.get("max_on_hours_per_day", 16),
        }

        # Which sensors are physically present. A driver advertises absence
        # with `available = False` (e.g. NullSoilSensor); anything without
        # the attribute is assumed present.
        sensors = {
            "soil":        getattr(hardware.soil, "available", True),
            "temperature": getattr(hardware.temperature, "available", True),
            "camera":      getattr(hardware.camera, "available", True),
        }

        return {
            "mode": cfg.get("mode", "simulation"),
            "cycle_count": db.cycle_count(),
            "light_on": light_on,
            "pump_on": pump_on,
            "latest": _cycle_to_dict(latest) if latest else None,
            "cycle_state": cycle_state,
            "plant": plant,
            "thresholds": thresholds,
            "sensors": sensors,
        }

    # ── API: cycles (for charts + reasoning log) ──────────────────────────────

    @app.get("/api/cycles")
    async def api_cycles(limit: int = 48) -> list[dict[str, Any]]:
        """Return recent cycles, newest first, for charts and the reasoning log."""
        limit = max(1, min(limit, 200))
        db = control_loop.db
        return [_cycle_to_dict(c) for c in db.recent_cycles(limit=limit)]

    # ── API: clear history (destructive!) ─────────────────────────────────────

    @app.delete("/api/cycles")
    async def api_cycles_clear() -> dict[str, Any]:
        """Wipe every recorded cycle and delete all stored photos.

        Irreversible. The UI button that calls this is gated by a JS
        confirm() dialog. We refuse to clear while a cycle is mid-flight,
        because that cycle is about to write a row and we'd be racing it.
        """
        if control_loop.cycle_state.get("stage") not in (None, "idle"):
            raise HTTPException(
                status_code=409,
                detail="cannot clear history while a cycle is running",
            )
        db = control_loop.db
        removed = db.clear_cycles()

        # Best-effort photo cleanup — only JPG/PNG inside the configured
        # photo_dir, never traverse symlinks, never reach outside the dir.
        photos_removed = 0
        photo_dir = Path(cfg["storage"]["photo_dir"]).resolve()
        if photo_dir.exists() and photo_dir.is_dir():
            for p in photo_dir.iterdir():
                if not p.is_file():
                    continue
                if p.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                    continue
                try:
                    p.unlink()
                    photos_removed += 1
                except OSError as exc:
                    log.warning("clear history: failed to delete %s — %s", p, exc)
        log.info(
            "Clear history: %d cycles + %d photos removed",
            removed, photos_removed,
        )
        return {"cycles_removed": removed, "photos_removed": photos_removed}

    # ── API: manual water ─────────────────────────────────────────────────────

    @app.post("/api/water")
    async def api_water(req: WaterRequest):
        """Trigger a manual watering pulse (capped at max_pulse_seconds)."""
        max_pulse = cfg["watering"]["max_pulse_seconds"]
        secs = max(0.5, min(req.seconds, max_pulse))
        log.info("Manual water: %.1fs", secs)
        await asyncio.get_event_loop().run_in_executor(
            None, hardware.valve.pulse, secs
        )
        return {"watered_seconds": secs}

    # ── API: stop water (emergency / manual override) ───────────────────────
    @app.post("/api/water/stop")
    async def api_water_stop():
        """Force the valve closed immediately. Interrupts any in-flight pulse
        because the pulse() method's relay.off() at the end is idempotent —
        the relay is already open by the time the sleep finishes."""
        log.info("Manual water STOP requested")
        hardware.valve.off()
        return {"pump_on": False}

    # ── API: manual light ─────────────────────────────────────────────────────

    @app.post("/api/light")
    async def api_light(req: LightRequest):
        """Set the grow light on or off.

        Defensively re-asserts the valve OFF immediately afterwards: the
        light relay's coil inrush briefly dips the shared 5V rail and has
        been observed to spuriously latch the valve relay closed on this
        HAT (same crosstalk the control loop already guards against during
        its capture step).
        """
        log.info("Manual light: %s", "ON" if req.on else "OFF")
        hardware.light.set(req.on)
        hardware.valve.off()
        return {"light_on": req.on}

    # ── API: trigger a cycle now ──────────────────────────────────────────────

    @app.post("/api/cycle")
    async def api_cycle():
        """Run the full decision pipeline immediately (async, non-blocking)."""
        if control_loop.cycle_state.get("stage") != "idle":
            log.info("Manual cycle trigger ignored — already running")
            raise HTTPException(status_code=409, detail="cycle already running")
        log.info("Manual cycle trigger via API")
        asyncio.get_event_loop().run_in_executor(None, control_loop.run_cycle)
        return {"status": "cycle started"}

    # ── API: config read/write ────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        """Return the current editable configuration."""
        plant = cfg.get("plant", {})
        soil = cfg.get("soil", {})
        temp = cfg.get("temperature", {})
        water = cfg.get("watering", {})
        light = cfg.get("light", {})
        return {
            "loop_interval_minutes": cfg.get("loop_interval_minutes", 60),
            "plant": {
                "name": plant.get("name", ""),
                "species": plant.get("species", ""),
                "species_notes": plant.get("species_notes", ""),
            },
            "soil": {
                "dry_pct": _adc_to_pct(
                    soil.get("dry_threshold", 700),
                    int(soil.get("cal_dry", 1023)),
                    int(soil.get("cal_wet", 0)),
                ),
                "wet_pct": _adc_to_pct(
                    soil.get("wet_threshold", 400),
                    int(soil.get("cal_dry", 1023)),
                    int(soil.get("cal_wet", 0)),
                ),
            },
            "temperature": {
                "too_cold_celsius": temp.get("too_cold_celsius", 15.0),
                "too_hot_celsius":  temp.get("too_hot_celsius", 32.0),
            },
            "watering": {
                "max_pulse_seconds":    water.get("max_pulse_seconds", 8),
                "min_interval_minutes": water.get("min_interval_minutes", 90),
            },
            "light": {
                "max_on_hours_per_day": light.get("max_on_hours_per_day", 16),
            },
        }

    @app.post("/api/config")
    async def post_config(req: ConfigUpdateRequest) -> dict[str, Any]:
        """Update editable config values in memory and write back to disk."""
        if req.loop_interval_minutes is not None:
            cfg["loop_interval_minutes"] = req.loop_interval_minutes
            control_loop._interval = req.loop_interval_minutes * 60
            log.info("Config: loop_interval_minutes → %d", req.loop_interval_minutes)

        if req.plant is not None:
            plant = cfg.setdefault("plant", {})
            if req.plant.name is not None:
                plant["name"] = req.plant.name
            if req.plant.species is not None:
                plant["species"] = req.plant.species
            if req.plant.species_notes is not None:
                plant["species_notes"] = req.plant.species_notes
            log.info("Config: plant updated")

        if req.soil is not None:
            soil = cfg.setdefault("soil", {})
            _cal_dry = int(soil.get("cal_dry", 1023))
            _cal_wet = int(soil.get("cal_wet", 0))
            if req.soil.dry_pct is not None:
                soil["dry_threshold"] = _pct_to_adc(req.soil.dry_pct, _cal_dry, _cal_wet)
            if req.soil.wet_pct is not None:
                soil["wet_threshold"] = _pct_to_adc(req.soil.wet_pct, _cal_dry, _cal_wet)
            log.info("Config: soil thresholds updated")

        if req.temperature is not None:
            t = cfg.setdefault("temperature", {})
            if req.temperature.too_cold_celsius is not None:
                t["too_cold_celsius"] = req.temperature.too_cold_celsius
            if req.temperature.too_hot_celsius is not None:
                t["too_hot_celsius"] = req.temperature.too_hot_celsius
            log.info("Config: temperature thresholds updated")

        if req.watering is not None:
            w = cfg.setdefault("watering", {})
            if req.watering.max_pulse_seconds is not None:
                w["max_pulse_seconds"] = req.watering.max_pulse_seconds
            if req.watering.min_interval_minutes is not None:
                w["min_interval_minutes"] = req.watering.min_interval_minutes
            log.info("Config: watering limits updated")

        if req.light is not None:
            l = cfg.setdefault("light", {})
            if req.light.max_on_hours_per_day is not None:
                l["max_on_hours_per_day"] = req.light.max_on_hours_per_day
            log.info("Config: light limits updated")

        config_path = cfg.get("_config_path")
        if config_path:
            to_write = {k: v for k, v in cfg.items() if not k.startswith("_")}
            with open(config_path, "w") as f:
                yaml.dump(to_write, f, default_flow_style=False, allow_unicode=True,
                          sort_keys=False)

        return {"ok": True}

    # ── API: live sensor read ─────────────────────────────────────────────────

    @app.get("/api/sensors")
    async def api_sensors() -> dict[str, Any]:
        """Read soil and temperature sensors right now (not from the last cycle).

        Runs the blocking hardware calls in a thread pool so the event loop
        stays unblocked. Returns quickly — SPI read ~1 ms, 1-Wire ~1 s.
        """
        loop = asyncio.get_event_loop()

        def _read():
            soil_reading = hardware.soil.read() if getattr(hardware.soil, "available", True) else None
            temp_reading = hardware.temperature.read()
            return soil_reading, temp_reading

        try:
            soil_r, temp_r = await loop.run_in_executor(None, _read)
        except Exception as exc:
            log.warning("api_sensors: read error — %s", exc)
            raise HTTPException(status_code=500, detail=str(exc))

        soil_cfg = cfg.get("soil", {})
        return {
            "soil_available": getattr(hardware.soil, "available", True),
            "soil_pct":       soil_r.moisture_pct if soil_r else None,
            "soil_raw":       soil_r.raw if soil_r else None,
            "temp_celsius":   round(temp_r.celsius, 2) if temp_r else None,
            "cal_dry":        int(soil_cfg.get("cal_dry", 1023)),
            "cal_wet":        int(soil_cfg.get("cal_wet", 0)),
        }

    # ── API: prompt read/write ────────────────────────────────────────────────

    @app.get("/api/prompts")
    async def get_prompts() -> dict[str, Any]:
        """Return the active prompts and their factory defaults."""
        from plant.ai.prompts import (
            get_vision_prompt, get_reasoning_system,
            VISION_PROMPT_DEFAULT, REASONING_SYSTEM_DEFAULT,
        )
        return {
            "vision":                   get_vision_prompt(cfg),
            "reasoning_system":         get_reasoning_system(cfg),
            "vision_default":           VISION_PROMPT_DEFAULT,
            "reasoning_system_default": REASONING_SYSTEM_DEFAULT,
        }

    @app.post("/api/prompts")
    async def post_prompts(req: PromptsUpdateRequest) -> dict[str, Any]:
        """Update one or both prompts in memory and write back to config.yaml.

        Pass an empty string to reset a prompt to its built-in default.
        """
        ai = cfg.setdefault("ai", {})
        prompts = ai.setdefault("prompts", {})
        if req.vision is not None:
            # Empty string → delete override so the default kicks back in
            prompts["vision"] = req.vision or None
            log.info("Config: vision prompt %s",
                     "reset to default" if not req.vision else "updated")
        if req.reasoning_system is not None:
            prompts["reasoning_system"] = req.reasoning_system or None
            log.info("Config: reasoning system prompt %s",
                     "reset to default" if not req.reasoning_system else "updated")

        config_path = cfg.get("_config_path")
        if config_path:
            to_write = {k: v for k, v in cfg.items() if not k.startswith("_")}
            with open(config_path, "w") as f:
                yaml.dump(to_write, f, default_flow_style=False, allow_unicode=True,
                          sort_keys=False)
        return {"ok": True}

    return app


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cycle_to_dict(c) -> dict[str, Any]:
    """Serialize a Cycle dataclass to a JSON-friendly dict."""
    photo_url = None
    if c.photo_path:
        photo_url = "/photos/" + Path(c.photo_path).name
    return {
        "id": c.id,
        "ts": c.ts,
        "soil_raw": c.soil_raw,
        "soil_pct": c.soil_pct,
        "temp_celsius": c.temp_celsius,
        "photo_url": photo_url,
        "plant_description": c.plant_description,
        "ai_water": c.ai_water,
        "ai_water_seconds": c.ai_water_seconds,
        "ai_light_on": c.ai_light_on,
        "ai_reasoning": c.ai_reasoning,
        "used_fallback": c.used_fallback,
        "final_water": c.final_water,
        "final_water_secs": c.final_water_secs,
        "final_light_on": c.final_light_on,
        "vision_ms": c.vision_ms,
        "reasoning_ms": c.reasoning_ms,
    }
