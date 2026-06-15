# Plant Life Support — Autonomous AI Plant Caretaker on Raspberry Pi

## Context

The goal is an autonomous, **fully offline** plant caretaker running on a Raspberry Pi 5
(8GB). Every hour it reads soil moisture, air temperature and humidity, takes a photo of
the plant, and uses a small local AI model to *reason* about the plant's condition and
decide whether to water it and whether to turn a grow light on/off. A web dashboard plus a
small physical screen show the latest measurements, the photo, and the AI's reasoning
behind each decision.

Hardware is based on ExplainingComputers' "Raspberry Pi Plant Watering (& Time Lapse)"
project (resistive moisture sensor + solenoid valve + relay + camera), extended here with
a thermometer, a vision model, an offline reasoning model, and a display.

Decisions confirmed with the user:
- **Pi 5, 8GB** — enough RAM to run a small offline LLM + vision model.
- **Small vision-language model** — describes the plant in plain language (no training needed).
- **Web dashboard + small physical screen** — screen runs the dashboard in kiosk mode.
- **No hardware yet** — plan includes a parts list + wiring guide, and the software ships
  with a **simulation mode** so it can be built and tested on the Mac before hardware arrives.

## Hardware

The reference build uses an analog resistive sensor (the Pi has no analog input, so that
needs an ADC). This plan instead standardises on **I²C digital sensors** — simpler wiring,
no ADC, more reliable readings.

### Parts list

| Part | Purpose | Notes |
|---|---|---|
| Raspberry Pi 5, 8GB + official 27W USB-C PSU | Compute | Active cooler strongly recommended (AI inference heats it up) |
| microSD 64GB (A2) or NVMe HAT + SSD | Storage | SSD preferred — faster model loading, more durable |
| Raspberry Pi Camera Module 3 + ribbon cable | "Looks at the plant" + time-lapse | Pi 5 uses the narrower CSI cable |
| Adafruit STEMMA capacitive soil moisture sensor (I²C, Seesaw) | Soil moisture | Capacitive = no corrosion; I²C = no ADC needed |
| BME280 breakout (I²C) | Air temperature + humidity | The "thermometer"; humidity also informs watering |
| 2-channel relay board (5V, opto-isolated) | Switch valve + light | Channel 1 = valve, Channel 2 = light |
| 12V DC solenoid valve (plastic, normally-closed) | Watering | Plus a flyback diode across the coil |
| 12V LED grow light panel/strip | Lighting | 12V keeps everything off one rail — **avoids mains AC wiring** |
| 12V DC power supply (≥3A) | Powers valve + light | Separate from the Pi's 5V supply |
| Small HDMI or DSI touchscreen (e.g. official Pi Touch Display 2, or 5" HDMI) | Physical display | Runs the dashboard in browser kiosk mode |
| Tubing, water reservoir, jumper wires, breadboard/proto-HAT | Plumbing + wiring | |

### Wiring summary
- **I²C bus** (Pi pins 3/SDA, 5/SCL, 3.3V, GND): soil sensor + BME280 share the bus (different addresses).
- **Relay board**: 5V + GND from Pi; two control pins to spare GPIOs (e.g. GPIO17 = valve, GPIO27 = light).
- **Valve & light**: switched on the 12V rail through the relay contacts. **Flyback diode**
  across the solenoid coil to protect the relay.
- **Camera**: CSI ribbon to the Pi 5 camera connector.
- **Display**: HDMI/DSI + USB (touch).
- Pi powered from its **own** 27W supply; 12V supply is independent (common ground at the relay).

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
      real.py              # Pi drivers: Seesaw soil, BME280, gpiozero relays, picamera2
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
    plantlife.service            # systemd unit (autostart + auto-restart)
    kiosk-setup.md               # Chromium kiosk-mode instructions for the screen
  tests/
    test_safety.py         # safety clamping/override tests
    test_loop_sim.py       # full pipeline run in simulation mode
  README.md                # parts list, wiring, install, run
```

### The hourly decision pipeline (`core/loop.py`)
1. **Sense** — read soil moisture, air temp, humidity.
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
separate display code. The dashboard shows: latest photo, live moisture/temp/humidity,
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
8. **Real hardware drivers** — `real.py` (Seesaw, BME280, gpiozero, picamera2), factory selection.
9. **Deploy assets** — systemd unit, kiosk setup, full `README.md` (parts, wiring, install).

Phases 1–7 are fully developable and testable **on the Mac**. Phases 8–9 are completed/
verified once the Pi and parts arrive.

## Verification

**On the Mac (simulation mode):**
- `pytest tests/` — safety clamping and override logic; a full simulated hourly cycle.
- Run `main.py` with `mode: simulation`; confirm the loop senses → reasons → "acts" →
  records, with Ollama producing real reasoning text from synthetic data.
- Open the dashboard; confirm live readings, charts, the reasoning log, and manual
  overrides all work.
- Force-fail Ollama; confirm the rule-based fallback keeps the plant "watered".

**On the Pi (hardware bring-up checklist, in `README.md`):**
- `i2cdetect` shows both sensors; readings look sane.
- Relay channels click; valve pulses for exactly the safety-clamped duration; light toggles.
- Camera captures a clear photo of the plant.
- `ollama run` works for both chosen models on-device; time one full cycle end-to-end.
- Reboot; confirm the systemd service and kiosk dashboard come up automatically.
- Run unattended for 24h; review the reasoning log for sensible hourly decisions.
