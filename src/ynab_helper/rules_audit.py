from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ynab_helper.models import TargetOrder


@dataclass(frozen=True)
class RuleRef:
    index: int
    pattern: str
    category: str


@dataclass(frozen=True)
class RuleHit:
    rule: RuleRef
    matched_text: str
    span: tuple[int, int]


@dataclass
class ItemAudit:
    name: str
    occurrences: int
    order_ids: list[str]
    line_total: int
    category: str
    winner: RuleHit | None
    collisions: list[RuleHit] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationIssue:
    severity: str  # "error" | "warning" | "info"
    code: str
    rule_index: int | None
    message: str


@dataclass
class AuditReport:
    rules: list[RuleRef]
    fallback_category: str
    matched: list[ItemAudit]
    fallback: list[ItemAudit]
    suspect: list[ItemAudit]
    issues: list[ValidationIssue]


def build_rule_refs(rules: list[dict[str, str]]) -> list[RuleRef]:
    return [
        RuleRef(index=i, pattern=r.get("pattern", ""), category=r.get("category", ""))
        for i, r in enumerate(rules)
    ]


def _split_alternation(pattern: str) -> list[str]:
    """Split on top-level '|' only, respecting (?:...) groups, [...] classes, and escapes."""
    branches: list[str] = []
    current: list[str] = []
    depth = 0
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\" and i + 1 < n:
            current.append(ch)
            current.append(pattern[i + 1])
            i += 2
            continue
        if in_class:
            current.append(ch)
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            current.append(ch)
            i += 1
            continue
        if ch == "(":
            depth += 1
            current.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            current.append(ch)
            i += 1
            continue
        if ch == "|" and depth == 0:
            branches.append("".join(current))
            current = []
            i += 1
            continue
        current.append(ch)
        i += 1
    branches.append("".join(current))
    return branches


_BAIL_CHARS = set(".^$*+[]{}")
_BAIL_ESCAPES = set("dwWDS")


def _branch_probe(branch: str) -> str | None:
    """Best-effort literal example string a branch would match, or None if too dynamic."""
    result: list[str] = []
    i = 0
    n = len(branch)
    while i < n:
        ch = branch[i]
        if ch == "\\":
            if i + 1 >= n:
                return None
            nxt = branch[i + 1]
            if nxt == "b":
                i += 2
                continue
            if nxt == "s":
                i += 2
                if i < n and branch[i] in "+*?":
                    i += 1
                result.append(" ")
                continue
            if nxt in _BAIL_ESCAPES:
                return None
            if nxt in ".^$*+?()[]{}|\\":
                result.append(nxt)
                i += 2
                continue
            return None
        if ch == "(":
            if branch[i : i + 3] == "(?:":
                depth = 1
                j = i + 3
                while j < n and depth > 0:
                    if branch[j] == "\\":
                        j += 2
                        continue
                    if branch[j] == "(":
                        depth += 1
                    elif branch[j] == ")":
                        depth -= 1
                    j += 1
                if depth != 0:
                    return None
                inner = branch[i + 3 : j - 1]
                sub_branches = _split_alternation(inner)
                first_probe = _branch_probe(sub_branches[0])
                if first_probe is None:
                    return None
                result.append(first_probe)
                i = j
                if i < n and branch[i] == "?":
                    i += 1
                continue
            return None
        if ch in _BAIL_CHARS:
            return None
        if ch == "?":
            i += 1
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _is_unanchored_start(branch: str) -> bool:
    if branch.startswith(r"\b") or branch.startswith("^"):
        return False
    return bool(branch) and branch[0].isalnum()


