from __future__ import annotations

from datetime import date

import pytest

from ynab_helper.amazon_matcher import match_amazon_orders_to_transactions
from ynab_helper.categorizer import Categorizer
from ynab_helper.models import AmazonOrder, LineItem, YnabTransaction
from ynab_helper.split_calculator import compute_splits


@pytest.fixture
def categories() -> dict[str, str]:
    return {
        "Home Supplies": "cat-home",
        "One Offs": "cat-one-offs",
    }


@pytest.fixture
def categorizer(categories: dict[str, str]) -> Categorizer:
    return Categorizer(
        rules=[{"pattern": r"storage bin", "category": "Home Supplies"}],
        fallback_category="One Offs",
        categories=categories,
    )


def _order(**overrides) -> AmazonOrder:
    defaults = dict(
        order_id="111-1239029-5887460",
        order_date=date(2026, 7, 24),
        total=30730,
        line_items=[LineItem(name="Battery", quantity=1, line_total=6780)],
        tax=2010,
    )
    defaults.update(overrides)
    return AmazonOrder(**defaults)


def test_amazon_order_subtotal() -> None:
    order = _order(
        line_items=[
            LineItem(name="A", quantity=1, line_total=1000),
            LineItem(name="B", quantity=2, line_total=2000),
        ]
    )
    assert order.subtotal == 3000


def test_compute_splits_reused_unmodified_for_amazon_order(categorizer: Categorizer) -> None:
    order = _order(
        total=30000,
        line_items=[
            LineItem(name="Storage Bin", quantity=1, line_total=15000),
            LineItem(name="Misc Widget", quantity=1, line_total=15000),
        ],
        tax=0,
    )
    categorized, _ = categorizer.categorize_all(order.line_items)
    splits, delta = compute_splits(order, categorized, -30000)
    assert sum(s.amount for s in splits) == -30000
    assert len(splits) == 2
    assert delta == 0


def test_match_amazon_orders_to_transactions_within_window(categorizer: Categorizer) -> None:
    order = _order()
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 26),  # 2 days after order date, within +/-3 window
        amount=-30730,
        payee_name="AMAZON.COM",
        category_id=None,
        memo=None,
        account_id="acct-visa",
        cleared="cleared",
        approved=False,
    )
    proposals, unmatched_orders, unmatched_txns = match_amazon_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 1
    assert len(unmatched_orders) == 0
    assert len(unmatched_txns) == 0
    assert proposals[0].amazon_order is order


def test_match_amazon_orders_to_transactions_no_match_when_amount_differs(
    categorizer: Categorizer,
) -> None:
    order = _order()
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 24),
        amount=-99999,
        payee_name="AMAZON.COM",
        category_id=None,
        memo=None,
        account_id="acct-visa",
        cleared="cleared",
        approved=False,
    )
    proposals, unmatched_orders, unmatched_txns = match_amazon_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 0
    assert len(unmatched_orders) == 1
    assert len(unmatched_txns) == 1


def test_match_amazon_orders_to_transactions_no_match_outside_window(
    categorizer: Categorizer,
) -> None:
    order = _order()
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 8, 1),  # far outside +/-3 day window
        amount=-30730,
        payee_name="AMAZON.COM",
        category_id=None,
        memo=None,
        account_id="acct-visa",
        cleared="cleared",
        approved=False,
    )
    proposals, unmatched_orders, unmatched_txns = match_amazon_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 0
    assert len(unmatched_orders) == 1
