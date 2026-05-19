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
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# Request models must live at module level so FastAPI can inspect them correctly
class WaterRequest(BaseModel):
    seconds: float = 3.0


class LightRequest(BaseModel):
    on: bool

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

    # ── API: status ───────────────────────────────────────────────────────────

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        """Return the latest cycle data and current hardware state."""
        db = control_loop.db
        latest = db.latest_cycle()
        light_on = hardware.light.is_on()

        return {
            "mode": cfg.get("mode", "simulation"),
            "cycle_count": db.cycle_count(),
            "light_on": light_on,
            "latest": _cycle_to_dict(latest) if latest else None,
        }

    # ── API: cycles (for charts + reasoning log) ──────────────────────────────

    @app.get("/api/cycles")
    async def api_cycles(limit: int = 48) -> list[dict[str, Any]]:
        """Return recent cycles, newest first, for charts and the reasoning log."""
        limit = max(1, min(limit, 200))
        db = control_loop.db
        return [_cycle_to_dict(c) for c in db.recent_cycles(limit=limit)]

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

    # ── API: manual light ─────────────────────────────────────────────────────

    @app.post("/api/light")
    async def api_light(req: LightRequest):
        """Set the grow light on or off."""
        log.info("Manual light: %s", "ON" if req.on else "OFF")
        hardware.light.set(req.on)
        return {"light_on": req.on}

    # ── API: trigger a cycle now ──────────────────────────────────────────────

    @app.post("/api/cycle")
    async def api_cycle():
        """Run the full decision pipeline immediately (async, non-blocking)."""
        log.info("Manual cycle trigger via API")
        asyncio.get_event_loop().run_in_executor(None, control_loop.run_cycle)
        return {"status": "cycle started"}

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
    }
