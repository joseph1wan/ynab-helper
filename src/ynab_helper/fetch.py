from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.categorizer import Categorizer
from ynab_helper.config import load_categories, load_config, load_rules, resolve_path
from ynab_helper.matcher import match_orders_to_transactions
from ynab_helper.models import CategorizedLine, FetchResult, LineItem, MatchProposal, ScrapeResult, TargetOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits
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


def _order_from_dict(order: dict[str, Any]) -> TargetOrder:
    return TargetOrder(
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


def _categorized_lines_from_dicts(lines: list[dict[str, Any]]) -> list[CategorizedLine]:
    return [
        CategorizedLine(
            line_item=LineItem(
                name=line["name"], quantity=line["quantity"], line_total=line["line_total"]
            ),
            category_name=line["category_name"],
            category_id=line["category_id"],
            matched_rule=line["matched_rule"],
        )
        for line in lines
    ]


def _load_proposal_for_edit(
    data: dict[str, Any], proposal_index: int, line_index: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    proposals = data.get("proposals", [])
    if proposal_index < 0 or proposal_index >= len(proposals):
        raise IndexError("Proposal index out of range")

    proposal = proposals[proposal_index]
    if proposal.get("status") == "applied":
        raise ValueError("Proposal already applied")

    lines = proposal["categorized_lines"]
    if line_index < 0 or line_index >= len(lines):
        raise IndexError("Line index out of range")

    return proposal, lines[line_index]


def _recompute_splits(proposal: dict[str, Any], lines: list[dict[str, Any]]) -> None:
    order = _order_from_dict(proposal["target_order"])
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


def recategorize_line(
    proposals_path: Path, proposal_index: int, line_index: int, category_name: str
) -> dict[str, Any]:
    """Override the category for a single line item and recompute the splits.

    Returns the updated proposal dict.
    """
    data = load_proposals(proposals_path)
    proposal, line = _load_proposal_for_edit(data, proposal_index, line_index)

    allowed_categories = load_rules().get("allowed_categories", [])
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


def set_line_note(
    proposals_path: Path, proposal_index: int, line_index: int, note: str
) -> dict[str, Any]:
    """Attach a free-text note to a line item, explaining a manual override.

    Notes never reach YNAB; they exist only to inform later rule review.
    Returns the updated proposal dict.
    """
    data = load_proposals(proposals_path)
    proposal, line = _load_proposal_for_edit(data, proposal_index, line_index)

    line["note"] = note or None

    with proposals_path.open("w") as f:
        json.dump(data, f, indent=2)

    return proposal


def clear_applied(proposals_path: Path) -> int:
    """Drop applied proposals from the review file. Returns count removed.

    Leaves data/target-orders/*.json, data/state.json, and data/undo/*.json
    untouched so audit-rules keeps its full item corpus and pushes stay undoable.
    """
    data = load_proposals(proposals_path)
    proposals = data.get("proposals", [])
    remaining = [p for p in proposals if p.get("status") != "applied"]
    removed = len(proposals) - len(remaining)
    data["proposals"] = remaining

    with proposals_path.open("w") as f:
        json.dump(data, f, indent=2)

    return removed


def run_fetch(
    since_override: date | None = None,
    until_override: date | None = None,
    overwrite: bool = False,
    skip_scrape: bool = False,
    headless: bool = False,
    debug_pause: bool = False,
) -> ScrapeResult:
    if until_override is not None and since_override is not None and until_override < since_override:
        raise ValueError(f"--until ({until_override}) is before --since ({since_override})")

    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    state_path = resolve_path(config["state_path"])
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
            overwrite=overwrite,
        )

        if skip_scrape:
            orders = load_cached_orders(orders_dir, since_date, until_date=until_override)
        else:
            # The live scraper always walks Target's order history newest-first
            # down to since_date, so an upper bound can only be applied by
            # filtering the results afterward — everything in between still
            # gets captured and cached on disk, which is harmless.
            orders = scrape_target_orders(
                target_auth,
                since_date,
                orders_dir,
                headless=headless,
                debug_pause=debug_pause,
                overwrite=overwrite,
            )
            if until_override is not None:
                orders = [o for o in orders if o.order_date <= until_override]

    result = ScrapeResult(
        orders=orders,
        since_date=since_date,
        fetched_at=datetime.now(timezone.utc),
    )
    mark_fetch_success(state_path, since_date, is_first_run)
    return result


def run_propose(
    since_override: date | None = None, until_override: date | None = None
) -> FetchResult:
    """Match saved Target orders to YNAB and write review proposals."""
    if until_override is not None and since_override is not None and until_override < since_override:
        raise ValueError(f"--until ({until_override}) is before --since ({since_override})")

    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    orders_dir = resolve_path("data/target-orders")
    all_orders = load_cached_orders(orders_dir, date.min)
    if not all_orders:
        raise ValueError("No saved Target orders found. Run fetch first.")
    since_date = since_override or min(order.order_date for order in all_orders)
    until_date = until_override
    orders = [
        order
        for order in all_orders
        if order.order_date >= since_date
        and (until_date is None or order.order_date <= until_date)
    ]

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        transactions = client.get_uncategorized_target_transactions(
            config.get("payee_pattern", "TARGET"),
            since_date=since_date,
            until_date=until_date,
        )

    proposals, unmatched_orders, unmatched_transactions = match_orders_to_transactions(
        orders, transactions, Categorizer()
    )
    result = FetchResult(
        proposals=proposals,
        unmatched_orders=unmatched_orders,
        unmatched_transactions=unmatched_transactions,
        since_date=since_date,
        fetched_at=datetime.now(timezone.utc),
    )
    save_proposals(resolve_path(config["proposals_path"]), result)
    return result
