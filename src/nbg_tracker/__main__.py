"""Command line: nbg-tracker [run|serve|replay] ..."""
from __future__ import annotations

import argparse
import logging
import threading
from pathlib import Path

import uvicorn

from .capture import Recorder
from .capture.logtail import JsonLinesParser, LogTailer, candidate_logs
from .capture.nbg_log import NorthgardLogParser
from .capture.screen import ScreenCapturer
from .capture.savefile import Enricher
from .db import Database
from .gamedata import GameData, catalog_from_log
from .server import create_app
from .vision import Recognizer

log = logging.getLogger("nbg_tracker")


class Tracker:
    """Shared pieces: database, names, screenshots folder and the recogniser."""

    def __init__(self, db: Database, log_path: Path | None):
        self.db = db
        self.status: dict = {}
        self.gd = GameData(catalog_from_log(log_path) if log_path else None)
        self.shots = db.path.parent / "shots"
        self.shots.mkdir(parents=True, exist_ok=True)
        self.recognizer = Recognizer(self.shots)
        if filled := db.fill_unknown_clans(self.gd.structure_clans()):
            log.info("worked out the clan of %d earlier runs", filled)
        self.recorder: Recorder | None = None
        self.capturer: ScreenCapturer | None = None
        self.retrain()

    def live(self) -> dict:
        state = self.recorder.live() if self.recorder else {"in_run": False}
        if state.get("in_run") and self.capturer and self.capturer.live:
            state["board"] = dict(self.capturer.live, structures=[dict(s) for s in self.capturer.live["structures"]])
        return state

    def retrain(self) -> None:
        threading.Thread(target=lambda: self.recognizer.train(self.db.labelled_structures(), self.db.reviewed_shots()),
                         name="train", daemon=True).start()

    def serve(self, port: int, background: bool) -> None:
        app = create_app(self.db, self.status, self.shots, self.gd.structure_names(), self.retrain, self.live,
                         self.gd.structure_clans())
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        if background:
            threading.Thread(target=server.run, name="dashboard", daemon=True).start()
        else:
            server.run()

    def watch(self, log_path: Path | None, screenshots: bool) -> tuple[Recorder, list]:
        recorder = Recorder(self.db, on_saved=lambda r: self.status.update(last_saved_run=r.id),
                            structure_clans=self.gd.structure_clans())
        self.recorder = recorder
        sink = recorder.handle
        watchers: list = []
        if screenshots:
            try:
                capturer = ScreenCapturer(recorder.handle, self.shots, self.recognizer)
                capturer.start()
                self.capturer = capturer
                watchers.append(capturer)
                sink = capturer
                self.status["screenshots"] = True
            except Exception as e:
                log.warning("screen capture off: %s", e)
        emit = Enricher(self.gd, sink)
        if log_path is None:
            self.status["watching"] = None
            log.warning("game log not found; pass --log to point at it")
        else:
            tailer = LogTailer(log_path, NorthgardLogParser(), emit,
                               catch_up=lambda on: setattr(recorder, "saving", not on))
            tailer.start()
            watchers.append(tailer)
            self.status["watching"] = str(log_path)
            log.info("watching %s", log_path)
        return recorder, watchers


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nbg-tracker")
    p.add_argument("--db", help="database file (default: %%LOCALAPPDATA%%\\nbg-tracker\\tracker.db)")
    p.add_argument("--port", type=int, default=8765)
    sub = p.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="watch the game and serve the dashboard (default)")
    run.add_argument("--log", help="path to the game's log file")
    run.add_argument("--no-screenshots", action="store_true", help="don't capture the board")
    run.add_argument("--no-tray", action="store_true", help="run in the console instead of the tray")
    sub.add_parser("serve", help="serve the dashboard only")
    rp = sub.add_parser("replay", help="record runs from an old Player.log, or a file of JSON events")
    rp.add_argument("events")
    rp.add_argument("--catalog", help="the game's StreamingAssets/aa/catalog.bin, for names")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    db = Database(args.db)
    url = f"http://127.0.0.1:{args.port}/"

    if args.cmd == "replay":
        recorder = Recorder(db, source="import")
        with open(args.events, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        is_events = any(l.startswith('{"type"') for l in lines[:20])
        parser = JsonLinesParser() if is_events else NorthgardLogParser()
        gd = GameData(args.catalog or catalog_from_log(args.events))
        tailer = LogTailer(args.events, parser, Enricher(gd, recorder.handle))
        before = len(db.load_runs())
        for line in lines:
            tailer.feed(line)
        if is_events:
            recorder.handle({"type": "run_end"})
        print(f"imported {len(db.load_runs()) - before} runs into {db.path}")
        return

    log_path = Path(args.log) if getattr(args, "log", None) else next(iter(candidate_logs()), None)
    tracker = Tracker(db, log_path)
    if args.cmd == "serve":
        print(f"dashboard: {url}")
        tracker.serve(args.port, background=False)
        return

    recorder, watchers = tracker.watch(log_path, not getattr(args, "no_screenshots", False))
    if getattr(args, "no_tray", False):
        print(f"dashboard: {url}")
        tracker.serve(args.port, background=False)
        return
    tracker.serve(args.port, background=True)
    from .tray import run_tray  # optional dependency
    run_tray(url, recorder, tracker.status)
    for w in watchers:
        w.stop()


if __name__ == "__main__":
    main()
