"""Reads the board (clan, structures, cards) from the game's player data.

Player.log prints the player-data JSON when the game loads it (the save file
itself is encrypted, so this is the only readable copy). When a run is in
progress it is in CurrentRun.SavedRunJSON as base64-encoded gzip JSON.
"""
from __future__ import annotations

import base64
import gzip
import json
from typing import Callable

from ..gamedata import GameData


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
