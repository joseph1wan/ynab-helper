# ADR 004: Imprecise Split Math

## Status

Accepted

## Context

RedCard 5% discount means Target line item totals may exceed the bank-posted YNAB amount. Cent-level accuracy is not required.

## Decision

- Allocate the YNAB transaction total proportionally by line item share
- Split tax/shipping/fees evenly across categorized groups
- Round each split to the nearest dollar; remainder goes to the largest split

## Consequences

- Splits always sum to the YNAB transaction amount
- Receipt-level reconciliation is approximate
- UI flags rounding deltas over $1
