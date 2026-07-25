"""Apply (or undo) payee renames from data/payee-clusters.json.

Only clusters with "action": "merge" are touched. Every rename is journalled to
data/payee-undo.json BEFORE the PATCH is issued.

    uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --dry-run
    uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --limit 1
    uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --undo
"""

from __future__ import annotations

import argparse
import json
import time

import httpx

from ynab_helper.cli import _load_dotenv
from ynab_helper.config import load_config, resolve_path
from ynab_helper.ynab_client import YnabClient

CLUSTERS_PATH = resolve_path("data/payee-clusters.json")
UNDO_PATH = resolve_path("data/payee-undo.json")

# YNAB allows 200 requests/hour. 18s between writes keeps a long run legal.
SLEEP_SECONDS = 18.0


def _rename(client: YnabClient, payee_id: str, new_name: str) -> None:
    resp = client._client.patch(
        f"/budgets/{client.budget_id}/payees/{payee_id}",
        json={"payee": {"name": new_name}},
    )
    resp.raise_for_status()


def _load_undo() -> list[dict]:
    if not UNDO_PATH.exists():
        return []
    with UNDO_PATH.open() as f:
        return json.load(f)


def _save_undo(entries: list[dict]) -> None:
    UNDO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UNDO_PATH.open("w") as f:
        json.dump(entries, f, indent=2)


def do_undo(client: YnabClient, count: int | None) -> None:
    entries = _load_undo()
    if not entries:
        print("Nothing to undo")
        return
    to_undo = entries if count is None else entries[-count:]
    for entry in reversed(to_undo):
        _rename(client, entry["payee_id"], entry["old_name"])
        print(f"  restored {entry['new_name']!r} -> {entry['old_name']!r}")
        time.sleep(SLEEP_SECONDS)
    _save_undo(entries[: len(entries) - len(to_undo)])
    print(f"Undid {len(to_undo)} renames")


def do_apply(client: YnabClient, dry_run: bool, limit: int | None) -> None:
    with CLUSTERS_PATH.open() as f:
        data = json.load(f)

    pending = [c for c in data["clusters"] if c.get("action") == "merge"]
    if limit is not None:
        pending = pending[:limit]
    if not pending:
        print('No clusters marked "merge"')
        return

    undo = _load_undo()
    renames = 0

    for cluster in pending:
        canonical = cluster["canonical"]
        losers = [m for m in cluster["members"] if m["name"] != canonical]
        for member in losers:
            if dry_run:
                print(f"  DRY RUN {member['name']!r} -> {canonical!r}")
                continue
            undo.append(
                {
                    "payee_id": member["id"],
                    "old_name": member["name"],
                    "new_name": canonical,
                }
            )
            _save_undo(undo)
            try:
                _rename(client, member["id"], canonical)
            except httpx.HTTPStatusError as exc:
                undo.pop()
                _save_undo(undo)
                if exc.response.status_code == 429:
                    print("Rate limited. Stopping; re-run later to resume.")
                    _write_clusters(data)
                    return
                print(f"  FAILED {member['name']!r}: {exc.response.status_code} "
                      f"{exc.response.text[:200]}")
                continue
            print(f"  {member['name']!r} -> {canonical!r}")
            renames += 1
            time.sleep(SLEEP_SECONDS)
        if not dry_run:
            cluster["action"] = "merged"

    if not dry_run:
        _write_clusters(data)
    print(f"{'Would rename' if dry_run else 'Renamed'} {renames or len(pending)} payees")


def _write_clusters(data: dict) -> None:
    with CLUSTERS_PATH.open("w") as f:
        json.dump(data, f, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="Max clusters to apply")
    ap.add_argument("--undo", action="store_true")
    ap.add_argument("--undo-count", type=int, default=None)
    args = ap.parse_args()

    _load_dotenv()
    config = load_config()
    with YnabClient(
        config.get("ynab_token", ""), config.get("budget_id", "last-used")
    ) as client:
        if args.undo:
            do_undo(client, args.undo_count)
        else:
            do_apply(client, args.dry_run, args.limit)


if __name__ == "__main__":
    main()
