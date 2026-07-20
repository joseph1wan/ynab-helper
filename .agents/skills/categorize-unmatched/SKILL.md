---
name: categorize-unmatched
description: Suggest YNAB categories and regex rules for unmatched Target line items. Use when reviewing ynab-helper proposals with uncategorized Target items.
---

# Categorize Unmatched Target Items

Help the user categorize Target line items that did not match any rule in `config/rules.yaml`.

## When to use

- After running `ynab-helper fetch`, when proposals contain `unmatched_items`
- When the user asks to categorize Target products for YNAB

## Workflow

1. Read `config/categories.json` for valid YNAB category names
2. Read `config/rules.yaml` for existing patterns (avoid duplicates)
3. For each unmatched item name, suggest:
   - The best YNAB category from `categories.json`
   - A regex pattern that would match similar items in the future
4. Present suggestions as a table: item → category → proposed pattern
5. After user approval, append new rules to `config/rules.yaml`:

```yaml
  - pattern: "proposed|pattern|here"
    category: "Category Name"
```

## Guidelines

- Prefer broad but specific patterns (e.g. `oat milk|almond milk` not just `milk` if milk maps to Groceries but oat milk is ambiguous)
- Only use category names that exist in `categories.json`
- Do not remove or rewrite existing rules without explicit user consent
- Keep patterns lowercase-friendly (rules use case-insensitive matching)
- If an item is truly one-off, suggest assigning it manually in review rather than adding a brittle rule

## Example output

| Item | Suggested category | Proposed rule |
|------|-------------------|---------------|
| Huggies Overnites Size 4 | Baby | `huggies\|overnites\|size 4` |
| Method dish soap | Household | `method\|dish soap` |

Ask the user to confirm before editing `rules.yaml`.
