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
from .capture.savefile import Enricher, SaveFileWatcher, candidate_saves
from .db import Database
from .gamedata import GameData, catalog_from_log
from .server import create_app

log = logging.getLogger("nbg_tracker")


def _serve(db: Database, status: dict, port: int, background: bool) -> None:
    config = uvicorn.Config(create_app(db, status), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    if background:
        threading.Thread(target=server.run, name="dashboard", daemon=True).start()
    else:
        server.run()


def _start_watcher(db: Database, status: dict, log_path: str | None,
                   save_path: str | None) -> tuple[Recorder, list]:
    recorder = Recorder(db, on_saved=lambda r: status.update(last_saved_run=r.id))
    path = Path(log_path) if log_path else next(iter(candidate_logs()), None)
    gd = GameData(catalog_from_log(path) if path else None)
    emit = Enricher(gd, recorder.handle)
    watchers: list = []
    if path is None:
        status["watching"] = None
        log.warning("game log not found; pass --log to point at it")
    else:
        watchers.append(LogTailer(path, NorthgardLogParser(), emit))
        status["watching"] = str(path)
        log.info("watching %s", path)
    save = Path(save_path) if save_path else next(iter(candidate_saves()), None)
    if save:
        watchers.append(SaveFileWatcher(save, emit))
        status["save_file"] = str(save)
        log.info("reading board from %s", save)
    else:
        log.warning("save file not found; structures and cards will be missing (pass --save)")
    for w in watchers:
        w.start()
    return recorder, watchers


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="nbg-tracker")
    p.add_argument("--db", help="database file (default: %%LOCALAPPDATA%%\\nbg-tracker\\tracker.db)")
    p.add_argument("--port", type=int, default=8765)
    sub = p.add_subparsers(dest="cmd")
    run = sub.add_parser("run", help="watch the game and serve the dashboard (default)")
    run.add_argument("--log", help="path to the game's log file")
    run.add_argument("--save", help="path to the game's save file")
    run.add_argument("--no-tray", action="store_true", help="run in the console instead of the tray")
    sub.add_parser("serve", help="serve the dashboard only")
    rp = sub.add_parser("replay", help="record runs from an old Player.log, or a file of JSON events")
    rp.add_argument("events")
    rp.add_argument("--catalog", help="the game's StreamingAssets/aa/catalog.bin, for names")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")

    db = Database(args.db)
    status: dict = {}
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

    if args.cmd == "serve":
        print(f"dashboard: {url}")
        _serve(db, status, args.port, background=False)
        return

    recorder, watchers = _start_watcher(db, status, getattr(args, "log", None), getattr(args, "save", None))
    if getattr(args, "no_tray", False):
        print(f"dashboard: {url}")
        _serve(db, status, args.port, background=False)
        return
    _serve(db, status, args.port, background=True)
    from .tray import run_tray  # optional dependency
    run_tray(url, recorder, status)
    for w in watchers:
        w.stop()


if __name__ == "__main__":
    main()
