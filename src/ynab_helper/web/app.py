from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ynab_helper.config import load_categories, load_config, load_rules, resolve_path
from ynab_helper.fetch import clear_applied, load_proposals, recategorize_line, set_line_note
from ynab_helper.rules_editor import append_rule
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
