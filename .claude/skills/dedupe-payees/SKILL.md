---
name: dedupe-payees
description: Find and merge duplicate YNAB payees created by bank-import name variants. Use when the user wants to clean up, deduplicate, or merge their YNAB payee list.
---

# Deduplicate YNAB Payees

Bank imports create a fresh payee for every string variant, so the payee list
accumulates duplicates like `Tjmaxx` / `T J Maxx` / `T.j.maxx`, and ACH feeds
append a unique reference to every single transaction. This is an occasional
cleanup, not part of the fetch/propose pipeline — it lives entirely in this
skill and touches nothing in `src/ynab_helper/`.

## What the YNAB API can actually do — verified, not assumed

Two facts were established empirically and both contradict the obvious plan:

1. **Renaming a payee does NOT merge it.** `PATCH /payees/{id}` with a name
   that already exists happily creates a second payee with the same name.
   Verified: after renaming one payee onto another's name, the budget still had
   2,535 payees and two distinct ids both named `Venmo`. Reports do not
   consolidate. **Never treat rename as merge.**
2. **There is no delete-payee endpoint.** `DELETE` on a payee URL returns
   `404.1 Invalid URI` (route absent) while `PATCH` on the same URL returns
   `404.2 resource_not_found` (route present). Only YNAB's web UI can delete
   payees.

So the only real merge is **reassigning transactions** to the surviving
payee_id, which `merge_payees.py` does via `patch_transactions_bulk`
([ynab_client.py](../../../src/ynab_helper/ynab_client.py)). That is fast (a
handful of calls, not one per payee) and it empties the duplicates, which is
what lets YNAB's UI delete them.

## Workflow

### 1. Build clusters

```bash
uv run python .claude/skills/dedupe-payees/scripts/cluster_payees.py
```

Writes `data/payee-clusters.json`: each cluster has `members`, a proposed
`canonical` name, a `confidence`, and `"action": "pending"`.

Defaults are deliberately conservative:

- `--min-txns 1` — skip clusters where no member has any transactions. Those
  are ~70% of all clusters and merging them changes no report.
- Fuzzy matching is **off**. `--fuzzy` enables it, but it produced false merges
  on real data (`Jonathan Eng`/`Jonathan Yen`, `Payment To Gao Vang`/`Payment
  From Kou Vang`, `Check 1198`/`Check 1199`). Only turn it on if you will read
  every cluster.

Clustering signals, in order of trust: `exact` (identical after normalization),
then `prefix` (one name is a whole-token prefix of another).

### 2. Review with the user

Present clusters in chat as a table — members, transaction counts, proposed
canonical. Then edit `data/payee-clusters.json`: set `"action": "merge"` on
approved clusters and `"skip"` on rejected ones, fixing `canonical` where the
user wants a different name. Only `"merge"` is acted on.

Watch for these, which review caught last time:

- A **generic head** absorbing a specific name (`Payment` ← `Payment To
  Sherilyn Ch'ng`). Skip these.
- A canonical that is an **ACH string** because no clean variant has
  transactions (`STATE OF MINNESO DES:PFML ID:...`). Retype it; the merge script
  renames the survivor for you.
- Suffixes that are **meaningfully distinct** (`Alameda County` vs `Alameda
  County Property Tax`).

### 3. Dry run, then merge

```bash
uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py --dry-run
uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py
```

For each merge cluster the script picks the member holding the **most
transactions** as the survivor, moves every other member's transactions onto
it, and renames the survivor if `canonical` differs. Both the moves and the
renames are journalled to `data/payee-merge-undo.json` before anything is
written.

### 4. Delete the emptied payees in YNAB's web UI

The merge leaves the duplicates with zero transactions. The API cannot remove
them — tell the user to do it in YNAB's web UI, which can bulk-delete unused
payees.

### 5. Undo

```bash
uv run python .claude/skills/dedupe-payees/scripts/merge_payees.py --undo
```

Restores every survivor name and moves every transaction back to its original
payee_id.

## Guidelines

- Merging rewrites history — past reports change retroactively. Say so first.
- Prefer the properly-cased human name as canonical (`Kim's Fresh Market`, not
  `KIMS FRESH MARKET MINNEAPOLIS MN`).
- Clustering is transitive (union-find), so check clusters with 4+ members
  carefully.
- `normalize()` deliberately does **not** strip digit runs: `Check 1198` and
  `TARGET 00021014` are distinct payees. Chain store numbers are collapsed
  separately, guarded by a denylist of generic heads (`check`, `payment`,
  `transfer`, …).
- City words are mined from the data (tokens before a trailing state code), and
  only stripped from names that actually ended in a state code — otherwise
  `State of Minnesota` loses `minnesota` and stops matching its own variants.
- This does **not** stop duplicates recurring; the next import brings the raw
  string back. The durable fix is a renaming rule in YNAB's own UI, which the
  API cannot create.
