from __future__ import annotations

from datetime import date

import pytest

from ynab_helper.amazon_invoice_text import parse_invoice_text
from ynab_helper.amazon_matcher import match_amazon_orders_to_transactions
from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_categories, load_rules_amazon
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


_MULTI_ITEM_INVOICE_TEXT = """\
Order Summary
Order placed July 29, 2026  Order # 113-5883076-6605868

Order Summary

* Item(s) Subtotal:
$26.25
* Shipping & Handling:
$2.99
* Free Shipping:
-$2.99
* Total before tax:
$26.25
* Estimated tax to be collected:
$1.92
* Grand Total:
$28.17

Delivered July 30
Your package was left near the front door or porch.
[Simple Joys by Carter's Toddler Girls' 4-Piece Long-Sleeve Shirts and Pants Playwear Set, Grey Love/Pink/White Floral/Yellow Dots, 5T](https://www.amazon.com/dp/B083X7F34Z)
Sold by: Amazon.com
Supplied by: Other
Return or replace items: Eligible through October 28, 2026
$15.73
2
[De Cecco Acini Di Pepe No. 78 Pasta, 16 Oz, Authentic, Slow Dried, Made with Semolina and Durum Wheat, Versatile Pasta for Sauces & Recipes, Made in Italy](https://www.amazon.com/dp/B004NPNRYO)
Sold by: Amazon.com
Supplied by: Other
Return items: Eligible through August 29, 2026
$2.77
[Nuby Nananubs Banana Massaging Teether](https://www.amazon.com/dp/B01LYHTATF)
Sold by: Amazon.com
Supplied by: Other
Return or replace items: Eligible through October 28, 2026
$4.98
"""


def test_multi_item_order_splits_into_distinct_categories() -> None:
    parsed = parse_invoice_text(_MULTI_ITEM_INVOICE_TEXT)
    assert parsed is not None
    assert len(parsed.line_items) == 3

    order = AmazonOrder(
        order_id=parsed.order_id,
        order_date=parsed.order_date,
        total=parsed.total,
        tax=parsed.tax,
        shipping=parsed.shipping,
        line_items=parsed.line_items,
    )
    txn = YnabTransaction(
        id="txn-1",
        date=order.order_date,
        amount=-order.total,
        payee_name="AMAZON.COM",
        category_id=None,
        memo=None,
        account_id="acct-visa",
        cleared="cleared",
        approved=False,
    )

    rules_data = load_rules_amazon()
    categorizer = Categorizer(
        rules=rules_data["rules"],
        fallback_category=rules_data["fallback_category"],
        categories=load_categories(),
    )

    proposals, unmatched_orders, unmatched_txns = match_amazon_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 1
    assert unmatched_orders == []
    assert unmatched_txns == []

    proposal = proposals[0]
    category_names = {s.category_name for s in proposal.splits}
    assert len(category_names) > 1
    assert sum(s.amount for s in proposal.splits) == -order.total


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
