# Plant Life Support — Autonomous AI Plant Caretaker on Raspberry Pi

## Context

The goal is an autonomous, **fully offline** plant caretaker running on a Raspberry Pi 5.
Every hour it reads soil moisture and temperature, takes a photo of
the plant, and uses a small local AI model to *reason* about the plant's condition and
decide whether to water it and whether to turn a grow light on/off. A web dashboard plus a
small physical screen show the latest measurements, the photo, and the AI's reasoning
behind each decision.

Hardware is based on ExplainingComputers' "Raspberry Pi Plant Watering (& Time Lapse)"
project (resistive moisture sensor + solenoid valve + relay + camera), extended here with
a thermometer, a vision model, an offline reasoning model, and a display.

Decisions confirmed with the user:
- **Pi 5** (confirm 8GB+ RAM) — enough RAM to run a small offline LLM + vision model.
- **Small vision-language model** — describes the plant in plain language (no training needed).
- **Web dashboard + HDMI display** — the display runs the dashboard in kiosk mode.
- **Simulation mode** — the software ships with a simulation mode so phases 1–7 can be
  built and tested on the Mac before the hardware is wired up.

Hardware has now been purchased — see the parts list below. It deviates from the original
plan (resistive soil sensor instead of capacitive I²C; DS18B20 temp-only instead of a
BME280, so **no humidity reading**; OV5647 camera instead of Camera Module 3). This
document reflects the **actual hardware**.

## Hardware

