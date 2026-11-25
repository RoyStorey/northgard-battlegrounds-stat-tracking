"""Local dashboard and JSON API."""
from __future__ import annotations

from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from . import stats
from .db import Database
from .models import Run


def create_app(db: Database, status: dict | None = None) -> FastAPI:
    app = FastAPI(title="Northgard Battlegrounds Tracker")
    status = status if status is not None else {}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("nbg_tracker").joinpath("web/index.html").read_text(encoding="utf-8")

    @app.get("/api/report")
    def report(patch: str | None = None, clan: str | None = None, mode: str | None = None) -> dict:
        return stats.report(db.load_runs(), patch or None, clan or None, mode or None) | {"status": status}

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return [r.to_dict() for r in db.load_runs()]

    @app.post("/api/runs", status_code=201)
    def add_run(body: dict) -> dict:
        try:
            run = Run.from_dict(body)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid run: {e}") from e
        return {"id": db.save_run(run)}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int) -> dict:
        if not db.delete_run(run_id):
            raise HTTPException(404, "no such run")
        return {"deleted": run_id}

    return app
