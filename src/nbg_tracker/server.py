"""Local dashboard, review page and JSON API."""
from __future__ import annotations

import csv
import io
import re
from importlib import resources
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from . import stats
from .db import Database
from .learning import Learner
from .models import Run, Structure

SHOT_NAME = re.compile(r"^[\w.-]+\.(jpg|png)$")
CLANS = ["Bear", "Raven", "Stag", "Wolf"]


def _page(name: str) -> str:
    return resources.files("nbg_tracker").joinpath(f"web/{name}").read_text(encoding="utf-8")


def create_app(db: Database, status: dict | None = None, shots_dir: str | Path | None = None,
               structure_names: list[str] | None = None,
               on_reviewed: Callable[[], None] | None = None,
               live: Callable[[], dict] | None = None,
               structure_clans: dict[str, str] | None = None,
               learner: Learner | None = None) -> FastAPI:
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

    def _history() -> dict[str, dict]:
        rows = stats.report(db.load_runs())["structures"]
        keep = ("impact", "battles", "win_rate", "expected_win_rate", "confidence", "runs", "usage")
        return {r["name"]: {k: r[k] for k in keep} for r in rows}

    def _verdict(h: dict | None) -> str:
        if not h:
            return "no data"
        if h["confidence"] == "low":
            return "unclear"
        return "buy" if h["impact"] >= 2 else "skip" if h["impact"] <= -2 else "neutral"

    @app.get("/api/live")
    def live_run() -> dict:
        state = live() if live else {"in_run": False}
        state["clans"] = CLANS
        state["structure_names"] = sorted(set(structure_names or []))
        board = state.get("board") or {}
        if board:
            history = _history()
            on_board = {s["name"] for s in board.get("structures", [])}
            for s in board.get("structures", []):
                s["history"] = history.get(s["name"])
            board["shop"] = [
                {"slot": i, "name": name, "history": history.get(name) if name else None,
                 "verdict": _verdict(history.get(name)) if name else "unknown", "owned": name in on_board}
                for i, name in enumerate(board.get("shop") or [])]
        return state

    @app.put("/api/live/structures")
    def live_structures(body: list[dict]) -> dict:
        if not learner:
            raise HTTPException(409, "not recording")
        try:
            return {"learned": learner.label_live(body)}
        except (LookupError, KeyError, ValueError) as e:
            raise HTTPException(409, str(e)) from e

    @app.put("/api/live/shop/{slot}")
    def live_shop(slot: int, body: dict) -> dict:
        name = str(body.get("name", "")).strip()
        if not learner or not name:
            raise HTTPException(422, "name required")
        try:
            learner.label_shop(slot, name)
        except (LookupError, IndexError) as e:
            raise HTTPException(409, str(e)) from e
        return {"slot": slot, "name": name}

    @app.put("/api/live/clan")
    def live_clan(body: dict) -> dict:
        clan = body.get("clan")
        if clan not in CLANS or not learner or not learner.set_live_clan(clan):
            raise HTTPException(409, "no run in progress, or unknown clan")
        return {"clan": clan}

    @app.put("/api/runs/{run_id}/clan")
    def run_clan(run_id: int, body: dict) -> dict:
        clan = body.get("clan")
        if clan not in CLANS:
            raise HTTPException(422, "unknown clan")
        ok = learner.set_run_clan(run_id, clan) if learner else db.set_run_clan(run_id, clan, "user")
        if not ok:
            raise HTTPException(404, "no such run")
        return {"clan": clan}

    @app.get("/api/export.csv")
    def export_csv() -> Response:
        """One row per battle, for spreadsheets."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["run_id", "started_at", "clan", "warchief", "mode", "patch", "run_wins", "run_losses",
                    "elo_before", "elo_after", "round", "season", "kind", "won", "structures", "cards", "reviewed"])
        for r in db.load_runs():
            for b in r.battles:
                w.writerow([r.id, r.started_at, r.clan, r.warchief, r.mode, r.patch, r.wins, r.losses,
                            r.elo_before, r.elo_after, b.round, b.season + 1, b.kind, int(b.won),
                            "; ".join(f"{s.name} {s.level}" for s in b.structures if s.name != "?"),
                            "; ".join(c.name for c in b.cards), int(b.reviewed)])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="battlegrounds-battles.csv"'})

    @app.get("/api/pieces")
    def pieces() -> dict:
        return {"structures": sorted(set(structure_names or []))}

    @app.get("/api/runs")
    def runs() -> list[dict]:
        return [r.to_dict() for r in db.load_runs()]

    @app.get("/api/runs/{run_id}")
    def run(run_id: int, refresh: bool = False) -> dict:
        if refresh and learner:  # re-read unreviewed rounds with everything learned so far
            learner.refresh_run(run_id)
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
        if structure_clans:
            db.fill_unknown_clans(structure_clans)
        if learner:
            learner.learn_battle(battle_id)
            if (run_id := db.run_of_battle(battle_id)) is not None:
                learner.refresh_run(run_id)
        if on_reviewed:
            on_reviewed()
        return {"saved": len(structures)}

    @app.delete("/api/runs/{run_id}")
    def delete_run(run_id: int) -> dict:
        if not db.delete_run(run_id):
            raise HTTPException(404, "no such run")
        return {"deleted": run_id}

    return app
