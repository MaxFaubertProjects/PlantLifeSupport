"""Generate the Plant Life Support wiring schematic.

Renders docs/schematic.svg and docs/schematic.png from the connections
documented in docs/WIRING.md. Run with the project venv:

    .venv/bin/python tools/draw_schematic.py
"""
from pathlib import Path

import schemdraw
from schemdraw import elements as elm

DOCS = Path(__file__).resolve().parent.parent / "docs"

BLUE = "#1f6feb"
GREEN = "#1a7f37"
ORANGE = "#bc4c00"
RED = "#cf222e"
GRAY = "#57606a"


def titled_ic(d, title, center, w, h, pins):
    """Add an IC block with its title placed above the box."""
    ic = elm.Ic(pins=pins, w=w, h=h)
    d += ic.at(center).anchor("center")
    d += elm.Label().label(title).at((center[0], center[1] + h / 2 + 0.6))
    return ic


def build() -> schemdraw.Drawing:
    d = schemdraw.Drawing()
    d.config(fontsize=11, lw=1.3)

    # ================================================================ title
    d += elm.Label().label("Plant Life Support — Wiring Schematic").at((9, 8.2)).color("black")

    # ========================================================= Raspberry Pi
    pi = titled_ic(
        d,
        "Raspberry Pi 5",
        center=(0, -2),
        w=5,
        h=15,
        pins=[
            elm.IcPin(name="3V3", anchorname="V33", side="right", slot="4/4"),
            elm.IcPin(name="GND", side="right", slot="3/4"),
            elm.IcPin(name="SPI0", side="right", slot="2/4"),
            elm.IcPin(name="GPIO4", side="right", slot="1/4"),
            elm.IcPin(name="40-pin header", anchorname="HAT", side="bottom", slot="2/3"),
        ],
    )
    # Pi is the source of 3.3V and logic ground
    d += elm.Line().at(pi.V33).right().length(0.8)
    d += elm.Vdd().label("3.3V")
    d += elm.Line().at(pi.GND).right().length(0.8)
    d += elm.Ground()

    # ============================================================== MCP3008
    mcp = titled_ic(
        d,
        "MCP3008 ADC",
        center=(13, 1),
        w=5,
        h=8,
        pins=[
            elm.IcPin(name="VDD", side="left", slot="5/5"),
            elm.IcPin(name="VREF", side="left", slot="4/5"),
            elm.IcPin(name="AGND", side="left", slot="3/5"),
            elm.IcPin(name="SPI", side="left", slot="2/5"),
            elm.IcPin(name="DGND", side="left", slot="1/5"),
            elm.IcPin(name="CH0", side="right", slot="1/1"),
        ],
    )

    # ========================================================== soil sensor
    soil = titled_ic(
        d,
        "Resistive soil moisture sensor",
        center=(24, 1),
        w=5.5,
        h=4.5,
        pins=[
            elm.IcPin(name="VCC", side="left", slot="3/3"),
            elm.IcPin(name="AOUT", side="left", slot="2/3"),
            elm.IcPin(name="GND", side="left", slot="1/3"),
        ],
    )

    # =============================================================== DS18B20
    ds = titled_ic(
        d,
        "DS18B20 temp probe",
        center=(13, -9),
        w=5,
        h=3.6,
        pins=[
            elm.IcPin(name="VCC", side="top", slot="2/3"),
            elm.IcPin(name="DATA", side="left", slot="1/1"),
            elm.IcPin(name="GND", side="bottom", slot="2/3"),
        ],
    )

    # ----------------------------------------------------- logic-side power
    for pin in (mcp.VDD, mcp.VREF, soil.VCC, ds.VCC):
        d += elm.Line().at(pin).left().length(0.7) if pin is not ds.VCC else elm.Line().at(pin).up().length(0.7)
        d += elm.Vdd().label("3.3V", fontsize=9)
    for pin in (mcp.AGND, mcp.DGND, soil.GND):
        d += elm.Line().at(pin).left().length(0.7)
        d += elm.Ground()
    d += elm.Line().at(ds.GND).down().length(0.5)
    d += elm.Ground()

    # ------------------------------------------------- SPI bus  (Pi -> MCP)
    d += (
        elm.Wire("-|")
        .at(pi.SPI0)
        .to(mcp.SPI)
        .color(BLUE)
        .label("SPI0\nSCLK · MISO · MOSI · CE0", fontsize=9, ofst=(0, 0.5))
    )

    # ------------------------------------------- analog moisture: CH0 <- AOUT
    d += elm.Wire("-").at(mcp.CH0).to(soil.AOUT).color(GREEN).label("analog level", fontsize=9)

    # --------------------------------------------- 1-Wire bus: GPIO4 -> DATA
    d += elm.Wire("-|").at(pi.GPIO4).to(ds.DATA).color(ORANGE).label("GPIO4 — 1-Wire", fontsize=9)
    # 4.7k pull-up from the data line to 3.3V
    d += elm.Dot().at(ds.DATA)
    d += elm.Line().at(ds.DATA).left().length(1.6)
    d += elm.Resistor().up().length(2.4).label("4.7k", fontsize=9)
    d += elm.Vdd().label("3.3V", fontsize=9)

    # ========================================================== relay board
    relay = titled_ic(
        d,
        "Relay board  (HAT — 3 relays, 2 used)",
        center=(2, -17),
        w=11,
        h=5,
        pins=[
            elm.IcPin(name="40-pin header", anchorname="HDR", side="top", slot="2/3"),
            elm.IcPin(name="R1 · COM", anchorname="R1C", side="right", slot="4/4"),
            elm.IcPin(name="R1 · NO", anchorname="R1NO", side="right", slot="3/4"),
            elm.IcPin(name="R2 · COM", anchorname="R2C", side="right", slot="2/4"),
            elm.IcPin(name="R2 · NO", anchorname="R2NO", side="right", slot="1/4"),
        ],
    )
    d += (
        elm.Wire("|-")
        .at(pi.HAT)
        .to(relay.HDR)
        .label("stacks on the 40-pin GPIO header\n(5V, GND, relay control GPIOs)", fontsize=9)
    )

    # --------------------------------------------------- domain divider line
    d += elm.Line().at((-7, -12.4)).to((30, -12.4)).color(GRAY).linestyle("--")
    d += elm.Label().label("— — —  12V domain: valve + grow light, isolated by the relay contacts  — — —").at(
        (12, -12.0)
    ).color(GRAY)

    # ============================================================ 12V supply
    psu = titled_ic(
        d,
        "12V DC supply",
        center=(-3.5, -23),
        w=4.5,
        h=3,
        pins=[
            elm.IcPin(name="+12V", anchorname="P12", side="right", slot="2/2"),
            elm.IcPin(name="-12V", anchorname="N12", side="right", slot="1/2"),
        ],
    )

    # +12V from the supply to both relay commons
    d += elm.Wire("-|").at(psu.P12).to(relay.R1C).color(RED).label("+12V", fontsize=9)
    d += elm.Wire("-|").at(psu.P12).to(relay.R2C).color(RED)

    # ---- solenoid valve branch (Relay 1), with flyback diode in parallel ----
    valve_x, diode_x = 13.0, 16.0
    top_y, bot_y = -16.2, -21.5
    d += elm.Line().at(relay.R1NO).to((valve_x, relay.R1NO[1]))
    d += elm.Line().at((valve_x, relay.R1NO[1])).to((valve_x, top_y))
    d += elm.Dot().at((valve_x, top_y))
    d += elm.Inductor2().at((valve_x, top_y)).to((valve_x, bot_y)).label("Solenoid\nvalve", fontsize=9)
    d += elm.Dot().at((valve_x, bot_y))
    # flyback diode: cathode (band) toward +12V / valve top
    d += elm.Line().at((valve_x, top_y)).to((diode_x, top_y))
    d += elm.Diode().at((diode_x, bot_y)).to((diode_x, top_y)).label("1N4007", fontsize=9)
    d += elm.Line().at((diode_x, bot_y)).to((valve_x, bot_y))

    # ---- grow light branch (Relay 2) ----
    light_x = 23.0
    d += elm.Line().at(relay.R2NO).to((light_x, relay.R2NO[1]))
    d += elm.Line().at((light_x, relay.R2NO[1])).to((light_x, top_y))
    d += elm.Lamp().at((light_x, top_y)).to((light_x, bot_y)).label("Grow light", fontsize=9)

    # ---- 12V return rail back to -12V ----
    d += elm.Line().at((valve_x, bot_y)).to((valve_x, -23.0))
    d += elm.Line().at((light_x, bot_y)).to((light_x, -23.0))
    d += elm.Line().at(psu.N12).to((valve_x, -23.0)).color("black")
    d += elm.Line().at((valve_x, -23.0)).to((light_x, -23.0))
    d += elm.Dot().at((valve_x, -23.0))
    d += elm.Label().label("12V return").at((9, -23.5)).color(GRAY)

    return d


if __name__ == "__main__":
    drawing = build()
    drawing.save(str(DOCS / "schematic.svg"))
    drawing.save(str(DOCS / "schematic.png"), dpi=200)
    print(f"wrote {DOCS / 'schematic.svg'}")
    print(f"wrote {DOCS / 'schematic.png'}")
