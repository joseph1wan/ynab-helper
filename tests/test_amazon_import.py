from __future__ import annotations

import json
from pathlib import Path

import pytest

from ynab_helper.amazon_import import import_pasted_amazon_orders

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


def test_import_writes_order_json_and_archives_source(tmp_dirs):
    inbox, archive, output = tmp_dirs
    dest = inbox / "1.txt"
    dest.write_text(_read("amazon_sample_1.txt"), encoding="utf-8")

    report = import_pasted_amazon_orders(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert len(report.imported) == 1
    assert not report.failed

    out_path = output / "111-1239029-5887460.json"
    assert out_path.exists()
    assert not dest.exists()
    assert (archive / "order_111-1239029-5887460.txt").exists()

    data = json.loads(out_path.read_text())
    assert data["order_id"] == "111-1239029-5887460"
    assert data["total"] == 30730
    assert len(data["line_items"]) == 3


def test_import_handles_multi_qty_order(tmp_dirs):
    inbox, archive, output = tmp_dirs
    (inbox / "2.txt").write_text(_read("amazon_sample_2.txt"), encoding="utf-8")

    report = import_pasted_amazon_orders(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert len(report.imported) == 1
    assert not report.failed
    out_path = output / "114-2174468-2163434.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["line_items"][0]["quantity"] == 2
    assert data["line_items"][0]["line_total"] == 15380


def test_import_reports_failure_and_leaves_file_in_place(tmp_dirs):
    inbox, archive, output = tmp_dirs
    bad = inbox / "bad.txt"
    bad.write_text("not an amazon order", encoding="utf-8")

    report = import_pasted_amazon_orders(inbox_dir=inbox, archive_dir=archive, output_dir=output)

    assert not report.imported
    assert len(report.failed) == 1
    assert bad.exists()
