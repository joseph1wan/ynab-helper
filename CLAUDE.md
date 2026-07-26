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

- **`fetch.py`** — orchestrates both phases via `run_fetch()` and `run_propose()`

- **`matcher.py`** — exact match on `(order_date, total_milliunits)` ↔ `(txn_date, abs(txn_amount))`

- **`categorizer.py`** — first-match regex from `config/rules.yaml`; items with no match fall back to `fallback_category` and surface as `unmatched_items`

- **`rules_audit.py`** — pure audit/lint logic behind `ynab-helper audit-rules`: for every distinct line-item name in cached orders, reports the winning rule *and* every other rule that also matched (a "collision" — the first-match-wins design means an item can match the wrong rule silently, which `unmatched_items` alone can never surface). Also statically validates `rules.yaml` (unknown/non-allowlisted categories, invalid regex, unanchored tokens, dead/shadowed rules, the YAML `\b`-escape trap)

- **`split_calculator.py`** — proportional split: each category's share = its line items' subtotal / order subtotal × YNAB total. Fees split evenly. Results rounded to nearest dollar (1000 milliunits); remainder goes to the largest split.

- **`state.py`** — `data/state.json` tracks `last_successful_run` for incremental scraping

### Milliunits

All monetary values are stored as integer milliunits (YNAB's unit: $1.00 = 1000). The `_to_milliunits()` function converts dollar strings/floats. **Do not call `_to_milliunits()` on values already loaded from our own JSON files** — they're already milliunits and will be double-multiplied. Use `_order_from_json()` for that path.

### Config files

- `config/config.yaml` — `${YNAB_TOKEN}` interpolated from `.env` at startup
- `config/rules.yaml` — human-edited regex rules, plus `allowed_categories` (the curated subset of YNAB categories a Target split may target — `categories.json` is a raw dump that also includes credit-card payment categories and transfer categories that should never be a split target); re-run `sync-categories` when YNAB categories change
- `data/target-orders/*.json` — one file per order; line items are re-parsed from invoice HTML on every `load_cached_orders` call when `debug/invoice_*.html` exists

### Invoice HTML parser details

Target invoice pages have one `data-test="invoice-details-card"` per line item. Within each card:
- Product name: first `<b><p>…</p></b>` inside `styles_infoRow` (strip leading TCIN like `"94924105 - "`)
- Qty: `<b>N</b>` inside `data-test="item-quantity"`
- Amount (qty × unit price, pre-discount): `Amount<b>$X.XX</b>` scoped to the `styles_infoRow` div

Discount, subtotal, tax, and item-total rows appear outside the `infoRow` and must not be parsed as line items.

### Rules file gotchas

- **Patterns must be single-quoted in YAML.** A double-quoted `"\bham\b"` is parsed by PyYAML as the backspace escape `\x08`, not the regex anchor `\b` — it compiles without error and just never matches. `uv run ynab-helper audit-rules` flags this as `yaml-backspace` if it happens.
- **Rules are first-match-wins, not best-match.** An unanchored keyword (`ham` instead of `\bham\b`) can match inside an unrelated word (`s-HAM-poo`) and silently steal an item from a later, more-specific rule. Anchor every literal keyword with `\b`; run `uv run ynab-helper audit-rules` after any edit to confirm no new collisions or validation errors.
