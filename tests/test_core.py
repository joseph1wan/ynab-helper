from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from ynab_helper.categorizer import Categorizer
from ynab_helper.cli import main
from ynab_helper.matcher import match_orders_to_transactions
from ynab_helper.models import LineItem, TargetOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits, round_to_dollar


@pytest.fixture
def categories() -> dict[str, str]:
    return {
        "Baby": "cat-baby",
        "Groceries": "cat-groceries",
        "Shopping": "cat-shopping",
    }


@pytest.fixture
def categorizer(categories: dict[str, str]) -> Categorizer:
    return Categorizer(
        rules=[
            {"pattern": "diaper|wipes", "category": "Baby"},
            {"pattern": "milk|cheerios", "category": "Groceries"},
        ],
        fallback_category="Shopping",
        categories=categories,
    )


def test_round_to_dollar() -> None:
    assert round_to_dollar(-87430) == -87000
    assert round_to_dollar(-87500) == -88000


def test_compute_splits_even_fees(categorizer: Categorizer) -> None:
    order = TargetOrder(
        order_id="1",
        order_date=date(2026, 7, 1),
        total=50000,
        line_items=[
            LineItem(name="Diapers", quantity=1, line_total=30000),
            LineItem(name="Milk", quantity=1, line_total=20000),
        ],
        tax=1000,
        shipping=0,
        fees=0,
    )
    categorized, _ = categorizer.categorize_all(order.line_items)
    splits, delta = compute_splits(order, categorized, -50000)
    assert sum(s.amount for s in splits) == -50000
    assert len(splits) == 2
    assert delta == 0


def test_exact_match(categorizer: Categorizer) -> None:
    order = TargetOrder(
        order_id="ord-1",
        order_date=date(2026, 7, 10),
        total=87430,
        line_items=[LineItem(name="Diapers", quantity=1, line_total=87430)],
    )
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 10),
        amount=-87430,
        payee_name="TARGET STORE",
        category_id=None,
        memo=None,
        account_id="acct-1",
        cleared="cleared",
        approved=True,
    )
    proposals, unmatched_orders, unmatched_txns = match_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 1
    assert len(unmatched_orders) == 0
    assert len(unmatched_txns) == 0
    assert proposals[0].categorized_lines[0].category_name == "Baby"


def test_no_match_when_amount_differs(categorizer: Categorizer) -> None:
    order = TargetOrder(
        order_id="ord-1",
        order_date=date(2026, 7, 10),
        total=87430,
        line_items=[LineItem(name="Diapers", quantity=1, line_total=87430)],
    )
    txn = YnabTransaction(
        id="txn-1",
        date=date(2026, 7, 10),
        amount=-90000,
        payee_name="TARGET STORE",
        category_id=None,
        memo=None,
        account_id="acct-1",
        cleared="cleared",
        approved=True,
    )
    proposals, unmatched_orders, unmatched_txns = match_orders_to_transactions(
        [order], [txn], categorizer
    )
    assert len(proposals) == 0
    assert len(unmatched_orders) == 1
    assert len(unmatched_txns) == 1


def test_fetch_defaults_to_visible_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_fetch(
        *,
        since_override: date | None = None,
        skip_scrape: bool = False,
        headless: bool = True,
        debug_pause: bool = False,
    ) -> SimpleNamespace:
        captured["headless"] = headless
        return SimpleNamespace(
            proposals=[],
            unmatched_orders=[],
            unmatched_transactions=[],
            since_date=date(2026, 7, 20),
        )

    monkeypatch.setattr("ynab_helper.cli.run_fetch", fake_run_fetch)
    monkeypatch.setattr(
        "ynab_helper.cli.load_config",
        lambda: {"proposals_path": "data/proposals.json"},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["fetch"])

    assert result.exit_code == 0
    assert captured["headless"] is False
