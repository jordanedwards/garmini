"""Configuration: load a .env file into the environment (no external deps)."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(env_path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ (without overriding
    variables already set in the real environment).
    """
    path = env_path or (REPO_ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def get(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Fetch an env var, optionally raising if a required one is missing/empty."""
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required config: {name} (set it in .env)")
    return value
