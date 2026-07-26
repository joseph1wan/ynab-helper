---
name: categorize-unmatched
description: Audit ynab-helper's Target categorization rules — find items with no rule, items caught by the WRONG rule, and rule collisions — then propose regex fixes. Use when reviewing Target/YNAB proposals, when an item landed in the wrong category, or when editing config/rules.yaml.
---

# Categorize Target Items

Help the user fix `config/rules.yaml` so Target line items land in the right YNAB category. This covers two failure modes:

1. An item matched **no** rule (falls to `fallback_category`).
2. An item matched the **wrong** rule — invisible unless you go looking, since `matched_rule` in a proposal only tells you what won, not what else could have.

## When to use

- After running `ynab-helper fetch`/`propose`, to review categorization quality
- When the user says an item landed in the wrong category
- When editing `config/rules.yaml` for any reason

## Workflow

1. Run `uv run ynab-helper audit-rules --json` and parse the output. This single command replaces reading `rules.yaml` and `categories.json` separately — it already cross-references both, using every cached order in `data/target-orders/` as its corpus.

2. **Also read `data/proposals/latest.json` directly** and filter `proposals[]` to `status == "applied"`. These are the user's own reviewed, confirmed corrections — the highest-signal input available, since they're places the rules demonstrably got it wrong and the user said why. For each applied proposal, walk `categorized_lines[]` and collect any entry with a `note` set or `matched_rule == "manual override"`: that triple — `name`, `category_name`, `note` — is exactly what a new or fixed rule should generalize. `status == "pending"` entries are not yet trustworthy; ignore them for rule-writing. **This step reads JSON only — the web review template (`index.html`) is not an input to rule review.**

3. Triage in this order:
   - **`issues` with `"severity": "error"`** — fix these first. They mean a rule references a category that doesn't exist or isn't allowlisted, or the regex itself is broken; `approve` will hard-fail on these at split time.
   - **Applied lines with a `note` or `matched_rule == "manual override"`** (from step 2) — the user already told you the right answer and why; this is the strongest source for a new or corrected rule.
   - **`matched[]` entries where `collisions` is non-empty** — the item matched more than one rule. Decide whether `winner` or one of the `collisions` is actually correct. This is the case the old unmatched-only workflow could never see.
   - **`matched[]` entries with no collisions** — spot-check that `winner.matched_text` is real signal, not a coincidence (e.g. a rule for `hair` matching inside "chair" isn't a collision *yet* only because no Home Decor rule for "chair" exists — anchoring prevents that class of bug before it needs a second rule to reveal it).
   - **`fallback[]`** — items with no rule at all. Propose new rules for these (the original workflow).
   - **`suspect[]`** — items with `$0` line total. These are invoice-parser artifacts (status strings like `DELIVERED`, not real products). **Never propose a rule for these.**

4. For each fallback item or manual-override note, suggest:
   - The best category from `allowed_categories` in `config/rules.yaml` — **never** a category from `categories.json` that isn't in that allowlist (it contains credit-card payment categories, transfers, and `Inflow`/`Uncategorized`, none of which a Target purchase should target).
   - A regex pattern that would match similar items in the future.

5. Present suggestions as a table with a "Currently" column so miscategorizations are visible, e.g.:

   | Item | Currently | Should be | Proposed change |
   |------|-----------|-----------|------------------|
   | Paul Mitchell Two Hair Shampoo | Groceries (`ham`, unanchored) | Personal Care | anchor rule 1's `ham` → `\bham\b` |
   | Huggies Overnites Size 4 | fallback | Baby | add `\bhuggies\b\|\bovernites\b` |
   | Toddler Girls' Ribbed Bike Shorts | manual override, note: "girl clothes" | Chloe | add `\bToddler\s+Girl\b` |

6. **Before proposing a new or edited rule, verify it with `--try-pattern`:**
   ```bash
   uv run ynab-helper audit-rules --try-pattern '<regex>' --try-category '<Name>' --try-at <index>
   ```
   Rules are first-match-wins, so where you insert matters — a rule appended at the end never fires if an earlier rule already claims the item. Show the user the before/after.

   The review UI's own "Make rule" button (in `ynab-helper review`) does this same validation and appends directly to `rules.yaml` — if the user already added a rule that way, you don't need to re-add it, just confirm it in `audit-rules` output.

7. After the user approves and the edit lands, **re-run `uv run ynab-helper audit-rules` and confirm 0 errors and no new collisions** before considering the change done.

8. Once the reviewed rules are in place and `propose` has been re-run to pick them up, the user may ask you to clear applied proposals out of the review queue (the "Clear applied" button in the review UI, or by asking you to call `clear_applied()` in `src/ynab_helper/fetch.py`). This only removes entries from `data/proposals/latest.json` — it never touches `data/target-orders/*.json`, so the item corpus `audit-rules` sees stays complete, and it never touches `data/undo/*.json`, so applied pushes stay undoable.

## Hard pattern rules

- Anchor every literal keyword with a leading `\b` (e.g. `\bham\b`, not `ham`) — unanchored keywords match inside unrelated words (`ham` inside "sHAMpoo", "Chambray"). Add a trailing `\b` too unless prefix-matching is intentional (e.g. `\bgrocer` to catch "grocery"/"groceries").
- **Patterns must be single-quoted in `rules.yaml`.** A double-quoted `"\bham\b"` is parsed as a backspace character by YAML and silently never matches anything — it compiles fine, it just never fires. `audit-rules` flags this as `yaml-backspace` if you get it wrong.
- Prefer broad but specific patterns (e.g. `\boat milk\b|\balmond milk\b`, not bare `\bmilk\b`, if plain milk maps to Groceries but oat/almond milk should go elsewhere).
- A word that's real signal in one product can be noise in another (e.g. `\bformula\b` correctly tags baby formula but also matches "DEET-Free Formula" bug spray) — narrow to context when a bare keyword is too broad, e.g. `\b(?:baby|toddler)\s+formula\b`.
- If an item is truly one-off, suggest assigning it manually in review rather than adding a brittle rule.
- Do not remove or rewrite existing rules without explicit user consent. **Anchoring an existing pattern counts as a rewrite** and needs the same consent — it can change what that rule matches.
