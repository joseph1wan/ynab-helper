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


def list_rules(rules_path: Path = RULES_PATH) -> list[dict[str, Any]]:
    """Return every rule with its file-order index (index 0 = highest priority)."""
    import yaml

    rules_data = yaml.safe_load(rules_path.read_text()) or {}
    return [
        {
            "index": i,
            "pattern": r.get("pattern", ""),
            "category": r.get("category", ""),
            "note": r.get("note"),
        }
        for i, r in enumerate(rules_data.get("rules", []))
    ]


def _split_rule_blocks(text: str) -> tuple[str, list[str], str]:
    """Split rules.yaml's raw text into (header, per-rule blocks, footer).

    header is everything up to the first rule; footer starts at
    'fallback_category:' (inclusive) through the end of the file. Each block
    runs from its own "  - pattern:" line up to (but not including) the next
    one, so re-joining header + blocks + footer verbatim reproduces the
    original file byte-for-byte, and reordering/dropping/replacing blocks
    never disturbs any other rule's comments or formatting.
    """
    marker = "  - pattern:"
    footer_marker = "\nfallback_category:"

    footer_idx = text.find(footer_marker)
    if footer_idx == -1:
        raise ValueError("Could not find fallback_category: in rules.yaml")
    footer = text[footer_idx + 1 :]
    body = text[: footer_idx + 1]

    first_idx = body.find(marker)
    if first_idx == -1:
        raise ValueError("Could not find any '  - pattern:' rule entries in rules.yaml")
    header = body[:first_idx]

    positions = []
    start = first_idx
    while start != -1:
        positions.append(start)
        start = body.find(marker, start + 1)

    blocks = [
        body[positions[i] : positions[i + 1] if i + 1 < len(positions) else len(body)]
        for i in range(len(positions))
    ]
    return header, blocks, footer


def reorder_rule(from_index: int, to_index: int, rules_path: Path = RULES_PATH) -> None:
    """Move the rule at from_index to to_index, preserving every block's formatting."""
    header, blocks, footer = _split_rule_blocks(rules_path.read_text())
    if from_index < 0 or from_index >= len(blocks):
        raise IndexError("Rule index out of range")
    to_index = max(0, min(to_index, len(blocks) - 1))

    block = blocks.pop(from_index)
    blocks.insert(to_index, block)

    rules_path.write_text(header + "".join(blocks) + footer)


def delete_rule(index: int, rules_path: Path = RULES_PATH) -> None:
    """Remove the rule at index, preserving every other block's formatting."""
    header, blocks, footer = _split_rule_blocks(rules_path.read_text())
    if index < 0 or index >= len(blocks):
        raise IndexError("Rule index out of range")

    blocks.pop(index)
    rules_path.write_text(header + "".join(blocks) + footer)


def update_rule(
    index: int,
    pattern: str,
    category: str,
    note: str | None = None,
    rules_path: Path = RULES_PATH,
) -> RuleAppendResult:
    """Replace the rule at index, validating the resulting rule set like append_rule."""
    rules_data = load_rules()
    rules = rules_data.get("rules", [])
    fallback_category = rules_data.get("fallback_category", "Shopping")

    if index < 0 or index >= len(rules):
        raise IndexError("Rule index out of range")

    other_rules = rules[:index] + rules[index + 1 :]
    issues = _validate_new_rule(pattern, category, other_rules, fallback_category)

    orders_dir = resolve_path("data/target-orders")
    orders = load_cached_orders(orders_dir, date.min)
    candidate_rules = [*other_rules, {"pattern": pattern, "category": category}]
    matched, _fallback, _suspect = audit_orders(orders, candidate_rules, fallback_category)
    new_pattern_index = len(other_rules)
    collisions = [
        item.name
        for item in matched
        if item.winner is not None and item.winner.rule.index == new_pattern_index
    ]

    header, blocks, footer = _split_rule_blocks(rules_path.read_text())
    if index >= len(blocks):
        raise IndexError("Rule index out of range")

    lines = ["  - pattern: " + _quote(pattern), "    category: " + _quote(category)]
    if note:
        lines.append("    note: " + _quote(note))
    blocks[index] = "\n".join(lines) + "\n"

    rules_path.write_text(header + "".join(blocks) + footer)

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
