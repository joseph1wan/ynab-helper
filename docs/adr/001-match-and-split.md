# ADR 001: Match and Split Entry Point

## Status

Accepted

## Context

Target purchases appear in YNAB as single lump-sum bank transactions. Target order history contains item-level detail.

## Decision

Scrape Target for line items, match orders to existing uncategorized YNAB TARGET transactions by exact date and amount, then propose split transactions.

## Consequences

- Requires Target session auth and YNAB PAT
- Does not create new YNAB transactions
- Unmatched orders or transactions surface for manual review
