---
name: dedupe-payees
description: Find and merge duplicate YNAB payees created by bank-import name variants. Use when the user wants to clean up, deduplicate, or merge their YNAB payee list.
---

# Deduplicate YNAB Payees

Bank imports create a fresh payee for every string variant, so the payee list
accumulates duplicates like `Tjmaxx` / `T J Maxx` / `T.j.maxx`. This is an
occasional cleanup, not part of the fetch/propose pipeline — it lives entirely in
this skill and touches nothing in `src/ynab_helper/`.

## What YNAB's API can and cannot do

- Payee endpoints are only `GET /payees` and `PATCH /payees/{id}` (name only).
  **There is no merge endpoint.**
- So the approach is **rename-only**: rename the duplicates to a single canonical
  name. Whether YNAB collapses the entities on a name collision is undocumented —
  see "Probe first" below. Either way reports group by name, and the user can
  finish any leftover tidy-up in YNAB's web UI.
- Rate limit is **200 requests/hour**. `apply_renames.py` sleeps 18s between
  writes and resumes cleanly after a 429.

## Workflow

### 1. Build clusters

```bash
uv run python .claude/skills/dedupe-payees/scripts/cluster_payees.py
```

Writes `data/payee-clusters.json`. Each cluster has `members`, a proposed
`canonical` name (the member with the most transactions), a `confidence`, and
`"action": "pending"`.

Confidence tiers, in the order they should be reviewed:

| Tier | Signal | Trust |
|---|---|---|
| `exact` | identical after normalization | high — skim these |
| `prefix` | one name is a prefix of the other (trailing city/state noise) | high |
| `fuzzy` | edit-distance ratio ≥ threshold | **low — read every one** |

Fuzzy is where false pairs live (`Minnesota State Fair` vs
`Minnesota State Parks`). Never approve the fuzzy tier in bulk.

### 2. Review with the user

Present clusters in batches in chat as a table — members, transaction counts, and
the proposed canonical name. Do not dump all of them at once; go tier by tier and
let the user approve, reject, or retype a canonical name.

Then edit `data/payee-clusters.json`: set `"action": "merge"` on approved
clusters, `"skip"` on rejected ones, and fix `canonical` where the user gave a
better name. Leave the rest `"pending"` — only `"merge"` is acted on.

### 3. Probe first

Before any batch, apply exactly one small low-stakes cluster:

```bash
uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --limit 1
```

Re-run `cluster_payees.py` and compare `total_payees`:

- Count dropped → YNAB merges on collision. Proceed.
- Count unchanged, names now identical → cosmetic dedup only; final merge happens
  in YNAB's UI. Still worth doing, but tell the user.
- `400`/`409` in the output → collision is rejected; stop and rethink.

### 4. Apply

```bash
uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --dry-run
uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py
```

At 18s per rename, ~200 renames takes about an hour. Run it in the background and
tell the user rather than blocking. It is resumable — `action` flips to
`"merged"` per cluster as it goes.

### 5. Undo

Every rename is journalled to `data/payee-undo.json` before it is issued.

```bash
uv run python .claude/skills/dedupe-payees/scripts/apply_renames.py --undo --undo-count 5
```

Omit `--undo-count` to reverse everything in the journal.

## Guidelines

- Renames rewrite history — past reports change retroactively. Say so before the
  first batch.
- Prefer a properly-cased human name as canonical (`Kim's Fresh Market`, not
  `KIMS FRESH MARKET MINNEAPOLIS MN`), even when the ugly one has more
  transactions. Offer to override the default.
- Clustering is transitive (union-find), so a long chain can pull in an unrelated
  name. Check clusters with 4+ members carefully.
- This does **not** stop duplicates recurring — the next import brings the raw
  string back. The durable fix is a renaming rule in YNAB's own UI, which the API
  cannot create.
