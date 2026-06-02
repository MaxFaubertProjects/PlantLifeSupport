"""LLM reasoning client — context → structured Decision JSON.

Sends the assembled context to Ollama and parses the JSON response
into a validated Decision object.  Falls back to a safe no-op if the
model is unavailable or returns unparseable output.
"""

from __future__ import annotations

import json
import logging

import ollama
from pydantic import ValidationError

from plant.ai.schema import Decision
from plant.ai.prompts import build_reasoning_prompt, summarise_recent_cycles
from plant.hardware.interfaces import SoilReading, TemperatureReading

log = logging.getLogger(__name__)


class ReasoningClient:
    def __init__(self, cfg: dict) -> None:
        ai = cfg["ai"]
        self._model = ai["reasoning_model"]
        self._timeout = ai["timeout_seconds"]
        self._cfg = cfg
        self._client = ollama.Client(
            host=ai["ollama_base_url"],
            timeout=self._timeout,
        )

    def decide(
        self,
        soil: SoilReading | None,
        temp: TemperatureReading,
        plant_description: str,
        recent_cycles: list,
    ) -> tuple[Decision, bool]:
        """Ask the LLM to make a care decision.

        Args:
            soil: Current soil reading, or None if no soil sensor is installed.
            temp: Current temperature reading.
            plant_description: VLM text description of the plant photo.
            recent_cycles: Recent Cycle objects from the database (newest first).

        Returns:
            A (decision, used_fallback) tuple.
            ``used_fallback`` is True if the LLM was unavailable or returned
            invalid JSON — in that case the decision comes from rules.py.
        """
        history = summarise_recent_cycles(recent_cycles)
        system, user = build_reasoning_prompt(
            self._cfg, soil, temp, plant_description, history,
            recent_cycles=recent_cycles,
        )

        try:
            log.info("ReasoningClient: querying %s …", self._model)
            resp = self._client.chat(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                # format="json" tells Ollama to force valid JSON output without
                # constraining every token to a strict schema. For a small model
                # like llama3.2:3b this gives reliably parseable output with less
                # chance of stilted/incomplete generations than full schema-
                # constrained decoding (format=Decision.model_json_schema()).
                # Pydantic still validates the structure on our end after decoding.
                format="json",
                options={
                    "temperature": 0.2,   # low temp → consistent JSON
                    "num_ctx": 2048,      # small KV cache — our prompts are short
                },
            )
            raw = resp.message.content.strip()
            log.debug("ReasoningClient: raw response: %s", raw)

            # Strip accidental markdown fences (``` json ... ```)
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)
            decision = Decision(**data)
            log.info(
                "ReasoningClient: water=%s (%ds), light=%s",
                decision.water, decision.water_seconds, decision.light_on,
            )
            return decision, False

        except json.JSONDecodeError as exc:
            log.warning("ReasoningClient: JSON parse error — %s", exc)
            return self._rule_fallback(soil, temp, reason=f"JSON error: {exc}"), True

        except ValidationError as exc:
            log.warning("ReasoningClient: schema validation error — %s", exc)
            return self._rule_fallback(soil, temp, reason=f"schema error: {exc}"), True

        except Exception as exc:
            log.warning("ReasoningClient: Ollama error — %s", exc)
            return self._rule_fallback(soil, temp, reason=f"Ollama unavailable: {exc}"), True

    def _rule_fallback(
        self, soil: SoilReading | None, temp: TemperatureReading, reason: str
    ) -> Decision:
        """Import rules lazily to avoid a circular dependency."""
        from plant.core.rules import rule_based_decision
        return rule_based_decision(self._cfg, soil, temp, fallback_reason=reason)
