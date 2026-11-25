"""Reads the board (clan, structures, cards) from the game's player data.

The same player-data JSON shows up in two places: the game's save file, and
lines of Player.log when the game loads it. The run in progress is in
CurrentRun.SavedRunJSON as base64-encoded gzip JSON.
"""
from __future__ import annotations

import base64
import glob
import gzip
import json
import logging
import os
import threading
from pathlib import Path
from typing import Callable

from ..gamedata import GameData

log = logging.getLogger(__name__)


def decode_blob(raw: bytes | str) -> dict | None:
    """JSON, gzip JSON, or base64 gzip JSON."""
    data = raw.encode() if isinstance(raw, str) else raw
    for attempt in (lambda b: b, gzip.decompress, lambda b: gzip.decompress(base64.b64decode(b))):
        try:
            return json.loads(attempt(data).decode("utf-8-sig"))
        except Exception:
            continue
    return None


def board_events(player_data: dict, gd: GameData) -> list[dict]:
    """run_info (clan) and board events for the run in progress, if any."""
    current = player_data.get("CurrentRun")
    if not current or not current.get("SavedRunJSON"):
        return []
    run = decode_blob(current["SavedRunJSON"])
    if not run:
        return []
    events: list[dict] = []
    clan = gd.piece(run.get("ClanID", "")) if run.get("ClanID") else None
    if clan and clan.kind == "clan":
        events.append({"type": "run_info", "clan": clan.name})

    structures, cards = [], []
    for entry in (run.get("Items") or {}).get("Items", []):
        inst = entry.get("ItemInstance") or {}
        p = gd.piece(inst.get("GUID", ""))
        if p.kind == "structure":
            structures.append({"name": p.name, "level": int(inst.get("l", 1) or 1)})
        for charm in inst.get("CharmGUIDs") or []:
            c = gd.piece(charm)
            cards.append({"name": c.name, "god": c.god, "kind": "rune"})
    for guid in (run.get("Powers") or {}).get("PowerGUIDs", []):
        c = gd.piece(guid)
        if c.kind in ("card", "favor", "clan_skill"):
            cards.append({"name": c.name, "god": c.god, "kind": "blessing" if c.kind == "card" else c.kind})
    seen = {c["name"] for c in cards}
    for guid in ((run.get("CustomData") or {}).get("CardPurchaseCounts") or {}):
        c = gd.piece(guid)
        if c.kind == "card" and c.name not in seen:
            cards.append({"name": c.name, "god": c.god, "kind": "card"})
            seen.add(c.name)
    events.append({"type": "board", "structures": structures, "cards": cards})
    return events


STEAM_APP_ID = "4487060"


def candidate_saves() -> list[Path]:
    """Where the save might be: Steam Cloud's local copy, or next to the log."""
    roots = [os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.environ.get("ProgramFiles", r"C:\Program Files"), "C:\\", "D:\\", "E:\\"]
    patterns = [os.path.join(r, "Steam", "userdata", "*", STEAM_APP_ID, "remote", "*") for r in roots]
    if base := os.environ.get("USERPROFILE"):
        patterns.append(os.path.join(base, "AppData", "LocalLow", "*", "*Northgard*", "*"))
    found = []
    for pattern in patterns:
        for p in glob.glob(pattern):
            path = Path(p)
            if path.is_file() and path.suffix.lower() not in (".log", ".txt", ".ini") and not path.name.startswith("Player"):
                found.append(path)
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


class Enricher:
    """Sits between parsers and the recorder: expands player_data into board events."""

    def __init__(self, gd: GameData, emit: Callable[[dict], None]):
        self.gd = gd
        self.emit = emit

    def __call__(self, event: dict) -> None:
        if event.get("type") == "player_data":
            for e in board_events(event["data"], self.gd):
                self.emit(e)
        else:
            self.emit(event)


class SaveFileWatcher:
    """Polls the save file and emits its board whenever the game rewrites it."""

    def __init__(self, path: str | Path, emit: Callable[[dict], None], poll: float = 1.0):
        self.path = Path(path)
        self.emit = emit
        self.poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="save-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        last = None
        while not self._stop.is_set():
            try:
                stamp = self.path.stat().st_mtime_ns
                if stamp != last:
                    last = stamp
                    data = decode_blob(self.path.read_bytes())
                    if data:
                        self.emit({"type": "player_data", "data": data})
            except FileNotFoundError:
                pass
            except Exception:
                log.exception("could not read save file %s", self.path)
            self._stop.wait(self.poll)
