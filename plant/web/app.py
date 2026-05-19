"""FastAPI web application — dashboard + manual overrides.

Full implementation: Phase 6.  This stub returns a minimal health-check
so main.py boots and the server is reachable.
"""

from __future__ import annotations

from fastapi import FastAPI


def create_app(cfg: dict, hardware, control_loop) -> FastAPI:
    app = FastAPI(title="Plant Life Support", version="0.1.0")

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": cfg.get("mode")}

    return app
