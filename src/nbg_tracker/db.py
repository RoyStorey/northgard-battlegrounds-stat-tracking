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
        self._migrate()

    def _migrate(self) -> None:
        cols = {t: {r[1] for r in self._conn.execute(f"PRAGMA table_info({t})")}
                for t in ("battles", "battle_structures")}
        adds = [("battles", "screenshot", "TEXT"), ("battles", "reviewed", "INTEGER NOT NULL DEFAULT 0"),
                ("battle_structures", "x", "REAL"), ("battle_structures", "y", "REAL"),
                ("battle_structures", "source", "TEXT")]
        with self._conn:
            for table, col, decl in adds:
                if col not in cols[table]:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")

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
                    "INSERT INTO battles (run_id, round, won, kind, opponent_clan, screenshot, reviewed)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (run_id, b.round, int(b.won), b.kind, b.opponent_clan, b.screenshot, int(b.reviewed)),
                ).lastrowid
                b.id = bid
                self._insert_structures(bid, b.structures)
                self._conn.executemany(
                    "INSERT INTO battle_cards (battle_id, name, god, kind) VALUES (?,?,?,?)",
                    [(bid, c.name, c.god, c.kind) for c in b.cards],
                )
        run.id = run_id
        return run_id

    def _insert_structures(self, battle_id: int, structures) -> None:
        self._conn.executemany(
            "INSERT INTO battle_structures (battle_id, name, level, x, y, source) VALUES (?,?,?,?,?,?)",
            [(battle_id, s.name, s.level, s.x, s.y, s.source) for s in structures],
        )

    def set_battle_structures(self, battle_id: int, structures, reviewed: bool = True) -> bool:
        """Replace a battle's structures, e.g. after the player reviews its screenshot."""
        with self._lock, self._conn:
            if not self._conn.execute("SELECT 1 FROM battles WHERE id = ?", (battle_id,)).fetchone():
                return False
            self._conn.execute("DELETE FROM battle_structures WHERE battle_id = ?", (battle_id,))
            self._insert_structures(battle_id, structures)
            self._conn.execute("UPDATE battles SET reviewed = ? WHERE id = ?", (int(reviewed), battle_id))
        return True

    def labelled_structures(self) -> list[tuple[str, str, int, float, float]]:
        """(screenshot, name, level, x, y) for every structure a player placed or confirmed."""
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT b.screenshot, s.name, s.level, s.x, s.y FROM battle_structures s"
                " JOIN battles b ON b.id = s.battle_id"
                " WHERE b.reviewed = 1 AND b.screenshot IS NOT NULL AND s.x IS NOT NULL")]

    def reviewed_shots(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT screenshot FROM battles WHERE reviewed = 1 AND screenshot IS NOT NULL")]

    def battle_marks(self, battle_id: int) -> tuple[str | None, list[tuple[str, int, float, float]]]:
        """A battle's screenshot and its structures with positions."""
        with self._lock:
            row = self._conn.execute("SELECT screenshot FROM battles WHERE id = ?", (battle_id,)).fetchone()
            marks = [tuple(r) for r in self._conn.execute(
                "SELECT name, level, x, y FROM battle_structures WHERE battle_id = ? AND x IS NOT NULL", (battle_id,))]
        return (row[0] if row else None), marks

    def unreviewed_shots(self, run_id: int) -> list[tuple[int, str]]:
        """(battle id, screenshot) for a run's battles that have a screenshot and aren't reviewed."""
        with self._lock:
            return [tuple(r) for r in self._conn.execute(
                "SELECT id, screenshot FROM battles WHERE run_id = ? AND reviewed = 0 AND screenshot IS NOT NULL"
                " ORDER BY round", (run_id,))]

    def run_of_battle(self, battle_id: int) -> int | None:
        with self._lock:
            r = self._conn.execute("SELECT run_id FROM battles WHERE id = ?", (battle_id,)).fetchone()
        return r[0] if r else None

    def set_run_clan(self, run_id: int, clan: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE runs SET clan = ? WHERE id = ?", (clan, run_id))

    def fill_unknown_clans(self, structure_clans: dict[str, str]) -> int:
        """Name the clan of runs recorded as Unknown, from clan-only structures on their boards."""
        from .gamedata import clan_from_structures
        filled = 0
        for run in self.load_runs():
            if run.clan not in (None, "", "Unknown"):
                continue
            clan = clan_from_structures((s.name for b in run.battles for s in b.structures), structure_clans)
            if clan:
                self.set_run_clan(run.id, clan)
                filled += 1
        return filled

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
                           opponent_clan=r["opponent_clan"], screenshot=r["screenshot"],
                           reviewed=bool(r["reviewed"]), id=r["id"])
                battles[r["id"]] = b
                runs[r["run_id"]].battles.append(b)
            for r in c.execute("SELECT * FROM battle_structures"):
                battles[r["battle_id"]].structures.append(
                    Structure(r["name"], r["level"], r["x"], r["y"], r["source"]))
            for r in c.execute("SELECT * FROM battle_cards"):
                battles[r["battle_id"]].cards.append(Card(r["name"], r["god"], r["kind"]))
        return list(runs.values())
