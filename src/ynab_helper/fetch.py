from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_config, resolve_path
from ynab_helper.matcher import match_orders_to_transactions
from ynab_helper.models import FetchResult, LineItem, MatchProposal, TargetOrder, YnabTransaction
from ynab_helper.state import mark_fetch_success, resolve_since_date
from ynab_helper.target_scraper import load_cached_orders, scrape_target_orders
from ynab_helper.ynab_client import YnabClient


def _serialize_line_item(item: LineItem) -> dict[str, Any]:
    return {"name": item.name, "quantity": item.quantity, "line_total": item.line_total}


def _serialize_order(order: TargetOrder) -> dict[str, Any]:
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


def _serialize_proposal(proposal: MatchProposal) -> dict[str, Any]:
    return {
        "target_order": _serialize_order(proposal.target_order),
        "ynab_transaction": _serialize_txn(proposal.ynab_transaction),
        "categorized_lines": [
            {
                "name": cl.line_item.name,
                "quantity": cl.line_item.quantity,
                "line_total": cl.line_item.line_total,
                "category_name": cl.category_name,
                "category_id": cl.category_id,
                "matched_rule": cl.matched_rule,
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


def serialize_fetch_result(result: FetchResult) -> dict[str, Any]:
    return {
        "fetched_at": result.fetched_at.isoformat(),
        "since_date": result.since_date.isoformat(),
        "proposals": [_serialize_proposal(p) for p in result.proposals],
        "unmatched_orders": [_serialize_order(o) for o in result.unmatched_orders],
        "unmatched_transactions": [
            _serialize_txn(t) for t in result.unmatched_transactions
        ],
    }


def save_proposals(path: Path, result: FetchResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(serialize_fetch_result(result), f, indent=2)


def load_proposals(path: Path) -> dict[str, Any]:
    with path.open() as f:
        return json.load(f)


def run_fetch(
    since_override: date | None = None,
    skip_scrape: bool = False,
    headless: bool = True,
    debug_pause: bool = False,
) -> FetchResult:
    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    state_path = resolve_path(config["state_path"])
    proposals_path = resolve_path(config["proposals_path"])
    target_auth = resolve_path(config["target_auth_path"])
    orders_dir = resolve_path("data/target-orders")
    payee_pattern = config.get("payee_pattern", "TARGET")

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        bootstrap_date = client.oldest_uncategorized_target_date(payee_pattern)
        since_date, is_first_run = resolve_since_date(
            state_path,
            config.get("initial_since"),
            since_override,
            bootstrap_date,
        )

        if skip_scrape:
            orders = load_cached_orders(orders_dir, since_date)
        else:
            orders = scrape_target_orders(
                target_auth,
                since_date,
                orders_dir,
                headless=headless,
                debug_pause=debug_pause,
            )

        transactions = client.get_uncategorized_target_transactions(
            payee_pattern, since_date=since_date
        )

    categorizer = Categorizer()
    proposals, unmatched_orders, unmatched_transactions = match_orders_to_transactions(
        orders, transactions, categorizer
    )

    result = FetchResult(
        proposals=proposals,
        unmatched_orders=unmatched_orders,
        unmatched_transactions=unmatched_transactions,
        since_date=since_date,
        fetched_at=datetime.now(timezone.utc),
    )

    save_proposals(proposals_path, result)
    mark_fetch_success(state_path, since_date, is_first_run)
    return result
