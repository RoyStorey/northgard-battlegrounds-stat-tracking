"""SQLite storage for runs."""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

from .models import Battle, Card, Run, Structure

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    clan TEXT NOT NULL,
    warchief TEXT,
    patch TEXT,
    mode TEXT,
    started_at TEXT,
    ended_at TEXT,
    elo_before INTEGER,
    elo_after INTEGER,
    source TEXT NOT NULL DEFAULT 'manual'
);
CREATE TABLE IF NOT EXISTS battles (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,
    won INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'pvp',
    opponent_clan TEXT
);
CREATE TABLE IF NOT EXISTS battle_structures (
    battle_id INTEGER NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS battle_cards (
    battle_id INTEGER NOT NULL REFERENCES battles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    god TEXT,
    kind TEXT
);
CREATE INDEX IF NOT EXISTS ix_battles_run ON battles(run_id);
CREATE INDEX IF NOT EXISTS ix_structures_battle ON battle_structures(battle_id);
CREATE INDEX IF NOT EXISTS ix_cards_battle ON battle_cards(battle_id);
"""


def default_db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / "nbg-tracker" / "tracker.db"


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def save_run(self, run: Run) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO runs (clan, warchief, patch, mode, started_at, ended_at,"
                " elo_before, elo_after, source) VALUES (?,?,?,?,?,?,?,?,?)",
                (run.clan, run.warchief, run.patch, run.mode, run.started_at,
                 run.ended_at, run.elo_before, run.elo_after, run.source),
            )
            run_id = cur.lastrowid
            for b in run.battles:
                bid = self._conn.execute(
                    "INSERT INTO battles (run_id, round, won, kind, opponent_clan)"
                    " VALUES (?,?,?,?,?)",
                    (run_id, b.round, int(b.won), b.kind, b.opponent_clan),
                ).lastrowid
                self._conn.executemany(
                    "INSERT INTO battle_structures (battle_id, name, level) VALUES (?,?,?)",
                    [(bid, s.name, s.level) for s in b.structures],
                )
                self._conn.executemany(
                    "INSERT INTO battle_cards (battle_id, name, god, kind) VALUES (?,?,?,?)",
                    [(bid, c.name, c.god, c.kind) for c in b.cards],
                )
        run.id = run_id
        return run_id

    def delete_run(self, run_id: int) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount > 0

    def load_runs(self) -> list[Run]:
        with self._lock:
            c = self._conn
            runs = {r["id"]: Run(**{k: r[k] for k in r.keys()})
                    for r in c.execute("SELECT * FROM runs ORDER BY id")}
            battles: dict[int, Battle] = {}
            for r in c.execute("SELECT * FROM battles ORDER BY run_id, round, id"):
                b = Battle(round=r["round"], won=bool(r["won"]), kind=r["kind"],
                           opponent_clan=r["opponent_clan"])
                battles[r["id"]] = b
                runs[r["run_id"]].battles.append(b)
            for r in c.execute("SELECT * FROM battle_structures"):
                battles[r["battle_id"]].structures.append(Structure(r["name"], r["level"]))
            for r in c.execute("SELECT * FROM battle_cards"):
                battles[r["battle_id"]].cards.append(Card(r["name"], r["god"], r["kind"]))
        return list(runs.values())
