from __future__ import annotations

import json
from pathlib import Path

import pytest

from ynab_helper.costco_import import import_pasted_receipts

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def tmp_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    inbox = tmp_path / "pasted" / "inbox"
    archive = tmp_path / "pasted"
    output = tmp_path
    inbox.mkdir(parents=True)
    return inbox, archive, output


def test_import_writes_receipt_json_and_archives_source(tmp_dirs):
    inbox, archive, output = tmp_dirs
    dest = inbox / "1.txt"
    dest.write_text(_read("costco_gas_774_2026-07-16_16397.txt"), encoding="utf-8")

    report = import_pasted_receipts(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert len(report.imported) == 1
    assert not report.failed

    out_path = output / "774_2026-07-16_16397.json"
    assert out_path.exists()
    assert not dest.exists()
    assert (archive / "receipt_774_2026-07-16_16397.txt").exists()

    data = json.loads(out_path.read_text())
    assert data["receipt_id"] == "774_2026-07-16_16397"
    assert data["total"] == 54690
    assert data["receipt_type"] == "gas"
    assert len(data["line_items"]) == 1


def test_import_handles_both_receipt_types(tmp_dirs):
    inbox, archive, output = tmp_dirs
    (inbox / "gas.txt").write_text(_read("costco_gas_774_2026-07-16_16397.txt"), encoding="utf-8")
    (inbox / "warehouse.txt").write_text(
        _read("costco_warehouse_774_2026-07-16_439.txt"), encoding="utf-8"
    )

    report = import_pasted_receipts(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert len(report.imported) == 2
    assert not report.failed
    assert (output / "774_2026-07-16_16397.json").exists()
    assert (output / "774_2026-07-16_439.json").exists()


def test_import_reports_failure_and_leaves_file_in_place(tmp_dirs):
    inbox, archive, output = tmp_dirs
    bad = inbox / "bad.txt"
    bad.write_text("not a receipt", encoding="utf-8")

    report = import_pasted_receipts(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert not report.imported
    assert len(report.failed) == 1
    assert bad.exists()
