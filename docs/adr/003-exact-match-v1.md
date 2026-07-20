# ADR 003: Exact Match v1

## Status

Accepted

## Context

Target orders and bank transactions can differ in date (posting delay) or amount (RedCard discount, returns).

## Decision

Match only on exact `(date, amount)` pairs in v1. Non-matching pairs appear in the review UI as unmatched.

## Consequences

- Simple, predictable matching
- Posting delays and discount mismatches require manual handling or future fuzzy matching
