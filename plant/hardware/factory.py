"""Hardware factory.

Picks the right driver bundle based on config.yaml ``mode``.
All other code imports only from this module — never directly from
real.py or mock.py.
"""

from __future__ import annotations

import logging

from plant.hardware.interfaces import Hardware

log = logging.getLogger(__name__)


def get_hardware(cfg: dict) -> Hardware:
    """Return a Hardware bundle appropriate for the configured mode.

    Args:
        cfg: The top-level config dict loaded from config.yaml.

    Returns:
        A :class:`Hardware` instance ready to use.

    Raises:
        ValueError: If ``mode`` is not "simulation" or "hardware".
        RuntimeError: If "hardware" mode is requested on a non-Pi system.
    """
    mode = cfg.get("mode", "simulation")

    if mode == "simulation":
        from plant.hardware.mock import build_mock_hardware
        log.info("Hardware mode: SIMULATION (mock drivers)")
        return build_mock_hardware(cfg)

    if mode == "hardware":
        from plant.hardware.real import build_real_hardware
        log.info("Hardware mode: REAL (Pi drivers)")
        return build_real_hardware(cfg)

    if mode == "hybrid":
        from plant.hardware.mock import (
            MockSoilSensor, MockValveActuator, MockLightActuator,
        )
        from plant.hardware.real import RealCamera, RealTemperatureSensor
        log.info("Hardware mode: HYBRID (real camera+temp, mock soil/valve/light)")
        soil = MockSoilSensor(cfg)
        return Hardware(
            soil=soil,
            temperature=RealTemperatureSensor(),
            camera=RealCamera(),
            valve=MockValveActuator(soil_sensor=soil),
            light=MockLightActuator(),
        )

    raise ValueError(
        f"Unknown mode {mode!r} in config.yaml — must be 'simulation', 'hybrid', or 'hardware'."
    )
