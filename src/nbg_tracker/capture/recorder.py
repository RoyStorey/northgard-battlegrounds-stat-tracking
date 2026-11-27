"""Builds runs from a stream of game events and saves each one when it ends.

Event types (dicts with a "type" key):
  run_start          clan, warchief?, patch?, mode?, elo?, time?
  run_info           clan?, mode?, elo?   (fills in a run already started)
  opponent           name
  board              structures: [{name, level, x?, y?, source?}], cards?: [{name, god, kind}]
  screenshot         file   (the board just before the next battle)
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
from ..gamedata import clan_from_structures
from ..models import CLAN_SOURCE_RANK, LIVES, ROUNDS_PER_SEASON, WINS_TO_CLEAR, Battle, Card, Run, Structure

SEASONS = ["Spring", "Summer", "Autumn", "Winter"]

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, db: Database, on_saved: Callable[[Run], None] | None = None,
                 source: str = "log", structure_clans: dict[str, str] | None = None):
        self.db = db
        self.structure_clans = structure_clans or {}
        self.on_saved = on_saved
        self.source = source
        self.run: Run | None = None
        self.structures: list[Structure] = []
        self.cards: list[Card] = []
        self.shot: str | None = None
        self.paused = False
        self.saving = True  # off while catching up on a log written before the tracker started
        self._lock = threading.Lock()

    def live(self) -> dict:
        """The run in progress, for the dashboard."""
        with self._lock:
            run = self.run
            if run is None:
                return {"in_run": False}
            next_round = len(run.battles) + 1
            return {
                "in_run": True,
                "clan": run.clan, "clan_source": run.clan_source, "mode": run.mode, "elo_before": run.elo_before,
                "started_at": run.started_at,
                "round": next_round,
                "season": SEASONS[min((next_round - 1) // ROUNDS_PER_SEASON, len(SEASONS) - 1)],
                "wins": run.wins, "losses": run.losses,
                "wins_to_go": max(0, WINS_TO_CLEAR - run.wins),
                "lives_left": max(0, LIVES - run.losses),
                "battles": [{"round": b.round, "won": b.won, "kind": b.kind} for b in run.battles],
                "cards": [{"name": c.name, "god": c.god, "kind": c.kind} for c in self.cards],
            }

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
        self.structures, self.cards, self.shot = [], [], None

    def _on_screenshot(self, e: dict) -> None:
        self.shot = e.get("file")

    def _on_run_info(self, e: dict) -> None:
        if self.run is None:
            return
        self.run.mode = e.get("mode") or self.run.mode
        if e.get("clan"):
            self._set_clan(e["clan"], e.get("clan_source"))
        if self.run.elo_before is None:
            self.run.elo_before = e.get("elo")

    def _set_clan(self, clan: str, source: str | None) -> None:
        run = self.run
        if run is None:
            return
        unknown = run.clan in (None, "", "Unknown")
        if unknown or CLAN_SOURCE_RANK.get(source, 0) >= CLAN_SOURCE_RANK.get(run.clan_source, 0):
            run.clan, run.clan_source = clan, source

    def set_clan(self, clan: str) -> bool:
        """You picked the clan on the current-run panel."""
        with self._lock:
            if self.run is None:
                return False
            self._set_clan(clan, "user")
            return True

    def _on_opponent(self, e: dict) -> None:
        pass  # opponent names are not stored

    def _on_board(self, e: dict) -> None:
        self.structures = [Structure(s["name"], int(s.get("level", 1)), s.get("x"), s.get("y"), s.get("source"))
                           for s in e.get("structures", [])]
        if "cards" in e:
            self.cards = [Card(c["name"], c.get("god"), c.get("kind")) for c in e["cards"]]
        if self.run is not None:
            confirmed = (s.name for s in self.structures if s.source in ("user", "save", None))
            if clan := clan_from_structures(confirmed, self.structure_clans):
                self._set_clan(clan, "structures")

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
            structures=[Structure(s.name, s.level, s.x, s.y, s.source) for s in self.structures],
            cards=[Card(c.name, c.god, c.kind) for c in self.cards],
            screenshot=self.shot,
        ))
        self.shot = None

    def _on_run_end(self, e: dict) -> None:
        self._finish(e.get("elo"), e.get("time"))

    def _finish(self, elo: int | None, time: str | None) -> None:
        run, self.run = self.run, None
        if run is None or not run.battles:
            return
        if not self.saving:
            return  # finished before the tracker started; it was saved then, or can be imported
        run.elo_after = elo
        run.ended_at = time or _now()
        self.db.save_run(run)
        log.info("saved run %s: %s %d-%d", run.id, run.clan, run.wins, run.losses)
        if self.on_saved:
            self.on_saved(run)
