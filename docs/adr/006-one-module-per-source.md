# ADR 006: One Review Module Per Source, Not a Shared Engine

## Status

Accepted

## Context

The PayPal review tab was first built as a generic "all unapproved transactions,
any account" queue, intended as shared infra that Costco/Amazon imports could plug
into later. In practice it also surfaced Target and Amazon rows on `/queue`, which
wasn't the goal — PayPal review was meant to be scoped to the Paypal account only.

Costco and Amazon imports are already planned as separate near-term work. Each
source has its own enrichment shape (PayPal: CSV note; Costco: receipt line items;
Amazon: order line items) and its own YNAB account(s) to scope to.

## Decision

Each Source gets its own dedicated module, config file, and web tab
(`paypal_review.py` + `config/paypal.yaml` + `/paypal`, and later
`costco_review.py` + `config/costco.yaml` + `/costco`, etc.) rather than one
generic engine parameterized per source. Shared *patterns* (approve/undo,
category dropdown, autosave, local rule file) are copied per module, not
factored into a shared library.

## Consequences

- Adding a new source means writing a new module, not touching a shared one —
  lower risk of one source's needs (e.g. Costco's per-item split vs PayPal's
  single category) forcing an awkward abstraction on the others.
- Some duplication across `*_review.py` modules and their templates. Accepted
  as the cost of independent evolution — see the Source definition in
  CONTEXT.md.
- If a genuine shared need emerges later (e.g. identical approve/undo
  mechanics across three sources with zero divergence), revisit this — but
  don't extract preemptively.
