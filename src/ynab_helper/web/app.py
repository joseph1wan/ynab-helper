from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ynab_helper.config import (
    load_categories,
    load_config,
    load_paypal_categories,
    load_rules,
    load_rules_costco,
    resolve_path,
)
from ynab_helper.costco_fetch import recategorize_line_costco
from ynab_helper.costco_orders import load_cached_costco_orders
from ynab_helper.fetch import clear_applied, load_proposals, recategorize_line, set_line_note
from ynab_helper.paypal_review import (
    apply_all_pending_paypal_items,
    apply_paypal_item,
    clear_applied_paypal_items,
    load_paypal_review,
    recategorize_item,
)
from ynab_helper.rules_editor import (
    COSTCO_RULES_PATH,
    append_rule,
    delete_rule,
    list_rules,
    reorder_rule,
    update_rule,
)
from ynab_helper.undo import (
    apply_all_pending,
    apply_all_pending_costco,
    apply_costco_proposal,
    apply_proposal,
    list_undo_snapshots,
    undo_last,
)

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


@app.get("/costco", response_class=HTMLResponse)
def costco_index(request: Request) -> HTMLResponse:
    config = load_config()
    proposals_path = resolve_path(config.get("costco_proposals_path", "data/proposals/costco-latest.json"))
    if not proposals_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No Costco proposals found. Run: ynab-helper propose-costco",
        )
    data = load_proposals(proposals_path)
    proposals = data.get("proposals", [])
    pending = [(i, p) for i, p in enumerate(proposals) if p.get("status") != "applied"]
    applied = [(i, p) for i, p in enumerate(proposals) if p.get("status") == "applied"]
    return TEMPLATES.TemplateResponse(
        request,
        "costco.html",
        {
            "data": data,
            "pending": pending,
            "applied": applied,
            "undo_count": len(list_undo_snapshots()),
            "pending_count": len(pending),
            "fmt": _milliunits_to_dollars,
            "categories": sorted(load_rules_costco().get("allowed_categories", [])),
        },
    )


@app.post("/costco/approve/{index}")
def costco_approve(index: int) -> RedirectResponse:
    try:
        apply_costco_proposal(index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/costco", status_code=303)


@app.post("/costco/recategorize/{proposal_index}/{line_index}")
def costco_recategorize(
    proposal_index: int, line_index: int, category_name: str = Form(...)
) -> JSONResponse:
    config = load_config()
    proposals_path = resolve_path(config.get("costco_proposals_path", "data/proposals/costco-latest.json"))
    try:
        proposal = recategorize_line_costco(proposals_path, proposal_index, line_index, category_name)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _line_patch_response(proposal, line_index)


@app.post("/costco/note/{proposal_index}/{line_index}")
def costco_note(proposal_index: int, line_index: int, note: str = Form("")) -> JSONResponse:
    config = load_config()
    proposals_path = resolve_path(config.get("costco_proposals_path", "data/proposals/costco-latest.json"))
    try:
        proposal = set_line_note(proposals_path, proposal_index, line_index, note)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _line_patch_response(proposal, line_index)


@app.post("/costco/rules")
def costco_add_rule(
    pattern: str = Form(...), category_name: str = Form(...), note: str = Form("")
) -> JSONResponse:
    config = load_config()
    orders_dir = resolve_path(config.get("costco_orders_dir", "data/costco-orders"))
    try:
        result = append_rule(
            pattern,
            category_name,
            note or None,
            rules_path=COSTCO_RULES_PATH,
            rules_data=load_rules_costco(),
            orders=load_cached_costco_orders(orders_dir, date.min),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "collisions": result.collisions,
            "warnings": [i.message for i in result.issues if i.severity == "warning"],
        }
    )


@app.get("/costco/rules", response_class=HTMLResponse)
def costco_rules_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "costco_rules.html",
        {
            "rules": list_rules(rules_path=COSTCO_RULES_PATH),
            "categories": sorted(load_rules_costco().get("allowed_categories", [])),
        },
    )


@app.post("/costco/rules/{index}/move")
def costco_move_rule(index: int, direction: str = Form(...)) -> JSONResponse:
    rules = list_rules(rules_path=COSTCO_RULES_PATH)
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
        reorder_rule(index, to_index, rules_path=COSTCO_RULES_PATH)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"rules": list_rules(rules_path=COSTCO_RULES_PATH)})


@app.post("/costco/rules/{index}")
def costco_edit_rule(
    index: int, pattern: str = Form(...), category_name: str = Form(...), note: str = Form("")
) -> JSONResponse:
    config = load_config()
    orders_dir = resolve_path(config.get("costco_orders_dir", "data/costco-orders"))
    try:
        result = update_rule(
            index,
            pattern,
            category_name,
            note or None,
            rules_path=COSTCO_RULES_PATH,
            rules_data=load_rules_costco(),
            orders=load_cached_costco_orders(orders_dir, date.min),
        )
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(
        {
            "ok": True,
            "collisions": result.collisions,
            "warnings": [i.message for i in result.issues if i.severity == "warning"],
        }
    )


@app.post("/costco/rules/{index}/delete")
def costco_delete_rule_route(index: int) -> JSONResponse:
    try:
        delete_rule(index, rules_path=COSTCO_RULES_PATH)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True})


@app.post("/costco/clear-applied")
def costco_clear_applied_route() -> RedirectResponse:
    config = load_config()
    proposals_path = resolve_path(config.get("costco_proposals_path", "data/proposals/costco-latest.json"))
    clear_applied(proposals_path)
    return RedirectResponse(url="/costco", status_code=303)


@app.post("/costco/approve-all")
def costco_approve_all() -> RedirectResponse:
    apply_all_pending_costco()
    return RedirectResponse(url="/costco", status_code=303)


@app.post("/costco/undo")
def costco_undo() -> RedirectResponse:
    restored = undo_last(1)
    if not restored:
        raise HTTPException(status_code=404, detail="Nothing to undo")
    return RedirectResponse(url="/costco", status_code=303)


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
