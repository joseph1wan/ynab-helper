from __future__ import annotations

from dataclasses import dataclass

from ynab_helper.models import YnabTransaction


@dataclass(frozen=True)
class SourceScope:
    """What a Source claims. None on an axis means "no constraint there"."""

    account_ids: set[str] | None = None
    payee_pattern: str | None = None

    def claims(self, txn: YnabTransaction) -> bool:
        if self.account_ids is not None and txn.account_id not in self.account_ids:
            return False
        if self.payee_pattern is not None:
            payee = (txn.payee_name or "").upper()
            if self.payee_pattern.upper() not in payee:
                return False
        return True
