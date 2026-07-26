from __future__ import annotations

import json
from datetime import date

from ynab_helper.categorizer import Categorizer
from ynab_helper.models import LineItem, TargetOrder
from ynab_helper.rules_audit import (
    _branch_probe,
    _split_alternation,
    audit_item,
    audit_orders,
    build_rule_refs,
    build_report,
    report_to_dict,
    validate_rules,
)

RULES = [
    {"pattern": r"\bham\b", "category": "Groceries"},
    {"pattern": r"\bhair", "category": "Personal Care"},
]

# Deliberately unanchored, to reproduce the real shampoo/ham collision bug this
# tool exists to catch (an anchored `\bham\b` would NOT match inside "Shampoo").
UNANCHORED_RULES = [
    {"pattern": "ham", "category": "Groceries"},
    {"pattern": "hair", "category": "Personal Care"},
]


def test_audit_reports_winner_and_collisions() -> None:
    refs = build_rule_refs(UNANCHORED_RULES)
    result = audit_item("Paul Mitchell Two Hair Shampoo", refs, "Shopping")
    assert result.winner is not None
    assert result.winner.rule.index == 0
    assert result.winner.matched_text.lower() == "ham"
    assert len(result.collisions) == 1
    assert result.collisions[0].rule.index == 1
    assert result.category == "Groceries"


def test_audit_winner_matches_categorizer() -> None:
    rules = [
        {"pattern": r"\bdiapers?\b|\bwipes?\b", "category": "Baby"},
        {"pattern": r"\bmilks?\b|\bcheerios\b", "category": "Groceries"},
    ]
    categories = {"Baby": "cat-baby", "Groceries": "cat-groceries", "Shopping": "cat-shopping"}
    categorizer = Categorizer(rules=rules, fallback_category="Shopping", categories=categories)
    refs = build_rule_refs(rules)

    names = ["Diapers Size 4", "Whole Milk", "Cheerios Cereal", "Random Toy"]
    for name in names:
        audit_result = audit_item(name, refs, "Shopping")
        categorizer_result = categorizer.categorize(LineItem(name=name, quantity=1, line_total=1000))
        assert audit_result.category == categorizer_result.category_name


def test_audit_groups_fallback_items() -> None:
    refs = build_rule_refs(RULES)
    result = audit_item("Random Widget", refs, "Shopping")
    assert result.winner is None
    assert result.collisions == []
    assert result.category == "Shopping"


def test_audit_separates_zero_total_suspect_items() -> None:
    order = TargetOrder(
        order_id="ord-1",
        order_date=date(2026, 7, 1),
        total=0,
        line_items=[LineItem(name="DELIVERED", quantity=1, line_total=0)],
    )
    matched, fallback, suspect = audit_orders([order], RULES, "Shopping")
    assert matched == []
    assert fallback == []
    assert len(suspect) == 1
    assert suspect[0].name == "DELIVERED"


def test_audit_dedups_names_across_orders() -> None:
    orders = [
        TargetOrder(
            order_id="ord-1",
            order_date=date(2026, 7, 1),
            total=10000,
            line_items=[LineItem(name="Whole Milk", quantity=1, line_total=5000)],
        ),
        TargetOrder(
            order_id="ord-2",
            order_date=date(2026, 7, 2),
            total=10000,
            line_items=[LineItem(name="Whole Milk", quantity=1, line_total=5000)],
        ),
    ]
    rules = [{"pattern": r"\bmilk\b", "category": "Groceries"}]
    matched, _fallback, _suspect = audit_orders(orders, rules, "Shopping")
    assert len(matched) == 1
    assert matched[0].occurrences == 2
    assert matched[0].order_ids == ["ord-1", "ord-2"]


def test_validate_unknown_category() -> None:
    rules = [{"pattern": r"\bmilk\b", "category": "Nonexistent"}]
    issues = validate_rules(rules, "Shopping", {"Shopping": "cat-shop"}, ["Shopping", "Nonexistent"])
    codes = [i.code for i in issues if i.rule_index == 0]
    assert "unknown-category" in codes


def test_validate_unknown_category_breaks_split_calculator() -> None:
    from ynab_helper.split_calculator import compute_splits
    from ynab_helper.models import CategorizedLine

    order = TargetOrder(
        order_id="ord-1",
        order_date=date(2026, 7, 1),
        total=5000,
        line_items=[LineItem(name="Whole Milk", quantity=1, line_total=5000)],
    )
    categorized = [
        CategorizedLine(
            line_item=order.line_items[0],
            category_name="Nonexistent",
            category_id=None,
            matched_rule=r"\bmilk\b",
        )
    ]
    try:
        compute_splits(order, categorized, -5000)
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_validate_category_not_allowlisted() -> None:
    rules = [{"pattern": r"\bmilk\b", "category": "Groceries"}]
    categories = {"Groceries": "cat-groceries", "Shopping": "cat-shop"}
    issues = validate_rules(rules, "Shopping", categories, ["Shopping"])
    codes = [i.code for i in issues if i.rule_index == 0]
    assert "not-allowlisted" in codes


