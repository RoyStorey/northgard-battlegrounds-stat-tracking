"""Recognises structures on board screenshots by comparing them with ones you labelled.

The camera over the village does not move, so each tile sits at the same
spot on every screenshot. Every structure you mark in the review page gives
a "slot" (a tile position) and a picture of that structure; slots you left
empty give pictures of empty ground. A new screenshot is read slot by slot:
the closest labelled picture wins, if it is close enough.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

from .models import Structure

log = logging.getLogger(__name__)

PATCH = 0.05  # patch side, as a fraction of screenshot width
FEATURE = 20  # patches are compared at FEATURE x FEATURE pixels
SLOT_RADIUS = 0.02  # marks closer than this (fraction of width) share a slot
MAX_DISTANCE = 0.9  # beyond this, a match is not trusted
EMPTY = ("", 0)


def patch_feature(img: Image.Image, x: float, y: float) -> np.ndarray:
    w, h = img.size
    half = PATCH * w / 2
    cx, cy = x * w, y * h
    box = (int(cx - half), int(cy - half), int(cx + half), int(cy + half))
    patch = img.crop(box).convert("RGB").resize((FEATURE, FEATURE), Image.BILINEAR)
    a = np.asarray(patch, dtype=np.float32).ravel()
    a -= a.mean()
    return a / (np.linalg.norm(a) + 1e-6)


def _aspect(img: Image.Image) -> float:
    return img.size[1] / img.size[0]


class Recognizer:
    def __init__(self, shots_dir: str | Path):
        self.shots_dir = Path(shots_dir)
        self.slots: list[tuple[float, float]] = []
        self.features = np.zeros((0, FEATURE * FEATURE * 3), dtype=np.float32)
        self.labels: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return bool(self.slots) and len(self.labels) > 0

    def _open(self, name: str) -> Image.Image | None:
        try:
            return Image.open(self.shots_dir / name)
        except OSError:
            return None

    def train(self, marks: list[tuple[str, str, int, float, float]], reviewed: list[str]) -> None:
        """marks: (screenshot, name, level, x, y) from reviewed battles."""
        by_shot: dict[str, list[tuple[str, int, float, float]]] = defaultdict(list)
        for shot, name, level, x, y in marks:
            by_shot[shot].append((name, level, x, y))
        slots: list[list[float]] = []  # running mean x, y, count
        for x, y in ((m[2], m[3]) for ms in by_shot.values() for m in ms):
            for s in slots:
                if abs(s[0] - x) < SLOT_RADIUS and abs(s[1] - y) < SLOT_RADIUS:
                    s[0], s[1], s[2] = (s[0] * s[2] + x) / (s[2] + 1), (s[1] * s[2] + y) / (s[2] + 1), s[2] + 1
                    break
            else:
                slots.append([x, y, 1])
        feats, labels = [], []
        for shot in set(reviewed):
            img = self._open(shot)
            if img is None:
                continue
            ms = by_shot.get(shot, [])
            for name, level, x, y in ms:
                feats.append(patch_feature(img, x, y))
                labels.append((name, level))
            for sx, sy, _ in slots:
                if not any(abs(sx - x) < SLOT_RADIUS and abs(sy - y) < SLOT_RADIUS for _, _, x, y in ms):
                    feats.append(patch_feature(img, sx, sy))
                    labels.append(EMPTY)
        with self._lock:
            self.slots = [(s[0], s[1]) for s in slots]
            self.features = np.stack(feats) if feats else np.zeros((0, FEATURE * FEATURE * 3), np.float32)
            self.labels = labels
        log.info("recogniser trained: %d slots, %d samples", len(self.slots), len(labels))

    def recognize(self, img: Image.Image) -> list[Structure]:
        with self._lock:
            if not self.slots or not self.labels:
                return []
            found = []
            for x, y in self.slots:
                f = patch_feature(img, x, y)
                d = np.linalg.norm(self.features - f, axis=1)
                i = int(d.argmin())
                if self.labels[i] != EMPTY and d[i] < MAX_DISTANCE:
                    name, level = self.labels[i]
                    found.append(Structure(name, level, round(x, 4), round(y, 4), "auto"))
            return found
