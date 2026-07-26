"""Config regression tests: unlike other test files, these deliberately read the
real config/rules.yaml and data/target-orders/ rather than injecting fixtures.
They exist to catch the exact class of bug this tooling was built to prevent —
a future rules.yaml edit silently reintroducing an unanchored collision or a
category typo."""

from __future__ import annotations

import re

import pytest

from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_categories, load_rules, resolve_path
from ynab_helper.rules_audit import build_report
from ynab_helper.target_scraper import load_cached_orders
from datetime import date


def test_shipped_rules_compile_and_are_allowlisted() -> None:
    rules_data = load_rules()
    rules = rules_data["rules"]
    fallback_category = rules_data["fallback_category"]
    allowed_categories = rules_data["allowed_categories"]
    categories = load_categories()

    for rule in rules:
        compiled = re.compile(rule["pattern"], re.IGNORECASE)
        assert "\x08" not in rule["pattern"], (
            f"pattern {rule['pattern']!r} contains a literal backspace — "
            "check for a double-quoted \\b in rules.yaml"
        )
        del compiled
        assert rule["category"] in allowed_categories
        assert rule["category"] in categories

    assert fallback_category in allowed_categories
    assert fallback_category in categories
    for entry in allowed_categories:
        assert entry in categories


@pytest.mark.parametrize(
    "name,expected_category",
    [
        (
            "Paul Mitchell Two Hair Shampoo - 10.14 fl oz: Shine Enhancing, "
            "Clarifying, For Oily & All Hair Types, Liquid Form",
            "Personal Care",
        ),
        (
            "Basic Folding Outdoor Portable Camping Chair Green - All In Motion™",
            "Home Decor",
        ),
        (
            "OFF! Clean Feel Bug Spray & Mosquito Repellent with Picaridin "
            "DEET-Free Formula - 5oz",
            "Home Supplies",
        ),
        (
            "Similac 360 Total Care Gentle Comfort Powder Infant Formula - 29.8oz",
            "Nathanael",
        ),
        (
            "Uncured Black Forest Ham Ultra-Thin Deli Slices - 9oz - Good & Gather™",
            "Groceries",
        ),
    ],
)
def test_shipped_rules_categorize_known_items(name: str, expected_category: str) -> None:
    from ynab_helper.models import LineItem

    rules_data = load_rules()
    categorizer = Categorizer(
        rules=rules_data["rules"],
        fallback_category=rules_data["fallback_category"],
        categories=load_categories(),
    )
    result = categorizer.categorize(LineItem(name=name, quantity=1, line_total=1000))
    assert result.category_name == expected_category


def test_shipped_rules_have_no_collisions_on_cached_orders() -> None:
    orders_dir = resolve_path("data/target-orders")
    orders = load_cached_orders(orders_dir, date.min)
    if not orders:
        pytest.skip("no cached Target orders available")

    rules_data = load_rules()
    report = build_report(
        orders,
        rules_data["rules"],
        rules_data["fallback_category"],
        load_categories(),
        rules_data["allowed_categories"],
    )

    collisions = [item for item in report.matched if item.collisions]
    assert collisions == [], (
        "unanchored/overlapping rules found a real collision — run "
        "`uv run ynab-helper audit-rules` to see details"
    )
    assert not any(issue.severity == "error" for issue in report.issues)
