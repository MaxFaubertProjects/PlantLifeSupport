"""Abstract hardware interfaces.

Every driver — mock or real — must implement these protocols.
The core loop only ever talks to these abstractions, so swapping
simulation for real hardware requires zero changes outside factory.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


# ── Sensor readings ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SoilReading:
    """Raw ADC value from the MCP3008 (0–1023, 10-bit).

    Higher value = drier soil (resistive probe convention).
    Converted to a 0–100 % moisture percentage for display:
        moisture_pct = round((1023 - raw) / 1023 * 100)
    """
    raw: int          # 0–1023 ADC counts
    moisture_pct: float  # 0 = bone dry, 100 = saturated


@dataclass(frozen=True)
class TemperatureReading:
    celsius: float


# ── Protocols ─────────────────────────────────────────────────────────────────

@runtime_checkable
class SoilSensor(Protocol):
    """Reads soil moisture from the MCP3008 ADC over SPI.

    ``read()`` may return ``None`` when no soil sensor is installed.
    Every caller must treat soil moisture as optional — the system runs
    fine without it, just with less information for the AI to reason on.
    """

    def read(self) -> SoilReading | None:
        """Return the current soil moisture reading, or None if unavailable."""
        ...


@runtime_checkable
class TemperatureSensor(Protocol):
    """Reads air/soil temperature from the DS18B20 over 1-Wire."""

    def read(self) -> TemperatureReading:
        """Return the current temperature reading."""
        ...


@runtime_checkable
class Camera(Protocol):
    """Captures a photo of the plant."""

    def capture(self, path: str) -> str:
        """Capture a photo, save it to *path*, and return the saved path."""
        ...

    def snapshot(self) -> bytes:
        """Return a fresh JPEG (bytes) for the dashboard's live view.

        Lower-resolution / lower-quality than :meth:`capture` to keep
        repeated polling cheap, and does NOT trigger the grow-light flash
        — that's reserved for the cycle's official capture only.
        """
        ...


@runtime_checkable
class ValveActuator(Protocol):
    """Controls the solenoid valve via Relay 1."""

    def pulse(self, seconds: float) -> None:
        """Open the valve for *seconds*, then close it."""
        ...

    def off(self) -> None:
        """Force the valve closed (defensive — called at startup and around
        operations that energize other relays on the same HAT to ensure CH1
        is never accidentally left/triggered on)."""
        ...

    def is_open(self) -> bool:
        """Return True if the valve is currently open (water flowing)."""
        ...


@runtime_checkable
class LightActuator(Protocol):
    """Controls the grow light via Relay 2."""

    def set(self, on: bool) -> None:
        """Turn the light on (True) or off (False)."""
        ...

    def is_on(self) -> bool:
        """Return current light state."""
        ...


# ── Convenience bundle ────────────────────────────────────────────────────────

@dataclass
class Hardware:
    """All hardware drivers bundled together.

    The factory returns one of these; the core loop consumes it.
    """
    soil: SoilSensor
    temperature: TemperatureSensor
    camera: Camera
    valve: ValveActuator
    light: LightActuator
