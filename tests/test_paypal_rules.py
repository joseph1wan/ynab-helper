from __future__ import annotations

from pathlib import Path

import pytest

from ynab_helper.paypal_rules import append_paypal_rule, list_paypal_rules, lookup

CONFIG_TEMPLATE = """# comment
paypal_categories:
  - 'Treating Others'
  - 'Charity'
  - 'Tithe'
rules: []
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "paypal.yaml"
    path.write_text(CONFIG_TEMPLATE)
    return path


def test_append_and_lookup_by_note(config_path: Path) -> None:
    append_paypal_rule("meal gift", "Treating Others", config_path=config_path)

    rule = lookup("Thanks for the meal gift!", config_path=config_path)
    assert rule is not None
    assert rule.category == "Treating Others"


def test_lookup_does_not_match_payee(config_path: Path) -> None:
    append_paypal_rule("scholarship", "Charity", config_path=config_path)

    assert lookup("Gave full scholarship", config_path=config_path) is not None
    assert lookup("unrelated note", config_path=config_path) is None


def test_first_match_wins(config_path: Path) -> None:
    append_paypal_rule("Mai Ye reimbursement", "Charity", config_path=config_path)
    append_paypal_rule("reimbursement", "Tithe", config_path=config_path)

    rule = lookup("Mai Ye reimbursement", config_path=config_path)
    assert rule.category == "Charity"


def test_append_is_idempotent_for_overlapping_pattern(config_path: Path) -> None:
    append_paypal_rule("scholarship", "Charity", config_path=config_path)
    append_paypal_rule("scholarship", "Charity", config_path=config_path)

    assert len(list_paypal_rules(config_path)) == 1


def test_backslash_b_survives_yaml_round_trip(config_path: Path) -> None:
    append_paypal_rule(r"\bgift\b", "Charity", config_path=config_path)

    text = config_path.read_text()
    assert "\\x08" not in text
    rule = lookup("happy birthday gift", config_path=config_path)
    assert rule is not None


def test_rejects_category_outside_allowlist(config_path: Path) -> None:
    with pytest.raises(ValueError):
        append_paypal_rule("Someone", "Not Allowed", config_path=config_path)


def test_rejects_invalid_regex(config_path: Path) -> None:
    with pytest.raises(ValueError):
        append_paypal_rule("(unclosed", "Charity", config_path=config_path)
