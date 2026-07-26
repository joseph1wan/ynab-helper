"""True-merge payees by reassigning their transactions to the canonical payee.

Renaming a payee does NOT merge it in YNAB: the API happily creates two payees
with the same name (verified). The only real merge is to move every
transaction off the duplicates and onto the surviving payee_id, which also
leaves the duplicates unused so YNAB's web UI can bulk-delete them.

    uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py --dry-run
    uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py
    uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py --undo
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone

from ynab_helper.cli import _load_dotenv
from ynab_helper.config import load_config, resolve_path
from ynab_helper.ynab_client import YnabClient

CLUSTERS_PATH = resolve_path("data/payee-clusters.json")
UNDO_PATH = resolve_path("data/payee-merge-undo.json")

# YNAB caps a bulk transaction update; stay well under it.
BATCH_SIZE = 200


def _plan(
    clusters: list[dict], txns: list[dict]
) -> tuple[list[dict], list[dict], dict]:
    """Build the transaction moves, the survivor renames, and a summary."""
    by_payee: dict[str, list[dict]] = defaultdict(list)
    for t in txns:
        if t.get("payee_id"):
            by_payee[t["payee_id"]].append(t)

    moves: list[dict] = []
    renames: list[dict] = []
    summary: dict[str, int] = {}
    for c in clusters:
        if c.get("action") != "merge":
            continue
        # The survivor is whichever member already holds the most
        # transactions, regardless of its name: moving 3 transactions onto a
        # payee that has 149 beats the reverse. If the chosen canonical name
        # differs, the survivor gets renamed to it afterwards.
        survivor = max(c["members"], key=lambda m: m["txn_count"])
        if survivor["name"] != c["canonical"]:
            renames.append(
                {
                    "payee_id": survivor["id"],
                    "old_name": survivor["name"],
                    "new_name": c["canonical"],
                }
            )
        moved = 0
        for m in c["members"]:
            if m["id"] == survivor["id"]:
                continue
            for t in by_payee.get(m["id"], []):
                moves.append(
                    {
                        "id": t["id"],
                        "old_payee_id": m["id"],
                        "new_payee_id": survivor["id"],
                    }
                )
                moved += 1
        summary[c["canonical"]] = moved
    return moves, renames, summary


def do_merge(client: YnabClient, dry_run: bool) -> None:
    with CLUSTERS_PATH.open() as f:
        data = json.load(f)

    txns = client._get(f"/budgets/{client.budget_id}/transactions")["transactions"]
    moves, renames, summary = _plan(data["clusters"], txns)

    if not moves and not renames:
        print("Nothing to do")
        return

    for name, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4} txns -> {name}")
    print(f"\n{len(moves)} transactions across {len(summary)} clusters")
    if renames:
        print(f"{len(renames)} survivor payees also get renamed:")
        for r in renames:
            print(f"    {r['old_name'][:60]!r} -> {r['new_name']!r}")

    if dry_run:
        print("\nDRY RUN - nothing written")
        return

    # Journal before writing so an interrupted run is still reversible.
    UNDO_PATH.parent.mkdir(parents=True, exist_ok=True)
    with UNDO_PATH.open("w") as f:
        json.dump(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "moves": moves,
                "renames": renames,
            },
            f,
            indent=2,
        )

    done = 0
    for i in range(0, len(moves), BATCH_SIZE):
        batch = moves[i : i + BATCH_SIZE]
        client.patch_transactions_bulk(
            [{"id": m["id"], "payee_id": m["new_payee_id"]} for m in batch]
        )
        done += len(batch)
        print(f"  moved {done}/{len(moves)}")

    for r in renames:
        resp = client._client.patch(
            f"/budgets/{client.budget_id}/payees/{r['payee_id']}",
            json={"payee": {"name": r["new_name"]}},
        )
        if resp.is_error:
            print(f"  rename FAILED {r['old_name'][:40]!r}: "
                  f"{resp.status_code} {resp.text[:120]}")
        else:
            print(f"  renamed -> {r['new_name']!r}")

    for c in data["clusters"]:
        if c.get("action") == "merge":
            c["action"] = "merged"
    with CLUSTERS_PATH.open("w") as f:
        json.dump(data, f, indent=2)

    print(
        f"\nDone. {len(summary)} payees consolidated.\n"
        "The emptied duplicates now have zero transactions. The API cannot\n"
        "delete payees - remove them in YNAB's web UI (it can bulk-delete\n"
        "unused payees)."
    )


def do_undo(client: YnabClient) -> None:
    if not UNDO_PATH.exists():
        print("No merge undo journal found")
        return
    with UNDO_PATH.open() as f:
        journal = json.load(f)
    moves = journal["moves"]

    for r in journal.get("renames", []):
        client._client.patch(
            f"/budgets/{client.budget_id}/payees/{r['payee_id']}",
            json={"payee": {"name": r["old_name"]}},
        )
    if journal.get("renames"):
        print(f"  restored {len(journal['renames'])} payee names")

    done = 0
    for i in range(0, len(moves), BATCH_SIZE):
        batch = moves[i : i + BATCH_SIZE]
        client.patch_transactions_bulk(
            [{"id": m["id"], "payee_id": m["old_payee_id"]} for m in batch]
        )
        done += len(batch)
        print(f"  restored {done}/{len(moves)}")

    UNDO_PATH.unlink()
    print(f"Reverted {len(moves)} transaction moves")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--undo", action="store_true")
    args = ap.parse_args()

    _load_dotenv()
    config = load_config()
    with YnabClient(
        config.get("ynab_token", ""), config.get("budget_id", "last-used")
    ) as client:
        if args.undo:
            do_undo(client)
        else:
            do_merge(client, args.dry_run)


if __name__ == "__main__":
    main()
