"""Fetch YNAB payees, cluster near-duplicates, write a review file.

Standalone throwaway tooling for the dedupe-payees skill. Reads nothing from
the ynab_helper package except config/auth plumbing.

    uv run python .claude/skills/dedupe-payees/scripts/cluster_payees.py
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

from ynab_helper.cli import _load_dotenv
from ynab_helper.config import load_config, resolve_path
from ynab_helper.ynab_client import YnabClient

OUT_PATH = resolve_path("data/payee-clusters.json")

# ACH / card-processor junk that gets appended to imported payee strings.
# Everything from the first DES:/ID:/INDN: marker onward is transaction-specific.
_ACH_NOISE = re.compile(r"\b(des|indn|co id|ppd id|web id|id)\s*:.*", re.I)
_TRAILING_STATE = re.compile(r"(?:\s+[a-z]{2})+$")
_PUNCT = re.compile(r"[^a-z0-9]+")
_STORE_NUM = re.compile(r"\s+#?\d{3,}\b")

# Heads that are transaction bookkeeping, not merchants. A trailing number is
# the *identity* of these ("Check 1198" vs "Check 1199", "Account #5816"), so
# store-number stripping must never apply to them.
_GENERIC_HEADS = {
    "check", "payment", "payments", "transfer", "transfers", "deposit",
    "withdrawal", "interest", "ach", "bill pay", "account", "atm", "refund",
    "credit", "debit", "fee", "loan", "invoice", "online payment",
}

# Common abbreviations that make the same city look like two.
_CITY_ALIASES = {"st": "saint", "ft": "fort", "mt": "mount", "n": "north",
                 "s": "south", "e": "east", "w": "west"}


def mine_city_tokens(names: list[str], min_count: int = 3) -> set[str]:
    """Learn city words from the data instead of hardcoding geography.

    Imported names end in "<CITY> <STATE>", so any token immediately
    preceding a trailing two-letter state code is a city candidate. Requiring
    it to appear `min_count` times keeps merchant words out.
    """
    counts: dict[str, int] = defaultdict(int)
    for name in names:
        s = _PUNCT.sub(" ", name.casefold()).strip()
        parts = s.split()
        if len(parts) < 2 or len(parts[-1]) != 2 or parts[-1].isdigit():
            continue
        for tok in parts[-3:-1]:
            if tok.isalpha() and len(tok) > 1:
                counts[_CITY_ALIASES.get(tok, tok)] += 1
    return {tok for tok, n in counts.items() if n >= min_count}


def normalize(name: str, cities: set[str] | None = None) -> str:
    """Reduce a payee name to a comparable core string."""
    s = name.casefold()
    s = _ACH_NOISE.sub(" ", s)
    s = _PUNCT.sub(" ", s).strip()
    # Strip trailing state codes, possibly doubled ("wa wa"), e.g.
    # "tmobile auto pay wa wa" -> "tmobile auto pay".
    had_state = bool(_TRAILING_STATE.search(s))
    s = _TRAILING_STATE.sub("", s).strip()
    s = re.sub(r"\s+", " ", s)

    # Only peel city words off names that actually carried a location suffix
    # (i.e. ended in a state code). Without this guard, "State of Minnesota"
    # loses "minnesota" and stops matching its own ACH variants.
    if cities and had_state:
        parts = [_CITY_ALIASES.get(p, p) for p in s.split()]
        while len(parts) > 1 and parts[-1] in cities:
            parts.pop()
        s = " ".join(parts)

    # Collapse chain store numbers: "target 00021014" -> "target", but leave
    # "check 1198" alone.
    stripped = _STORE_NUM.sub(" ", s).strip()
    stripped = re.sub(r"\s+", " ", stripped)
    if stripped and stripped not in _GENERIC_HEADS:
        s = stripped
    return s


def _is_prefix_of(short: str, long_: str) -> bool:
    """True if `short` is a leading whole-token run of `long_`.

    Requires at least two tokens so a single shared first name ("jonathan")
    cannot pull "Jonathan Ching" and "Jonathan Fu" into one cluster, and
    requires the match to end on a token boundary so "michoacana pure" does
    not swallow "michoacana purepec".
    """
    if short == long_ or len(short) >= len(long_):
        return False
    if len(short) < 10 or short.count(" ") < 1:
        return False
    return long_.startswith(short) and long_[len(short)] == " "


class _Union:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def cluster(
    payees: list[dict],
    threshold: float,
    use_fuzzy: bool = False,
) -> list[dict]:
    """Group payees by exact normalized match and whole-token prefix
    containment. Fuzzy edit-distance is off by default: it cannot tell
    "Jonathan Eng"/"Jonathan Yen" apart from real duplicates."""
    cities = mine_city_tokens([p["name"] for p in payees])
    norms = [normalize(p["name"], cities) for p in payees]
    uf = _Union(len(payees))
    reason = {}

    # 1. exact normalized match
    by_norm: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(norms):
        if n:
            by_norm[n].append(i)
    for group in by_norm.values():
        for j in group[1:]:
            uf.union(group[0], j)
            reason[uf.find(group[0])] = "exact"

    # 2 & 3. bucket by first token, then compare within bucket
    by_token: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(norms):
        if n:
            by_token[n.split()[0]].append(i)

    for bucket in by_token.values():
        if len(bucket) < 2 or len(bucket) > 400:
            continue
        for x in range(len(bucket)):
            for y in range(x + 1, len(bucket)):
                i, j = bucket[x], bucket[y]
                if uf.find(i) == uf.find(j):
                    continue
                a, b = norms[i], norms[j]
                if _is_prefix_of(a, b) or _is_prefix_of(b, a):
                    uf.union(i, j)
                    reason.setdefault(uf.find(i), "prefix")
                elif use_fuzzy and SequenceMatcher(None, a, b).ratio() >= threshold:
                    uf.union(i, j)
                    reason.setdefault(uf.find(i), "fuzzy")

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(payees)):
        groups[uf.find(i)].append(i)

    clusters = []
    for root, idxs in groups.items():
        if len(idxs) < 2:
            continue
        members = sorted(
            (payees[i] for i in idxs),
            key=lambda p: -p["txn_count"],
        )
        clusters.append(
            {
                "confidence": reason.get(root, "fuzzy"),
                "canonical": pick_canonical(members),
                "action": "pending",
                "members": [
                    {"id": m["id"], "name": m["name"], "txn_count": m["txn_count"]}
                    for m in members
                ],
            }
        )

    order = {"exact": 0, "prefix": 1, "fuzzy": 2}
    clusters.sort(key=lambda c: (order[c["confidence"]], -len(c["members"])))
    return clusters


def pick_canonical(members: list[dict]) -> str:
    """Pick the cleanest-looking name among members that have transactions.

    Ranking purely by transaction count picks the ugly import string, because
    that is exactly the one the bank kept reusing ("TARGET 00021014 SAINT PAUL
    MN" over "Target"). Restricting to names you have actually transacted with
    avoids resurrecting a stale variant, and cleanliness picks the human one.
    """
    used = [m for m in members if m["txn_count"] > 0]
    tied = used or members

    def ugliness(m: dict) -> tuple:
        name = m["name"]
        return (
            name.isupper(),
            bool(re.search(r"\d", name)),
            bool(re.search(r"\b[A-Z]{2}$", name)),
            len(name),
        )

    return min(tied, key=ugliness)["name"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.90)
    ap.add_argument(
        "--fuzzy",
        action="store_true",
        help="Also cluster by edit distance (noisy; produces false merges)",
    )
    ap.add_argument(
        "--min-txns",
        type=int,
        default=1,
        help="Drop clusters where no member has at least this many transactions",
    )
    args = ap.parse_args()

    _load_dotenv()
    config = load_config()
    with YnabClient(
        config.get("ynab_token", ""), config.get("budget_id", "last-used")
    ) as client:
        raw = client._get(f"/budgets/{client.budget_id}/payees")["payees"]
        txns = client._get(f"/budgets/{client.budget_id}/transactions")["transactions"]

    counts: dict[str, int] = defaultdict(int)
    for t in txns:
        if t.get("payee_id"):
            counts[t["payee_id"]] += 1

    payees = [
        {"id": p["id"], "name": p["name"], "txn_count": counts.get(p["id"], 0)}
        for p in raw
        if not p.get("deleted")
    ]

    clusters = cluster(payees, args.threshold, use_fuzzy=args.fuzzy)
    if args.min_txns > 0:
        clusters = [
            c
            for c in clusters
            if max(m["txn_count"] for m in c["members"]) >= args.min_txns
        ]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_payees": len(payees),
                "clusters": clusters,
            },
            f,
            indent=2,
        )

    by_conf: dict[str, int] = defaultdict(int)
    for c in clusters:
        by_conf[c["confidence"]] += 1
    print(f"{len(payees)} payees -> {len(clusters)} clusters")
    for k in ("exact", "prefix", "fuzzy"):
        print(f"  {k}: {by_conf[k]}")
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
