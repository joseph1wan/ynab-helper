from __future__ import annotations

from collections import defaultdict

from ynab_helper.categorizer import Categorizer
from ynab_helper.models import MatchProposal, TargetOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits

# Card auth-to-post lag (purchases) and refund processing lag both mean the
# YNAB posting date can land a few days off the invoice date, not just
# same-day. Matching is still amount-exact — only the date gets slack.
DEFAULT_DATE_TOLERANCE_DAYS = 3


def match_orders_to_transactions(
    orders: list[TargetOrder],
    transactions: list[YnabTransaction],
    categorizer: Categorizer,
    date_tolerance_days: int = DEFAULT_DATE_TOLERANCE_DAYS,
) -> tuple[list[MatchProposal], list[TargetOrder], list[YnabTransaction]]:
    proposals: list[MatchProposal] = []
    unmatched_orders: list[TargetOrder] = []
    used_txn_ids: set[str] = set()

    txn_index: dict[int, list[YnabTransaction]] = defaultdict(list)
    for txn in transactions:
        txn_index[txn.abs_amount].append(txn)

    for order in orders:
        candidates = [
            t
            for t in txn_index.get(order.total, [])
            if t.id not in used_txn_ids
            and abs((t.date - order.order_date).days) <= date_tolerance_days
        ]
        if not candidates:
            unmatched_orders.append(order)
            continue

        # Closest date wins — same-day exact matches are still picked first.
        txn = min(candidates, key=lambda t: abs((t.date - order.order_date).days))
        used_txn_ids.add(txn.id)

        categorized_lines, unmatched_items = categorizer.categorize_all(
            order.line_items
        )
        splits, rounding_delta = compute_splits(
            order, categorized_lines, txn.amount
        )

        proposals.append(
            MatchProposal(
                target_order=order,
                ynab_transaction=txn,
                categorized_lines=categorized_lines,
                splits=splits,
                unmatched_items=unmatched_items,
                rounding_delta=rounding_delta,
            )
        )

    unmatched_transactions = [
        t for t in transactions if t.id not in used_txn_ids
    ]
    return proposals, unmatched_orders, unmatched_transactions
