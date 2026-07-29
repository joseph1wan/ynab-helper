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
- **`paypal_rules.py`** / **`config/paypal.yaml`** — first-match rules, separate from `rules.yaml` (which is Target-line-item-only and audited against cached orders; `paypal.yaml` isn't). **Rules match only the PayPal note/description text, never the payee name** — payee-based matching was tried and deliberately removed (a rule should describe what a payment was for, not who sent it). This means recurring counterparties whose notes vary per-payment (e.g. a nonprofit's reimbursement payouts, each note a different recipient's name) can't get an automated rule at all — those stay manually categorized every time; don't reintroduce payee matching to work around this without asking, since it was an explicit prior decision. Rules are hand-edited only — the review UI no longer auto-appends a rule on approve (it used to learn a payee-name pattern per approval, which doesn't fit the description-only model).
- **`paypal_review.py`** — builds/approves the review for `/paypal`, scoped to the YNAB account named in `config.yaml`'s `paypal_account_name` (resolved via `YnabClient.get_account_id_by_name`). **Never widen this to "all unapproved transactions, any account"** — that was tried and explicitly rejected; each Source stays scoped to its own account(s). `YnabClient.get_unapproved_account_transactions()` also excludes rows already categorized `Inflow: Ready to Assign` — these are bank-deposit transfers into Paypal with no counterparty or note, so there's nothing for a human to review.
- Two CLI commands drive `/paypal`: `build-paypal-review [--since]` does a full rebuild — re-fetches unapproved transactions from YNAB, re-links CSV records, re-applies rules from scratch. `propose-paypal` is the lightweight alternative — re-runs current `paypal.yaml` rules against the existing `data/paypal/review.json` on disk, filling in categories only on pending items that don't have one yet (never touches manually-set or applied items, never re-fetches from YNAB). Use `propose-paypal` after editing a rule; use `build-paypal-review` after importing new CSV data.

Costco (`costco_fetch.py` + `config/rules_costco.yaml` + `/costco`) and Amazon (`amazon_fetch.py` + `config/rules_amazon.yaml` + `/amazon`) are built Sources following this same shape — see their own sections below.

### Other review (the catch-all, `/other`)

`/other` shows every unapproved YNAB transaction, any account, that isn't claimed by any of the sources above. It is not itself a Source — it has no import/parse logic and no rules file, by design (anything it shows has already evaded every Source-specific matcher, so there's no established pattern to auto-apply — see `other_review.py`'s module docstring).

**Every Source must expose what it claims, or its transactions leak into `/other`.** The mechanism:

- `source_scope.py` — `SourceScope(account_ids: set[str] | None, payee_pattern: str | None)`. `None` on an axis means "no constraint on that axis"; `.claims(txn)` checks both.
- Each Source module exposes `get_source_scope(config, client) -> SourceScope` reusing its own existing config keys (see `fetch.py` — payee-only, `costco_fetch.py` — account+payee, `paypal_review.py` — account-only, `amazon_fetch.py` — payee-only).
- `sources.py` — `SCOPE_GETTERS`, a flat list of every Source's `get_source_scope`. `other_review.build_other_review()` calls all of them and excludes any transaction any scope claims.

**When adding a new Source, you MUST add its `get_source_scope()` to `SCOPE_GETTERS` in `sources.py` as part of that work** — otherwise its transactions will double-appear in both its own new tab and in `/other` (harmless but confusing: it'll look uncategorized in two places, and approving it in one tab won't clear it from the other since they're separate JSON files). This is the one integration point every new Source must touch beyond its own module/config/tab; nothing else in `other_review.py` needs to change.

### Costco review (a third Source)

`/costco` mirrors Target's fetch/propose/split shape (not PayPal's single-category shape), but Costco has no live scraper — receipts are pasted, like Amazon.

- **`costco_receipt_text.py`** — parses pasted Costco receipt text (two layouts: gas station and in-warehouse, detected from the title line). Fail-clean: returns `None` on any missing anchor or reconciliation mismatch rather than emitting bad data.
- **`costco_import.py`** — drains `inbox/costco_*.txt` into `data/costco-orders/*.json`, archiving sources to `data/costco-orders/pasted/`.
- **`costco_matcher.py`** — fuzzy match by amount within a ±3-day window (card charges post late), unlike Target's exact-date match.
- **`costco_fetch.py`** — `run_costco_propose()`, scoped by `costco_account_names` + `costco_payee_pattern`; also exposes `get_source_scope()` for `/other`.
- `propose-costco [--since] [--until]` CLI command builds the review; `/costco/rules` edits `config/rules_costco.yaml`.

### Amazon review (a fourth Source)

`/amazon` mirrors Costco's shape exactly (paste text → parse → cache JSON → fuzzy-match by amount → split by line item), scoped by payee pattern only (`amazon_payee_pattern`, default `AMAZON`) — Amazon purchases can land on any card, unlike Costco's dedicated accounts, so `amazon_fetch.py` reuses `YnabClient.get_uncategorized_target_transactions()` as-is rather than adding a new client method.

- **`amazon_invoice_text.py`** — parses pasted Amazon order-confirmation-page text (a from-scratch parser, NOT a port of Costco's fixed-width receipt parser — Amazon's paste shape is markdown-link-style items with a bare `$X.XX` price line closing each item, and `* Label:` / `$Amount` total pairs). Quantity is signaled by a bare digit-only line immediately preceding an item's name (defaults to 1 when absent) — when present, the item's price line is a *per-unit* price, not the line total, so `line_total = unit_price * quantity`. Unrecognized total rows (e.g. `Gift Card Amount`) are simply not looked up and have no effect on parsing — `Grand Total` is read directly and is the only total value matched against a YNAB transaction; nothing is computed from the other rows.
- **`amazon_import.py`** / **`amazon_orders.py`** / **`amazon_matcher.py`** — orchestration/cache/fuzzy-match layers, near-verbatim copies of Costco's equivalents (paste-shape-agnostic).
- **`amazon_fetch.py`** — `run_amazon_propose()`; also exposes `get_source_scope()` for `/other` (payee-only, see above).
- `propose-amazon [--since] [--until]` CLI command builds the review; `inbox/amazon_*.txt` is drained by `import-invoices`; `/amazon/rules` edits `config/rules_amazon.yaml`.

### Milliunits

All monetary values are stored as integer milliunits (YNAB's unit: $1.00 = 1000). The `_to_milliunits()` function converts dollar strings/floats. **Do not call `_to_milliunits()` on values already loaded from our own JSON files** — they're already milliunits and will be double-multiplied. Use `_order_from_json()` for that path.

### Config files

- `config/config.yaml` — `${YNAB_TOKEN}` interpolated from `.env` at startup
- `config/rules.yaml` — human-edited regex rules, plus `allowed_categories` (the curated subset of YNAB categories a split may target — `categories.json` is a raw dump that also includes credit-card payment categories and transfer categories that should never be a split target); re-run `sync-categories` when YNAB categories change. This same category list is duplicated verbatim as `allowed_categories` in `config/rules_costco.yaml` and `config/rules_amazon.yaml`, and as `paypal_categories` in `config/paypal.yaml` — all four sources share one unified allowlist so any source can target any category any of them needs (e.g. PayPal's `Tithe`/`Charity` or Costco's `Gas & Parking`). Adding a category to one requires adding it to the other three. `tests/test_rules_amazon_config.py::test_amazon_allowlist_matches_other_sources` cross-checks all four files stay identical.
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
