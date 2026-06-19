"""Simulation (mock) hardware drivers.

Realistic synthetic behaviour so the full pipeline can be developed and
tested on the Mac without any physical hardware.

Soil moisture model
───────────────────
The raw ADC value starts at ``sim_initial`` and rises by ``sim_dry_rate``
each hour (soil drying out).  A watering pulse drops it by ``sim_wet_effect``
(negative value in config, so the ADC goes down = wetter).
Values are clamped to [0, 1023].

Temperature model
─────────────────
Starts at ``sim_initial_celsius`` and random-walks ± ``sim_variance_celsius``
each cycle, clamped to a plausible [5, 40] °C range.

Camera
──────
Writes a small PNG placeholder image so the rest of the pipeline (VLM,
dashboard) has a real file to work with.

Valve / Light
─────────────
Log every action; track light state in memory.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path

from plant.hardware.interfaces import (
    SoilReading,
    TemperatureReading,
    Hardware,
)

log = logging.getLogger(__name__)


class MockSoilSensor:
    def __init__(self, cfg: dict) -> None:
        soil_cfg = cfg["soil"]
        self._raw = float(soil_cfg["sim_initial"])
        self._dry_rate = float(soil_cfg["sim_dry_rate"])
        self._wet_effect = float(soil_cfg["sim_wet_effect"])

    def read(self) -> SoilReading:
        # Soil dries a little each read (approximates hourly decay)
        self._raw = min(1023.0, self._raw + self._dry_rate)
        raw_int = int(round(self._raw))
        moisture_pct = round((1023 - raw_int) / 1023 * 100, 1)
        log.debug("MockSoilSensor: raw=%d moisture=%.1f%%", raw_int, moisture_pct)
        return SoilReading(raw=raw_int, moisture_pct=moisture_pct)

    def simulate_watering(self) -> None:
        """Called by MockValveActuator after a pulse to update soil state."""
        self._raw = max(0.0, self._raw + self._wet_effect)
        log.debug("MockSoilSensor: watered → raw now %.0f", self._raw)


class MockTemperatureSensor:
    def __init__(self, cfg: dict) -> None:
        temp_cfg = cfg["temperature"]
        self._celsius = float(temp_cfg["sim_initial_celsius"])
        self._variance = float(temp_cfg["sim_variance_celsius"])

    def read(self) -> TemperatureReading:
        delta = random.uniform(-self._variance, self._variance)
        self._celsius = max(5.0, min(40.0, self._celsius + delta))
        log.debug("MockTemperatureSensor: %.2f °C", self._celsius)
        return TemperatureReading(celsius=round(self._celsius, 2))


class MockCamera:
    """Writes a tiny placeholder PNG so downstream code has a real file."""

    def capture(self, path: str) -> str:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Minimal valid 1×1 white PNG (67 bytes) — no Pillow needed.
        PNG_1x1_WHITE = bytes([
            0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,  # PNG signature
            0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
            0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41,  # IDAT chunk
            0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
            0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc,
            0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,  # IEND chunk
            0x44, 0xae, 0x42, 0x60, 0x82,
        ])
        dest.write_bytes(PNG_1x1_WHITE)
        log.info("MockCamera: placeholder photo → %s", dest)
        return str(dest)

    def snapshot(self) -> bytes:
        """Return the same 1×1 placeholder as raw bytes (simulation mode
        has no real camera — dashboard live view shows the placeholder)."""
        # The placeholder is a PNG, not a JPEG, but browsers don't care
        # about the Content-Type vs actual bytes mismatch for display.
        PNG_1x1_WHITE = bytes([
            0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
            0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
            0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
            0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
            0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41,
            0x54, 0x08, 0xd7, 0x63, 0xf8, 0xcf, 0xc0, 0x00,
            0x00, 0x00, 0x02, 0x00, 0x01, 0xe2, 0x21, 0xbc,
            0x33, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
            0x44, 0xae, 0x42, 0x60, 0x82,
        ])
        return PNG_1x1_WHITE


class MockValveActuator:
    def __init__(self, soil_sensor: MockSoilSensor | None = None) -> None:
        self._soil = soil_sensor
        self._open = False

    def pulse(self, seconds: float) -> None:
        self._open = True
        log.info("MockValve: OPEN for %.1fs … CLOSE", seconds)
        time.sleep(min(seconds, 0.05))   # don't actually wait in simulation
        if self._soil is not None:
            self._soil.simulate_watering()
        self._open = False

    def off(self) -> None:
        """Force the valve closed."""
        self._open = False

    def is_open(self) -> bool:
        return self._open


class MockLightActuator:
    def __init__(self) -> None:
        self._on = False

    def set(self, on: bool) -> None:
        if on != self._on:
            log.info("MockLight: %s", "ON" if on else "OFF")
        self._on = on

    def is_on(self) -> bool:
        return self._on


# ── Factory ───────────────────────────────────────────────────────────────────

def build_mock_hardware(cfg: dict) -> Hardware:
    """Return a fully-wired Hardware bundle using simulation drivers."""
    soil = MockSoilSensor(cfg)
    temp = MockTemperatureSensor(cfg)
    camera = MockCamera()
    valve = MockValveActuator(soil_sensor=soil)
    light = MockLightActuator()
    return Hardware(soil=soil, temperature=temp, camera=camera,
                    valve=valve, light=light)
