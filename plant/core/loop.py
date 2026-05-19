"""Control loop — hourly decision pipeline.

Full implementation: Phase 5.  This stub lets main.py start up cleanly.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)


class ControlLoop:
    def __init__(self, cfg: dict, hardware) -> None:
        self.cfg = cfg
        self.hardware = hardware
        self._interval = cfg.get("loop_interval_minutes", 60) * 60

    async def run_forever(self) -> None:
        log.info("ControlLoop: starting (interval=%ds) — full pipeline TBD in Phase 5",
                 self._interval)
        while True:
            await asyncio.sleep(self._interval)
