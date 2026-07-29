from __future__ import annotations

from pathlib import Path

import pytest

from ynab_helper.config import CONFIG_DIR
from ynab_helper.rules_editor import append_rule, delete_rule, list_rules, reorder_rule, update_rule


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
        r"\bToddler\s+Girl\b", "Health", "health item", rules_path=rules_copy
    )

    updated = rules_copy.read_text()
    assert updated.startswith(header)
    assert "- pattern: '\\bToddler\\s+Girl\\b'" in updated
    assert "category: 'Health'" in updated
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
    append_rule(r"\bZzyzxWidget\b", "Health", "health item", rules_path=rules_copy)

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
    assert result.category_name == "Health"


def test_reorder_rule_moves_block_and_preserves_others(rules_copy: Path) -> None:
    before = list_rules(rules_path=rules_copy)
    assert len(before) >= 2

    reorder_rule(len(before) - 1, 0, rules_path=rules_copy)

    after = list_rules(rules_path=rules_copy)
    assert after[0]["pattern"] == before[-1]["pattern"]
    assert after[1]["pattern"] == before[0]["pattern"]
    assert len(after) == len(before)
    # header/comments/allowed_categories block untouched
    updated = rules_copy.read_text()
    assert "allowed_categories:" in updated
    assert "Patterns are case-insensitive" in updated


def test_reorder_rule_clamps_out_of_range_target(rules_copy: Path) -> None:
    before = list_rules(rules_path=rules_copy)
    reorder_rule(0, 9999, rules_path=rules_copy)
    after = list_rules(rules_path=rules_copy)
    assert after[-1]["pattern"] == before[0]["pattern"]
    assert len(after) == len(before)


def test_reorder_rule_rejects_out_of_range_source(rules_copy: Path) -> None:
    with pytest.raises(IndexError):
        reorder_rule(9999, 0, rules_path=rules_copy)


def test_delete_rule_removes_only_target_block(rules_copy: Path) -> None:
    before = list_rules(rules_path=rules_copy)
    target = before[-1]

    delete_rule(target["index"], rules_path=rules_copy)

    after = list_rules(rules_path=rules_copy)
    assert len(after) == len(before) - 1
    assert target["pattern"] not in [r["pattern"] for r in after]
    updated = rules_copy.read_text()
    assert "allowed_categories:" in updated


def test_delete_rule_rejects_out_of_range(rules_copy: Path) -> None:
    with pytest.raises(IndexError):
        delete_rule(9999, rules_path=rules_copy)


def test_update_rule_replaces_pattern_category_note(rules_copy: Path) -> None:
    before = list_rules(rules_path=rules_copy)
    target_index = before[-1]["index"]

    result = update_rule(
        target_index, r"\bZzyzxUpdated\b", "Health", "updated note", rules_path=rules_copy
    )

    after = list_rules(rules_path=rules_copy)
    assert after[target_index]["pattern"] == r"\bZzyzxUpdated\b"
    assert after[target_index]["category"] == "Health"
    assert after[target_index]["note"] == "updated note"
    assert len(after) == len(before)
    assert result.issues is not None


def test_update_rule_note_optional_clears_existing_note(rules_copy: Path) -> None:
    before = list_rules(rules_path=rules_copy)
    target_index = before[-1]["index"]

    update_rule(target_index, r"\bZzyzxNoNote\b", "Groceries", rules_path=rules_copy)

    after = list_rules(rules_path=rules_copy)
    assert after[target_index]["note"] is None


def test_update_rule_rejects_invalid_regex_and_leaves_file_untouched(rules_copy: Path) -> None:
    original = rules_copy.read_text()
    with pytest.raises(ValueError):
        update_rule(0, r"\b(unclosed", "Groceries", rules_path=rules_copy)
    assert rules_copy.read_text() == original


def test_update_rule_rejects_unknown_category(rules_copy: Path) -> None:
    with pytest.raises(ValueError):
        update_rule(0, r"\bexampleitem\b", "Not A Real Category", rules_path=rules_copy)


def test_update_rule_rejects_out_of_range(rules_copy: Path) -> None:
    with pytest.raises(IndexError):
        update_rule(9999, r"\bexampleitem\b", "Groceries", rules_path=rules_copy)


@pytest.fixture
def costco_rules_copy(tmp_path: Path) -> Path:
    """A throwaway copy of rules_costco.yaml, for testing the optional
    rules_data/orders params without touching the shipped file."""
    dest = tmp_path / "rules_costco.yaml"
    dest.write_text((CONFIG_DIR / "rules_costco.yaml").read_text())
    return dest


def test_append_rule_with_explicit_rules_data_targets_costco_categories(
    costco_rules_copy: Path,
) -> None:
    import yaml

    rules_data = yaml.safe_load(costco_rules_copy.read_text())

    append_rule(
        r"\bexampleitem\b",
        "Groceries",
        rules_path=costco_rules_copy,
        rules_data=rules_data,
        orders=[],
    )

    updated = costco_rules_copy.read_text()
    assert "exampleitem" in updated
    assert "Gas & Parking" in updated  # header/shipped rule untouched


def test_append_rule_with_explicit_rules_data_rejects_category_not_in_allowlist(
    costco_rules_copy: Path,
) -> None:
    import yaml

    rules_data = yaml.safe_load(costco_rules_copy.read_text())
    # Force a restricted allowlist that excludes "Chloe" — since all three
    # sources now share one unified allowed_categories list (Target,
    # Costco, PayPal), the shipped rules_costco.yaml itself no longer
    # excludes any Target category. Fabricate a narrower one here to prove
    # allowed_categories comes from the passed rules_data, not whatever the
    # file (or the default Target rules.yaml) actually contains.
    rules_data["allowed_categories"] = ["Groceries", "Gas & Parking"]

    with pytest.raises(ValueError):
        append_rule(
            r"\bexampleitem\b",
            "Chloe",
            rules_path=costco_rules_copy,
            rules_data=rules_data,
            orders=[],
        )


def test_default_target_call_sites_unaffected_by_new_optional_params(rules_copy: Path) -> None:
    """Regression check: every existing Target call site omits rules_data/orders
    and must behave exactly as before."""
    before = list_rules(rules_path=rules_copy)
    append_rule(r"\bZzyzxRegression\b", "Groceries", rules_path=rules_copy)
    after = list_rules(rules_path=rules_copy)
    assert len(after) == len(before) + 1
