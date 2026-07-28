"""Link PayPal CSV records to unapproved YNAB transactions.

Distinct from matcher.py (which matches Target orders to a single YNAB
transaction and is silent about ambiguity): this returns candidates, never a
silent pick. Paypal is an on-budget checking account, so each payment record
maps to at most one YNAB transaction; the CSV's only real contribution is the
note ("Item Title"), since YNAB already carries the counterparty as the
transaction's payee name.

Matching is amount-exact within a +/-3 day window (PayPal's activity date and
the bank's posting date routinely differ by a day or two). Same-amount
same-window collisions are common in P2P activity, so a tie is first broken
by payee name; anything still ambiguous is left for the human to resolve in
the review UI.
"""

from __future__ import annotations

from datetime import timedelta

from ynab_helper.models import PaypalRecord, YnabTransaction

MATCH_WINDOW_DAYS = 3


def link_records(
    records: list[PaypalRecord], transactions: list[YnabTransaction]
) -> dict[str, tuple[PaypalRecord | None, str | None, list[PaypalRecord]]]:
    """Return txn.id -> (linked_record_or_None, matched_via, candidates).

    matched_via is "amount" for a unique amount+date match, "name" when a
    payee-name tiebreak resolved multiple candidates, or None when nothing
    matched. candidates holds every same-amount/window record considered,
    for the UI to render when the result is ambiguous.
    """
    by_amount: dict[int, list[PaypalRecord]] = {}
    for record in records:
        by_amount.setdefault(record.amount, []).append(record)

    result: dict[str, tuple[PaypalRecord | None, str | None, list[PaypalRecord]]] = {}
    for txn in transactions:
        window = by_amount.get(txn.amount, [])
        candidates = [
            r for r in window if abs((r.date - txn.date).days) <= MATCH_WINDOW_DAYS
        ]

        if len(candidates) == 1:
            result[txn.id] = (candidates[0], "amount", candidates)
            continue

        if len(candidates) > 1:
            payee = (txn.payee_name or "").strip().lower()
            by_name = [c for c in candidates if c.name.strip().lower() == payee]
            if len(by_name) == 1:
                result[txn.id] = (by_name[0], "name", candidates)
            else:
                result[txn.id] = (None, None, candidates)
            continue

        result[txn.id] = (None, None, [])

    return result
