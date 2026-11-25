"""Data model: a run is a sequence of battles, each with a snapshot of the board."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

ROUNDS_PER_SEASON = 4  # the season changes every 4 rounds
WINS_TO_CLEAR = 10
LIVES = 5


@dataclass
class Structure:
    name: str
    level: int = 1
    x: float | None = None  # position on the battle's screenshot, 0-1
    y: float | None = None
    source: str | None = None  # auto (recognised), user (reviewed), save (game data)


@dataclass
class Card:
    name: str
    god: str | None = None  # Odin, Thor, Baldr, Loki, or None for neutral
    kind: str | None = None  # gift, blessing, rune


@dataclass
class Battle:
    """One fight, with the board as it stood when the fight started."""

    round: int
    won: bool
    kind: Literal["pvp", "pve"] = "pvp"
    opponent_clan: str | None = None
    structures: list[Structure] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    screenshot: str | None = None  # file name in the shots folder
    reviewed: bool = False
    id: int | None = None

    @property
    def season(self) -> int:
        """0-based season index (rounds 1-4 are season 0)."""
        return (self.round - 1) // ROUNDS_PER_SEASON


@dataclass
class Run:
    clan: str
    warchief: str | None = None
    patch: str | None = None
    mode: str | None = None  # ranked, free
    started_at: str | None = None  # ISO 8601
    ended_at: str | None = None
    elo_before: int | None = None
    elo_after: int | None = None
    source: str = "manual"  # log, manual, import
    battles: list[Battle] = field(default_factory=list)
    id: int | None = None

    @property
    def wins(self) -> int:
        return sum(1 for b in self.battles if b.kind == "pvp" and b.won)

    @property
    def losses(self) -> int:
        return sum(1 for b in self.battles if b.kind == "pvp" and not b.won)

    @property
    def cleared(self) -> bool:
        return self.wins >= WINS_TO_CLEAR

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(wins=self.wins, losses=self.losses, cleared=self.cleared)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Run":
        battles = [
            Battle(
                round=int(b["round"]),
                won=bool(b["won"]),
                kind=b.get("kind", "pvp"),
                opponent_clan=b.get("opponent_clan"),
                structures=[_structure(s) for s in b.get("structures", [])],
                cards=[_card(c) for c in b.get("cards", [])],
                screenshot=b.get("screenshot"),
                reviewed=bool(b.get("reviewed", False)),
            )
            for b in d.get("battles", [])
        ]
        if not d.get("clan"):
            raise ValueError("clan is required")
        fields = {k: d.get(k) for k in (
            "clan", "warchief", "patch", "mode", "started_at", "ended_at",
            "elo_before", "elo_after", "id")}
        return cls(**fields, source=d.get("source") or "manual", battles=battles)


def _structure(s: dict | str) -> Structure:
    if isinstance(s, str):
        return Structure(s)
    return Structure(s["name"], int(s.get("level", 1)), s.get("x"), s.get("y"), s.get("source"))


def _card(c: dict | str) -> Card:
    return Card(c) if isinstance(c, str) else Card(c["name"], c.get("god"), c.get("kind"))
