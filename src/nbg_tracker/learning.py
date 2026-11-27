"""Learning from what you tell the tracker, as you tell it.

* Saving a reviewed round teaches the structure recogniser that round at once
  and re-reads the run's other unreviewed rounds.
* Naming a structure on the current-run panel saves a copy of that frame and
  learns from it straight away.
* Picking your clan teaches it what that clan's skill icons look like, so the
  next run of that clan is recognised by itself.
* Naming a shop item teaches it that icon.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import cv2

from .db import Database
from .models import Structure
from .vision import (CLAN_DISTANCE, SHOP_DISTANCE, Memory, Recognizer, clan_feature, clan_strip,
                     shop_slots)

log = logging.getLogger(__name__)


class Learner:
    def __init__(self, db: Database, recognizer: Recognizer, shots_dir: str | Path):
        self.db = db
        self.recognizer = recognizer
        self.shots_dir = Path(shots_dir)
        self.clans = Memory(CLAN_DISTANCE, clan_feature)
        self.shop = Memory(SHOP_DISTANCE)
        self.recorder = None  # set by the app once watching starts
        self.capturer = None
        self._lock = threading.Lock()

    # ---- start-up
    def load(self) -> None:
        """Rebuild the clan and shop memories from what you've named before."""
        self.clans.clear()
        for run in self.db.load_runs():
            if run.clan_source in ("user", "structures"):
                shot = next((b.screenshot for b in run.battles if b.screenshot), None)
                img = cv2.imread(str(self.shots_dir / shot)) if shot else None
                if img is not None:
                    self.clans.add(clan_strip(img), run.clan)
        self.shop.clear()
        for file, name in self.db.shop_labels():
            tile = cv2.imread(str(self.shots_dir / file))
            if tile is not None:
                self.shop.add(tile, name)
        log.info("remembered %d clan and %d shop examples", len(self.clans.labels), len(self.shop.labels))

    # ---- reviewed rounds
    def learn_battle(self, battle_id: int) -> int:
        shot, marks = self.db.battle_marks(battle_id)
        if not shot:
            return 0
        with self._lock:
            return self.recognizer.learn(shot, marks)

    def refresh_run(self, run_id: int) -> int:
        """Re-read every unreviewed round of a run with what the recogniser knows now."""
        updated = 0
        with self._lock:
            for battle_id, shot in self.db.unreviewed_shots(run_id):
                img = cv2.imread(str(self.shots_dir / shot))
                if img is None:
                    continue
                self.db.set_battle_structures(battle_id, self.recognizer.recognize(img), reviewed=False)
                updated += 1
        return updated

    def set_run_clan(self, run_id: int, clan: str) -> bool:
        if not self.db.set_run_clan(run_id, clan, "user"):
            return False
        run = next((r for r in self.db.load_runs() if r.id == run_id), None)
        shot = next((b.screenshot for b in run.battles if b.screenshot), None) if run else None
        img = cv2.imread(str(self.shots_dir / shot)) if shot else None
        if img is not None:
            self.clans.add(clan_strip(img), clan)
        return True

    # ---- reading the screen (called by the capturer)
    def read_clan(self, img) -> str | None:
        return self.clans.recall(clan_strip(img))[0]

    def read_shop(self, img) -> list[str | None]:
        return [self.shop.recall(tile)[0] for tile in shop_slots(img)]

    # ---- the current-run panel
    def _live_frame(self):
        img = cv2.imread(str(self.shots_dir / "live.jpg"))
        if img is None:
            raise LookupError("no live board yet")
        return img

    def label_live(self, marks: list[dict]) -> int:
        """You named structures on the current board: keep that frame and learn from it."""
        img = self._live_frame()
        name = f"label-{time.strftime('%Y%m%d-%H%M%S')}.jpg"
        cv2.imwrite(str(self.shots_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        rows = [(m["name"], int(m.get("level") or 1), float(m["x"]), float(m["y"]))
                for m in marks if m.get("name") and m["name"] != "?"]
        self.db.add_labels(name, rows)
        with self._lock:
            learned = self.recognizer.learn(name, rows)
        if self.capturer is not None and self.capturer.live:
            self.capturer.live["structures"] = [
                {"name": m.get("name") or "?", "level": int(m.get("level") or 1), "x": m["x"], "y": m["y"]} for m in marks]
        if self.recorder is not None:  # the next battle's board uses your names
            self.recorder.handle({"type": "board", "structures": [
                {"name": n, "level": lv, "x": x, "y": y, "source": "user"} for n, lv, x, y in rows]})
        return learned

    def label_shop(self, slot: int, name: str) -> None:
        tiles = shop_slots(self._live_frame())
        if not 0 <= slot < len(tiles):
            raise IndexError("no such shop slot")
        file = f"shop-{time.strftime('%Y%m%d-%H%M%S')}-{slot}.png"
        cv2.imwrite(str(self.shots_dir / file), tiles[slot])
        self.db.add_shop_label(file, name)
        self.shop.add(tiles[slot], name)
        if self.capturer is not None and self.capturer.live and "shop" in self.capturer.live:
            self.capturer.live["shop"][slot] = name

    def set_live_clan(self, clan: str) -> bool:
        if self.recorder is None or not self.recorder.set_clan(clan):
            return False
        try:
            self.clans.add(clan_strip(self._live_frame()), clan)
        except LookupError:
            pass
        return True
