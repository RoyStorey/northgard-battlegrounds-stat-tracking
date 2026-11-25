"""Parser for Northgard Battlegrounds' own log output.

Not written yet: the log format has to be read from a real game install
first. Until then this parser recognises nothing, and runs can be added
through the dashboard API or replayed from an event file.
"""
from __future__ import annotations


class NorthgardLogParser:
    def parse(self, line: str) -> list[dict]:
        return []
