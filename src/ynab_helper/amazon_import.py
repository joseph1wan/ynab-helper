"""Drain manually pasted Amazon order .txt files into cached order JSON.

Workflow: copy an Amazon order-confirmation/invoice page's rendered text
into inbox/amazon_N.txt, then run `ynab-helper import-invoices` to drain the
unified inbox. Each file is parsed by amazon_invoice_text.parse_invoice_text,
written as data/amazon-orders/{order_id}.json, and archived to
data/amazon-orders/pasted/order_{order_id}.txt so it can be re-parsed later
without re-copying from Amazon. Files that fail to parse are left in the
inbox untouched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ynab_helper.amazon_invoice_text import ParsedAmazonOrder, parse_invoice_text


@dataclass
class ImportedOrder:
    source: Path
    order_id: str
    output_path: Path
    item_count: int
    total: int


@dataclass
class ImportFailure:
    source: Path
    reason: str


@dataclass
class ImportReport:
    imported: list[ImportedOrder] = field(default_factory=list)
    failed: list[ImportFailure] = field(default_factory=list)


def _order_json_dict(parsed: ParsedAmazonOrder) -> dict:
    return {
        "order_id": parsed.order_id,
        "order_date": parsed.order_date.isoformat(),
        "total": parsed.total,
        "tax": parsed.tax,
        "shipping": parsed.shipping,
        "fees": 0,
        "line_items": [
            {"name": li.name, "quantity": li.quantity, "line_total": li.line_total}
            for li in parsed.line_items
        ],
    }


def import_pasted_amazon_orders(
    inbox_dir: Path,
    archive_dir: Path,
    output_dir: Path,
    files: list[Path] | None = None,
    keep: bool = False,
) -> ImportReport:
    """Parse pasted Amazon order .txt files and write them into output_dir.

    If `files` is given, only those files are processed (still archived to
    archive_dir / moved out of wherever they were, unless `keep`). Otherwise
    every *.txt in inbox_dir is processed.
    """
    report = ImportReport()
    targets = files if files is not None else sorted(inbox_dir.glob("*.txt"))

    output_dir.mkdir(parents=True, exist_ok=True)
    if not keep:
        archive_dir.mkdir(parents=True, exist_ok=True)

    for path in targets:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            report.failed.append(ImportFailure(source=path, reason=f"could not read file: {exc}"))
            continue

        parsed = parse_invoice_text(text)
        if parsed is None:
            report.failed.append(
                ImportFailure(
                    source=path,
                    reason="could not find order id, date, totals, or items, or totals didn't reconcile",
                )
            )
            continue

        safe_order_id = parsed.order_id.replace("/", "-")
        out_path = output_dir / f"{safe_order_id}.json"
        payload = _order_json_dict(parsed)
        with out_path.open("w") as f:
            json.dump(payload, f, indent=2)

        if not keep:
            archive_path = archive_dir / f"order_{safe_order_id}.txt"
            path.replace(archive_path)

        report.imported.append(
            ImportedOrder(
                source=path,
                order_id=parsed.order_id,
                output_path=out_path,
                item_count=len(parsed.line_items),
                total=parsed.total,
            )
        )

    return report
