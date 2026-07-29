from __future__ import annotations

from ynab_helper.categorizer import Categorizer
from ynab_helper.models import CostcoMatchProposal, CostcoOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits

# Costco card charges routinely post 1-2 days after the receipt date (same
# post-delay PayPal's linker works around) — an exact-date match misses
# nearly everything, so match on amount within a window instead.
MATCH_WINDOW_DAYS = 3


def match_costco_orders_to_transactions(
    orders: list[CostcoOrder],
    transactions: list[YnabTransaction],
    categorizer: Categorizer,
) -> tuple[list[CostcoMatchProposal], list[CostcoOrder], list[YnabTransaction]]:
    """Match Costco receipts to YNAB transactions by amount, within a +/-3
    day window of the receipt date.

    Account and payee filtering already happened when `transactions` was
    fetched (see YnabClient.get_uncategorized_costco_transactions). When
    more than one transaction shares an order's amount within the window,
    the closest by date wins.
    """
    proposals: list[CostcoMatchProposal] = []
    unmatched_orders: list[CostcoOrder] = []
    used_txn_ids: set[str] = set()

    for order in orders:
        candidates = [
            t
            for t in transactions
            if t.id not in used_txn_ids
            and t.abs_amount == order.total
            and abs((t.date - order.receipt_date).days) <= MATCH_WINDOW_DAYS
        ]
        if not candidates:
            unmatched_orders.append(order)
            continue

        txn = min(candidates, key=lambda t: abs((t.date - order.receipt_date).days))
        used_txn_ids.add(txn.id)

        categorized_lines, unmatched_items = categorizer.categorize_all(
            order.line_items
        )
        splits, rounding_delta = compute_splits(
            order, categorized_lines, txn.amount
        )

        proposals.append(
            CostcoMatchProposal(
                costco_order=order,
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
