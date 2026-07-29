"""Central registry of Source scope-getters, used by the Other review tab.

To register a new Source, add one import + one entry to SCOPE_GETTERS below.
Nothing in other_review.py needs to change.
"""
from __future__ import annotations

from typing import Any

from ynab_helper.amazon_fetch import get_source_scope as amazon_scope
from ynab_helper.costco_fetch import get_source_scope as costco_scope
from ynab_helper.fetch import get_source_scope as target_scope
from ynab_helper.models import YnabTransaction
from ynab_helper.paypal_review import get_source_scope as paypal_scope
from ynab_helper.source_scope import SourceScope
from ynab_helper.ynab_client import YnabClient

SCOPE_GETTERS = [target_scope, costco_scope, paypal_scope, amazon_scope]


def all_source_scopes(config: dict[str, Any], client: YnabClient) -> list[SourceScope]:
    return [getter(config, client) for getter in SCOPE_GETTERS]


def is_claimed(txn: YnabTransaction, scopes: list[SourceScope]) -> bool:
    return any(scope.claims(txn) for scope in scopes)
