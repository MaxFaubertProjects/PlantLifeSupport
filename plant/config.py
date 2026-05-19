"""Config loader.

Reads config.yaml from the project root (or a path passed explicitly).
Returns the raw dict — callers pick out the sections they need.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load(path: str | Path | None = None) -> dict[str, Any]:
    """Load and return the config dict.

    Args:
        path: Path to config.yaml.  Defaults to the project root.

    Returns:
        Parsed YAML as a plain dict.
    """
    cfg_path = Path(path) if path else _DEFAULT_PATH
    with cfg_path.open() as f:
        return yaml.safe_load(f)
