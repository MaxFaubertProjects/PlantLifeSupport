"""Live video feeds for the dashboard (Pi camera + thermal camera).

Each camera gets one :class:`LiveFeed`: a single background thread that
produces frames and shares the latest one with every connected viewer.

The feed only runs while all of these hold, and re-checks them every frame:
  * at least one viewer is connected (the HTTP stream is open),
  * the control loop is idle — a cycle needs the camera, the RAM and the CPU
    for the vision and reasoning models, so streaming stops as soon as a cycle
    starts (stage "starting") and only resumes once it is back to "idle",
  * nothing has suspended it (e.g. thermal calibration needs the device).

The producer is a ``frames(keep_going)`` iterator supplied by the caller,
typically a camera driver's own streaming generator, which holds the driver's
lock while it runs and must stop promptly once ``keep_going()`` is False.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import Any, AsyncIterator, Callable, Iterator, Optional

log = logging.getLogger(__name__)

FrameSource = Callable[[Callable[[], bool]], Iterator[Any]]


class LiveFeed:
    def __init__(self, name: str, source: FrameSource, is_idle: Callable[[], bool]) -> None:
        self.name = name
        self._source = source
        self._is_idle = is_idle
        self._cond = threading.Condition()
        self._viewers = 0
        self._suspended = 0
        self._thread: Optional[threading.Thread] = None
        self.latest: Any = None
        self.latest_ts = 0.0
        self.version = 0

    # -- state ----------------------------------------------------------------

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def viewers(self) -> int:
        return self._viewers

    def fresh(self, max_age: float = 2.0) -> Any:
        """The latest frame if the feed is running and it is recent, else None."""
        if self.running and self.latest is not None and time.time() - self.latest_ts < max_age:
            return self.latest
        return None

    def _keep_going(self) -> bool:
        return self._viewers > 0 and self._suspended == 0 and self._is_idle()

    # -- producer thread --------------------------------------------------------

    def _ensure_running(self) -> None:
        with self._cond:
            if self.running or not self._keep_going():
                return
            self._thread = threading.Thread(target=self._run, name=f"live-{self.name}", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        log.info("Live %s: started", self.name)
        frames = None
        try:
            frames = self._source(self._keep_going)
            for item in frames:
                with self._cond:
                    self.latest, self.latest_ts = item, time.time()
                    self.version += 1
                    self._cond.notify_all()
                if not self._keep_going():
                    break
        except Exception as exc:  # camera unplugged, device busy, …
            log.warning("Live %s: stopped with error — %s", self.name, exc)
        finally:
            if frames is not None and hasattr(frames, "close"):
                frames.close()          # runs the driver's cleanup (closes the device)
            with self._cond:
                self.version += 1
                self._cond.notify_all()
            log.info("Live %s: stopped", self.name)

    # -- viewers ----------------------------------------------------------------

    async def frames(self, is_disconnected: Callable[[], Any], max_fps: float = 8.0) -> AsyncIterator[Any]:
        """Yield frames for one viewer until it disconnects or the feed stops."""
        with self._cond:
            self._viewers += 1
        try:
            self._ensure_running()
            # Give the producer a moment to open the device and send a first frame.
            deadline = time.time() + 8.0
            seen = -1
            while True:
                if await is_disconnected():
                    break
                if not self.running:
                    if time.time() > deadline or not self._keep_going():
                        break
                    self._ensure_running()
                if self.version != seen and self.latest is not None and self.running:
                    seen = self.version
                    deadline = time.time() + 8.0
                    yield self.latest
                await asyncio.sleep(1.0 / max_fps)
        finally:
            with self._cond:
                self._viewers -= 1

    # -- exclusive access ---------------------------------------------------------

    @contextlib.contextmanager
    def suspended(self, timeout: float = 6.0):
        """Stop the feed (and wait for it to release the device) for the duration."""
        with self._cond:
            self._suspended += 1
        t = self._thread
        if t is not None:
            t.join(timeout)
        try:
            yield
        finally:
            with self._cond:
                self._suspended -= 1


def polling_source(grab: Callable[[], Any], fps: float) -> FrameSource:
    """Fallback producer for drivers without a native stream: call ``grab``
    at ``fps`` (used for the simulation mocks)."""
    period = 1.0 / max(0.5, fps)

    def frames(keep_going: Callable[[], bool]) -> Iterator[Any]:
        while keep_going():
            t0 = time.time()
            yield grab()
            time.sleep(max(0.0, period - (time.time() - t0)))

    return frames
