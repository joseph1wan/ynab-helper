---
name: categorize-unmatched
description: Review ynab-helper's applied Target proposals against config/rules.yaml to find manual corrections that should become new or fixed regex rules. Use when reviewing Target/YNAB proposals, when an item landed in the wrong category, or when editing config/rules.yaml.
---

# Categorize Target Items

Help the user fix `config/rules.yaml` so Target line items land in the right YNAB category, by mining the corrections they've *already made and applied* — not by re-scanning the whole order corpus.

## When to use

- After the user has reviewed and applied some proposals in `ynab-helper review`, to turn those corrections into durable rules
- When the user says an item landed in the wrong category
- When editing `config/rules.yaml` for any reason

## Workflow

1. **Read `config/rules.yaml` directly.** Note the ordered `rules:` list (pattern → category, first-match-wins) and the `allowed_categories` allowlist. This is the only source of truth for existing rules — do not run `audit-rules` or otherwise scan `data/target-orders/`.

2. **Read `data/proposals/latest.json` directly** and filter `proposals[]` to `status == "applied"`. These are the user's own reviewed, confirmed corrections — the only input for rule-writing. `status == "pending"` entries are not yet trustworthy; ignore them. **This step reads JSON only — the web review template (`index.html`) is not an input to rule review.**

3. For each applied proposal, walk `categorized_lines[]` and collect any entry with a `note` set or `matched_rule == "manual override"`. That triple — `name`, `category_name`, `note` — is exactly what a new or fixed rule should generalize.

4. For each collected line, check it against the rules read in step 1 (mentally, or with a quick regex test) to classify it:
   - **No existing rule matches the name at all** → a genuine gap; propose a new rule.
   - **An existing rule matches the name but assigned the wrong category** (i.e. the applied category differs from what that rule would have produced) → propose fixing or reordering that rule; flag it as a rewrite (see Hard pattern rules).
   - **The applied category matches what an existing rule would already produce** → nothing to do, skip it.

5. For each genuine gap or miscategorization, suggest:
   - The best category from `allowed_categories` in `config/rules.yaml` — **never** a category from `categories.json` that isn't in that allowlist (it contains credit-card payment categories, transfers, and `Inflow`/`Uncategorized`, none of which a Target purchase should target).
   - A regex pattern that would match similar items in the future.
   - If the item is truly one-off — a single unusual purchase unlikely to recur, or a categorization that depended on price/context rather than item type — say so and suggest leaving it to manual review instead of writing a brittle rule.

6. Present suggestions as a table with a "Currently" column so miscategorizations are visible, e.g.:

   | Item | Currently | Should be | Proposed change |
   |------|-----------|-----------|------------------|
   | Paul Mitchell Two Hair Shampoo | Groceries (`ham`, unanchored) | Personal Care | anchor rule 1's `ham` → `\bham\b` |
   | Huggies Overnites Size 4 | no rule | Baby | add `\bhuggies\b\|\bovernites\b` |
   | Toddler Girls' Ribbed Bike Shorts | manual override, note: "girl clothes" | Chloe | add `\bToddler\s+Girl\b` |

7. Confirm each proposed pattern against the applied-proposal item names you collected in step 3 (a plain regex test is enough — no need to run it against the full order corpus). Rules are first-match-wins, so where you insert matters — a rule appended at the end never fires if an earlier rule already claims the item. Show the user the before/after.

   The review UI's own "Make rule" button (in `ynab-helper review`) does this same kind of edit and appends directly to `rules.yaml` — if the user already added a rule that way, you don't need to re-add it.

8. After the user approves and the edit lands, re-read `config/rules.yaml` and re-check the applied-proposal lines from step 3 against it to confirm the fix actually resolves them, with no new pattern errors (bad regex, wrong-quoted `\b`, non-allowlisted category).

9. Once the reviewed rules are in place and `propose` has been re-run to pick them up, the user may ask you to clear applied proposals out of the review queue (the "Clear applied" button in the review UI, or by asking you to call `clear_applied()` in `src/ynab_helper/fetch.py`). This only removes entries from `data/proposals/latest.json` — it never touches `data/target-orders/*.json` or `data/undo/*.json`, so applied pushes stay undoable.

## Hard pattern rules

- Anchor every literal keyword with a leading `\b` (e.g. `\bham\b`, not `ham`) — unanchored keywords match inside unrelated words (`ham` inside "sHAMpoo", "Chambray"). Add a trailing `\b` too unless prefix-matching is intentional (e.g. `\bgrocer` to catch "grocery"/"groceries").
- **Patterns must be single-quoted in `rules.yaml`.** A double-quoted `"\bham\b"` is parsed as a backspace character by YAML and silently never matches anything — it compiles fine, it just never fires.
- Prefer broad but specific patterns (e.g. `\boat milk\b|\balmond milk\b`, not bare `\bmilk\b`, if plain milk maps to Groceries but oat/almond milk should go elsewhere).
- A word that's real signal in one product can be noise in another (e.g. `\bformula\b` correctly tags baby formula but also matches "DEET-Free Formula" bug spray) — narrow to context when a bare keyword is too broad, e.g. `\b(?:baby|toddler)\s+formula\b`.
- If an item is truly one-off, suggest assigning it manually in review rather than adding a brittle rule.
- Do not remove or rewrite existing rules without explicit user consent. **Anchoring an existing pattern counts as a rewrite** and needs the same consent — it can change what that rule matches.
