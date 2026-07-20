from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ynab_helper.config import load_config, resolve_path
from ynab_helper.fetch import load_proposals
from ynab_helper.undo import apply_proposal, list_undo_snapshots, undo_last

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
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "data": data,
            "undo_count": len(list_undo_snapshots()),
            "fmt": _milliunits_to_dollars,
        },
    )


@app.post("/approve/{index}")
def approve(index: int) -> dict[str, str]:
    try:
        apply_proposal(index)
    except (IndexError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok"}


@app.post("/undo")
def undo() -> dict[str, object]:
    restored = undo_last(1)
    if not restored:
        raise HTTPException(status_code=404, detail="Nothing to undo")
    return {"status": "ok", "restored": restored}
