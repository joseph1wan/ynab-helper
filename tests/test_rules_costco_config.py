"""Config regression tests for config/rules_costco.yaml — mirrors
test_rules_config.py's approach of reading the real shipped file rather
than injecting fixtures, to catch a future edit reintroducing an
unanchored collision, a category typo, or the YAML \\b-escape trap."""

from __future__ import annotations

import re

from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_categories, load_rules_costco
from ynab_helper.models import LineItem


def test_shipped_costco_rules_compile_and_are_allowlisted() -> None:
    rules_data = load_rules_costco()
    rules = rules_data["rules"]
    fallback_category = rules_data["fallback_category"]
    allowed_categories = rules_data["allowed_categories"]
    categories = load_categories()

    for rule in rules:
        compiled = re.compile(rule["pattern"], re.IGNORECASE)
        assert "\x08" not in rule["pattern"], (
            f"pattern {rule['pattern']!r} contains a literal backspace — "
            "check for a double-quoted \\b in rules_costco.yaml"
        )
        del compiled
        assert rule["category"] in allowed_categories
        assert rule["category"] in categories

    assert fallback_category in allowed_categories
    assert fallback_category in categories
    for entry in allowed_categories:
        assert entry in categories


def test_shipped_costco_rules_categorize_gas_receipt_line_item() -> None:
    rules_data = load_rules_costco()
    categorizer = Categorizer(
        rules=rules_data["rules"],
        fallback_category=rules_data["fallback_category"],
        categories=load_categories(),
    )
    result = categorizer.categorize(LineItem(name="Costco Gas - Regular", quantity=1, line_total=54690))
    assert result.category_name == "Gas & Parking"
