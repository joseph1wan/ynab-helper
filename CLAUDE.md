# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run any CLI command
uv run ynab-helper <command>

# Run tests
uv run pytest

# Run a single test
uv run pytest tests/test_core.py::test_exact_match

# Install deps (uv handles the venv automatically)
uv sync
```

The project uses `uv` — always prefix with `uv run` rather than activating a venv manually.

## Architecture

The tool has two phases: **fetch** (scrape + save) and **propose** (match + categorize).

### Data flow

```
fetch:   Target website → Playwright scraper → invoice HTML → order JSONs
propose: order JSONs + YNAB API → matcher → categorizer → split calculator → proposals/latest.json
review:  proposals/latest.json → FastAPI web UI → PATCH YNAB API
```

### Key modules

- **`target_scraper.py`** — Playwright scraper + invoice HTML parser. Two entry points:
  - `scrape_target_orders()` — live browser scrape, saves invoice HTML to `data/target-orders/debug/`
  - `load_cached_orders()` — reads saved order JSONs; always re-parses invoice HTML from `debug/` when present to override stale line items
  - `_parse_invoice_html_line_items()` — scopes parsing to each `data-test="invoice-details-card"` card; extracts name from `<b><p>`, qty from `data-test="item-quantity"`, Amount from the infoRow's last innerDiv

- **`invoice_text.py` / `invoice_import.py`** — manual fallback for when the scraper is soft-blocked or captures mid-hydration HTML: copy an invoice page's rendered text (select-all + copy, no page source needed) into `inbox/target_N.txt` (the `pb_target` shell alias does this from clipboard), then run `uv run ynab-helper import-invoices`. `invoice_text.parse_invoice_text()` parses the plain-text layout (order/invoice id, date, per-item name/qty/Amount, Invoice total — stops scanning at "Invoice total" so giftcard/coupon rows below it are never read as items); `invoice_import.import_pasted_invoices()` writes `data/target-orders/{order_id}_{invoice_id}.json` (same shape the scraper writes) and archives the source `.txt` to `data/target-orders/pasted/`. A pasted invoice takes precedence over scraped HTML for the same invoice id — `_parse_invoices_for_order()` skips re-parsing HTML when a matching paste archive exists, and never emits a duplicate bare `{order_id}.json` when an invoice-keyed JSON for that order is already on disk.

- **`import_dispatch.py`** — `import-invoices` is now the single unified CLI command for every manually-pasted source. It drains the top-level `inbox/` directory and dispatches each file by filename convention: `target_*.txt` → `invoice_import.import_pasted_invoices()`, `costco_*.txt` → `costco_import.import_pasted_receipts()`, `*.csv` (any name — PayPal is the only CSV source) → `paypal_csv.import_paypal_csvs()`. Files that don't match any pattern are reported as failures and left in `inbox/` rather than guessed at. The `pb_target` / `pb_costco` shell aliases write clipboard contents to `inbox/target_N.txt` / `inbox/costco_N.txt`; PayPal CSV exports are just dropped into `inbox/` under whatever name the browser downloaded them as. Each underlying importer still archives to its own source-specific directory (`data/target-orders/pasted/`, `data/costco-orders/pasted/`, `data/paypal/`).

- **`fetch.py`** — orchestrates both phases via `run_fetch()` and `run_propose()`

- **`matcher.py`** — exact match on `(order_date, total_milliunits)` ↔ `(txn_date, abs(txn_amount))`

- **`categorizer.py`** — first-match regex from `config/rules.yaml`; items with no match fall back to `fallback_category` and surface as `unmatched_items`

- **`rules_audit.py`** — validation/collision-detection logic used internally by `rules_editor.py` when adding or updating a rule from the web UI: for a candidate rule set, reports the winning rule *and* every other rule that also matched (a "collision" — the first-match-wins design means an item can match the wrong rule silently). Also statically validates `rules.yaml` (unknown/non-allowlisted categories, invalid regex, unanchored tokens, dead/shadowed rules, the YAML `\b`-escape trap). There is no longer a standalone CLI command for this — see the `categorize-unmatched` skill for the current workflow of reviewing applied proposals against `rules.yaml`.

- **`split_calculator.py`** — proportional split: each category's share = its line items' subtotal / order subtotal × YNAB total. Fees split evenly. Results rounded to nearest dollar (1000 milliunits); remainder goes to the largest split.

- **`state.py`** — `data/state.json` tracks `last_successful_run` for incremental scraping

### PayPal review (a second Source)

`/paypal` is a separate review flow, not part of the Target fetch/propose pipeline. See `CONTEXT.md` for the *Source* / *Review item* vocabulary and `docs/adr/006-one-module-per-source.md` for why it's its own module instead of a shared cross-account engine.

- **`paypal_csv.py`** — parses PayPal activity CSV exports (`Item Title` or `Memo` column — PayPal has used both names across exports of the same account) into `PaypalRecord`s. Drops `Bank Deposit to PP Account` rows: Paypal is an on-budget YNAB checking account, so those are BoA→Paypal transfers YNAB already models as transfers, not something to categorize. `import_paypal_csvs()` is invoked by the unified `import_dispatch.py` (see above) for any `*.csv` in `inbox/`.
- **`paypal_linker.py`** — links a `PaypalRecord` to a YNAB transaction by amount within ±3 days; ties broken by payee-name match; unresolved ties surface as candidates in the UI rather than being silently picked.
- **`paypal_rules.py`** / **`config/paypal.yaml`** — first-match payee/note → category rules, separate from `rules.yaml` (which is Target-line-item-only and audited against cached orders; `paypal.yaml` isn't).
- **`paypal_review.py`** — builds/approves the review queue for `/paypal`, scoped to the YNAB account named in `config.yaml`'s `paypal_account_name` (resolved via `YnabClient.get_account_id_by_name`). **Never widen this to "all unapproved transactions, any account"** — that was tried and explicitly rejected; each Source stays scoped to its own account(s).

**Costco and Amazon are planned as future Sources**, following the same shape: their own `*_csv.py`/`*_linker.py`/`*_review.py`/`config/*.yaml`/`/*` tab, scoped to their own YNAB account(s) or payee pattern. Expect their enrichment shape to differ (e.g. Costco/Amazon likely need per-item splits like Target, not PayPal's single-category-per-transaction) — don't force them into PayPal's shape either.

### Milliunits

All monetary values are stored as integer milliunits (YNAB's unit: $1.00 = 1000). The `_to_milliunits()` function converts dollar strings/floats. **Do not call `_to_milliunits()` on values already loaded from our own JSON files** — they're already milliunits and will be double-multiplied. Use `_order_from_json()` for that path.

### Config files

- `config/config.yaml` — `${YNAB_TOKEN}` interpolated from `.env` at startup
- `config/rules.yaml` — human-edited regex rules, plus `allowed_categories` (the curated subset of YNAB categories a Target split may target — `categories.json` is a raw dump that also includes credit-card payment categories and transfer categories that should never be a split target); re-run `sync-categories` when YNAB categories change
- `data/target-orders/*.json` — one file per order; line items are re-parsed from invoice HTML on every `load_cached_orders` call when `debug/invoice_*.html` exists
- `inbox/*.txt` — drop zone for manually copy-pasted invoice text; drained by `uv run ynab-helper import-invoices` (see `invoice_text.py` above). Successfully imported files are archived to `data/target-orders/pasted/`; failures are left in the inbox.

### Invoice HTML parser details

Target invoice pages have one `data-test="invoice-details-card"` per line item. Within each card:
- Product name: first `<b><p>…</p></b>` inside `styles_infoRow` (strip leading TCIN like `"94924105 - "`)
- Qty: `<b>N</b>` inside `data-test="item-quantity"`
- Amount (qty × unit price, pre-discount): `Amount<b>$X.XX</b>` scoped to the `styles_infoRow` div

Discount, subtotal, tax, and item-total rows appear outside the `infoRow` and must not be parsed as line items.

### Rules file gotchas

- **Patterns must be single-quoted in YAML.** A double-quoted `"\bham\b"` is parsed by PyYAML as the backspace escape `\x08`, not the regex anchor `\b` — it compiles without error and just never matches. `tests/test_rules_config.py::test_shipped_rules_compile_and_are_allowlisted` flags this if it happens.
- **Rules are first-match-wins, not best-match.** An unanchored keyword (`ham` instead of `\bham\b`) can match inside an unrelated word (`s-HAM-poo`) and silently steal an item from a later, more-specific rule. Anchor every literal keyword with `\b`; run `uv run pytest tests/test_rules_config.py` after any edit to confirm no new collisions or validation errors, and use the `categorize-unmatched` skill to review applied proposals for miscategorized items.