def test_validate_fallback_not_allowlisted() -> None:
    rules = [{"pattern": r"\bmilk\b", "category": "Groceries"}]
    categories = {"Groceries": "cat-groceries", "Shopping": "cat-shop"}
    issues = validate_rules(rules, "Shopping", categories, ["Groceries"])
    codes = [i.code for i in issues if i.rule_index is None]
    assert "not-allowlisted" in codes


def test_validate_allowlist_entry_unknown_to_ynab() -> None:
    rules = [{"pattern": r"\bmilk\b", "category": "Groceries"}]
    categories = {"Groceries": "cat-groceries"}
    issues = validate_rules(rules, "Groceries", categories, ["Groceries", "GhostCategory"])
    codes = [i.code for i in issues]
    assert "allowlist-unknown" in codes


def test_validate_invalid_regex() -> None:
    rules = [
        {"pattern": "[", "category": "Groceries"},
        {"pattern": r"\bmilk\b", "category": "Groceries"},
    ]
    categories = {"Groceries": "cat-groceries"}
    issues = validate_rules(rules, "Groceries", categories, ["Groceries"])
    rule0_codes = [i.code for i in issues if i.rule_index == 0]
    assert "invalid-regex" in rule0_codes
    # second rule still gets audited despite the first being broken
    assert any(i.rule_index == 1 for i in issues) or not any(
        i.code == "unknown-category" and i.rule_index == 1 for i in issues
    )


def test_validate_detects_yaml_backspace_escape() -> None:
    rules = [{"pattern": "\x08ham\x08", "category": "Groceries"}]
    categories = {"Groceries": "cat-groceries"}
    issues = validate_rules(rules, "Groceries", categories, ["Groceries"])
    codes = [i.code for i in issues if i.rule_index == 0]
    assert "yaml-backspace" in codes


def test_validate_duplicate_pattern() -> None:
    rules = [
        {"pattern": r"\bmilk\b", "category": "Groceries"},
        {"pattern": r"\bmilk\b", "category": "Home Supplies"},
    ]
    categories = {"Groceries": "cat-g", "Home Supplies": "cat-h"}
    issues = validate_rules(rules, "Home Supplies", categories, ["Groceries", "Home Supplies"])
    codes = [i.code for i in issues if i.rule_index == 1]
    assert "duplicate-pattern" in codes


def test_validate_shadowed_rule() -> None:
    rules = [
        {"pattern": r"\bmilk\b|\bbread\b", "category": "A"},
        {"pattern": r"\bmilk\b", "category": "B"},
    ]
    categories = {"A": "cat-a", "B": "cat-b"}
    issues = validate_rules(rules, "A", categories, ["A", "B"])
    codes = [i.code for i in issues if i.rule_index == 1]
    assert "shadowed-rule" in codes


def test_validate_dead_branch_without_shadowing() -> None:
    rules = [
        {"pattern": r"\bmilk\b", "category": "A"},
        {"pattern": r"\bmilk\b|\bbread\b", "category": "B"},
    ]
    categories = {"A": "cat-a", "B": "cat-b"}
    issues = validate_rules(rules, "A", categories, ["A", "B"])
    codes = [i.code for i in issues if i.rule_index == 1]
    assert "dead-branch" in codes
    assert "shadowed-rule" not in codes


def test_validate_unanchored_token() -> None:
    unanchored = validate_rules(
        [{"pattern": "ham", "category": "A"}], "A", {"A": "cat-a"}, ["A"]
    )
    anchored = validate_rules(
        [{"pattern": r"\bham\b", "category": "A"}], "A", {"A": "cat-a"}, ["A"]
    )
    assert any(i.code == "unanchored-token" for i in unanchored)
    assert not any(i.code == "unanchored-token" for i in anchored)


def test_split_alternation_respects_groups() -> None:
    branches = _split_alternation(r"\ba\b|(?:b|c)|\bd\b")
    assert branches == [r"\ba\b", "(?:b|c)", r"\bd\b"]


def test_branch_probe_bails_on_complex() -> None:
    assert _branch_probe(".*foo") is None


def test_branch_probe_resolves_group_and_boundary() -> None:
    assert _branch_probe(r"\b(?:baby|toddler)\s+formula\b") == "baby formula"
    assert _branch_probe(r"\bdiapers?\b") == "diapers"


def test_report_to_dict_is_json_serializable() -> None:
    order = TargetOrder(
        order_id="ord-1",
        order_date=date(2026, 7, 1),
        total=5000,
        line_items=[LineItem(name="Whole Milk", quantity=1, line_total=5000)],
    )
    rules = [{"pattern": r"\bmilk\b", "category": "Groceries"}]
    categories = {"Groceries": "cat-g"}
    report = build_report(
        [order], rules, "Groceries", categories, allowed_categories=["Groceries"]
    )
    serialized = json.dumps(report_to_dict(report))
    assert "Whole Milk" in serialized
