"""VLM client — image → plant description text.

Sends the plant photo to Ollama and returns a plain-text description.
Falls back gracefully if Ollama is unavailable.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import ollama

from plant.ai.prompts import VISION_PROMPT

log = logging.getLogger(__name__)


class VisionClient:
    def __init__(self, cfg: dict) -> None:
        ai = cfg["ai"]
        self._model = ai["vision_model"]
        self._timeout = ai["timeout_seconds"]
        self._client = ollama.Client(
            host=ai["ollama_base_url"],
            timeout=self._timeout,
        )

    def describe(self, photo_path: str) -> str:
        """Send *photo_path* to the VLM and return a plant description.

        Returns a placeholder string if Ollama is unavailable or the model
        fails — the reasoning stage will still run using whatever text
        the fallback returns.
        """
        path = Path(photo_path)
        if not path.exists():
            log.warning("VisionClient: photo not found at %s — skipping VLM", path)
            return "[No photo available — visual assessment skipped.]"

        try:
            image_b64 = base64.b64encode(path.read_bytes()).decode()
            log.info("VisionClient: sending photo to %s …", self._model)
            resp = self._client.chat(
                model=self._model,
                messages=[
                    {
                        "role": "user",
                        "content": VISION_PROMPT,
                        "images": [image_b64],
                    }
                ],
            )
            description = resp.message.content.strip()
            log.info("VisionClient: got description (%d chars)", len(description))
            return description

        except Exception as exc:
            log.warning("VisionClient: Ollama error — %s", exc)
            return f"[VLM unavailable: {exc}]"
