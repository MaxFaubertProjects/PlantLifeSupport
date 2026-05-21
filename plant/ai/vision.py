"""VLM client — image → plant description text.

Sends the plant photo to Ollama and returns a plain-text description.
Falls back gracefully if Ollama is unavailable.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path

import ollama
from PIL import Image

from plant.ai.prompts import VISION_PROMPT

log = logging.getLogger(__name__)

# Moondream downsamples internally; sending the full 5MP capture wastes RAM
# and time. Shrink the longest side to this before encoding.
VLM_IMAGE_MAX_SIDE = 640


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
            image_b64 = _encode_resized(path)
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


def _encode_resized(path: Path) -> str:
    img = Image.open(path)
    img.thumbnail((VLM_IMAGE_MAX_SIDE, VLM_IMAGE_MAX_SIDE))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    log.debug("VisionClient: resized image %dx%d (%d KB)",
             img.width, img.height, buf.tell() // 1024)
    return base64.b64encode(buf.getvalue()).decode()
