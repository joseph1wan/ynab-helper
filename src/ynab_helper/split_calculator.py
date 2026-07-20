from __future__ import annotations

from collections import defaultdict

from ynab_helper.models import CategorizedLine, ProposedSplit, TargetOrder


def round_to_dollar(milliunits: int) -> int:
    """Round to nearest 1000 milliunits (dollar)."""
    sign = -1 if milliunits < 0 else 1
    abs_val = abs(milliunits)
    rounded = round(abs_val / 1000) * 1000
    return sign * int(rounded)


def compute_splits(
    order: TargetOrder,
    categorized_lines: list[CategorizedLine],
    ynab_total: int,
) -> tuple[list[ProposedSplit], int]:
    """
    Compute split amounts proportional to line items, with fees split evenly.
    ynab_total is negative (outflow). Returns (splits, rounding_delta).
    """
    abs_total = abs(ynab_total)
    subtotal = order.subtotal or 1
    extra_fees = order.tax + order.shipping + order.fees
    num_lines = max(len(categorized_lines), 1)
    fee_per_line = extra_fees // num_lines

    # Group line items by category
    category_lines: dict[str, list[CategorizedLine]] = defaultdict(list)
    for line in categorized_lines:
        category_lines[line.category_name].append(line)

    raw_amounts: dict[str, int] = {}
    for category_name, lines in category_lines.items():
        cat_subtotal = sum(cl.line_item.line_total for cl in lines)
        share = cat_subtotal / subtotal
        base = int(abs_total * share)
        fee_share = fee_per_line * len(lines)
        raw_amounts[category_name] = base + fee_share

    # Adjust for any remainder from fee division
    allocated = sum(raw_amounts.values())
    remainder = abs_total - allocated
    if raw_amounts:
        largest = max(raw_amounts, key=lambda k: raw_amounts[k])
        raw_amounts[largest] += remainder

    splits: list[ProposedSplit] = []
    rounded_sum = 0
    for category_name, amount in raw_amounts.items():
        negative_amount = -amount
        rounded = round_to_dollar(negative_amount)
        line_names = [
            cl.line_item.name for cl in category_lines[category_name]
        ]
        category_id = category_lines[category_name][0].category_id
        if not category_id:
            raise ValueError(f"Unknown category: {category_name}")
        splits.append(
            ProposedSplit(
                category_name=category_name,
                category_id=category_id,
                amount=rounded,
                line_items=line_names,
            )
        )
        rounded_sum += rounded

    # Fix rounding remainder on largest split
    rounding_delta = ynab_total - rounded_sum
    if splits and rounding_delta != 0:
        largest_split = max(splits, key=lambda s: abs(s.amount))
        largest_split.amount += rounding_delta

    final_delta = ynab_total - sum(s.amount for s in splits)
    return splits, final_delta
