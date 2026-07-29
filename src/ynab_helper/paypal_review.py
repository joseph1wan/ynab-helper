"""Build and apply the PayPal review tab (`/paypal`).

Scoped strictly to the Paypal YNAB account (resolved by name via
config["paypal_account_name"]) — Target, Amazon, and every other account
never appear here. Each source gets its own review module like this one
rather than a shared cross-account engine; see CONTEXT.md.

A review item gets exactly one category, not a proportional split, unlike
fetch.py's Target-split flow. Its source data is any unapproved,
non-transfer transaction on the Paypal account, enriched with a PayPal CSV
note where one links via paypal_linker.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.config import load_categories, load_config, load_paypal_categories, resolve_path
from ynab_helper.models import PaypalRecord, YnabTransaction
from ynab_helper.paypal_csv import load_paypal_records
from ynab_helper.paypal_linker import link_records
from ynab_helper.paypal_rules import lookup
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


def _serialize_record(record: PaypalRecord) -> dict[str, Any]:
    return {
        "date": record.date.isoformat(),
        "name": record.name,
        "type": record.type,
        "amount": record.amount,
        "note": record.note,
        "transaction_id": record.transaction_id,
    }


def build_paypal_review(since_override: date | None = None) -> dict[str, Any]:
    config = load_config()
    token = config.get("ynab_token", "")
    if not token:
        raise ValueError("YNAB_TOKEN not set. Add it to .env or config/config.yaml")

    account_name = config.get("paypal_account_name", "Paypal")
    records_path = resolve_path(config.get("paypal_records_path", "data/paypal/records.json"))
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    records = load_paypal_records(records_path)
    categories = load_categories()

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        account_id = client.get_account_id_by_name(account_name)
        if account_id is None:
            raise ValueError(f"No YNAB account named {account_name!r} found")
        transactions = client.get_unapproved_account_transactions(account_id, since_override)

    links = link_records(records, transactions)

    items: list[dict[str, Any]] = []
    for txn in transactions:
        record, matched_via, candidates = links.get(txn.id, (None, None, []))
        note = record.note if record else None
        rule = lookup(note)
        category_name = rule.category if rule else None
        category_id = categories.get(category_name) if category_name else None

        items.append(
            {
                "ynab_transaction": _serialize_txn(txn),
                "paypal": _serialize_record(record) if record else None,
                "matched_via": matched_via,
                "candidates": [_serialize_record(c) for c in candidates],
                "category_name": category_name,
                "category_id": category_id,
                "matched_rule": rule.pattern if rule else None,
                "status": "pending",
            }
        )

    data = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "since_date": since_override.isoformat() if since_override else None,
        "account_name": account_name,
        "items": items,
    }
    review_path.parent.mkdir(parents=True, exist_ok=True)
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)
    return data


def load_paypal_review(review_path: Path) -> dict[str, Any]:
    with review_path.open() as f:
        return json.load(f)


def recategorize_item(review_path: Path, index: int, category_name: str) -> dict[str, Any]:
    data = load_paypal_review(review_path)
    items = data.get("items", [])
    if index < 0 or index >= len(items):
        raise IndexError("Item index out of range")

    allowed = load_paypal_categories()
    if category_name not in allowed:
        raise ValueError(f"Category not in paypal_categories: {category_name}")
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


def apply_paypal_item(index: int) -> dict[str, Any]:
    config = load_config()
    token = config.get("ynab_token", "")
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    data = load_paypal_review(review_path)
    items = data.get("items", [])

    if index < 0 or index >= len(items):
        raise IndexError("Item index out of range")

    item = items[index]
    if item.get("status") == "applied":
        raise ValueError("Item already applied")
    if not item.get("category_id"):
        raise ValueError("Pick a category before approving")

    txn = item["ynab_transaction"]
    paypal = item.get("paypal")
    memo = paypal["note"] if paypal and paypal.get("note") else txn.get("memo")

    original = {
        "amount": txn["amount"],
        "payee_name": txn.get("payee_name"),
        "memo": txn.get("memo"),
        "category_id": txn.get("category_id"),
        "approved": False,
    }

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        result = client.patch_transaction_fields(
            txn["id"], item["category_id"], memo, approved=True
        )

    save_undo_snapshot(txn["id"], original)

    item["status"] = "applied"
    item["applied_at"] = datetime.now(timezone.utc).isoformat()
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)

    return result


def apply_all_pending_paypal_items() -> list[str]:
    config = load_config()
    token = config.get("ynab_token", "")
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    data = load_paypal_review(review_path)
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
        paypal = item.get("paypal")
        memo = paypal["note"] if paypal and paypal.get("note") else txn.get("memo")
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
                "memo": memo,
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


def reapply_paypal_rules(review_path: Path) -> int:
    """Re-run local rules against pending items already on disk, filling in blanks.

    Does not touch items that already have a category (manually picked or
    matched by an earlier build/reapply) or items already applied to YNAB.
    """
    data = load_paypal_review(review_path)
    items = data.get("items", [])
    categories = load_categories()

    updated = 0
    for item in items:
        if item.get("status") == "applied" or item.get("category_name"):
            continue
        paypal = item.get("paypal")
        note = paypal["note"] if paypal else None
        rule = lookup(note)
        if rule is None:
            continue
        item["category_name"] = rule.category
        item["category_id"] = categories.get(rule.category)
        item["matched_rule"] = rule.pattern
        updated += 1

    if updated:
        with review_path.open("w") as f:
            json.dump(data, f, indent=2)
    return updated


def clear_applied_paypal_items(review_path: Path) -> int:
    """Drop applied items from the review file. Returns count removed.

    Also deletes each removed item's data/undo/{txn_id}.json snapshot —
    once an item is gone from the review file there's no way to flip its
    status back to "pending" on undo, so leaving the snapshot around would
    let undo_last() silently revert the YNAB transaction with nothing in
    the UI to show for it.
    """
    data = load_paypal_review(review_path)
    items = data.get("items", [])
    remaining = [i for i in items if i.get("status") != "applied"]
    removed_items = [i for i in items if i.get("status") == "applied"]
    removed = len(removed_items)
    data["items"] = remaining
    with review_path.open("w") as f:
        json.dump(data, f, indent=2)

    undo_dir = resolve_path("data/undo")
    for item in removed_items:
        txn_id = item.get("ynab_transaction", {}).get("id")
        if not txn_id:
            continue
        snapshot_path = undo_dir / f"{txn_id}.json"
        if snapshot_path.exists():
            snapshot_path.unlink()

    return removed
