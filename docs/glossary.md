# Glossary

Terms used in the Target → YNAB auto-categorizer.

## YNAB

**Budget** — An independent budget file (e.g. Personal). Most API calls require a `budget_id`.

**Category** — An envelope for a purpose (Groceries, Baby). Categories belong to category groups.

**Category group** — A container for related categories (e.g. "Monthly Bills").

**Inbox / uncategorized** — A bank-imported transaction with no `category_id`. The tool targets these for splitting.

**Milliunits** — YNAB's integer currency format. $10.50 = `10500`. Outflows are negative (`-10500`).

**Ready to Assign** — Pool of unallocated dollars. Not modified by this tool.

**Split transaction** — One parent transaction divided into subtransactions, each with its own category and amount.

**Subtransaction** — A line within a split. Created via PATCH with a `subtransactions` array.

## Target

**Order** — A purchase with a date, total, and line items. Scraped from Target order history.

**Line item** — A single product on an order (name, quantity, line price).

**Order total** — What Target reports. May differ from the bank-posted YNAB amount due to RedCard discounts.

## This tool

**Bootstrap date** — First-run scrape start date, auto-detected from the oldest uncategorized TARGET transaction in YNAB.

**Match** — A Target order paired with a YNAB transaction by exact date and exact amount.

**Proposal** — A matched pair plus proposed category splits, written to `data/proposals/latest.json`.

**Rule** — A regex pattern in `config/rules.yaml` mapping item name keywords to a YNAB category.

**Fallback category** — Category used when no rule matches. Items using fallback are flagged as unmatched for rule learning.

**Undo snapshot** — JSON saved before PATCHing YNAB, allowing restore of the original lump-sum transaction.
