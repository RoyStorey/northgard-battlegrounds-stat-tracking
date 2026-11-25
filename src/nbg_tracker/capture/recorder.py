"""Builds runs from a stream of game events and saves each one when it ends.

Event types (dicts with a "type" key):
  run_start          clan, warchief?, patch?, mode?, elo?, time?
  board              structures: [{name, level}], cards?: [{name, god, kind}]
  structure_placed   name, level?
  structure_upgraded name, level
  structure_removed  name
  card_picked        name, god?, kind?
  battle_result      round, won, kind? ("pvp"/"pve"), opponent_clan?
  run_end            elo?, time?
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable

from ..db import Database
from ..models import Battle, Card, Run, Structure

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, db: Database, on_saved: Callable[[Run], None] | None = None,
                 source: str = "log"):
        self.db = db
        self.on_saved = on_saved
        self.source = source
        self.run: Run | None = None
        self.structures: list[Structure] = []
        self.cards: list[Card] = []
        self.paused = False
        self._lock = threading.Lock()

    def handle(self, event: dict) -> None:
        if self.paused:
            return
        with self._lock:
            getattr(self, "_on_" + event.get("type", ""), self._unknown)(event)

    def _unknown(self, e: dict) -> None:
        log.debug("ignoring event %s", e)

    def _on_run_start(self, e: dict) -> None:
        self._finish(None, e.get("time"))  # keep a run the game never closed
        self.run = Run(clan=e["clan"], warchief=e.get("warchief"), patch=e.get("patch"),
                       mode=e.get("mode"), started_at=e.get("time") or _now(),
                       elo_before=e.get("elo"), source=self.source)
        self.structures, self.cards = [], []

    def _on_board(self, e: dict) -> None:
        self.structures = [Structure(s["name"], int(s.get("level", 1))) for s in e.get("structures", [])]
        if "cards" in e:
            self.cards = [Card(c["name"], c.get("god"), c.get("kind")) for c in e["cards"]]

    def _on_structure_placed(self, e: dict) -> None:
        self.structures.append(Structure(e["name"], int(e.get("level", 1))))

    def _on_structure_upgraded(self, e: dict) -> None:
        for s in self.structures:
            if s.name == e["name"] and s.level < int(e["level"]):
                s.level = int(e["level"])
                return

    def _on_structure_removed(self, e: dict) -> None:
        for i, s in enumerate(self.structures):
            if s.name == e["name"]:
                del self.structures[i]
                return

    def _on_card_picked(self, e: dict) -> None:
        self.cards.append(Card(e["name"], e.get("god"), e.get("kind")))

    def _on_battle_result(self, e: dict) -> None:
        if self.run is None:
            log.warning("battle result outside a run: %s", e)
            return
        self.run.battles.append(Battle(
            round=int(e["round"]), won=bool(e["won"]), kind=e.get("kind", "pvp"),
            opponent_clan=e.get("opponent_clan"),
            structures=[Structure(s.name, s.level) for s in self.structures],
            cards=[Card(c.name, c.god, c.kind) for c in self.cards],
        ))

    def _on_run_end(self, e: dict) -> None:
        self._finish(e.get("elo"), e.get("time"))

    def _finish(self, elo: int | None, time: str | None) -> None:
        run, self.run = self.run, None
        if run is None or not run.battles:
            return
        run.elo_after = elo
        run.ended_at = time or _now()
        self.db.save_run(run)
        log.info("saved run %s: %s %d-%d", run.id, run.clan, run.wins, run.losses)
        if self.on_saved:
            self.on_saved(run)
