"""Plant Life Support — main entrypoint.

Starts the control loop and the web server concurrently.

    .venv/bin/python main.py                    # simulation mode (default)
    .venv/bin/python main.py --config my.yaml   # custom config file
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from plant.config import load as load_config


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


async def main(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    _setup_logging()

    log = logging.getLogger("main")
    log.info("Plant Life Support starting up (mode=%s)", cfg.get("mode"))

    # Lazy imports — keeps startup fast and avoids circular deps.
    from plant.hardware.factory import get_hardware
    from plant.core.loop import ControlLoop
    from plant.web.app import create_app

    hardware = get_hardware(cfg)

    # Defensive: force both actuators OFF immediately after hardware init.
    # gpiozero's `initial_value=False` should already set them low, but
    # rail dips or HAT-level glitches during pin claim can briefly trigger
    # neighboring relays. This explicit reset is cheap insurance.
    hardware.valve.off()
    hardware.light.set(False)
    log.info("Startup: valve + light forced OFF")

    loop = ControlLoop(cfg=cfg, hardware=hardware)
    app = create_app(cfg=cfg, hardware=hardware, control_loop=loop)

    import uvicorn

    server_cfg = uvicorn.Config(
        app,
        host=cfg["web"]["host"],
        port=cfg["web"]["port"],
        log_level="warning",   # uvicorn's own logs — keep quiet
    )
    server = uvicorn.Server(server_cfg)

    log.info(
        "Dashboard: http://localhost:%d", cfg["web"]["port"]
    )

    # Run the control loop and web server concurrently.
    await asyncio.gather(
        loop.run_forever(),
        server.serve(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Plant Life Support")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.config))
    except KeyboardInterrupt:
        print("\nShutting down.")