def audit_item(name: str, rules: list[RuleRef], fallback_category: str) -> ItemAudit:
    hits: list[RuleHit] = []
    for rule in rules:
        try:
            compiled = re.compile(rule.pattern, re.IGNORECASE)
        except re.error:
            continue
        match = compiled.search(name)
        if match:
            hits.append(RuleHit(rule=rule, matched_text=match.group(0), span=match.span()))

    winner = hits[0] if hits else None
    collisions = hits[1:] if hits else []
    category = winner.rule.category if winner else fallback_category

    return ItemAudit(
        name=name,
        occurrences=0,
        order_ids=[],
        line_total=0,
        category=category,
        winner=winner,
        collisions=collisions,
    )


def audit_orders(
    orders: list[TargetOrder],
    rules: list[dict[str, str]],
    fallback_category: str,
) -> tuple[list[ItemAudit], list[ItemAudit], list[ItemAudit]]:
    """Dedup line items by name across all orders; return (matched, fallback, suspect)."""
    rule_refs = build_rule_refs(rules)

    order_of_names: list[str] = []
    audits_by_name: dict[str, ItemAudit] = {}
    occurrences_by_name: dict[str, int] = defaultdict(int)
    order_ids_by_name: dict[str, list[str]] = defaultdict(list)

    for order in orders:
        for item in order.line_items:
            name = item.name
            if name not in audits_by_name:
                order_of_names.append(name)
                audits_by_name[name] = audit_item(name, rule_refs, fallback_category)
                audits_by_name[name].line_total = item.line_total
            occurrences_by_name[name] += 1
            if order.order_id not in order_ids_by_name[name]:
                order_ids_by_name[name].append(order.order_id)

    matched: list[ItemAudit] = []
    fallback: list[ItemAudit] = []
    suspect: list[ItemAudit] = []
    for name in order_of_names:
        item_audit = audits_by_name[name]
        item_audit.occurrences = occurrences_by_name[name]
        item_audit.order_ids = order_ids_by_name[name]
        if item_audit.line_total == 0:
            suspect.append(item_audit)
        elif item_audit.winner is not None:
            matched.append(item_audit)
        else:
            fallback.append(item_audit)
    return matched, fallback, suspect


