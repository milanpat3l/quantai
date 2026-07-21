"""
config.py
=========
Tiny, dependency-free configuration loader for the OI-PCR toolkit.

- Loads a local ``.env`` file (KEY=value per line) into ``os.environ`` WITHOUT
  overwriting variables already set in the real environment. This keeps the
  Upstox token out of source code and out of the shell history.
- ``.env`` is git-ignored. Never commit a real token.

Recognised variables (see ``.env.example``):
    UPSTOX_ACCESS_TOKEN   Upstox Analytics Token: read-only, ~1yr validity,
                          generated from the Developer Apps "Analytics" tab.
                          Required for any live data pull.
    OI_PCR_DATA_DIR       where Parquet is written  (default ./data)
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(os.environ.get("OI_PCR_ENV_FILE", ".env"))


def load_env(path: Path | str = ENV_PATH, *, override: bool = False) -> dict[str, str]:
    """Parse a ``.env`` file into ``os.environ``.

    Real environment variables win over the file unless ``override=True``.
    Returns the parsed key/value pairs (values are NOT logged).
    """
    path = Path(path)
    parsed: dict[str, str] = {}
    if not path.exists():
        return parsed
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # quotes are stored literally elsewhere; strip a single matching pair here
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        parsed[key] = val
        if override or key not in os.environ:
            os.environ[key] = val
    return parsed


def require(name: str) -> str:
    """Return an env var or exit with a clear, non-leaking message."""
    load_env()
    val = os.environ.get(name)
    if not val:
        raise SystemExit(
            f"ERROR: {name} is not set. Add it to your environment or .env "
            f"(see .env.example). Never hard-code secrets."
        )
    return val
