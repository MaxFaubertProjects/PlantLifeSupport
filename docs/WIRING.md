# Wiring Guide — Plant Life Support

Pin-by-pin connection reference for the build. The visual schematic
(`docs/schematic.svg`) is generated from these same connections.

> **Read first — two isolated power domains.** The Raspberry Pi runs on its own
> 5V USB-C supply (logic at 3.3V/5V). The valve and grow light run on a separate
> **12V supply**. The two domains are kept **electrically isolated** by the relay
> contacts — the 12V circuit is a self-contained loop and its ground does **not**
> connect to the Pi's ground. Never feed 12V into a Pi GPIO pin.

## Subsystem 1 — MCP3008 ADC ↔ Raspberry Pi (SPI)

The Pi has no analog input, so the soil sensor is read through an MCP3008.
The relay board is a HAT covering the 40-pin header — make these connections to
the **pass-through (stacking) header on top of the relay board**.

| MCP3008 pin | Name | Connects to | Pi physical pin |
|---|---|---|---|
| 16 | VDD | 3.3V | 1 |
| 15 | VREF | 3.3V | 1 |
| 14 | AGND | GND | 9 |
| 13 | CLK | GPIO11 (SCLK) | 23 |
| 12 | DOUT | GPIO9 (MISO) | 21 |
| 11 | DIN | GPIO10 (MOSI) | 19 |
| 10 | CS/SHDN | GPIO8 (CE0) | 24 |
| 9 | DGND | GND | 25 |
| 1 | CH0 | Soil sensor AOUT | — |

Enable SPI on the Pi: `sudo raspi-config` → Interface Options → SPI → Enable.

## Subsystem 2 — Resistive soil moisture sensor ↔ MCP3008

| Soil sensor pin | Connects to | Notes |
|---|---|---|
| VCC | Pi **3.3V** | **Use 3.3V, not 5V** — AOUT must stay within the MCP3008's 3.3V VREF |
| GND | Pi GND | |
| AOUT (A0) | MCP3008 CH0 (pin 1) | Analog moisture level |
| DOUT (D0) | *(optional)* spare GPIO | Comparator wet/dry flag; not required |

Resistive probes corrode — the software powers/reads only once per hour to slow this.

## Subsystem 3 — DS18B20 temperature probe ↔ Raspberry Pi (1-Wire)

| DS18B20 wire | Connects to | Pi physical pin |
|---|---|---|
| Red (VCC) | 3.3V | 1 / 17 |
| Black (GND) | GND | 6 |
| Yellow (DATA) | GPIO4 | 7 |

Add a **4.7 kΩ pull-up resistor** between DATA (yellow) and 3.3V.
Enable 1-Wire: `sudo raspi-config` → Interface Options → 1-Wire → Enable.

## Subsystem 4 — Relay board (HAT)

The Electronics-Salon RPi Power Relay Board plugs directly onto the 40-pin header.
It carries **3 relays** (5V coils, powered from the header); this build uses **2**:

| Relay | Drives | Output terminals used |
|---|---|---|
| Relay 1 | Solenoid valve | COM + NO |
| Relay 2 | Grow light | COM + NO |
| Relay 3 | *(spare)* | — |

The 3 relay control lines are labelled on the board's silkscreen. Note which GPIO
drives each relay and set them in `config.yaml` (`valve_gpio`, `light_gpio`).

## Subsystem 5 — 12V circuit (valve + grow light)

Switched by the relay contacts only — fully isolated from the Pi.

| From | To |
|---|---|
| 12V PSU (+) | Relay 1 COM **and** Relay 2 COM |
| Relay 1 NO | Solenoid valve (+) |
| Solenoid valve (−) | 12V PSU (−) |
| Relay 2 NO | Grow light (+) |
| Grow light (−) | 12V PSU (−) |

**Flyback diode (1N4007)** across the solenoid valve coil: band (cathode) to the
valve's (+) terminal, plain end (anode) to the (−) terminal. It clamps the voltage
spike when the coil de-energises and protects the relay contacts.

## Non-electrical connections

- **Camera** — Arducam OV5647 ribbon into the Pi 5 CSI camera connector (narrow cable).
- **Display** — HDMI cable, Pi → monitor.
- **Pi power** — 27W USB-C supply into the Pi.

## Block diagram

```
            +----------------------+
   USB-C 27W |   Raspberry Pi 5     |  HDMI ----> [ HDMI display ]
   --------->|                      |  CSI  ----> [ OV5647 camera ]
             |  3.3V 5V GND         |
             |  SPI0  GPIO4         |
             +--+----+----+----+----+
                |    |    |    |
       3.3V/GND |    |SPI |    | GPIO4 (1-Wire)
                |    |    |    |
                v    v    |    v
           +---------+    | +-----------+
           | MCP3008 |    | | DS18B20   |--[4.7k]--3.3V
           |   ADC   |    | | temp probe|
           +----+----+    | +-----------+
            CH0 |         |
                v         |
        +---------------+ |   [ Relay board HAT — sits on the 40-pin header ]
        | Soil moisture | |        |  Relay 1        |  Relay 2
        | sensor (3.3V) | |        |  COM/NO         |  COM/NO
        +---------------+ |        |                 |
                          |        v                 v
   ===== LOGIC DOMAIN ====|==== isolated by relay contacts ====
                                   |                 |
        12V PSU (+) --------+------ COM1       +----- COM2
                            |       NO1 --+    |      NO2 --+
                            |             |    |           |
                            |        [Solenoid]|      [Grow light]
                            |        [+1N4007 ]|           |
        12V PSU (-) --------+-------------+----+-----------+
```

## Pre-power-on checklist

- [ ] Soil sensor VCC is on **3.3V** (not 5V).
- [ ] MCP3008 VREF and VDD both on 3.3V; AGND and DGND both grounded.
- [ ] 4.7 kΩ pull-up present on the DS18B20 data line.
- [ ] Flyback diode across the solenoid coil, band toward (+).
- [ ] 12V ground is **not** tied to Pi ground.
- [ ] SPI and 1-Wire enabled in `raspi-config`.
- [ ] Relay control GPIOs noted from the silkscreen and set in `config.yaml`.
