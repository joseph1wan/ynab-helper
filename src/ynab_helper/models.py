from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class LineItem:
    name: str
    quantity: int
    line_total: int  # milliunits (positive)


@dataclass
class TargetOrder:
    order_id: str
    order_date: date
    total: int  # milliunits (positive)
    line_items: list[LineItem]
    tax: int = 0
    shipping: int = 0
    fees: int = 0

    @property
    def subtotal(self) -> int:
        return sum(item.line_total for item in self.line_items)


@dataclass
class YnabTransaction:
    id: str
    date: date
    amount: int  # milliunits (negative for outflow)
    payee_name: str | None
    category_id: str | None
    memo: str | None
    account_id: str
    cleared: str
    approved: bool | None
    subtransactions: list[dict[str, Any]] = field(default_factory=list)
    transfer_account_id: str | None = None

    @property
    def abs_amount(self) -> int:
        return abs(self.amount)


@dataclass
class CategorizedLine:
    line_item: LineItem
    category_name: str
    category_id: str | None = None
    matched_rule: str | None = None


@dataclass
class ProposedSplit:
    category_name: str
    category_id: str
    amount: int  # milliunits (negative for outflow)
    line_items: list[str] = field(default_factory=list)


@dataclass
class MatchProposal:
    target_order: TargetOrder
    ynab_transaction: YnabTransaction
    categorized_lines: list[CategorizedLine]
    splits: list[ProposedSplit]
    unmatched_items: list[LineItem] = field(default_factory=list)
    rounding_delta: int = 0


@dataclass
class FetchResult:
    proposals: list[MatchProposal]
    unmatched_orders: list[TargetOrder]
    unmatched_transactions: list[YnabTransaction]
    since_date: date
    fetched_at: datetime


@dataclass
class ScrapeResult:
    orders: list[TargetOrder]
    since_date: date
    fetched_at: datetime


@dataclass
class CostcoOrder:
    receipt_id: str
    receipt_date: date
    total: int  # milliunits (positive)
    line_items: list[LineItem]
    receipt_type: str  # "gas" | "warehouse"
    store_number: str
    transaction_number: str
    tax: int = 0
    shipping: int = 0  # always 0; kept so compute_splits() works unmodified via duck typing
    fees: int = 0  # always 0; ditto

    @property
    def subtotal(self) -> int:
        return sum(item.line_total for item in self.line_items)

    @property
    def order_id(self) -> str:
        """Alias so rules_audit.audit_orders() (which reads .order_id) works unmodified."""
        return self.receipt_id


@dataclass
class CostcoMatchProposal:
    costco_order: CostcoOrder
    ynab_transaction: YnabTransaction
    categorized_lines: list[CategorizedLine]
    splits: list[ProposedSplit]
    unmatched_items: list[LineItem] = field(default_factory=list)
    rounding_delta: int = 0


@dataclass
class CostcoFetchResult:
    proposals: list[CostcoMatchProposal]
    unmatched_orders: list[CostcoOrder]
    unmatched_transactions: list[YnabTransaction]
    since_date: date
    fetched_at: datetime


@dataclass
class AmazonOrder:
    order_id: str
    order_date: date
    total: int  # milliunits (positive) — Grand Total
    line_items: list[LineItem]
    tax: int = 0
    shipping: int = 0
    fees: int = 0  # always 0; kept so compute_splits() works unmodified via duck typing

    @property
    def subtotal(self) -> int:
        return sum(item.line_total for item in self.line_items)


@dataclass
class AmazonMatchProposal:
    amazon_order: AmazonOrder
    ynab_transaction: YnabTransaction
    categorized_lines: list[CategorizedLine]
    splits: list[ProposedSplit]
    unmatched_items: list[LineItem] = field(default_factory=list)
    rounding_delta: int = 0


@dataclass
class AmazonFetchResult:
    proposals: list[AmazonMatchProposal]
    unmatched_orders: list[AmazonOrder]
    unmatched_transactions: list[YnabTransaction]
    since_date: date
    fetched_at: datetime


@dataclass
class PaypalRecord:
    date: date
    name: str  # counterparty; empty on rows that never carry one
    type: str  # "General Payment" | "Mobile Payment" | "Donation Payment" | ...
    amount: int  # milliunits, signed
    note: str  # the CSV "Item Title" column
    transaction_id: str
