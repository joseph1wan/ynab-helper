from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.amazon_matcher import match_amazon_orders_to_transactions
from ynab_helper.amazon_orders import load_cached_amazon_orders
from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_categories, load_config, load_rules_amazon, resolve_path
from ynab_helper.fetch import _categorized_lines_from_dicts, _load_proposal_for_edit, load_proposals
from ynab_helper.models import (
    AmazonFetchResult,
    AmazonMatchProposal,
    AmazonOrder,
    LineItem,
    YnabTransaction,
)
from ynab_helper.source_scope import SourceScope
from ynab_helper.split_calculator import compute_splits
from ynab_helper.ynab_client import YnabClient


def get_source_scope(config: dict, client: YnabClient) -> SourceScope:
    """Amazon's scope for the Other review tab: dedicated account."""
    account_names = config.get("amazon_account_names", [])
    accounts = client.list_accounts()
    account_ids = {accounts[name] for name in account_names if name in accounts}
    return SourceScope(account_ids=account_ids)


def _serialize_line_item(item: LineItem) -> dict[str, Any]:
    return {"name": item.name, "quantity": item.quantity, "line_total": item.line_total}


def _serialize_order(order: AmazonOrder) -> dict[str, Any]:
    return {
        "order_id": order.order_id,
        "order_date": order.order_date.isoformat(),
        "total": order.total,
        "tax": order.tax,
        "shipping": order.shipping,
        "fees": order.fees,
        "line_items": [_serialize_line_item(li) for li in order.line_items],
    }


def _serialize_txn(txn: YnabTransaction) -> dict[str, Any]:
    return {
        "id": txn.id,
        "date": txn.date.isoformat(),
        "amount": txn.amount,
        "payee_name": txn.payee_name,
        "category_id": txn.category_id,
        "memo": txn.memo,
        "account_id": txn.account_id,
    }


def _serialize_proposal(proposal: AmazonMatchProposal) -> dict[str, Any]:
    return {
        "amazon_order": _serialize_order(proposal.amazon_order),
        "ynab_transaction": _serialize_txn(proposal.ynab_transaction),
        "categorized_lines": [
            {
                "name": cl.line_item.name,
                "quantity": cl.line_item.quantity,
                "line_total": cl.line_item.line_total,
                "category_name": cl.category_name,
                "category_id": cl.category_id,
                "matched_rule": cl.matched_rule,
                "note": None,
            }
            for cl in proposal.categorized_lines
        ],
        "splits": [
            {
                "category_name": s.category_name,
                "category_id": s.category_id,
                "amount": s.amount,
                "line_items": s.line_items,
            }
            for s in proposal.splits
        ],
        "unmatched_items": [
            _serialize_line_item(item) for item in proposal.unmatched_items
        ],
        "rounding_delta": proposal.rounding_delta,
        "status": "pending",
    }


def serialize_fetch_result(result: AmazonFetchResult) -> dict[str, Any]:
    return {
        "fetched_at": result.fetched_at.isoformat(),
        "since_date": result.since_date.isoformat(),
        "proposals": [_serialize_proposal(p) for p in result.proposals],
        "unmatched_orders": [_serialize_order(o) for o in result.unmatched_orders],
        "unmatched_transactions": [
            _serialize_txn(t) for t in result.unmatched_transactions
        ],
    }


