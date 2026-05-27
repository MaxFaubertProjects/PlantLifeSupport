"""Real hardware drivers for the Raspberry Pi.

Requires Pi-only packages (spidev, gpiozero, picamera2, w1thermsensor).
This module is intentionally NOT imported on the Mac — factory.py handles
the conditional import so the rest of the codebase stays platform-agnostic.

Wiring recap (see docs/WIRING.md for the full pin table):
  Soil (MCP3008) : SPI0 — GPIO8/9/10/11, CH0 = soil AOUT, 3.3V power
  Temperature    : GPIO4 (1-Wire), 4.7kΩ pull-up to 3.3V
  Valve          : GPIO <valve_gpio> → Relay 1 coil
  Light          : GPIO <light_gpio> → Relay 2 coil
  Camera         : CSI ribbon
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from plant.hardware.interfaces import (
    SoilReading,
    TemperatureReading,
    Hardware,
)

log = logging.getLogger(__name__)

# These imports will fail on the Mac — that's intentional.
try:
    import spidev                          # type: ignore[import]
    from gpiozero import OutputDevice      # type: ignore[import]
    from picamera2 import Picamera2        # type: ignore[import]
    from w1thermsensor import W1ThermSensor, Unit  # type: ignore[import]
    _PI_LIBS_AVAILABLE = True
except ImportError:
    _PI_LIBS_AVAILABLE = False


class RealSoilSensor:
    """Reads the MCP3008 channel 0 over SPI0."""

    _SPI_BUS = 0
    _SPI_DEVICE = 0   # CE0 → GPIO8
    _CHANNEL = 0      # CH0 on the MCP3008

    def __init__(self) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("spidev not available — are you on the Pi?")
        self._spi = spidev.SpiDev()
        self._spi.open(self._SPI_BUS, self._SPI_DEVICE)
        self._spi.max_speed_hz = 1_350_000

    def _read_adc(self, channel: int) -> int:
        """Read a 10-bit value from the MCP3008."""
        assert 0 <= channel <= 7
        r = self._spi.xfer2([1, (8 + channel) << 4, 0])
        return ((r[1] & 3) << 8) | r[2]

    def read(self) -> SoilReading:
        raw = self._read_adc(self._CHANNEL)
        moisture_pct = round((1023 - raw) / 1023 * 100, 1)
        log.debug("RealSoilSensor: raw=%d moisture=%.1f%%", raw, moisture_pct)
        return SoilReading(raw=raw, moisture_pct=moisture_pct)

    def close(self) -> None:
        self._spi.close()


class NullSoilSensor:
    """Placeholder for when no soil moisture sensor is installed.

    Returns ``None`` from :meth:`read` so the rest of the pipeline reports
    soil moisture as *unavailable* rather than fabricating a value. This is
    the honest stand-in until the MCP3008 ADC + resistive probe are wired.

    Needs no Pi-only libraries — it is pure Python.
    """

    available = False

    def read(self) -> SoilReading | None:
        return None


class RealTemperatureSensor:
    """Reads the DS18B20 on the 1-Wire bus (GPIO4)."""

    def __init__(self) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("w1thermsensor not available — are you on the Pi?")
        self._sensor = W1ThermSensor()

    def read(self) -> TemperatureReading:
        celsius = self._sensor.get_temperature(Unit.DEGREES_C)
        log.debug("RealTemperatureSensor: %.2f °C", celsius)
        return TemperatureReading(celsius=round(celsius, 2))


class RealCamera:
    """Captures a JPEG using picamera2 (OV5647 via CSI).

    The camera is opened only for the duration of a capture and closed
    immediately afterwards. Keeping its framebuffers out of RAM while the
    AI models run matters a lot on the memory-constrained 8 GB Pi — a cycle
    runs hourly, so the few seconds of open/settle cost is irrelevant.

    Two access paths share the camera:
      - :meth:`capture`  — full-res still saved to disk for the AI cycle
      - :meth:`snapshot` — lower-res in-memory JPEG for the dashboard's
                           periodic live view
    Both serialize on ``_lock`` so they never collide on the camera device.
    """

    # The OV5647 full sensor is 2592x1944 (5 MP) — far more than the vision
    # model needs, and the framebuffers cost real memory. 1920x1080 is plenty
    # for both moondream and the dashboard photo.
    _STILL_SIZE = (1920, 1080)
    # Smaller + faster-settling config for dashboard snapshots. Dashboard
    # polls every few seconds, so keep this cheap.
    _SNAPSHOT_SIZE = (1280, 720)

    def __init__(self) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("picamera2 not available — are you on the Pi?")
        # Serializes camera access between the cycle's capture() and the
        # dashboard's snapshot() polling — Picamera2 cannot be opened twice.
        import threading
        self._lock = threading.Lock()

    def _grab(self, size, settle_seconds: float):
        """Open camera, grab one frame as RGB numpy array, close. Caller
        must hold ``self._lock``."""
        cam = Picamera2()
        try:
            # Picamera2's format naming is counter-intuitive: "RGB888" actually
            # delivers the numpy array in BGR byte order (the name describes
            # the packed pixel layout, not memory order). We pick RGB888 here
            # for compatibility with all picamera2 versions, then swap channels
            # ourselves so the caller always gets a true RGB array.
            config = cam.create_still_configuration(
                main={"size": size, "format": "RGB888"}
            )
            cam.configure(config)
            cam.start()
            time.sleep(settle_seconds)   # let auto-exposure settle
            arr = cam.capture_array("main")
        finally:
            cam.stop()
            cam.close()
        # BGR → RGB (take first 3 channels in reverse). Guards against a
        # 4-channel RGBA frame just in case the sensor mode changes.
        if arr.ndim == 3 and arr.shape[-1] >= 3:
            arr = arr[..., 2::-1]
        return arr

    def capture(self, path: str) -> str:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        from PIL import Image
        with self._lock:
            arr = self._grab(self._STILL_SIZE, settle_seconds=2)
        Image.fromarray(arr).save(str(dest), "JPEG", quality=90)
        log.info("RealCamera: captured → %s", dest)
        return str(dest)

    def snapshot(self) -> bytes:
        """Fresh dashboard JPEG. Smaller + faster + lower quality than capture()."""
        from PIL import Image
        from io import BytesIO
        with self._lock:
            arr = self._grab(self._SNAPSHOT_SIZE, settle_seconds=1.0)
        buf = BytesIO()
        Image.fromarray(arr).save(buf, "JPEG", quality=65)
        return buf.getvalue()

    def close(self) -> None:
        # Nothing persistent to release — the camera is closed after each capture.
        pass


class RealValveActuator:
    """Pulses Relay 1 (solenoid valve) via gpiozero."""

    def __init__(self, gpio_pin: int) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("gpiozero not available — are you on the Pi?")
        # active_high=True: driving the pin HIGH energises the relay coil
        self._relay = OutputDevice(gpio_pin, active_high=True, initial_value=False)

    def pulse(self, seconds: float) -> None:
        log.info("RealValve: OPEN for %.1fs", seconds)
        self._relay.on()
        time.sleep(seconds)
        self._relay.off()
        log.info("RealValve: CLOSED")

    def off(self) -> None:
        """Force the relay open. Idempotent — safe to call any time. Reads
        live GPIO state via :meth:`is_open` so a stop request mid-pulse will
        immediately open the valve even though the pulse() sleep is still
        running in another thread."""
        self._relay.off()

    def is_open(self) -> bool:
        """True if the relay is currently energized (water flowing)."""
        return bool(self._relay.value)


class RealLightActuator:
    """Controls Relay 2 (grow light) via gpiozero."""

    def __init__(self, gpio_pin: int) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("gpiozero not available — are you on the Pi?")
        self._relay = OutputDevice(gpio_pin, active_high=True, initial_value=False)

    def set(self, on: bool) -> None:
        if on:
            self._relay.on()
        else:
            self._relay.off()
        log.info("RealLight: %s", "ON" if on else "OFF")

    def is_on(self) -> bool:
        return bool(self._relay.value)


class UhubctlLightActuator:
    """Controls grow light by switching all USB hub power via uhubctl.

    Temporary solution until the relay HAT arrives. Requires:
      - uhubctl installed (sudo apt install uhubctl)
      - Grow light plugged into any USB port
      - Light's button bypassed so it auto-on when powered
    """

    _HUBS = ["1", "3"]   # both xHCI controllers on Pi 5
    # uhubctl installs to /usr/sbin, which is not on PATH for non-login shells
    # or systemd services — so we resolve it by absolute path, not via PATH.
    _SEARCH_PATHS = (
        "/usr/sbin/uhubctl",
        "/usr/bin/uhubctl",
        "/usr/local/bin/uhubctl",
    )

    def __init__(self) -> None:
        self._bin = self._find_uhubctl()
        if self._bin is None:
            raise RuntimeError("uhubctl not found — run: sudo apt install uhubctl")
        self._on = False

    @classmethod
    def _find_uhubctl(cls) -> str | None:
        """Locate the uhubctl binary, checking PATH then known install dirs."""
        import os
        import shutil
        found = shutil.which("uhubctl")
        if found:
            return found
        for path in cls._SEARCH_PATHS:
            if os.path.exists(path):
                return path
        return None

    def _run(self, action: str) -> None:
        import subprocess
        for hub in self._HUBS:
            subprocess.run(
                ["sudo", self._bin, "-l", hub, "-a", action],
                capture_output=True,
            )

    def set(self, on: bool) -> None:
        self._run("on" if on else "off")
        self._on = on
        log.info("UhubctlLight: %s", "ON" if on else "OFF")

    def is_on(self) -> bool:
        return self._on


# ── Factory ───────────────────────────────────────────────────────────────────

def build_real_hardware(cfg: dict) -> Hardware:
    """Return a fully-wired Hardware bundle using real Pi drivers."""
    if not _PI_LIBS_AVAILABLE:
        raise RuntimeError(
            "Real hardware drivers require Pi-only packages. "
            "Set mode: simulation in config.yaml to run on the Mac."
        )
    gpio = cfg["gpio"]
    soil = RealSoilSensor()
    temp = RealTemperatureSensor()
    camera = RealCamera()
    valve = RealValveActuator(gpio_pin=gpio["valve_gpio"])
    light = RealLightActuator(gpio_pin=gpio["light_gpio"])
    return Hardware(soil=soil, temperature=temp, camera=camera,
                    valve=valve, light=light)
