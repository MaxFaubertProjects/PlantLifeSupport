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
    """Captures a JPEG using picamera2 (OV5647 via CSI)."""

    def __init__(self) -> None:
        if not _PI_LIBS_AVAILABLE:
            raise RuntimeError("picamera2 not available — are you on the Pi?")
        self._cam = Picamera2()
        config = self._cam.create_still_configuration()
        self._cam.configure(config)
        self._cam.start()
        time.sleep(2)   # let auto-exposure settle

    def capture(self, path: str) -> str:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self._cam.capture_file(str(dest))
        log.info("RealCamera: captured → %s", dest)
        return str(dest)

    def close(self) -> None:
        self._cam.stop()
        self._cam.close()


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