def validate_rules(
    rules: list[dict[str, str]],
    fallback_category: str,
    categories: dict[str, str],
    allowed_categories: list[str],
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    seen_patterns: dict[str, int] = {}
    compiled_rules: list[tuple[int, str, str, re.Pattern[str] | None]] = []

    for idx, rule in enumerate(rules):
        pattern = rule.get("pattern")
        category = rule.get("category")

        if not pattern or not category:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="missing-field",
                    rule_index=idx,
                    message=f"rule {idx} is missing 'pattern' or 'category'",
                )
            )
            compiled_rules.append((idx, pattern or "", category or "", None))
            continue

        try:
            compiled = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="invalid-regex",
                    rule_index=idx,
                    message=f"rule {idx} pattern {pattern!r} failed to compile: {exc}",
                )
            )
            compiled_rules.append((idx, pattern, category, None))
            continue

        compiled_rules.append((idx, pattern, category, compiled))

        if "\x08" in pattern:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="yaml-backspace",
                    rule_index=idx,
                    message=(
                        f"rule {idx} pattern contains a literal backspace (\\x08) — "
                        "this usually means \\b was written inside a double-quoted "
                        "YAML string; use single quotes instead"
                    ),
                )
            )

        if category not in categories:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="unknown-category",
                    rule_index=idx,
                    message=f"rule {idx} category {category!r} not found in categories.json",
                )
            )
        if category not in allowed_categories:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="not-allowlisted",
                    rule_index=idx,
                    message=f"rule {idx} category {category!r} is not in allowed_categories",
                )
            )

        if pattern in seen_patterns:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="duplicate-pattern",
                    rule_index=idx,
                    message=f"rule {idx} pattern is identical to rule {seen_patterns[pattern]}",
                )
            )
        else:
            seen_patterns[pattern] = idx

        if category == fallback_category:
            issues.append(
                ValidationIssue(
                    severity="info",
                    code="rule-targets-fallback",
                    rule_index=idx,
                    message=f"rule {idx} category {category!r} matches fallback_category",
                )
            )

    if fallback_category not in categories:
        issues.append(
            ValidationIssue(
                severity="error",
                code="unknown-category",
                rule_index=None,
                message=f"fallback_category {fallback_category!r} not found in categories.json",
            )
        )
    if fallback_category not in allowed_categories:
        issues.append(
            ValidationIssue(
                severity="error",
                code="not-allowlisted",
                rule_index=None,
                message=f"fallback_category {fallback_category!r} is not in allowed_categories",
            )
        )
    for entry in allowed_categories:
        if entry not in categories:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="allowlist-unknown",
                    rule_index=None,
                    message=f"allowed_categories entry {entry!r} not found in categories.json",
                )
            )

    # Branch-level checks: unanchored tokens + unanalyzable branches
    for idx, pattern, _category, compiled in compiled_rules:
        if compiled is None:
            continue
        for branch in _split_alternation(pattern):
            probe = _branch_probe(branch)
            if probe is None:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        code="unanalyzable-branch",
                        rule_index=idx,
                        message=f"rule {idx} branch {branch!r} could not be statically analyzed",
                    )
                )
            if _is_unanchored_start(branch):
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="unanchored-token",
                        rule_index=idx,
                        message=(
                            f"rule {idx} branch {branch!r} has no \\b/^ anchor at its "
                            f"start; it can match inside a longer word"
                        ),
                    )
                )

    # Shadowing: does an earlier branch (from an earlier rule, or an earlier
    # branch of the same rule) already match every analyzable branch of a rule?
    prior_branches: list[tuple[int, str, re.Pattern[str]]] = []
    for idx, pattern, _category, compiled in compiled_rules:
        if compiled is None:
            continue
        analyzable_count = 0
        shadowed_count = 0
        for branch in _split_alternation(pattern):
            probe = _branch_probe(branch)
            if probe is None:
                continue
            analyzable_count += 1
            padded = f" {probe} "
            shadowing_hit: tuple[int, str] | None = None
            for prior_idx, prior_branch, prior_compiled in prior_branches:
                if prior_compiled.search(padded):
                    shadowing_hit = (prior_idx, prior_branch)
                    break
            if shadowing_hit is not None:
                shadowed_count += 1
                prior_idx, prior_branch = shadowing_hit
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="dead-branch",
                        rule_index=idx,
                        message=(
                            f"rule {idx} branch {branch!r} is already matched by "
                            f"rule {prior_idx} branch {prior_branch!r} (via {probe!r})"
                        ),
                    )
                )
            try:
                branch_compiled = re.compile(branch, re.IGNORECASE)
            except re.error:
                branch_compiled = None
            if branch_compiled is not None:
                prior_branches.append((idx, branch, branch_compiled))
        if analyzable_count > 0 and shadowed_count == analyzable_count:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="shadowed-rule",
                    rule_index=idx,
                    message=(
                        f"rule {idx} can never fire — every analyzable branch is "
                        "shadowed by an earlier rule"
                    ),
                )
            )

    return issues


def build_report(
    orders: list[TargetOrder],
    rules: list[dict[str, str]],
    fallback_category: str,
    categories: dict[str, str],
    allowed_categories: list[str],
) -> AuditReport:
    rule_refs = build_rule_refs(rules)
    matched, fallback, suspect = audit_orders(orders, rules, fallback_category)
    issues = validate_rules(rules, fallback_category, categories, allowed_categories)
    return AuditReport(
        rules=rule_refs,
        fallback_category=fallback_category,
        matched=matched,
        fallback=fallback,
        suspect=suspect,
        issues=issues,
    )


