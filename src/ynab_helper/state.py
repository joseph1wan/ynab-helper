from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open() as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        json.dump(state, f, indent=2, default=str)


def resolve_since_date(
    state_path: Path,
    initial_since: str | None,
    cli_since: date | None,
    bootstrap_date: date | None,
    overwrite: bool = False,
) -> tuple[date, bool]:
    """Return the effective scrape cutoff and whether this is the first run.

    Normal fetches never go farther back than a completed fetch.  A supplied
    ``--since`` (or ``initial_since``) can narrow that window, but cannot make
    it older.  ``--overwrite`` deliberately ignores the completed-fetch
    cutoff so it can be used for a backfill.
    """
    state = load_state(state_path)
    is_first_run = not bool(state and state.get("last_successful_run"))

    configured_since = cli_since
    if configured_since is None and initial_since:
        configured_since = date.fromisoformat(initial_since)
    if configured_since is None and state and state.get("bootstrap_since"):
        configured_since = date.fromisoformat(state["bootstrap_since"])

    last_successful_run = None
    if not overwrite and state and state.get("last_successful_run"):
        last_successful_run = datetime.fromisoformat(
            state["last_successful_run"]
        ).date()

    if configured_since and last_successful_run:
        return max(configured_since, last_successful_run), False
    if last_successful_run:
        return last_successful_run, False
    if configured_since:
        return configured_since, is_first_run

    if bootstrap_date is None:
        raise ValueError(
            "No uncategorized TARGET transactions found in YNAB. "
            "Pass --since YYYY-MM-DD to set a scrape start date."
        )
    return bootstrap_date, is_first_run


def mark_fetch_success(
    state_path: Path,
    since_date: date,
    is_first_run: bool,
) -> None:
    state = load_state(state_path) or {}
    now = datetime.now(timezone.utc).isoformat()
    state["last_successful_run"] = now
    if is_first_run:
        state["bootstrap_since"] = since_date.isoformat()
    state.setdefault("processed_order_ids", [])
    state.setdefault("processed_ynab_txn_ids", [])
    save_state(state_path, state)


def mark_applied(
    state_path: Path,
    order_id: str,
    txn_id: str,
) -> None:
    state = load_state(state_path) or {}
    state.setdefault("processed_order_ids", [])
    state.setdefault("processed_ynab_txn_ids", [])
    if order_id not in state["processed_order_ids"]:
        state["processed_order_ids"].append(order_id)
    if txn_id not in state["processed_ynab_txn_ids"]:
        state["processed_ynab_txn_ids"].append(txn_id)
    save_state(state_path, state)
