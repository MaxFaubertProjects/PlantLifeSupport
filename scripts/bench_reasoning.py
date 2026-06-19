#!/usr/bin/env python3
"""Benchmark reasoning models against the *real* Plant Life Support task.

Runs a fixed set of representative scenarios through each candidate Ollama
model using the project's own prompt builder (``build_reasoning_prompt``) and
output schema (``Decision``), so "quality" here means task quality — not a
generic leaderboard score.

For each (model, scenario) it measures:
  • latency  — wall-clock seconds for the ollama chat call
  • valid    — did the output parse + validate against the Decision schema?
  • sane     — does the decision agree with the scenario's expected behaviour?

Usage (run on the Pi, from the repo root):
    python scripts/bench_reasoning.py \
        --models llama3.1:8b gemma4:e4b-it-qat gemma4:e2b-it-qat \
        --reps 2 --out bench_results.json

Results stream to stdout as they complete and are also written to --out.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

# Make the project importable when run as a plain script from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ollama  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from plant.config import load as load_config  # noqa: E402
from plant.ai.prompts import build_reasoning_prompt, summarise_recent_cycles  # noqa: E402
from plant.ai.schema import Decision  # noqa: E402


# ── Test scenarios ──────────────────────────────────────────────────────────
# Each scenario mirrors a real cycle's inputs. `expect` encodes the sane
# decision; None means "don't score this field" (model has legitimate freedom).
#
# soil/temp are SimpleNamespace stand-ins for SoilReading / TemperatureReading
# (the prompt builder only reads .moisture_pct/.raw and .celsius).

def _soil(pct, raw):
    return SimpleNamespace(moisture_pct=pct, raw=raw)


def _temp(c):
    return SimpleNamespace(celsius=c)


def _cycle(ts, water, secs, light, soil_pct):
    return SimpleNamespace(
        ts=ts, final_water=water, final_water_secs=secs,
        final_light_on=light, soil_pct=soil_pct,
    )


_HISTORY_CALM = [
    _cycle("2026-06-19T08:00:00+00:00", False, 0, True, 40),
    _cycle("2026-06-19T07:00:00+00:00", False, 0, True, 42),
]

SCENARIOS = [
    {
        "name": "dry_soil_wilting",
        "soil": _soil(18.0, 760), "temp": _temp(22.5),
        "desc": "The leaves are drooping and the lower ones show slight curling. "
                "The soil surface looks dry and lightly cracked. No pests visible.",
        "history": _HISTORY_CALM,
        "expect": {"water": True},      # dry + wilting → should water
    },
    {
        "name": "moist_soil_healthy",
        "soil": _soil(56.0, 430), "temp": _temp(23.0),
        "desc": "Leaves are deep green, turgid and upright. Soil surface looks "
                "evenly moist. The plant appears healthy with no visible damage.",
        "history": [_cycle("2026-06-19T08:00:00+00:00", True, 4, True, 55)],
        "expect": {"water": False},     # already wet + healthy → don't water
    },
    {
        "name": "too_hot_heat_stress",
        "soil": _soil(30.0, 640), "temp": _temp(34.5),  # > too_hot guardrail
        "desc": "Some leaf edges look slightly crisp. Posture is mostly upright. "
                "Soil surface is dryish but not cracked.",
        "history": _HISTORY_CALM,
        "expect": {"water": False},     # heat-stress guardrail → suppress water
    },
    {
        "name": "cold_night",
        "soil": _soil(45.0, 520), "temp": _temp(11.5),  # < too_cold guardrail
        "desc": "Leaves look healthy and green, posture upright. Soil moist. "
                "Nothing abnormal visible.",
        "history": _HISTORY_CALM,
        "expect": {"light_on": True},   # too cold → light forced on for warmth
    },
    {
        "name": "no_sensor_drooping",
        "soil": None, "temp": _temp(24.0),
        "desc": "The plant is visibly drooping with several wilted leaves. The "
                "visible soil looks dry and pale. No pests seen.",
        "history": _HISTORY_CALM,
        "expect": {"water": True},      # no sensor but clearly thirsty → intent True
    },
]


def run_one(client, model, cfg, scen, temperature):
    """Run a single (model, scenario) inference; return a result dict."""
    system, user = build_reasoning_prompt(
        cfg, scen["soil"], scen["temp"], scen["desc"],
        summarise_recent_cycles(scen["history"]),
        recent_cycles=scen["history"],
    )

    t0 = time.monotonic()
    err = None
    decision = None
    raw = ""
    try:
        resp = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            format="json",
            options={"temperature": temperature, "num_ctx": 2048},
        )
        raw = resp.message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        decision = Decision(**json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        err = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # ollama / network / timeout
        err = f"{type(exc).__name__}: {exc}"
    latency = time.monotonic() - t0

    valid = decision is not None
    sane = None
    if valid:
        sane = True
        for field, want in scen["expect"].items():
            if getattr(decision, field) != want:
                sane = False
                break

    return {
        "scenario": scen["name"],
        "latency_s": round(latency, 2),
        "valid": valid,
        "sane": sane,
        "expect": scen["expect"],
        "decision": decision.model_dump() if valid else None,
        "error": err,
        "raw": raw[:300] if not valid else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", required=True)
    ap.add_argument("--reps", type=int, default=2,
                    help="repetitions per scenario (for latency stats)")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="bench_results.json")
    args = ap.parse_args()

    cfg = load_config(args.config)
    temperature = float(cfg.get("ai", {}).get("temperature", 0.2))
    timeout = cfg.get("ai", {}).get("timeout_seconds", 1000)
    host = cfg["ai"]["ollama_base_url"]
    client = ollama.Client(host=host, timeout=timeout)

    all_results = {}
    for model in args.models:
        print(f"\n===== {model} =====", flush=True)
        results = []
        # Warm-up: first call pays the model-load cost; do it once, untimed-ish.
        print(f"  warming up {model} (loading into RAM)…", flush=True)
        try:
            run_one(client, model, cfg, SCENARIOS[0], temperature)
        except Exception as exc:
            print(f"  ⚠️  warm-up failed: {exc}", flush=True)
        for rep in range(args.reps):
            for scen in SCENARIOS:
                r = run_one(client, model, cfg, scen, temperature)
                r["rep"] = rep
                results.append(r)
                flag = "ok " if r["valid"] else "BAD"
                sane = {True: "sane", False: "OFF ", None: "—"}[r["sane"]]
                print(
                    f"  [{flag}|{sane}] {r['scenario']:<22} "
                    f"{r['latency_s']:>6.1f}s"
                    + (f"  err={r['error'][:60]}" if r["error"] else ""),
                    flush=True,
                )
        all_results[model] = results

        # Per-model summary
        lat = [r["latency_s"] for r in results]
        valid_rate = sum(r["valid"] for r in results) / len(results)
        scored = [r for r in results if r["sane"] is not None]
        sane_rate = (sum(bool(r["sane"]) for r in scored) / len(scored)
                     if scored else float("nan"))
        print(
            f"  --- {model}: median {statistics.median(lat):.1f}s "
            f"(min {min(lat):.1f}, max {max(lat):.1f})  "
            f"valid {valid_rate:.0%}  sane {sane_rate:.0%}",
            flush=True,
        )

    Path(args.out).write_text(json.dumps(all_results, indent=2))
    print(f"\nWrote {args.out}", flush=True)

    # Final comparison table
    print("\n================ SUMMARY ================")
    print(f"{'model':<24} {'med_s':>7} {'max_s':>7} {'valid':>6} {'sane':>6}")
    for model, results in all_results.items():
        lat = [r["latency_s"] for r in results]
        valid_rate = sum(r["valid"] for r in results) / len(results)
        scored = [r for r in results if r["sane"] is not None]
        sane_rate = (sum(bool(r["sane"]) for r in scored) / len(scored)
                     if scored else float("nan"))
        print(f"{model:<24} {statistics.median(lat):>7.1f} {max(lat):>7.1f} "
              f"{valid_rate:>5.0%} {sane_rate:>5.0%}")


if __name__ == "__main__":
    main()
