from __future__ import annotations

from pathlib import Path

import pytest

from ynab_helper.config import CONFIG_DIR
from ynab_helper.rules_editor import append_rule


@pytest.fixture
def rules_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the real rules.yaml, so tests never touch the shipped file."""
    dest = tmp_path / "rules.yaml"
    dest.write_text((CONFIG_DIR / "rules.yaml").read_text())
    return dest


def test_append_rule_preserves_header_comments_and_formatting(rules_copy: Path) -> None:
    original = rules_copy.read_text()
    header = original.split("rules:")[0]

    append_rule(
        r"\bToddler\s+Girl\b", "Clothes/Shoes", "girl clothes", rules_path=rules_copy
    )

    updated = rules_copy.read_text()
    assert updated.startswith(header)
    assert "- pattern: '\\bToddler\\s+Girl\\b'" in updated
    assert "category: 'Clothes/Shoes'" in updated
    assert "note: 'girl clothes'" in updated
    # existing rules and their single-quoting are untouched
    assert "- pattern: '\\bdiapers?\\b" in updated
    assert "allowed_categories:" in updated


def test_append_rule_lands_before_fallback_category(rules_copy: Path) -> None:
    append_rule(r"\bexampleitem\b", "Groceries", rules_path=rules_copy)
    updated = rules_copy.read_text()
    rule_pos = updated.index("exampleitem")
    fallback_pos = updated.index("fallback_category:")
    assert rule_pos < fallback_pos


def test_append_rule_note_is_optional(rules_copy: Path) -> None:
    append_rule(r"\bexampleitem\b", "Groceries", rules_path=rules_copy)
    updated = rules_copy.read_text()
    assert "exampleitem" in updated
    # no dangling "note:" line was added for this rule
    block = updated.split("exampleitem")[0].splitlines()[-1]
    assert "note" not in block


def test_append_rule_doubles_embedded_single_quotes(rules_copy: Path) -> None:
    append_rule(r"\bJoe's\b", "Groceries", "it's for joe", rules_path=rules_copy)
    updated = rules_copy.read_text()
    assert "'\\bJoe''s\\b'" in updated
    assert "'it''s for joe'" in updated


def test_append_rule_rejects_invalid_regex(rules_copy: Path) -> None:
    with pytest.raises(ValueError):
        append_rule(r"\b(unclosed", "Groceries", rules_path=rules_copy)
    assert rules_copy.read_text() == (CONFIG_DIR / "rules.yaml").read_text()


def test_append_rule_rejects_unknown_category(rules_copy: Path) -> None:
    with pytest.raises(ValueError):
        append_rule(r"\bexampleitem\b", "Not A Real Category", rules_path=rules_copy)
    assert rules_copy.read_text() == (CONFIG_DIR / "rules.yaml").read_text()


def test_append_rule_rejects_backspace_escape(rules_copy: Path) -> None:
    with pytest.raises(ValueError):
        append_rule("\x08item\x08", "Groceries", rules_path=rules_copy)


def test_appended_rule_with_note_still_loads_through_categorizer(rules_copy: Path) -> None:
    # A distinctive pattern that can't collide with any shipped rule, so this test
    # stays valid regardless of what's already in the real rules.yaml.
    append_rule(r"\bZzyzxWidget\b", "Clothes/Shoes", "girl clothes", rules_path=rules_copy)

    import yaml

    from ynab_helper.categorizer import Categorizer
    from ynab_helper.config import load_categories
    from ynab_helper.models import LineItem

    rules_data = yaml.safe_load(rules_copy.read_text())
    categorizer = Categorizer(
        rules=rules_data["rules"],
        fallback_category=rules_data["fallback_category"],
        categories=load_categories(),
    )
    result = categorizer.categorize(LineItem(name="ZzyzxWidget Dress", quantity=1, line_total=1000))
    assert result.category_name == "Clothes/Shoes"
