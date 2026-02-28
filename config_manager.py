from __future__ import annotations
"""
config_manager.py

Safe atomic helpers for reading and writing config.yaml.
Used by cli.py (podcast/receiver/config commands) and mcp_server.py.
"""

import os
import tempfile
from pathlib import Path

import yaml


def load_config(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_config(path: str | Path, data: dict) -> None:
    """Atomically write config to path via temp file + rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    ) as tf:
        yaml.dump(data, tf, allow_unicode=True, default_flow_style=False, sort_keys=False)
        tmp_path = tf.name
    os.replace(tmp_path, path)
