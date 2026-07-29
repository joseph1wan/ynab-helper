# ynab-helper

Reconciles purchases and payments from external systems against a YNAB budget:
matching, categorizing, and (for single-category cases) approving transactions.

## Language

**Source**:
An external system this tool imports activity from (Target, PayPal, and
planned: Costco, Amazon). Each Source has its own import/parse logic, is
scoped to its own YNAB account(s), and gets its own review module and web
tab rather than sharing a generic cross-account engine — see ADR 006.
_Avoid_: Integration, provider.

**Split proposal**:
A Target order matched to a single YNAB transaction and divided
proportionally across categories by line-item subtotal. Produced by the
Target flow (`fetch.py`, `matcher.py`, `split_calculator.py`) and reviewed on
the `/` (Review) tab. Distinct from a Review item, which gets exactly one
category, never a split.
_Avoid_: Proposal (ambiguous outside Target context), match.

**Review item**:
A single unapproved YNAB transaction from one Source's scope, given exactly
one category (never split) and approved via that Source's review tab. The
PayPal review tab (`/paypal`) is the first of these.
_Avoid_: Queue item (the earlier, rejected all-accounts design used this
term — a Review item is always scoped to one Source).

**PayPal record**:
One row parsed from a PayPal activity CSV export (counterparty name, note,
amount, date). Distinct from a YNAB transaction — PayPal records only ever
enrich a YNAB transaction's review item; they are never written to YNAB
directly.
_Avoid_: PayPal transaction (reserve "transaction" for the YNAB side).

**Local rule**:
A first-match regex → category mapping kept in a per-Source config file
(`config/rules.yaml` for Target line items, `config/paypal.yaml` for PayPal
payee/note text), separate from YNAB's own category-name allowlist per
Source. Appended automatically when a human categorizes a Review item in the
UI.
_Avoid_: Auto-categorization (implies ML/heuristics — this is human-written,
human-reviewable regex only).
