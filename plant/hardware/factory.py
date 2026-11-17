"""Hardware factory.

Picks the right driver bundle based on config.yaml ``mode``.
All other code imports only from this module — never directly from
real.py or mock.py.
"""

from __future__ import annotations

import logging

from plant.hardware.interfaces import Hardware

log = logging.getLogger(__name__)


def _build_soil_sensor(cfg: dict):
    """Return the configured soil moisture sensor.

    Reads ``soil.sensor`` from config:
      - ``"mcp3008"`` → RealSoilSensor (MCP3008 ADC over SPI)
      - ``"none"``    → NullSoilSensor (no sensor wired — readings unavailable)
      - ``"mock"``    → MockSoilSensor (synthetic simulation data)
      - missing       → defaults to "mcp3008"
    """
    sensor = cfg.get("soil", {}).get("sensor", "mcp3008")

    if sensor == "none":
        from plant.hardware.real import NullSoilSensor
        log.info("Soil sensor: none (not installed — readings unavailable)")
        return NullSoilSensor()

    if sensor == "mock":
        from plant.hardware.mock import MockSoilSensor
        log.info("Soil sensor: mock (synthetic data)")
        return MockSoilSensor(cfg)

    from plant.hardware.real import RealSoilSensor
    log.info("Soil sensor: mcp3008 (SPI ADC)")
    return RealSoilSensor(cfg)


def _build_light_actuator(cfg: dict):
    """Return the configured light actuator.

    Reads ``light.control`` from config:
      - ``"uhubctl"``  → UhubctlLightActuator (temporary USB-hub solution)
      - ``"relay"``    → RealLightActuator via gpiozero (Waveshare HAT)
      - anything else / missing → RealLightActuator (default)
    """
    control = cfg.get("light", {}).get("control", "relay")
    gpio_pin = cfg.get("gpio", {}).get("light_gpio", 20)

    if control == "uhubctl":
        from plant.hardware.real import UhubctlLightActuator
        log.info("Light actuator: uhubctl (USB hub switching)")
        return UhubctlLightActuator()

    from plant.hardware.real import RealLightActuator
    log.info("Light actuator: relay (GPIO %d)", gpio_pin)
    return RealLightActuator(gpio_pin=gpio_pin)


def _attach_thermal(hw: Hardware, cfg: dict, mock: bool = False) -> Hardware:
    """Attach the optional USB thermal camera to *hw*.

    ``thermal.enabled`` in config: ``auto`` (default — attach on the Pi; the
    driver shows "not connected" until the camera is plugged in), ``true``,
    or ``false``. The DS18B20 air temperature is passed in as the
    calibration reference and for the leaf−air difference.
    """
    enabled = str((cfg.get("thermal", {}) or {}).get("enabled", "auto")).lower()
    if enabled in ("false", "no", "off", "0"):
        return hw

    def air_temp():
        r = hw.temperature.read()
        return r.celsius if r else None

    try:
        if mock:
            from plant.hardware.thermal import MockThermalCamera
            hw.thermal = MockThermalCamera(cfg, air_temp=air_temp)
        else:
            from plant.hardware.thermal import ThermalCamera
            hw.thermal = ThermalCamera(cfg, air_temp=air_temp)
            log.info("Thermal camera: %s", hw.thermal.device or "not connected")
    except Exception as exc:  # never let an optional sensor stop the service
        log.warning("Thermal camera disabled — %s", exc)
    return hw


def get_hardware(cfg: dict) -> Hardware:
    """Return a Hardware bundle appropriate for the configured mode.

    Args:
        cfg: The top-level config dict loaded from config.yaml.

    Returns:
        A :class:`Hardware` instance ready to use.

    Raises:
        ValueError: If ``mode`` is not "simulation", "hybrid", or "hardware".
        RuntimeError: If "hardware" mode is requested on a non-Pi system.
    """
    mode = cfg.get("mode", "simulation")

    if mode == "simulation":
        from plant.hardware.mock import build_mock_hardware
        log.info("Hardware mode: SIMULATION (mock drivers)")
        return _attach_thermal(build_mock_hardware(cfg), cfg, mock=True)

    # Shared kwargs for RealCamera — picked up from the optional `camera:`
    # section in config.yaml. Defaults match the original hardcoded values.
    cam_cfg = cfg.get("camera", {}) or {}
    cam_kwargs = {
        "settle_seconds":          float(cam_cfg.get("settle_seconds", 2.0)),
        "snapshot_settle_seconds": float(cam_cfg.get("snapshot_settle_seconds", 1.0)),
    }

    if mode == "hardware":
        from plant.hardware.real import (
            RealTemperatureSensor, RealCamera, RealValveActuator,
        )
        gpio = cfg["gpio"]
        log.info("Hardware mode: REAL (Pi drivers)")
        return _attach_thermal(Hardware(
            soil=_build_soil_sensor(cfg),
            temperature=RealTemperatureSensor(),
            camera=RealCamera(**cam_kwargs),
            valve=RealValveActuator(gpio_pin=gpio["valve_gpio"]),
            light=_build_light_actuator(cfg),
        ), cfg)

    if mode == "hybrid":
        from plant.hardware.mock import (
            MockSoilSensor, MockValveActuator, MockLightActuator,
        )
        from plant.hardware.real import RealCamera, RealTemperatureSensor
        control = cfg.get("light", {}).get("control", "mock")
        log.info("Hardware mode: HYBRID (real camera+temp, %s light)", control)
        soil = _build_soil_sensor(cfg)
        # The mock valve only feeds back into a *mock* soil model; with a real
        # or null soil sensor there is nothing to simulate.
        sim_soil = soil if isinstance(soil, MockSoilSensor) else None
        light = _build_light_actuator(cfg) if control != "mock" else MockLightActuator()
        return _attach_thermal(Hardware(
            soil=soil,
            temperature=RealTemperatureSensor(),
            camera=RealCamera(**cam_kwargs),
            valve=MockValveActuator(soil_sensor=sim_soil),
            light=light,
        ), cfg)

    raise ValueError(
        f"Unknown mode {mode!r} in config.yaml — must be 'simulation', 'hybrid', or 'hardware'."
    )
