"""Learning from reviews as they happen.

When you save a reviewed round, the recogniser learns that round straight
away, and the other rounds of the run you haven't reviewed yet are read
again, so a structure you named once is already named in the next round.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2

from .db import Database
from .vision import Recognizer

log = logging.getLogger(__name__)


class Learner:
    def __init__(self, db: Database, recognizer: Recognizer, shots_dir: str | Path):
        self.db = db
        self.recognizer = recognizer
        self.shots_dir = Path(shots_dir)
        self._lock = threading.Lock()

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
