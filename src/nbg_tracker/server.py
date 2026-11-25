"""Local dashboard, review page and JSON API."""
from __future__ import annotations

import re
from importlib import resources
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from . import stats
from .db import Database
from .models import Run, Structure

SHOT_NAME = re.compile(r"^[\w.-]+\.(jpg|png)$")


def _page(name: str) -> str:
    return resources.files("nbg_tracker").joinpath(f"web/{name}").read_text(encoding="utf-8")


def create_app(db: Database, status: dict | None = None, shots_dir: str | Path | None = None,
               structure_names: list[str] | None = None,
               on_reviewed: Callable[[], None] | None = None) -> FastAPI:
    app = FastAPI(title="Northgard Battlegrounds Tracker")
    status = status if status is not None else {}
    shots = Path(shots_dir) if shots_dir else None

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page("index.html")

    @app.get("/review", response_class=HTMLResponse)
    def review_page() -> str:
        return _page("review.html")

    @app.get("/shots/{name}")
    def shot(name: str) -> FileResponse:
        if shots is None or not SHOT_NAME.match(name) or not (shots / name).is_file():
            raise HTTPException(404, "no such screenshot")
        return FileResponse(shots / name)

    @app.get("/api/report")
    def report(patch: str | None = None, clan: str | None = None, mode: str | None = None) -> dict:
        return stats.report(db.load_runs(), patch or None, clan or None, mode or None) | {"status": status}

    @app.get("/api/pieces")
    def pieces() -> dict:
        return {"structures": sorted(set(structure_names or []))}

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return [r.to_dict() for r in db.load_runs()]

    @app.get("/api/runs/{run_id}")
    def run(run_id: int) -> dict:
        for r in db.load_runs():
            if r.id == run_id:
                return r.to_dict()
        raise HTTPException(404, "no such run")

    @app.post("/api/runs", status_code=201)
    def add_run(body: dict) -> dict:
        try:
            run = Run.from_dict(body)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid run: {e}") from e
        return {"id": db.save_run(run)}

    @app.put("/api/battles/{battle_id}/structures")
    def set_structures(battle_id: int, body: list[dict]) -> dict:
        try:
            structures = [Structure(str(s["name"]), int(s.get("level", 1)),
                                    float(s["x"]) if s.get("x") is not None else None,
                                    float(s["y"]) if s.get("y") is not None else None, "user")
                          for s in body if str(s.get("name", "")).strip() not in ("", "?")]
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(422, f"invalid structures: {e}") from e
        if not db.set_battle_structures(battle_id, structures):
            raise HTTPException(404, "no such battle")
        if on_reviewed:
            on_reviewed()
        return {"saved": len(structures)}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int) -> dict:
        if not db.delete_run(run_id):
            raise HTTPException(404, "no such run")
        return {"deleted": run_id}

    return app