The Pi has **no analog input**, so the resistive soil sensor's analog output is read
through an **MCP3008 ADC** over SPI. Temperature uses a **DS18B20** on the 1-wire bus.
(The relay board occupies fixed GPIOs — confirm its pinout from the board's docs.)

### Parts list — purchased

| Part | Purpose | Notes |
|---|---|---|
| Raspberry Pi 5 (+ 27W USB-C PSU) | Compute | **Confirm 8GB+ RAM** for the offline LLM |
| Arducam OV5647 5MP Camera Module V1 + CSI cable | Plant photos + time-lapse | **Fixed focus** — mount at a fixed distance from the plant; verify the kit's cable fits the Pi 5's narrower CSI port |
| Electronics-Salon RPi Power Relay Board | Switch valve + light | Confirm it has **≥2 channels**; only one board is needed (two ordered) |
| Beduan 12V 1/4" solenoid valve, normally-closed (RO) | Watering | RO valve — **verify minimum operating pressure** (see risk note below) |
| 1/4" RO tubing + push-connect fittings | Water line | Matches the valve's 1/4" inlet |
| Icstation resistive soil moisture sensor | Soil moisture | Analog out → MCP3008 ADC; power probe **only during reads** (resistive probes corrode) |
| DS18B20 waterproof temperature probe | Temperature | 1-wire; **temperature only, no humidity**. Probe can sit in the soil (soil temp) or in air |
| HDMI display | Dashboard screen | Runs the dashboard in browser kiosk mode |

### Parts list — still to buy

| Part | Purpose | Notes |
|---|---|---|
| MCP3008 ADC | Reads the analog soil sensor over SPI | ~$5; skip only if accepting a binary wet/dry reading from the sensor's digital pin |
| 12V DC power supply (≥3A) | Powers valve + light | Independent of the Pi's 5V supply |
| 12V LED grow light panel/strip | Lighting | 12V keeps everything on one rail — **avoids mains AC wiring** |
| Flyback diode (e.g. 1N4007) | Protects the relay from the solenoid coil | Wired across the valve's coil |
| Pi 5 active cooler | Thermal | LLM inference heats the Pi — strongly recommended |
| microSD (A2) or NVMe HAT + SSD | Storage | SSD preferred — faster model loading |
| Water reservoir, jumper wires, breadboard/proto-HAT | Plumbing + wiring | |

### Wiring summary
- **Soil sensor → MCP3008 → Pi**: sensor analog output to an MCP3008 channel; MCP3008
  talks to the Pi over **SPI** (SPI0: GPIO 8/9/10/11). Power the sensor from a spare GPIO
  so it is energised **only during a reading**.
- **DS18B20**: data line to **GPIO4** (1-wire bus), with a **4.7 kΩ pull-up** to 3.3V.
- **Relay board**: powered from the Pi 5V/GND; its channels are driven by fixed GPIOs —
  one for the valve, one for the light (confirm pins from the Electronics-Salon docs).
- **Valve & light**: switched on the 12V rail through the relay contacts. **Flyback diode**
  across the solenoid coil to protect the relay.
- **Camera**: CSI ribbon to the Pi 5 camera connector.
- **Display**: HDMI.
- Pi powered from its **own** 27W supply; the 12V supply is independent (common ground at the relay).

**Key risk to validate early — solenoid valve pressure.** RO solenoid valves are often
diaphragm/pilot-operated and need a **minimum water pressure** to open; a gravity-fed
reservoir provides almost none (~0.4 psi per foot of height). Before committing to the
build, test whether this valve actually flows under the intended gravity feed. If it does
not, add a small **12V diaphragm pump** (switched by the relay — it can even replace the
valve as the actuator).

A full wiring table + an ASCII diagram will go in `README.md`, with a from-scratch setup guide.

## Software architecture

Language: **Python 3.11+**. Developed on the Mac in simulation mode, deployed to the Pi.

```
PlantLifeSupport/
  config.yaml              # plant profile, thresholds, safety limits, schedule, model names
  requirements.txt
  main.py                  # entrypoint: starts control loop + web server
  plant/
    hardware/
      interfaces.py        # abstract Sensor / Actuator / Camera protocols
      real.py              # Pi drivers: MCP3008 soil ADC, DS18B20, gpiozero relays, picamera2
      mock.py              # simulation drivers: synthetic readings, logged actuators
      factory.py           # picks real vs mock from config / platform detection
    ai/
      vision.py            # VLM client (Ollama) — image -> plain-language description
      reasoning.py         # LLM client (Ollama) — context -> structured decision
      schema.py            # Pydantic Decision model (water, water_seconds, light, reasoning)
      prompts.py           # prompt templates + plant profile injection
    core/
      loop.py              # hourly orchestrator (the decision pipeline)
      safety.py            # deterministic guardrails — clamps/overrides AI decisions
      rules.py             # rule-based fallback if the AI is unavailable
    storage/
      db.py                # SQLite: readings, decisions, actions, photo paths
    web/
      app.py               # FastAPI: dashboard API + manual overrides
      static/              # dashboard HTML/JS/CSS (latest photo, charts, reasoning log)
  data/                    # SQLite db + captured photos (gitignored)
  deploy/
    plant-life-support.service   # systemd unit (autostart + auto-restart)
    kiosk-setup.md               # Chromium kiosk-mode instructions for the screen
  tests/
    test_safety.py         # safety clamping/override tests
    test_loop_sim.py       # full pipeline run in simulation mode
  README.md                # parts list, wiring, install, run
```

### The hourly decision pipeline (`core/loop.py`)
1. **Sense** — read soil moisture and temperature.
2. **See** — capture a photo (also saved for time-lapse).
3. **Describe** — VLM turns the photo into text ("leaves slightly drooped, mild yellowing
   on lower leaves, soil surface dry").
4. **Assemble context** — current readings + recent history + VLM description + plant
   profile + the last few actions taken.
5. **Reason** — LLM produces a **structured `Decision`** (Pydantic-validated JSON) with a
   natural-language `reasoning` field.
6. **Validate** — `safety.py` clamps/overrides the decision against hard limits.
7. **Act** — pulse the valve for the (clamped) duration; set the light.
8. **Record** — persist readings, photo, raw AI output, final decision, and reasoning to
   SQLite; the dashboard reads from there.

### AI models (offline, via Ollama on the Pi)
- **Vision**: a small VLM — `moondream` (1.8B) or `qwen2.5-vl:3b`. Slow (~30s–2min/image)
  but the hourly cadence makes that fine.
- **Reasoning**: a small text LLM — `llama3.2:3b` or `qwen2.5:3b`.
- Two-stage (VLM → text) keeps each model's job simple; `config.yaml` lets either be swapped.
- Ollama also runs on the Mac, so the full AI pipeline is testable in simulation mode.

### Safety — the AI proposes, deterministic code disposes
`safety.py` enforces, independently of the LLM:
- **Max watering pulse** per cycle (e.g. 8s) and **minimum interval** between waterings.
- **No watering if soil is already moist** (sensor sanity check) — guards against a bad AI call flooding the plant.
- **Light cap** — max on-hours per day.
- **Schema validation** — anything the LLM returns that isn't valid `Decision` JSON is rejected.
- **Rule-based fallback** (`rules.py`) — if Ollama is down or times out, simple threshold
  logic still waters/lights the plant so it survives. Critically-low moisture always
  triggers watering regardless of the AI.

### Display
The small screen runs **Chromium in kiosk mode** pointed at the local dashboard — no
separate display code. The dashboard shows: latest photo, live moisture and temperature,
history charts, and a scrollable **reasoning log** of each hourly decision and why.
Manual override buttons (water now / light on-off) are included.

### Autostart
A `systemd` service starts `main.py` on boot and restarts on failure. Ollama runs as its
own service.

## Build phases

1. **Scaffold + config** — project structure, `config.yaml`, `requirements.txt`, hardware interfaces.
2. **Simulation drivers** — `mock.py` with realistic synthetic sensor behaviour (soil
   slowly dries, responds to watering); logged actuators.
3. **Storage** — SQLite schema + read/write layer.
4. **AI pipeline** — Ollama vision + reasoning clients, `Decision` schema, prompts.
5. **Core loop + safety** — orchestrator, safety guardrails, rule-based fallback.
6. **Web dashboard** — FastAPI API + frontend (photo, charts, reasoning log, overrides).
7. **Tests** — safety clamping tests + a full simulated pipeline run.
8. **Real hardware drivers** — `real.py` (MCP3008 soil ADC, DS18B20, gpiozero, picamera2), factory selection.
9. **Deploy assets** — systemd unit, kiosk setup, full `README.md` (parts, wiring, install).

Phases 1–7 are fully developable and testable **on the Mac**. Phases 8–9 are completed/
verified once the Pi and parts arrive.

## Future / v2 ideas

- **Custom PCB** — once the circuit is proven on a breadboard/proto-HAT, design a custom
  HAT (relay drivers, flyback diode, MCP3008, screw terminals for 12V/valve/light,
  sensor connectors) in Flux.ai for a clean, permanent build.
- **Humidity sensor** — the DS18B20 is temperature-only; adding a BME280 or DHT22 later
  would restore humidity as a watering-decision input.

## Verification

**On the Mac (simulation mode):**
- `pytest tests/` — safety clamping and override logic; a full simulated hourly cycle.
- Run `main.py` with `mode: simulation`; confirm the loop senses → reasons → "acts" →
  records, with Ollama producing real reasoning text from synthetic data.
- Open the dashboard; confirm live readings, charts, the reasoning log, and manual
  overrides all work.
- Force-fail Ollama; confirm the rule-based fallback keeps the plant "watered".

**On the Pi (hardware bring-up checklist, in `README.md`):**
- MCP3008 returns sane soil-moisture values over SPI; DS18B20 shows on the 1-wire bus.
- Relay channels click; valve pulses for exactly the safety-clamped duration; light toggles.
- **Solenoid valve flows under the intended gravity feed** (see the pressure risk note).
- Camera captures a clear, in-focus photo of the plant at its mounted distance.
- `ollama run` works for both chosen models on-device; time one full cycle end-to-end.
- Reboot; confirm the systemd service and kiosk dashboard come up automatically.
- Run unattended for 24h; review the reasoning log for sensible hourly decisions.
