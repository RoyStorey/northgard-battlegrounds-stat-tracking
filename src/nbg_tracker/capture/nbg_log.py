"""Parser for Northgard Battlegrounds' Player.log.

The game logs enough to follow a run:

  Getting opponents from data Name: <id> MatchMakingVersion: 0.21pranked Round: 1 ELO: 101
  Starting season: Spring           (logged twice when a run starts)
  Opponent name <name>
  PlayerUnit: True OpponentUnit: False playerPending: ...   (battle over; True/False = units left)
  {"PlayerName": ..., "CurrentRun": null, "_currentEra": {"RankedElo": ...}}   (back at the menu)

The first battle of each season is against creatures (PvE) and does not
count toward wins or shields. Structures, cards and clan are not in the log;
they come from the save file (see savefile.py).
"""
from __future__ import annotations

import json
import re

GETTING_OPPONENTS = re.compile(r"^Getting opponents from data .*MatchMakingVersion: (\S+?)(ranked|unranked)? Round: (\d+) ELO: (-?\d+)")
SEASON = re.compile(r"^Starting season: (\w+)")
OPPONENT = re.compile(r"^Opponent name (.*)$")
BATTLE_END = re.compile(r"^PlayerUnit: (True|False) OpponentUnit: (True|False)")


class NorthgardLogParser:
    def __init__(self) -> None:
        self.in_run = False
        self.round = 0
        self.season: str | None = None
        self.next_is_pve = False
        self.mode: str | None = None

    def parse(self, line: str) -> list[dict]:
        line = line.rstrip("\r\n")
        if not line:
            return []
        if line.startswith('{"PlayerName"'):
            return self._player_data(line)
        if m := SEASON.match(line):
            return self._season(m.group(1))
        if m := BATTLE_END.match(line):
            return self._battle(m.group(1) == "True" and m.group(2) == "False")
        if m := GETTING_OPPONENTS.match(line):
            self.mode = m.group(2) or self.mode
            return [{"type": "run_info", "mode": self.mode, "elo": int(m.group(4))}] if self.in_run else []
        if (m := OPPONENT.match(line)) and self.in_run:
            return [{"type": "opponent", "name": m.group(1)}]
        return []

    def _season(self, season: str) -> list[dict]:
        if self.season == season and self.in_run and self.round == 0:
            return []  # the duplicate line at run start
        events = []
        if season == "Spring" and (not self.in_run or self.round > 0):
            events.append({"type": "run_start", "clan": "Unknown"})
            self.in_run, self.round, self.mode = True, 0, None
        self.season = season
        self.next_is_pve = True
        return events

    def _battle(self, won: bool) -> list[dict]:
        if not self.in_run:
            return []
        self.round += 1
        kind = "pve" if self.next_is_pve else "pvp"
        self.next_is_pve = False
        return [{"type": "battle_result", "round": self.round, "won": won, "kind": kind}]

    def _player_data(self, line: str) -> list[dict]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return []
        if data.get("CurrentRun") or not self.in_run:
            return []
        self.in_run = False
        era = data.get("_currentEra") or {}
        elo = era.get("UnrankedElo" if self.mode == "unranked" else "RankedElo")
        return [{"type": "run_end", "elo": elo}]
