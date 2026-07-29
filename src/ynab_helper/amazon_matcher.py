from __future__ import annotations

from ynab_helper.categorizer import Categorizer
from ynab_helper.models import AmazonMatchProposal, AmazonOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits

# Amazon charges routinely post 1-2 days after the order date (same
# post-delay Costco/PayPal's linkers work around) — an exact-date match
# misses nearly everything, so match on amount within a window instead.
MATCH_WINDOW_DAYS = 3


def match_amazon_orders_to_transactions(
    orders: list[AmazonOrder],
    transactions: list[YnabTransaction],
    categorizer: Categorizer,
) -> tuple[list[AmazonMatchProposal], list[AmazonOrder], list[YnabTransaction]]:
    """Match Amazon orders to YNAB transactions by amount, within a +/-3 day
    window of the order date.

    Payee filtering already happened when `transactions` was fetched (see
    YnabClient.get_uncategorized_target_transactions, reused as-is for
    Amazon's payee-only scope). When more than one transaction shares an
    order's amount within the window, the closest by date wins.
    """
    proposals: list[AmazonMatchProposal] = []
    unmatched_orders: list[AmazonOrder] = []
    used_txn_ids: set[str] = set()

    for order in orders:
        candidates = [
            t
            for t in transactions
            if t.id not in used_txn_ids
            and t.abs_amount == order.total
            and abs((t.date - order.order_date).days) <= MATCH_WINDOW_DAYS
        ]
        if not candidates:
            unmatched_orders.append(order)
            continue

        txn = min(candidates, key=lambda t: abs((t.date - order.order_date).days))
        used_txn_ids.add(txn.id)

        categorized_lines, unmatched_items = categorizer.categorize_all(
            order.line_items
        )
        splits, rounding_delta = compute_splits(
            order, categorized_lines, txn.amount
        )

        proposals.append(
            AmazonMatchProposal(
                amazon_order=order,
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
