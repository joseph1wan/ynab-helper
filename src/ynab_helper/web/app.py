from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ynab_helper.config import load_categories, load_config, load_paypal_categories, load_rules, resolve_path
from ynab_helper.fetch import clear_applied, load_proposals, recategorize_line, set_line_note
from ynab_helper.paypal_review import (
    apply_all_pending_paypal_items,
    apply_paypal_item,
    clear_applied_paypal_items,
    load_paypal_review,
    recategorize_item,
)
from ynab_helper.rules_editor import append_rule, delete_rule, list_rules, reorder_rule, update_rule
from ynab_helper.undo import apply_all_pending, apply_proposal, list_undo_snapshots, undo_last

TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).parent / "templates")
)

app = FastAPI(title="YNAB Helper Review")


def _milliunits_to_dollars(milliunits: int) -> str:
    sign = "-" if milliunits < 0 else ""
    return f"{sign}${abs(milliunits) / 1000:.2f}"


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    config = load_config()
    proposals_path = resolve_path(config["proposals_path"])
    if not proposals_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No proposals found. Run: ynab-helper fetch",
        )
    data = load_proposals(proposals_path)
    proposals = data.get("proposals", [])
    pending = [(i, p) for i, p in enumerate(proposals) if p.get("status") != "applied"]
    applied = [(i, p) for i, p in enumerate(proposals) if p.get("status") == "applied"]
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "data": data,
            "pending": pending,
            "applied": applied,
            "undo_count": len(list_undo_snapshots()),
            "pending_count": len(pending),
            "fmt": _milliunits_to_dollars,
            "categories": sorted(load_rules().get("allowed_categories", [])),
        },
    )


def _line_patch_response(proposal: dict[str, Any], line_index: int) -> JSONResponse:
    line = proposal["categorized_lines"][line_index]
    return JSONResponse(
        {
            "category_name": line["category_name"],
            "matched_rule": line["matched_rule"],
            "note": line.get("note"),
            "splits": proposal["splits"],
            "rounding_delta": proposal["rounding_delta"],
            "unmatched_items": [item["name"] for item in proposal.get("unmatched_items", [])],
        }
    )


@app.post("/approve/{index}")
def approve(index: int) -> RedirectResponse:
    try:
        apply_proposal(index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@app.post("/recategorize/{proposal_index}/{line_index}")
def recategorize(
    proposal_index: int, line_index: int, category_name: str = Form(...)
) -> JSONResponse:
    config = load_config()
    proposals_path = resolve_path(config["proposals_path"])
    try:
        proposal = recategorize_line(proposals_path, proposal_index, line_index, category_name)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _line_patch_response(proposal, line_index)


@app.post("/note/{proposal_index}/{line_index}")
def note(proposal_index: int, line_index: int, note: str = Form("")) -> JSONResponse:
    config = load_config()
    proposals_path = resolve_path(config["proposals_path"])
    try:
        proposal = set_line_note(proposals_path, proposal_index, line_index, note)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _line_patch_response(proposal, line_index)


@app.post("/rules")
def add_rule(
    pattern: str = Form(...), category_name: str = Form(...), note: str = Form("")
) -> JSONResponse:
    try:
        result = append_rule(pattern, category_name, note or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "collisions": result.collisions,
            "warnings": [i.message for i in result.issues if i.severity == "warning"],
        }
    )


@app.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "rules.html",
        {
            "rules": list_rules(),
            "categories": sorted(load_rules().get("allowed_categories", [])),
        },
    )


@app.post("/rules/{index}/move")
def move_rule(index: int, direction: str = Form(...)) -> JSONResponse:
    rules = list_rules()
    if index < 0 or index >= len(rules):
        raise HTTPException(status_code=400, detail="Rule index out of range")

    if direction == "up":
        to_index = index - 1
    elif direction == "down":
        to_index = index + 1
    elif direction == "top":
        to_index = 0
    elif direction == "bottom":
        to_index = len(rules) - 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown direction: {direction}")

    try:
        reorder_rule(index, to_index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"rules": list_rules()})


@app.post("/rules/{index}")
def edit_rule(
    index: int, pattern: str = Form(...), category_name: str = Form(...), note: str = Form("")
) -> JSONResponse:
    try:
        result = update_rule(index, pattern, category_name, note or None)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "collisions": result.collisions,
            "warnings": [i.message for i in result.issues if i.severity == "warning"],
        }
    )


@app.post("/rules/{index}/delete")
def delete_rule_route(index: int) -> JSONResponse:
    try:
        delete_rule(index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


@app.post("/clear-applied")
def clear_applied_route() -> RedirectResponse:
    config = load_config()
    proposals_path = resolve_path(config["proposals_path"])
    clear_applied(proposals_path)
    return RedirectResponse(url="/", status_code=303)


@app.post("/approve-all")
def approve_all() -> RedirectResponse:
    apply_all_pending()
    return RedirectResponse(url="/", status_code=303)


@app.post("/undo")
def undo() -> RedirectResponse:
    restored = undo_last(1)
    if not restored:
        raise HTTPException(status_code=404, detail="Nothing to undo")
    return RedirectResponse(url="/", status_code=303)


@app.get("/paypal", response_class=HTMLResponse)
def paypal_page(request: Request) -> HTMLResponse:
    config = load_config()
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    if not review_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No PayPal review found. Run: ynab-helper build-paypal-review",
        )
    data = load_paypal_review(review_path)
    items = data.get("items", [])
    pending = [(i, item) for i, item in enumerate(items) if item.get("status") != "applied"]
    applied = [(i, item) for i, item in enumerate(items) if item.get("status") == "applied"]
    pending_categorized_count = sum(1 for _, item in pending if item.get("category_id"))
    return TEMPLATES.TemplateResponse(
        request,
        "paypal.html",
        {
            "data": data,
            "pending": pending,
            "applied": applied,
            "pending_count": pending_categorized_count,
            "undo_count": len(list_undo_snapshots()),
            "fmt": _milliunits_to_dollars,
            "categories": load_paypal_categories(),
        },
    )


@app.post("/paypal/recategorize/{index}")
def paypal_recategorize(index: int, category_name: str = Form(...)) -> JSONResponse:
    config = load_config()
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    try:
        item = recategorize_item(review_path, index, category_name)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"category_name": item["category_name"]})


@app.post("/paypal/approve/{index}")
def paypal_approve(index: int) -> RedirectResponse:
    try:
        apply_paypal_item(index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/paypal", status_code=303)


@app.post("/paypal/approve-all")
def paypal_approve_all() -> RedirectResponse:
    apply_all_pending_paypal_items()
    return RedirectResponse(url="/paypal", status_code=303)


@app.post("/paypal/clear-applied")
def paypal_clear_applied() -> RedirectResponse:
    config = load_config()
    review_path = resolve_path(config.get("paypal_review_path", "data/paypal/review.json"))
    clear_applied_paypal_items(review_path)
    return RedirectResponse(url="/paypal", status_code=303)
