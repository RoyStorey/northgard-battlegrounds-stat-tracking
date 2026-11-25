"""Follows a game log file and feeds each new line to a parser."""
from __future__ import annotations

import glob
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger(__name__)


class LineParser(Protocol):
    def parse(self, line: str) -> list[dict]: ...


class JsonLinesParser:
    """Each line is one event as JSON. Used for tests and replaying event files."""

    def parse(self, line: str) -> list[dict]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            return [json.loads(line)]
        except json.JSONDecodeError:
            return []


def candidate_logs() -> list[Path]:
    """Places a Unity game on Windows usually writes Player.log."""
    base = os.environ.get("USERPROFILE")
    if not base:
        return []
    pattern = os.path.join(base, "AppData", "LocalLow", "*", "*Northgard*", "Player*.log")
    return sorted((Path(p) for p in glob.glob(pattern)), key=lambda p: p.stat().st_mtime, reverse=True)


class LogTailer:
    def __init__(self, path: str | Path, parser: LineParser, emit: Callable[[dict], None],
                 from_start: bool = False, poll: float = 0.5,
                 catch_up: Callable[[bool], None] | None = None):
        """from_start: treat the whole existing log as new.
        catch_up: otherwise, the existing log is still parsed (so a run already in
        progress is known) between catch_up(True) and catch_up(False)."""
        self.path = Path(path)
        self.parser = parser
        self.emit = emit
        self.from_start = from_start
        self.poll = poll
        self.catch_up = catch_up
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="log-tail", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        offset = None
        buf = ""
        while not self._stop.is_set():
            try:
                size = self.path.stat().st_size
            except FileNotFoundError:
                offset, buf = 0, ""
                self._stop.wait(self.poll)
                continue
            if offset is None:
                if self.from_start:
                    offset = 0
                elif self.catch_up is None:
                    offset = size
                else:
                    offset, buf = self._catch_up()
                    self._stop.wait(self.poll)
                    continue
            if size < offset:  # the game truncates its log on launch
                offset, buf = 0, ""
            if size > offset:
                with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    buf += f.read()
                    offset = f.tell()
                *lines, buf = buf.split("\n")
                for line in lines:
                    self.feed(line)
            self._stop.wait(self.poll)

    def _catch_up(self) -> tuple[int, str]:
        self.catch_up(True)
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                *lines, rest = f.read().split("\n")
                offset = f.tell()
            for line in lines:
                self.feed(line)
        finally:
            self.catch_up(False)
        return offset, rest

    def feed(self, line: str) -> None:
        try:
            for event in self.parser.parse(line):
                self.emit(event)
        except Exception:  # a bad line must never stop the watcher
            log.exception("parser failed on line: %r", line[:200])