def save_amazon_proposals(path: Path, result: AmazonFetchResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(serialize_fetch_result(result), f, indent=2)


def _order_from_dict(order: dict[str, Any]) -> AmazonOrder:
    return AmazonOrder(
        order_id=order["order_id"],
        order_date=date.fromisoformat(order["order_date"]),
        total=order["total"],
        tax=order.get("tax", 0),
        shipping=order.get("shipping", 0),
        fees=order.get("fees", 0),
        line_items=[
            LineItem(name=li["name"], quantity=li["quantity"], line_total=li["line_total"])
            for li in order["line_items"]
        ],
    )


def _recompute_splits(proposal: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    order = _order_from_dict(proposal["amazon_order"])
    categorized_lines = _categorized_lines_from_dicts(lines)
    splits, rounding_delta = compute_splits(
        order, categorized_lines, proposal["ynab_transaction"]["amount"]
    )
    proposal["splits"] = [
        {
            "category_name": s.category_name,
            "category_id": s.category_id,
            "amount": s.amount,
            "line_items": s.line_items,
        }
        for s in splits
    ]
    proposal["rounding_delta"] = rounding_delta


def recategorize_line_amazon(
    proposals_path: Path, proposal_index: int, line_index: int, category_name: str
) -> dict[str, Any]:
    """Amazon counterpart of fetch.recategorize_line — validates against
    rules_amazon.yaml's allowed_categories instead of rules.yaml's."""
    data = load_proposals(proposals_path)
    proposal, line = _load_proposal_for_edit(data, proposal_index, line_index)

    allowed_categories = load_rules_amazon().get("allowed_categories", [])
    if category_name not in allowed_categories:
        raise ValueError(f"Category not in allowed_categories: {category_name}")
    categories = load_categories()
    category_id = categories.get(category_name)
    if not category_id:
        raise ValueError(f"Unknown category: {category_name}")

    line["category_name"] = category_name
    line["category_id"] = category_id
    line["matched_rule"] = "manual override"

    unmatched_names = {item["name"] for item in proposal.get("unmatched_items", [])}
    if line["name"] in unmatched_names:
        proposal["unmatched_items"] = [
            item for item in proposal["unmatched_items"] if item["name"] != line["name"]
        ]

    lines = proposal["categorized_lines"]
    _recompute_splits(proposal, lines)

    with proposals_path.open("w") as f:
        json.dump(data, f, indent=2)

    return proposal


def run_amazon_propose(
    since_override: date | None = None, until_override: date | None = None
) -> AmazonFetchResult:
    """Match saved Amazon orders to YNAB and write review proposals.

    No run_amazon_fetch() counterpart exists — Amazon has no live scraper,
    only import_pasted_amazon_orders(), so this is the only orchestration
    entry point (mirrors the propose-only half of costco_fetch.py's
    run_costco_propose)."""
    if until_override is not None and since_override is not None and until_override < since_override:
        raise ValueError(f"--until ({until_override}) is before --since ({since_override})")

    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    orders_dir = resolve_path(config.get("amazon_orders_dir", "data/amazon-orders"))
    all_orders = load_cached_amazon_orders(orders_dir, date.min)
    if not all_orders:
        raise ValueError("No saved Amazon orders found. Run import-invoices first (paste orders as inbox/amazon_N.txt).")

    since_date = since_override or min(order.order_date for order in all_orders)
    until_date = until_override
    orders = [
        order
        for order in all_orders
        if order.order_date >= since_date
        and (until_date is None or order.order_date <= until_date)
    ]

    payee_pattern = config.get("amazon_payee_pattern", "AMAZON")

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        transactions = client.get_uncategorized_target_transactions(
            payee_pattern, since_date=since_date, until_date=until_date
        )

    rules_data = load_rules_amazon()
    categorizer = Categorizer(
        rules=rules_data.get("rules", []),
        fallback_category=rules_data.get("fallback_category", "One Offs"),
        categories=load_categories(),
    )
    proposals, unmatched_orders, unmatched_transactions = match_amazon_orders_to_transactions(
        orders, transactions, categorizer
    )
    result = AmazonFetchResult(
        proposals=proposals,
        unmatched_orders=unmatched_orders,
        unmatched_transactions=unmatched_transactions,
        since_date=since_date,
        fetched_at=datetime.now(timezone.utc),
    )
    save_amazon_proposals(
        resolve_path(config.get("amazon_proposals_path", "data/proposals/amazon-latest.json")), result
    )
    return result
