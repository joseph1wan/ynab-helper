"""Local note -> category rules for the PayPal review tab (`/paypal`).

A separate file and module from rules_editor.py/rules.yaml on purpose:
rules.yaml patterns match Target line-item names and are validated by
rules_audit against cached Target orders. paypal.yaml patterns match a
PayPal note (the item description), and have no such corpus to audit
against.

Patterns match only the note/description text, never the payee name —
payee-based matching was tried and removed, since a rule should describe
what a payment was for, not who sent it.

First-match-wins, same semantics as categorizer.py's Categorizer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ynab_helper.config import CONFIG_DIR, load_paypal_categories, load_paypal_config

PAYPAL_CONFIG_PATH = CONFIG_DIR / "paypal.yaml"


@dataclass
class PaypalRule:
    pattern: str
    category: str
    note: str | None = None


def _quote(value: str) -> str:
    """Single-quote a YAML scalar, doubling embedded single quotes."""
    return "'" + value.replace("'", "''") + "'"


def _load_yaml(config_path: Path) -> dict[str, object]:
    import yaml

    with config_path.open() as f:
        return yaml.safe_load(f) or {}


def _config(config_path: Path) -> dict[str, object]:
    return load_paypal_config() if config_path == PAYPAL_CONFIG_PATH else _load_yaml(config_path)


def _compiled_rules(config_path: Path = PAYPAL_CONFIG_PATH) -> list[tuple[re.Pattern[str], str]]:
    data = _config(config_path)
    return [
        (re.compile(r["pattern"], re.IGNORECASE), r["category"])
        for r in data.get("rules", [])
    ]


def lookup(note: str | None, config_path: Path = PAYPAL_CONFIG_PATH) -> PaypalRule | None:
    """First-match the PayPal note/description against the local rules."""
    if not note:
        return None
    for pattern, category in _compiled_rules(config_path):
        if pattern.search(note):
            return PaypalRule(pattern=pattern.pattern, category=category)
    return None


def list_paypal_rules(config_path: Path = PAYPAL_CONFIG_PATH) -> list[dict[str, object]]:
    data = _config(config_path)
    return [
        {"index": i, "pattern": r.get("pattern", ""), "category": r.get("category", ""), "note": r.get("note")}
        for i, r in enumerate(data.get("rules", []))
    ]


def append_paypal_rule(
    pattern: str, category: str, note: str | None = None, config_path: Path = PAYPAL_CONFIG_PATH
) -> None:
    """Append a rule to paypal.yaml, unless an existing rule already matches
    the literal pattern text (idempotent — re-approving the same payee twice
    doesn't pile up duplicate rules)."""
    if not pattern:
        raise ValueError("Pattern is required")
    if not category:
        raise ValueError("Category is required")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    if "\x08" in pattern:
        raise ValueError(
            "Pattern contains a literal backspace character (\\x08) — "
            "this happens when \\b is typed into a double-quoted string elsewhere; "
            "re-check the pattern text"
        )

    allowed = load_paypal_categories() if config_path == PAYPAL_CONFIG_PATH else _load_yaml(config_path).get(
        "paypal_categories", []
    )
    if category not in allowed:
        raise ValueError(f"Category not in paypal_categories: {category}")

    for existing_pattern, _existing_category in _compiled_rules(config_path):
        if existing_pattern.search(pattern) or compiled.search(existing_pattern.pattern):
            return

    text = config_path.read_text()
    lines = ["  - pattern: " + _quote(pattern), "    category: " + _quote(category)]
    if note:
        lines.append("    note: " + _quote(note))
    block = "\n".join(lines) + "\n"

    marker = "rules: []"
    if marker in text:
        new_text = text.replace(marker, "rules:\n" + block.rstrip("\n"), 1)
    elif "\nrules:\n" in text:
        new_text = text.rstrip("\n") + "\n" + block
    else:
        raise ValueError("Could not find 'rules:' in paypal.yaml to anchor the insert")
    config_path.write_text(new_text if new_text.endswith("\n") else new_text + "\n")
