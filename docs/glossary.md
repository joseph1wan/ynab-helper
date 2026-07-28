# Glossary

Terms used in the Target → YNAB and Costco → YNAB auto-categorizers. These
run as separate, parallel pipelines with independent code, config, and data
directories, but share the vocabulary below.

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

## Costco

**Receipt** — A Costco purchase with a date, total, and line items. Manually pasted from a copied receipt page (Costco has no live scraper — paste is the only ingestion path).

**Gas Station Receipt** — A single-line-item receipt for a fuel purchase. The synthesized line item is always named `"Costco Gas - <grade>"` (e.g. "Costco Gas - Regular"), so gas rules can anchor on a stable prefix instead of the fuel grade alone.

**In-Warehouse Receipt** — A multi-item receipt from an in-store purchase. Item prices are listed pre-discount on the receipt; a discount line immediately below the item it applies to is netted into that item's line total during parsing, so a Line item's price already reflects any instant savings.

**Line item** — Same concept as Target's: a single product on a receipt (name, quantity, line price), after discounts have been netted in.

**Receipt ID** — Costco has no order/invoice id pair like Target. The stable identifier is a composite of warehouse number, date, and transaction number: `{store_number}_{receipt_date}_{transaction_number}` (e.g. `774_2026-07-16_439` for an in-warehouse receipt, or `774_2026-07-16_16397` for a gas station receipt using its `Invoice#` as the transaction number).

**Warehouse number** — Costco's store identifier (e.g. `774`), found as `Whse: 774` (in-warehouse receipts) or `#774` in the header (both receipt types).

**Instant Savings** — A receipt-level discount summary line on In-Warehouse receipts. Already reflected per-item via netted discount lines; never subtracted a second time.

## This tool

Match/Proposal/Rule/Fallback category/Undo snapshot apply identically to both the Target and Costco pipelines below — each pipeline has its own proposals file, rules file, and orders directory, but the concepts are the same.

**Bootstrap date** — First-run scrape start date, auto-detected from the oldest uncategorized TARGET transaction in YNAB. Costco has no scrape step, so it has no bootstrap date — its since-date comes from the oldest cached receipt.

**Match** — A Target order or Costco receipt paired with a YNAB transaction by exact date and exact amount. Costco transactions are additionally restricted to specific YNAB accounts (`costco_account_names` in config, e.g. Sapphire, Bilt) and a payee-name substring (`costco_payee_pattern`, default "COSTCO") before the date/amount match is attempted.

**Proposal** — A matched pair plus proposed category splits, written to `data/proposals/latest.json` (Target) or `data/proposals/costco-latest.json` (Costco).

**Rule** — A regex pattern in `config/rules.yaml` (Target) or `config/rules_costco.yaml` (Costco) mapping item name keywords to a YNAB category. The two rule sets are independent — a rule in one file is never consulted for the other pipeline's items.

**Fallback category** — Category used when no rule matches. Items using fallback are flagged as unmatched for rule learning.

**Undo snapshot** — JSON saved before PATCHing YNAB, allowing restore of the original lump-sum transaction. Undo snapshots are shared across both pipelines (keyed by YNAB transaction id), so `ynab-helper undo` reverts the most recent approval regardless of which pipeline applied it.
