from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")

        def replacer(match: re.Match[str]) -> str:
            key = match.group(1)
            return os.environ.get(key, "")

        return pattern.sub(replacer, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    return _expand_env(data)


def load_config() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "config.yaml")


def load_rules() -> dict[str, Any]:
    return load_yaml(CONFIG_DIR / "rules.yaml")


def load_categories() -> dict[str, str]:
    path = CONFIG_DIR / "categories.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run: ynab-helper sync-categories"
        )
    import json

    with path.open() as f:
        return json.load(f)


def resolve_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute():
        return path
    return ROOT / path
