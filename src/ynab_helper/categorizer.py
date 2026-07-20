from __future__ import annotations

import re

from ynab_helper.config import load_categories, load_rules
from ynab_helper.models import CategorizedLine, LineItem


class Categorizer:
    def __init__(
        self,
        rules: list[dict[str, str]] | None = None,
        fallback_category: str | None = None,
        categories: dict[str, str] | None = None,
    ) -> None:
        rules_data = load_rules() if rules is None else {"rules": rules}
        self.rules = [
            (re.compile(item["pattern"], re.IGNORECASE), item["category"])
            for item in rules_data.get("rules", [])
        ]
        self.fallback_category = (
            fallback_category or rules_data.get("fallback_category", "Shopping")
        )
        self.categories = categories or load_categories()

    def categorize(self, line_item: LineItem) -> CategorizedLine:
        for pattern, category_name in self.rules:
            if pattern.search(line_item.name):
                return CategorizedLine(
                    line_item=line_item,
                    category_name=category_name,
                    category_id=self.categories.get(category_name),
                    matched_rule=pattern.pattern,
                )
        return CategorizedLine(
            line_item=line_item,
            category_name=self.fallback_category,
            category_id=self.categories.get(self.fallback_category),
            matched_rule=None,
        )

    def categorize_all(
        self, line_items: list[LineItem]
    ) -> tuple[list[CategorizedLine], list[LineItem]]:
        categorized: list[CategorizedLine] = []
        unmatched: list[LineItem] = []
        for item in line_items:
            result = self.categorize(item)
            if result.matched_rule is None and result.category_name == self.fallback_category:
                unmatched.append(item)
            categorized.append(result)
        return categorized, unmatched
