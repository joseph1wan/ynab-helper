from __future__ import annotations

from datetime import date

import pytest

from ynab_helper.categorizer import Categorizer
from ynab_helper.costco_matcher import match_costco_orders_to_transactions
from ynab_helper.models import CostcoOrder, LineItem, YnabTransaction
from ynab_helper.split_calculator import compute_splits


@pytest.fixture
def categories() -> dict[str, str]:
    return {
        "Gas & Parking": "cat-gas",
        "Groceries": "cat-groceries",
    }


@pytest.fixture
def categorizer(categories: dict[str, str]) -> Categorizer:
    return Categorizer(
        rules=[{"pattern": r"^Costco Gas", "category": "Gas & Parking"}],
        fallback_category="Groceries",
        categories=categories,
    )


def _order(**overrides) -> CostcoOrder:
    defaults = dict(
        receipt_id="774_2026-07-16_16397",
        receipt_date=date(2026, 7, 16),
        total=54690,
        line_items=[LineItem(name="Costco Gas - Regular", quantity=1, line_total=54690)],
        receipt_type="gas",
        store_number="774",
        transaction_number="16397",
    )
    defaults.update(overrides)
    return CostcoOrder(**defaults)


def test_costco_order_subtotal_and_order_id_alias() -> None:
    order = _order(
        line_items=[
            LineItem(name="A", quantity=1, line_total=1000),
            LineItem(name="B", quantity=1, line_total=2000),
        ]
    )
    assert order.subtotal == 3000
    assert order.order_id == order.receipt_id


def test_compute_splits_reused_unmodified_for_costco_order(categorizer: Categorizer) -> None:
    order = _order(
        total=50000,
        line_items=[
            LineItem(name="Costco Gas - Regular", quantity=1, line_total=30000),
            LineItem(name="Milk", quantity=1, line_total=20000),
        ],
        tax=1000,
    )
    categorized, _ = categorizer.categorize_all(order.line_items)
    splits, delta = compute_splits(order, categorized, -50000)
    assert sum(s.amount for s in splits) == -50000
    assert len(splits) == 2
    assert delta == 0


def test_match_costco_orders_to_transactions_exact_date_amount(categorizer: Categorizer) -> None:
    order = _order()
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 16),
        amount=-54690,
        payee_name="COSTCO GAS #774",
        category_id=None,
        memo=None,
        account_id="acct-sapphire",
        cleared="cleared",
        approved=False,
    )
    proposals, unmatched_orders, unmatched_txns = match_costco_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 1
    assert len(unmatched_orders) == 0
    assert len(unmatched_txns) == 0
    assert proposals[0].costco_order is order
    assert proposals[0].categorized_lines[0].category_name == "Gas & Parking"


def test_match_costco_orders_to_transactions_no_match_when_amount_differs(
    categorizer: Categorizer,
) -> None:
    order = _order()
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 16),
        amount=-99999,
        payee_name="COSTCO GAS #774",
        category_id=None,
        memo=None,
        account_id="acct-sapphire",
        cleared="cleared",
        approved=False,
    )
    proposals, unmatched_orders, unmatched_txns = match_costco_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 0
    assert len(unmatched_orders) == 1
    assert len(unmatched_txns) == 1