def render_text(report: AuditReport) -> str:
    lines: list[str] = []
    total_names = len(report.matched) + len(report.fallback) + len(report.suspect)
    lines.append(
        f"config/rules.yaml — {len(report.rules)} rules, "
        f"fallback {report.fallback_category!r}"
    )
    lines.append(f"{total_names} distinct line-item names audited")
    lines.append("")

    if report.issues:
        lines.append("VALIDATION")
        for issue in report.issues:
            where = f"rule {issue.rule_index}" if issue.rule_index is not None else "global"
            lines.append(f"  {issue.severity.upper():7} {where:10} {issue.message}")
        lines.append("")

    if report.matched:
        lines.append(f"MATCHED ({len(report.matched)} names)")
        by_category: dict[str, list[ItemAudit]] = defaultdict(list)
        for item in report.matched:
            by_category[item.category].append(item)
        for category in sorted(by_category):
            lines.append(f"  {category}")
            for item in by_category[category]:
                assert item.winner is not None
                lines.append(
                    f"    #{item.winner.rule.index} {item.winner.rule.pattern!r} "
                    f"{item.name}   x{item.occurrences}"
                )
                for collision in item.collisions:
                    lines.append(
                        f"       !! also matches  #{collision.rule.index} "
                        f"{collision.rule.category}  {collision.rule.pattern!r}  "
                        f"via {collision.matched_text!r}"
                    )
        lines.append("")

    if report.fallback:
        lines.append(f"FALLBACK -> {report.fallback_category} ({len(report.fallback)} names)")
        for item in report.fallback:
            lines.append(f"    {item.name}   x{item.occurrences}")
        lines.append("")

    if report.suspect:
        lines.append(
            f"SUSPECT ({len(report.suspect)} names, $0 line total — "
            "likely invoice-parser artifacts)"
        )
        lines.append("    " + ", ".join(item.name for item in report.suspect))
        lines.append("")

    collisions_count = sum(1 for item in report.matched if item.collisions)
    errors = sum(1 for i in report.issues if i.severity == "error")
    warnings = sum(1 for i in report.issues if i.severity == "warning")
    lines.append(
        f"SUMMARY  {total_names} names | {len(report.matched)} matched | "
        f"{len(report.fallback)} fallback | {len(report.suspect)} suspect | "
        f"{collisions_count} collisions | {errors} errors, {warnings} warnings"
    )
    return "\n".join(lines)


def _rule_hit_to_dict(hit: RuleHit) -> dict[str, Any]:
    return {
        "rule_index": hit.rule.index,
        "pattern": hit.rule.pattern,
        "category": hit.rule.category,
        "matched_text": hit.matched_text,
        "span": list(hit.span),
    }


def _item_audit_to_dict(item: ItemAudit) -> dict[str, Any]:
    return {
        "name": item.name,
        "occurrences": item.occurrences,
        "order_ids": item.order_ids,
        "line_total": item.line_total,
        "category": item.category,
        "winner": _rule_hit_to_dict(item.winner) if item.winner else None,
        "collisions": [_rule_hit_to_dict(c) for c in item.collisions],
    }


def report_to_dict(report: AuditReport) -> dict[str, Any]:
    total_names = len(report.matched) + len(report.fallback) + len(report.suspect)
    errors = sum(1 for i in report.issues if i.severity == "error")
    warnings = sum(1 for i in report.issues if i.severity == "warning")
    collisions = sum(1 for item in report.matched if item.collisions)
    return {
        "fallback_category": report.fallback_category,
        "rules": [
            {"index": r.index, "pattern": r.pattern, "category": r.category}
            for r in report.rules
        ],
        "stats": {
            "distinct_names": total_names,
            "matched": len(report.matched),
            "fallback": len(report.fallback),
            "suspect": len(report.suspect),
            "collisions": collisions,
            "errors": errors,
            "warnings": warnings,
        },
        "issues": [
            {
                "severity": i.severity,
                "code": i.code,
                "rule_index": i.rule_index,
                "message": i.message,
            }
            for i in report.issues
        ],
        "matched": [_item_audit_to_dict(i) for i in report.matched],
        "fallback": [_item_audit_to_dict(i) for i in report.fallback],
        "suspect": [_item_audit_to_dict(i) for i in report.suspect],
    }
