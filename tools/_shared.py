"""Shared helpers for tools. Load env, resolve paths, standard I/O.

Deterministic layer. Tools import from here so env-loading and path
resolution stay consistent across scripts.
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dependency not installed yet
    load_dotenv = None

# Project root = parent of tools/
ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
ENV_PATH = ROOT / ".env"


def load_env() -> None:
    """Load .env from project root. No-op if python-dotenv missing."""
    if load_dotenv is not None and ENV_PATH.exists():
        load_dotenv(ENV_PATH)


def require_env(key: str) -> str:
    """Return env var or raise clear error naming the missing key."""
    load_env()
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}. Set it in {ENV_PATH}")
    return val


def tmp_path(name: str) -> Path:
    """Path inside .tmp/. Creates .tmp/ if absent. Files here are disposable."""
    TMP.mkdir(exist_ok=True)
    return TMP / name
