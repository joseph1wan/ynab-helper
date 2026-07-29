"""Build and apply the Other review tab (`/other`).

Catch-all: every unapproved transaction, across all accounts, that is not
claimed by any registered Source's scope (see sources.py). A future Source
registers itself in sources.py and its transactions stop appearing here
automatically — no change needed in this file. Like paypal_review.py, a
Review item here gets exactly one category (never split), and this module
is a self-contained copy of the same approve/undo/categorize pattern per
ADR 006 rather than a shared library. Unlike PayPal/Target/Costco, there is
no rules file here — by construction every transaction here has already
evaded all three Source-specific matchers, so there's no established
pattern to auto-apply.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.config import load_categories, load_config, resolve_path
from ynab_helper.models import YnabTransaction
from ynab_helper.sources import all_source_scopes, is_claimed
from ynab_helper.undo import save_undo_snapshot
from ynab_helper.ynab_client import YnabClient


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


def build_other_review(since_override: date | None = None) -> dict[str, Any]:
    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    review_path = resolve_path(config.get("other_review_path", "data/other/review.json"))

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        scopes = all_source_scopes(config, client)
        transactions = client.get_all_unapproved_transactions(since_override)

    unclaimed = [txn for txn in transactions if not is_claimed(txn, scopes)]

    items = [
        {
            "ynab_transaction": _serialize_txn(txn),
            "category_name": None,
            "category_id": None,
            "status": "pending",
        }
        for txn in unclaimed
    ]

    data = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "since_date": since_override.isoformat() if since_override else None,
        "items": items,
    }
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)
    return data


def load_other_review(review_path: Path) -> dict[str, Any]:
    with review_path.open() as f:
        return json.load(f)


def recategorize_other_item(review_path: Path, index: int, category_name: str) -> dict[str, Any]:
    """Unlike PayPal/Costco/Target, no allowlist — any real YNAB category is valid here."""
    data = load_other_review(review_path)
    items = data.get("items", [])
    if index < 0 or index >= len(items):
        raise IndexError("Item index out of range")

    categories = load_categories()
    category_id = categories.get(category_name)
    if not category_id:
        raise ValueError(f"Unknown category: {category_name}")

    item = items[index]
    item["category_name"] = category_name
    item["category_id"] = category_id

    with review_path.open("w") as f:
        json.dump(data, f, indent=2)
    return item


def apply_other_item(index: int) -> dict[str, Any]:
    config = load_config()
    token = config.get("ynab_token", "")
    review_path = resolve_path(config.get("other_review_path", "data/other/review.json"))
    data = load_other_review(review_path)
    items = data.get("items", [])

    if index < 0 or index >= len(items):
        raise IndexError("Item index out of range")

    item = items[index]
    if item.get("status") == "applied":
        raise ValueError("Item already applied")
    if not item.get("category_id"):
        raise ValueError("Pick a category before approving")

    txn = item["ynab_transaction"]
    original = {
        "amount": txn["amount"],
        "payee_name": txn.get("payee_name"),
        "memo": txn.get("memo"),
        "category_id": txn.get("category_id"),
        "approved": False,
    }

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        result = client.patch_transaction_fields(
            txn["id"], item["category_id"], txn.get("memo"), approved=True
        )

    save_undo_snapshot(txn["id"], original)

    item["status"] = "applied"
    item["applied_at"] = datetime.now(timezone.utc).isoformat()
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)

    return result


def apply_all_pending_other_items() -> list[str]:
    config = load_config()
    token = config.get("ynab_token", "")
    review_path = resolve_path(config.get("other_review_path", "data/other/review.json"))
    data = load_other_review(review_path)
    items = data.get("items", [])

    pending_indices = [
        i
        for i, item in enumerate(items)
        if item.get("status") != "applied" and item.get("category_id")
    ]
    if not pending_indices:
        return []

    bulk_payload = []
    originals: dict[str, dict[str, Any]] = {}
    for i in pending_indices:
        item = items[i]
        txn = item["ynab_transaction"]
        originals[txn["id"]] = {
            "amount": txn["amount"],
            "payee_name": txn.get("payee_name"),
            "memo": txn.get("memo"),
            "category_id": txn.get("category_id"),
            "approved": False,
        }
        bulk_payload.append(
            {
                "id": txn["id"],
                "category_id": item["category_id"],
                "memo": txn.get("memo"),
                "approved": True,
            }
        )

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        client.patch_transactions_bulk(bulk_payload)

    applied_at = datetime.now(timezone.utc).isoformat()
    applied_txn_ids: list[str] = []
    for i in pending_indices:
        item = items[i]
        txn = item["ynab_transaction"]
        save_undo_snapshot(txn["id"], originals[txn["id"]])
        item["status"] = "applied"
        item["applied_at"] = applied_at
        applied_txn_ids.append(txn["id"])

    with review_path.open("w") as f:
        json.dump(data, f, indent=2)

    return applied_txn_ids


def clear_applied_other_items(review_path: Path) -> int:
    data = load_other_review(review_path)
    items = data.get("items", [])
    remaining = [i for i in items if i.get("status") != "applied"]
    removed = len(items) - len(remaining)
    data["items"] = remaining
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)
    return removed
