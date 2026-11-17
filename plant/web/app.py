"""FastAPI web application — dashboard API + manual overrides.

Endpoints
─────────
GET  /                     → dashboard HTML
GET  /photos/{filename}    → serve plant photos
GET  /api/status           → latest cycle + current hardware state
GET  /api/cycles           → recent cycles for charts + reasoning log
GET  /api/stream.mjpg      → live camera video (MJPEG), only while the loop is idle
GET  /api/thermal/stream   → live thermal frames (server-sent events), same rule
GET  /api/live             → whether live video is possible right now
POST /api/water            → trigger a manual watering pulse
POST /api/light            → set light on/off
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from plant.core import schedule as sched
from plant.web.streams import LiveFeed, polling_source


# Request models must live at module level so FastAPI can inspect them correctly
class WaterRequest(BaseModel):
    seconds: float = 3.0


class LightRequest(BaseModel):
    on: bool


class LoginRequest(BaseModel):
    password: str


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


class AISettings(BaseModel):
    timeout_seconds: Optional[int] = None
    temperature: Optional[float] = None


class PromptsUpdateRequest(BaseModel):
    vision: Optional[str] = None
    reasoning_system: Optional[str] = None


class PhotoView(BaseModel):
    # How the dashboard frames the camera photo: zoom 1 shows the whole
    # frame; above 1 it crops around (x, y), percentages of the frame.
    zoom: float = 1.0
    x: float = 50.0
    y: float = 50.0


class ScheduleSettings(BaseModel):
    checks_per_day: Optional[int] = Field(None, ge=1, le=24)
    first_check: Optional[str] = Field(None, pattern=r"^\d{1,2}:\d{2}$")
    light_on_at: Optional[str] = Field(None, pattern=r"^\d{1,2}:\d{2}$")
    light_hours: Optional[float] = Field(None, ge=0, le=24)


class ConfigUpdateRequest(BaseModel):
    loop_interval_minutes: Optional[int] = None   # legacy: mapped to checks_per_day
    schedule: Optional[ScheduleSettings] = None
    plant: Optional[PlantConfigSection] = None
    soil: Optional[SoilRanges] = None
    temperature: Optional[TemperatureRanges] = None
    watering: Optional[WateringRanges] = None
    light: Optional[LightRanges] = None
    ai: Optional[AISettings] = None
    photo_view: Optional[PhotoView] = None


def _photo_view(cfg: dict) -> dict[str, float]:
    v = (cfg.get("web") or {}).get("photo_view") or {}
    clamp = lambda x, lo, hi, d: max(lo, min(hi, float(x if x is not None else d)))  # noqa: E731
    return {"zoom": clamp(v.get("zoom"), 1.0, 4.0, 1.0),
            "x": clamp(v.get("x"), 0.0, 100.0, 50.0),
            "y": clamp(v.get("y"), 0.0, 100.0, 50.0)}


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

    # ── Control auth ──────────────────────────────────────────────────────────
    # Mutating endpoints (water, light, cycle, config, prompts, clear history)
    # are gated by a shared password so the dashboard can be exposed publicly
    # while the plant's controls stay protected. Read endpoints stay open.
    #
    # The password comes from $PLANT_CONTROL_PASSWORD (preferred, kept out of
    # git) or web.control_password in config.yaml. If neither is set, controls
    # are left OPEN — so local/LAN use is unchanged until you opt in.
    _COOKIE = "plant_auth"
    _MAX_AGE = 30 * 86400  # 30 days

    def _control_password() -> Optional[str]:
        pw = os.environ.get("PLANT_CONTROL_PASSWORD")
        if not pw:
            pw = (cfg.get("web") or {}).get("control_password")
        return pw or None

    def _secret(password: str) -> bytes:
        # Signing key derived from the password, so issued cookies survive a
        # restart but are invalidated the moment the password changes.
        return hashlib.sha256(f"plantlife-v1:{password}".encode()).digest()

    def _make_token(password: str) -> str:
        msg = base64.urlsafe_b64encode(str(int(time.time())).encode()).decode()
        mac = hmac.new(_secret(password), msg.encode(), hashlib.sha256).hexdigest()
        return f"{msg}.{mac}"

    def _valid_token(password: str, token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        msg, _, mac = token.rpartition(".")
        expected = hmac.new(_secret(password), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return False
        try:
            issued = int(base64.urlsafe_b64decode(msg).decode())
        except Exception:
            return False
        return (time.time() - issued) <= _MAX_AGE

    def require_control(request: Request) -> None:
        """Dependency: allow the request only if controls are unlocked.

        No-op when no password is configured (controls open by design)."""
        password = _control_password()
        if not password:
            return
        if not _valid_token(password, request.cookies.get(_COOKIE)):
            raise HTTPException(status_code=401, detail="Login required to control the plant")

    @app.get("/api/auth")
    async def auth_status(request: Request) -> dict[str, Any]:
        password = _control_password()
        if not password:
            return {"required": False, "authed": True}
        return {"required": True,
                "authed": _valid_token(password, request.cookies.get(_COOKIE))}

    @app.post("/api/login")
    async def login(req: LoginRequest, request: Request):
        password = _control_password()
        if not password:
            # No password configured → controls are already open.
            return JSONResponse({"authed": True, "required": False})
        if not hmac.compare_digest(req.password, password):
            raise HTTPException(status_code=401, detail="Wrong password")
        resp = JSONResponse({"authed": True, "required": True})
        resp.set_cookie(
            _COOKIE, _make_token(password),
            max_age=_MAX_AGE, httponly=True, samesite="lax",
            secure=request.url.scheme == "https", path="/",
        )
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"authed": False})
        resp.delete_cookie(_COOKIE, path="/")
        return resp

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

    # ── Live video (only while the control loop is idle and someone watches) ──
    #
    # One shared producer per camera (see plant/web/streams.py). Streams end on
    # their own as soon as a cycle starts, so the vision/reasoning models get
    # the camera, RAM and CPU; the dashboard then falls back to stills. Both
    # streams are read-only, like the rest of the public dashboard.

    web_cfg = cfg.get("web", {}) or {}
    stream_fps = float(web_cfg.get("stream_fps", 4))
    thermal_fps = float(web_cfg.get("thermal_stream_fps", 4))

    stream_min_free_mb = float(web_cfg.get("stream_min_free_mb", 1200))

    def loop_idle() -> bool:
        return control_loop.cycle_state.get("stage") in (None, "idle")

    def _mem_available_mb() -> Optional[float]:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024
        except OSError:
            return None        # not Linux (simulation on a Mac): no guard
        return None

    def streaming_ok() -> bool:
        """Idle loop AND enough free RAM. After a cycle Ollama keeps the model
        loaded for a few minutes; don't add camera buffers on top of that."""
        if not loop_idle():
            return False
        free = _mem_available_mb()
        return free is None or free >= stream_min_free_mb

    _NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                 "Pragma": "no-cache", "X-Accel-Buffering": "no"}

    def _camera_source():
        cam = hardware.camera
        if hasattr(cam, "stream_jpegs"):
            return lambda keep_going: cam.stream_jpegs(keep_going, fps=stream_fps)
        if hasattr(cam, "snapshot"):
            return polling_source(cam.snapshot, stream_fps)
        return None

    def _thermal_source():
        cam = getattr(hardware, "thermal", None)
        if cam is None:
            return None
        if hasattr(cam, "stream"):
            return cam.stream
        return polling_source(lambda: cam.read(max_age=0), thermal_fps)

    _cam_src, _th_src = _camera_source(), _thermal_source()
    camera_feed = LiveFeed("camera", _cam_src, streaming_ok) if _cam_src else None
    thermal_feed = LiveFeed("thermal", _th_src, streaming_ok) if _th_src else None

    def _live_info() -> dict[str, Any]:
        th = getattr(hardware, "thermal", None)
        return {
            "idle": streaming_ok(),
            "loop_idle": loop_idle(),
            "mem_available_mb": _mem_available_mb(),
            "camera": camera_feed is not None and getattr(hardware.camera, "available", True),
            "thermal": thermal_feed is not None and bool(getattr(th, "available", False)),
            "camera_viewers": camera_feed.viewers if camera_feed else 0,
            "thermal_viewers": thermal_feed.viewers if thermal_feed else 0,
        }

    @app.get("/api/live")
    async def api_live() -> dict[str, Any]:
        return _live_info()

    @app.get("/api/stream.mjpg")
    async def api_stream(request: Request):
        """MJPEG live view of the plant camera (multipart/x-mixed-replace)."""
        if camera_feed is None:
            raise HTTPException(status_code=501, detail="live video not supported")
        if not streaming_ok():
            raise HTTPException(status_code=503, detail="live video paused (cycle running or the Pi is short of memory)")

        async def parts():
            async for jpeg in camera_feed.frames(request.is_disconnected, max_fps=stream_fps * 2):
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

        return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers=_NO_CACHE)

    @app.get("/api/thermal/stream")
    async def api_thermal_stream(request: Request):
        """Server-sent events: one /api/thermal-shaped JSON object per frame."""
        cam = getattr(hardware, "thermal", None)
        if thermal_feed is None or cam is None or not cam.available:
            raise HTTPException(status_code=503, detail="thermal camera not connected")
        if not streaming_ok():
            raise HTTPException(status_code=503, detail="live video paused (cycle running or the Pi is short of memory)")

        async def events():
            yield "retry: 15000\n\n"
            async for reading in thermal_feed.frames(request.is_disconnected, max_fps=thermal_fps * 2):
                yield "data: " + json.dumps({"available": True, "live": True, **reading.to_api()}) + "\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers=_NO_CACHE)

    # ── API: live snapshot ────────────────────────────────────────────────────

    @app.get("/api/snapshot.jpg")
    async def api_snapshot():
        """Fresh camera JPEG for the dashboard's still view.

        Lower resolution than the cycle's official capture, no grow-light
        flash. While the live feed is running its latest frame is returned
        (the camera can't be opened twice). Camera access is locked, so if a
        cycle is in its capture step this will wait a few seconds.
        """
        live = camera_feed.fresh() if camera_feed else None
        if live is not None:
            return Response(content=live, media_type="image/jpeg", headers=_NO_CACHE)
        snap = getattr(hardware.camera, "snapshot", None)
        if snap is None:
            raise HTTPException(status_code=501, detail="snapshot not supported")
        try:
            jpeg = await asyncio.get_event_loop().run_in_executor(None, snap)
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"snapshot failed: {exc}")
        return Response(content=jpeg, media_type="image/jpeg", headers=_NO_CACHE)

    # ── API: thermal camera ───────────────────────────────────────────────────

    @app.get("/api/thermal")
    async def api_thermal() -> dict[str, Any]:
        """Latest thermal frame as a false-colour PNG (data URL) + stats in °C.

        Frames are cached for a few seconds inside the driver, so several
        dashboards polling at once share one capture; while the live feed
        runs its latest frame is returned. Returns ``available: false``
        (HTTP 200) when the camera is unplugged so the dashboard can show a
        calm "not connected" state instead of an error.
        """
        cam = getattr(hardware, "thermal", None)
        if cam is None:
            return {"available": False, "reason": "disabled"}
        if not cam.available:
            return {"available": False, "reason": "not connected"}
        live = thermal_feed.fresh() if thermal_feed else None
        if live is not None:
            return {"available": True, **live.to_api()}
        try:
            reading = await asyncio.get_event_loop().run_in_executor(None, cam.read)
        except Exception as exc:
            log.warning("thermal read failed: %s", exc)
            return {"available": False, "reason": f"read failed: {exc}"}
        return {"available": True, **reading.to_api()}

    def _without_thermal_feed(fn):
        """Run fn with the live thermal feed stopped (it holds the device)."""
        if thermal_feed is None:
            return fn()
        with thermal_feed.suspended():
            return fn()

    @app.post("/api/thermal/calibrate")
    async def api_thermal_calibrate(_=Depends(require_control)) -> dict[str, Any]:
        """Store the flat-field reference. Point the camera at something of
        uniform temperature (a blank wall, a card held just in front of the
        lens) before calling — takes ~2 s."""
        cam = getattr(hardware, "thermal", None)
        if cam is None or not cam.available:
            raise HTTPException(status_code=503, detail="thermal camera not connected")
        try:
            info = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _without_thermal_feed(cam.calibrate))
        except Exception as exc:
            log.warning("thermal calibration failed: %s", exc)
            raise HTTPException(status_code=500, detail=f"calibration failed: {exc}")
        return {"ok": True, **info}

    @app.post("/api/thermal/rotate")
    async def api_thermal_rotate(_=Depends(require_control)) -> dict[str, Any]:
        """Turn the thermal image a further 90° clockwise (saved on the Pi)."""
        cam = getattr(hardware, "thermal", None)
        if cam is None or not hasattr(cam, "rotate"):
            raise HTTPException(status_code=503, detail="thermal camera not available")
        info = await asyncio.get_event_loop().run_in_executor(
            None, lambda: _without_thermal_feed(cam.rotate))
        return {"ok": True, **info}

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
            "thermal":     bool(getattr(getattr(hardware, "thermal", None), "available", False)),
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
            "loop_interval_minutes": sched.check_interval_minutes(cfg),
            "checks_per_day": sched.schedule_cfg(cfg)["checks_per_day"],
            "sensors": sensors,
            "live": _live_info(),
            "photo_view": _photo_view(cfg),
        }

    # ── API: cycles (for charts + reasoning log) ──────────────────────────────

    @app.get("/api/cycles")
    async def api_cycles(limit: int = 48) -> list[dict[str, Any]]:
        """Return recent cycles, newest first, for charts and the reasoning log."""
        limit = max(1, min(limit, 200))
        db = control_loop.db
        return [_cycle_to_dict(c) for c in db.recent_cycles(limit=limit)]

    # ── API: schedule between checks ──────────────────────────────────────────

    @app.get("/api/schedule")
    async def api_schedule() -> dict[str, Any]:
        """The plan from the last check until the next: actions + forecast."""
        plan = getattr(control_loop, "plan", None)
        if not plan:
            raise HTTPException(status_code=404, detail="No schedule yet — it is made at the next check")
        return {k: v for k, v in plan.items() if k != "ai_reasoning"}

    @app.get("/api/events")
    async def api_events(hours: float = 72) -> list[dict[str, Any]]:
        """Waterings, light switches and soil readings between checks, oldest first."""
        hours = max(1.0, min(hours, 24 * 14))
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        return control_loop.db.events_since(since)

    # ── API: timelapse frames ─────────────────────────────────────────────────

    @app.get("/api/timelapse")
    async def api_timelapse() -> list[dict[str, Any]]:
        """Return {ts, photo_url} for cycles that have photos, oldest first."""
        db = control_loop.db
        cycles = db.recent_cycles(limit=48)
        return [
            {"ts": c.ts, "photo_url": "/photos/" + Path(c.photo_path).name}
            for c in reversed(cycles)
            if c.photo_path
        ]

    # ── API: clear history (destructive!) ─────────────────────────────────────

    @app.delete("/api/cycles")
    async def api_cycles_clear(_=Depends(require_control)) -> dict[str, Any]:
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
    async def api_water(req: WaterRequest, _=Depends(require_control)):
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
    async def api_water_stop(_=Depends(require_control)):
        """Force the valve closed immediately. Interrupts any in-flight pulse
        because the pulse() method's relay.off() at the end is idempotent —
        the relay is already open by the time the sleep finishes."""
        log.info("Manual water STOP requested")
        hardware.valve.off()
        return {"pump_on": False}

    # ── API: manual light ─────────────────────────────────────────────────────

    @app.post("/api/light")
    async def api_light(req: LightRequest, _=Depends(require_control)):
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
    async def api_cycle(_=Depends(require_control)):
        """Run the full decision pipeline immediately (async, non-blocking)."""
        if control_loop.cycle_state.get("stage") != "idle":
            log.info("Manual cycle trigger ignored — already running")
            raise HTTPException(status_code=409, detail="cycle already running")
        log.info("Manual cycle trigger via API")
        asyncio.get_event_loop().run_in_executor(None, control_loop.run_cycle)
        return {"status": "cycle started"}

    # ── API: config read/write ────────────────────────────────────────────────

    @app.get("/api/config")
    async def get_config(_=Depends(require_control)) -> dict[str, Any]:
        """Return the current editable configuration (Settings — password-gated)."""
        plant = cfg.get("plant", {})
        soil = cfg.get("soil", {})
        temp = cfg.get("temperature", {})
        water = cfg.get("watering", {})
        light = cfg.get("light", {})
        return {
            "loop_interval_minutes": sched.check_interval_minutes(cfg),
            "schedule": sched.schedule_cfg(cfg),
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
            "ai": {
                "timeout_seconds": cfg.get("ai", {}).get("timeout_seconds", 1000),
                "temperature":     cfg.get("ai", {}).get("temperature", 0.2),
            },
            "photo_view": _photo_view(cfg),
        }

    @app.post("/api/config")
    async def post_config(req: ConfigUpdateRequest, _=Depends(require_control)) -> dict[str, Any]:
        """Update editable config values in memory and write back to disk."""
        replan = False
        if req.loop_interval_minutes is not None and req.schedule is None:
            n = max(1, min(24, round(24 * 60 / max(1, req.loop_interval_minutes))))
            cfg.setdefault("schedule", {})["checks_per_day"] = n
            replan = True
        if req.schedule is not None:
            sc = cfg.setdefault("schedule", {})
            for k, v in req.schedule.model_dump(exclude_none=True).items():
                sc[k] = v
            log.info("Config: schedule → %s", sched.schedule_cfg(cfg))
            replan = True

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
            replan = True

        if req.ai is not None:
            ai = cfg.setdefault("ai", {})
            if req.ai.timeout_seconds is not None:
                ai["timeout_seconds"] = req.ai.timeout_seconds
                log.info("Config: ai.timeout_seconds → %d", req.ai.timeout_seconds)
            if req.ai.temperature is not None:
                ai["temperature"] = req.ai.temperature
                log.info("Config: ai.temperature → %.2f", req.ai.temperature)

        if req.photo_view is not None:
            web = cfg.setdefault("web", {})
            web["photo_view"] = req.photo_view.model_dump()
            web["photo_view"] = _photo_view(cfg)
            log.info("Config: photo_view → %s", web["photo_view"])

        config_path = cfg.get("_config_path")
        if config_path:
            to_write = {k: v for k, v in cfg.items() if not k.startswith("_")}
            with open(config_path, "w") as f:
                yaml.dump(to_write, f, default_flow_style=False, allow_unicode=True,
                          sort_keys=False)

        if replan or req.soil is not None or req.watering is not None:
            # New check times / light window / limits → redo the plan until the
            # next check (no models, just the planner).
            control_loop.cycle_state["next_cycle_at"] = (
                sched.next_check(cfg).astimezone(timezone.utc).isoformat())
            if control_loop.cycle_state.get("stage") == "idle":
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: control_loop.replan(source="settings"))

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
    async def get_prompts(_=Depends(require_control)) -> dict[str, Any]:
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
    async def post_prompts(req: PromptsUpdateRequest, _=Depends(require_control)) -> dict[str, Any]:
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
        "warnings": c.warnings,
        "leaf_temp_c": getattr(c, "leaf_temp_c", None),
        "leaf_air_delta_c": getattr(c, "leaf_air_delta_c", None),
    }
