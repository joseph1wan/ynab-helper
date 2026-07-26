from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ynab_helper.config import CONFIG_DIR, load_categories, load_rules, resolve_path
from ynab_helper.rules_audit import ValidationIssue, audit_orders, validate_rules
from ynab_helper.target_scraper import load_cached_orders

RULES_PATH = CONFIG_DIR / "rules.yaml"


@dataclass
class RuleAppendResult:
    issues: list[ValidationIssue]
    collisions: list[str]  # item names that now collide with an earlier rule


def _quote(value: str) -> str:
    """Single-quote a YAML scalar, doubling embedded single quotes."""
    return "'" + value.replace("'", "''") + "'"


def _validate_new_rule(
    pattern: str, category: str, rules: list[dict[str, str]], fallback_category: str
) -> list[ValidationIssue]:
    if not pattern:
        raise ValueError("Pattern is required")
    if not category:
        raise ValueError("Category is required")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regex: {exc}") from exc
    if "\x08" in pattern:
        raise ValueError(
            "Pattern contains a literal backspace character (\\x08) — "
            "this happens when \\b is typed into a double-quoted string elsewhere; "
            "re-check the pattern text"
        )

    categories = load_categories()
    allowed_categories = load_rules().get("allowed_categories", [])
    if category not in allowed_categories:
        raise ValueError(f"Category not in allowed_categories: {category}")
    if category not in categories:
        raise ValueError(f"Category not known to YNAB: {category}")

    candidate_rules = [*rules, {"pattern": pattern, "category": category}]
    issues = validate_rules(candidate_rules, fallback_category, categories, allowed_categories)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        raise ValueError(
            "New rule fails validation: " + "; ".join(i.message for i in errors)
        )
    return issues


def preview_rule(pattern: str, category: str) -> RuleAppendResult:
    """Validate a candidate rule and report collisions, without writing anything."""
    rules_data = load_rules()
    rules = rules_data.get("rules", [])
    fallback_category = rules_data.get("fallback_category", "Shopping")

    issues = _validate_new_rule(pattern, category, rules, fallback_category)

    orders_dir = resolve_path("data/target-orders")
    orders = load_cached_orders(orders_dir, date.min)
    candidate_rules = [*rules, {"pattern": pattern, "category": category}]
    matched, _fallback, _suspect = audit_orders(orders, candidate_rules, fallback_category)
    new_pattern_index = len(rules)
    collisions = [
        item.name
        for item in matched
        if item.winner is not None and item.winner.rule.index == new_pattern_index
    ]

    return RuleAppendResult(issues=issues, collisions=collisions)


def append_rule(
    pattern: str, category: str, note: str | None = None, rules_path: Path = RULES_PATH
) -> RuleAppendResult:
    """Append a new rule to config/rules.yaml, preserving comments and formatting.

    Validates the pattern/category (compiles, single-quote-safe, allowlisted) and
    refuses on any error-severity issue from the shared rules_audit validator.
    Returns collisions (items that already matched an earlier rule) as a warning,
    but does not block on them.
    """
    rules_data = load_rules()
    rules = rules_data.get("rules", [])
    fallback_category = rules_data.get("fallback_category", "Shopping")

    issues = _validate_new_rule(pattern, category, rules, fallback_category)

    orders_dir = resolve_path("data/target-orders")
    orders = load_cached_orders(orders_dir, date.min)
    candidate_rules = [*rules, {"pattern": pattern, "category": category}]
    matched, _fallback, _suspect = audit_orders(orders, candidate_rules, fallback_category)
    new_pattern_index = len(rules)
    collisions = [
        item.name
        for item in matched
        if item.winner is not None and item.winner.rule.index == new_pattern_index
    ]

    text = rules_path.read_text()
    lines = ["  - pattern: " + _quote(pattern), "    category: " + _quote(category)]
    if note:
        lines.append("    note: " + _quote(note))
    block = "\n".join(lines) + "\n"

    marker = "\nfallback_category:"
    idx = text.find(marker)
    if idx == -1:
        raise ValueError("Could not find fallback_category: in rules.yaml to anchor the insert")
    new_text = text[: idx + 1] + block + text[idx + 1 :]
    rules_path.write_text(new_text)

    return RuleAppendResult(issues=issues, collisions=collisions)
