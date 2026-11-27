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
CREATE TABLE IF NOT EXISTS labels (
    shot TEXT NOT NULL,
    name TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 1,
    x REAL NOT NULL,
    y REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shop_labels (
    file TEXT NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS card_catalog (
    name TEXT PRIMARY KEY,
    kind TEXT,
    god TEXT,
    favor INTEGER NOT NULL DEFAULT 0,
    text TEXT,
    first_seen TEXT,
    last_seen TEXT,
    offered INTEGER NOT NULL DEFAULT 0,
    picked INTEGER NOT NULL DEFAULT 0
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
                for t in ("runs", "battles", "battle_structures")}
        adds = [("runs", "clan_source", "TEXT"), ("battles", "screenshot", "TEXT"), ("battles", "reviewed", "INTEGER NOT NULL DEFAULT 0"),
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
                " elo_before, elo_after, source, clan_source) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run.clan, run.warchief, run.patch, run.mode, run.started_at,
                 run.ended_at, run.elo_before, run.elo_after, run.source, run.clan_source),
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
                " WHERE b.reviewed = 1 AND b.screenshot IS NOT NULL AND s.x IS NOT NULL"
                " UNION ALL SELECT shot, name, level, x, y FROM labels")]

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

    def set_run_clan(self, run_id: int, clan: str, source: str | None = None) -> bool:
        with self._lock, self._conn:
            return self._conn.execute("UPDATE runs SET clan = ?, clan_source = ? WHERE id = ?",
                                      (clan, source, run_id)).rowcount > 0

    def fill_unknown_clans(self, structure_clans: dict[str, str]) -> int:
        """Work out runs' clans from clan-only structures you confirmed (never from unreviewed
        guesses), unless you picked the clan yourself."""
        from .gamedata import clan_from_structures
        from .models import CLAN_SOURCE_RANK
        filled = 0
        for run in self.load_runs():
            if CLAN_SOURCE_RANK.get(run.clan_source, 0) >= CLAN_SOURCE_RANK["structures"]:
                continue
            confirmed = (s.name for b in run.battles if b.reviewed for s in b.structures if s.source != "auto")
            clan = clan_from_structures(confirmed, structure_clans)
            if clan and clan != run.clan:
                self.set_run_clan(run.id, clan, "structures")
                filled += 1
        return filled

    def add_labels(self, shot: str, marks) -> None:
        """Structures you named on the current-run panel, on a saved copy of that frame."""
        with self._lock, self._conn:
            self._conn.executemany("INSERT INTO labels (shot, name, level, x, y) VALUES (?,?,?,?,?)",
                                   [(shot, n, lv, x, y) for n, lv, x, y in marks])

    def add_shop_label(self, file: str, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO shop_labels (file, name) VALUES (?, ?)", (file, name))

    def note_cards_offered(self, cards: list[dict], when: str) -> None:
        """Every card seen on the choice screen goes into the card library, read off the screen."""
        with self._lock, self._conn:
            for c in cards:
                name = (c.get("name") or "").strip()
                if not name:
                    continue
                gods = c.get("gods") or {}
                god = next(iter(gods), None)
                self._conn.execute(
                    "INSERT INTO card_catalog (name, kind, god, favor, text, first_seen, last_seen, offered)"
                    " VALUES (?,?,?,?,?,?,?,1)"
                    " ON CONFLICT(name) DO UPDATE SET offered = offered + 1, last_seen = excluded.last_seen,"
                    " kind = COALESCE(excluded.kind, kind), god = COALESCE(excluded.god, god),"
                    " favor = MAX(favor, excluded.favor),"
                    # keep the fullest reading of the description
                    " text = CASE WHEN length(COALESCE(excluded.text, '')) > length(COALESCE(text, ''))"
                    "             THEN excluded.text ELSE text END",
                    (name, c.get("kind"), god, sum(gods.values()), c.get("text") or None, when, when))

    def note_card_picked(self, name: str, kind: str | None, god: str | None, when: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO card_catalog (name, kind, god, first_seen, last_seen, picked) VALUES (?,?,?,?,?,1)"
                " ON CONFLICT(name) DO UPDATE SET picked = picked + 1, last_seen = excluded.last_seen,"
                " god = COALESCE(god, excluded.god)", (name, kind.title() if kind else None, god, when, when))

    def card_catalog(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM card_catalog ORDER BY name")]

    def shop_labels(self) -> list[tuple[str, str]]:
        with self._lock:
            return [tuple(r) for r in self._conn.execute("SELECT file, name FROM shop_labels")]

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
