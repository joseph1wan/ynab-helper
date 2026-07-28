from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ynab_helper.config import load_config, resolve_path
from ynab_helper.fetch import load_proposals
from ynab_helper.state import mark_applied
from ynab_helper.ynab_client import YnabClient

UNDO_DIR = resolve_path("data/undo")


def _undo_path(txn_id: str) -> Path:
    return UNDO_DIR / f"{txn_id}.json"


def save_undo_snapshot(txn_id: str, original: dict[str, Any]) -> None:
    UNDO_DIR.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "transaction_id": txn_id,
        "original": original,
        "applied_at": datetime.now(timezone.utc).isoformat(),
    }
    with _undo_path(txn_id).open("w") as f:
        json.dump(snapshot, f, indent=2)


def list_undo_snapshots() -> list[dict[str, Any]]:
    if not UNDO_DIR.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(UNDO_DIR.glob("*.json"), reverse=True):
        with path.open() as f:
            snapshots.append(json.load(f))
    return snapshots


def apply_proposal(proposal_index: int) -> dict[str, Any]:
    config = load_config()
    token = config.get("ynab_token", "")
    proposals_path = resolve_path(config["proposals_path"])
    state_path = resolve_path(config["state_path"])
    data = load_proposals(proposals_path)

    if proposal_index < 0 or proposal_index >= len(data["proposals"]):
        raise IndexError("Proposal index out of range")

    proposal = data["proposals"][proposal_index]
    if proposal.get("status") == "applied":
        raise ValueError("Proposal already applied")

    txn = proposal["ynab_transaction"]
    subtransactions = [
        {
            "amount": split["amount"],
            "category_id": split["category_id"],
        }
        for split in proposal["splits"]
    ]

    original = {
        "amount": txn["amount"],
        "payee_name": txn.get("payee_name"),
        "memo": txn.get("memo"),
        "category_id": txn.get("category_id"),
    }

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        result = client.patch_transaction_splits(txn["id"], subtransactions)

    save_undo_snapshot(txn["id"], original)
    proposal["status"] = "applied"
    proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    data["proposals"][proposal_index] = proposal

    with proposals_path.open("w") as f:
        json.dump(data, f, indent=2)

    mark_applied(
        state_path,
        proposal["target_order"]["order_id"],
        txn["id"],
    )
    return result


def apply_all_pending() -> list[str]:
    config = load_config()
    token = config.get("ynab_token", "")
    proposals_path = resolve_path(config["proposals_path"])
    state_path = resolve_path(config["state_path"])
    data = load_proposals(proposals_path)

    pending_indices = [
        i
        for i, proposal in enumerate(data.get("proposals", []))
        if proposal.get("status") != "applied"
    ]
    if not pending_indices:
        return []

    bulk_payload = []
    originals: dict[str, dict[str, Any]] = {}
    for i in pending_indices:
        proposal = data["proposals"][i]
        txn = proposal["ynab_transaction"]
        originals[txn["id"]] = {
            "amount": txn["amount"],
            "payee_name": txn.get("payee_name"),
            "memo": txn.get("memo"),
            "category_id": txn.get("category_id"),
        }
        bulk_payload.append(
            {
                "id": txn["id"],
                "approved": True,
                "subtransactions": [
                    {
                        "amount": split["amount"],
                        "category_id": split["category_id"],
                    }
                    for split in proposal["splits"]
                ],
            }
        )

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        client.patch_transactions_bulk(bulk_payload)

    applied_at = datetime.now(timezone.utc).isoformat()
    applied_txn_ids: list[str] = []
    for i in pending_indices:
        proposal = data["proposals"][i]
        txn_id = proposal["ynab_transaction"]["id"]
        save_undo_snapshot(txn_id, originals[txn_id])
        proposal["status"] = "applied"
        proposal["applied_at"] = applied_at
        mark_applied(state_path, proposal["target_order"]["order_id"], txn_id)
        applied_txn_ids.append(txn_id)

    with proposals_path.open("w") as f:
        json.dump(data, f, indent=2)

    return applied_txn_ids


def undo_last(count: int = 1) -> list[str]:
    config = load_config()
    token = config.get("ynab_token", "")
    proposals_path = resolve_path(config["proposals_path"])
    snapshots = list_undo_snapshots()[:count]
    restored: list[str] = []

    with YnabClient(token, config.get("budget_id", "last-used")) as client:
        for snapshot in snapshots:
            txn_id = snapshot["transaction_id"]
            client.restore_transaction(txn_id, snapshot["original"])
            restored.append(txn_id)
            path = _undo_path(txn_id)
            if path.exists():
                path.unlink()

    if proposals_path.exists():
        data = load_proposals(proposals_path)
        restored_set = set(restored)
        for proposal in data.get("proposals", []):
            if proposal.get("ynab_transaction", {}).get("id") in restored_set:
                proposal["status"] = "pending"
                proposal.pop("applied_at", None)
        with proposals_path.open("w") as f:
            json.dump(data, f, indent=2)

    return restored
