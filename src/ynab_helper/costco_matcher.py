from __future__ import annotations

from ynab_helper.categorizer import Categorizer
from ynab_helper.models import CostcoMatchProposal, CostcoOrder, YnabTransaction
from ynab_helper.split_calculator import compute_splits


def match_costco_orders_to_transactions(
    orders: list[CostcoOrder],
    transactions: list[YnabTransaction],
    categorizer: Categorizer,
) -> tuple[list[CostcoMatchProposal], list[CostcoOrder], list[YnabTransaction]]:
    """Match Costco receipts to YNAB transactions by exact date+total.

    Account and payee filtering already happened when `transactions` was
    fetched (see YnabClient.get_uncategorized_costco_transactions) — this
    function only does the date+amount join, same as Target's matcher.
    """
    proposals: list[CostcoMatchProposal] = []
    unmatched_orders: list[CostcoOrder] = []
    used_txn_ids: set[str] = set()

    txn_index: dict[tuple[str, int], list[YnabTransaction]] = {}
    for txn in transactions:
        key = (txn.date.isoformat(), txn.abs_amount)
        txn_index.setdefault(key, []).append(txn)

    for order in orders:
        key = (order.receipt_date.isoformat(), order.total)
        candidates = [
            t for t in txn_index.get(key, []) if t.id not in used_txn_ids
        ]
        if not candidates:
            unmatched_orders.append(order)
            continue

        txn = candidates[0]
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
