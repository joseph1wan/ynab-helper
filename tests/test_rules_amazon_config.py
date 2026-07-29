"""Config regression tests for config/rules_amazon.yaml — mirrors
test_rules_costco_config.py's approach of reading the real shipped file
rather than injecting fixtures, to catch a future edit reintroducing an
unanchored collision, a category typo, or the YAML \\b-escape trap."""

from __future__ import annotations

import re

import yaml

from ynab_helper.config import CONFIG_DIR, load_categories, load_rules_amazon


def test_shipped_amazon_rules_compile_and_are_allowlisted() -> None:
    rules_data = load_rules_amazon()
    rules = rules_data["rules"]
    fallback_category = rules_data["fallback_category"]
    allowed_categories = rules_data["allowed_categories"]
    categories = load_categories()

    for rule in rules:
        compiled = re.compile(rule["pattern"], re.IGNORECASE)
        assert "\x08" not in rule["pattern"], (
            f"pattern {rule['pattern']!r} contains a literal backspace — "
            "check for a double-quoted \\b in rules_amazon.yaml"
        )
        del compiled
        assert rule["category"] in allowed_categories
        assert rule["category"] in categories

    assert fallback_category in allowed_categories
    assert fallback_category in categories
    for entry in allowed_categories:
        assert entry in categories


def test_amazon_allowlist_matches_other_sources() -> None:
    amazon_categories = set(load_rules_amazon()["allowed_categories"])
    rules_yaml = yaml.safe_load((CONFIG_DIR / "rules.yaml").read_text())
    rules_costco_yaml = yaml.safe_load((CONFIG_DIR / "rules_costco.yaml").read_text())
    paypal_yaml = yaml.safe_load((CONFIG_DIR / "paypal.yaml").read_text())

    assert amazon_categories == set(rules_yaml["allowed_categories"])
    assert amazon_categories == set(rules_costco_yaml["allowed_categories"])
    assert amazon_categories == set(paypal_yaml["paypal_categories"])
