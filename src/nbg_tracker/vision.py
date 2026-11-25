"""Finds and recognises structures on board screenshots.

The camera pans and zooms, so nothing sits at a fixed spot. Two things don't
move with it, though:

* Every tile has a hex badge drawn under it, and the badge is screen UI: the
  same size at any zoom. Badges are found by template matching against a
  drawn hexagon rim. A tile with a structure has a number box beside its
  badge; an empty tile has a bare hex.
* The gap between neighbouring badges tells the local zoom, so each structure
  can be cropped (above and right of its badge) at a consistent size.

Crops are compared with crops of structures you named on the review page;
the nearest one wins if it is close enough, otherwise the structure is shown
as "?" for you to name.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .models import Structure

log = logging.getLogger(__name__)

REF_HEIGHT = 1080  # badge geometry below is for a 1080-pixel-high screenshot
BADGE_THRESHOLD = 0.6  # real badges score ~0.72-0.84, background < 0.45
BOX_THRESHOLD = 0.35  # share of number-box pixels beside a badge that marks a structure
MATCH_DISTANCE = 0.9  # nearest labelled crop must be closer than this
SNAP = 0.6  # a label snaps to a structure within this many badge-gaps
UNKNOWN = "?"

_BOX_LIGHT = np.array([0xA1, 0x9B, 0x7E])  # BGR: the grey number box
_BOX_DARK = np.array([0x5A, 0x52, 0x3A])  # BGR: the darker box behind worker icons


def _hexagon(c: float, r: float) -> np.ndarray:
    k = np.arange(6)
    return np.stack([c + r * np.cos(np.pi / 2 + k * np.pi / 3), c + r * np.sin(np.pi / 2 + k * np.pi / 3)], 1)


def _badge_template(size: int = 48) -> tuple[np.ndarray, np.ndarray]:
    """A drawn badge rim (dark outline, light rim, navy inside) and the mask of the rim."""
    sub, c = 8, (size - 1) / 2

    def poly(r: float) -> np.ndarray:
        return (_hexagon(c, r) * sub + sub / 2).astype(np.int32)

    big = np.full((size * sub, size * sub), 128, np.uint8)
    for r, shade in ((21.5, 30), (19.5, 185), (17.0, 60), (15.5, 36)):
        cv2.fillPoly(big, [poly(r)], shade)
    mask = np.zeros_like(big)
    cv2.fillPoly(mask, [poly(22)], 255)
    cv2.fillPoly(mask, [poly(13)], 0)
    shrink = lambda a: cv2.resize(a, (size, size), interpolation=cv2.INTER_AREA)
    return shrink(big), ((shrink(mask) > 127) * 255).astype(np.uint8)


_TEMPLATE, _MASK = _badge_template()


@dataclass
class Badge:
    x: float  # badge centre, pixels in the original screenshot
    y: float
    gap: float  # distance to the nearest neighbouring badge (local zoom)
    structure: bool

    def centre(self) -> tuple[float, float]:
        """Where the structure itself sits: above and to the right of its badge."""
        return self.x + 0.15 * self.gap, self.y - 0.42 * self.gap


def to_bgr(img) -> np.ndarray:
    """Accepts a PIL image or a BGR array."""
    if isinstance(img, np.ndarray):
        return img
    return cv2.cvtColor(np.asarray(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def find_badges(img) -> list[Badge]:
    bgr = to_bgr(img)
    scale = REF_HEIGHT / bgr.shape[0]
    if abs(scale - 1) > 0.01:
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    h, w = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    score = cv2.matchTemplate(gray, _TEMPLATE, cv2.TM_CCOEFF_NORMED, mask=_MASK)
    score = np.nan_to_num(score, nan=0, posinf=0, neginf=0)
    score[score > 1.01] = 0
    half = _TEMPLATE.shape[0] // 2
    points = []
    while True:
        _, v, _, (x, y) = cv2.minMaxLoc(score)
        if v < BADGE_THRESHOLD:
            break
        score[max(0, y - 30):y + 30, max(0, x - 30):x + 30] = 0
        cx, cy = x + half, y + half
        if cx < 0.9 * w and cy > 0.08 * h:  # skip the shop column and top bar
            points.append((cx, cy))
    badges = []
    for i, (x, y) in enumerate(points):
        same_row = [np.hypot(px - x, py - y) for j, (px, py) in enumerate(points) if j != i and abs(py - y) < 25]
        others = same_row or [np.hypot(px - x, py - y) for j, (px, py) in enumerate(points) if j != i]
        gap = min(others) if others else 220.0
        if gap > 320:
            gap = 220.0
        badges.append(Badge(x / scale, y / scale, gap / scale, _has_box(bgr, x, y)))
    return badges


def _has_box(bgr: np.ndarray, x: int, y: int) -> bool:
    band = bgr[max(0, y - 4):y + 5, x + 28:x + 56].astype(int)
    if band.size == 0:
        return False
    near = (np.abs(band - _BOX_LIGHT).sum(axis=2) < 50) | (np.abs(band - _BOX_DARK).sum(axis=2) < 40)
    return bool(near.mean() > BOX_THRESHOLD)


def crop(img, badge: Badge, size: int = 64) -> np.ndarray | None:
    bgr = to_bgr(img)
    cx, cy = badge.centre()
    half = 0.38 * badge.gap
    x0, y0, x1, y1 = int(cx - half), int(cy - half), int(cx + half), int(cy + half)
    if x0 < 0 or y0 < 0 or x1 > bgr.shape[1] or y1 > bgr.shape[0]:
        return None
    return cv2.resize(bgr[y0:y1, x0:x1], (size, size), interpolation=cv2.INTER_AREA)


def feature(tile: np.ndarray) -> np.ndarray:
    """Colour histogram (tolerant of small shifts) plus a coarse grey thumbnail (shape)."""
    hsv = cv2.cvtColor(tile, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 8], [0, 180, 0, 256]).ravel()
    hist = np.sqrt(hist / (hist.sum() + 1e-6))
    thumb = cv2.resize(cv2.cvtColor(tile, cv2.COLOR_BGR2GRAY), (16, 16)).astype(np.float32).ravel()
    thumb -= thumb.mean()
    thumb /= np.linalg.norm(thumb) + 1e-6
    return np.concatenate([hist, 0.7 * thumb]).astype(np.float32)


class Recognizer:
    def __init__(self, shots_dir: str | Path):
        self.shots_dir = Path(shots_dir)
        self.features = np.zeros((0, 128 + 256), np.float32)
        self.labels: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return True  # even untrained, it can point out where structures are

    def train(self, marks: list[tuple[str, str, int, float, float]], reviewed: list[str] | None = None) -> None:
        """marks: (screenshot, name, level, x, y) with x, y as fractions of the screenshot."""
        by_shot: dict[str, list[tuple[str, int, float, float]]] = {}
        for shot, name, level, x, y in marks:
            if name and name != UNKNOWN:
                by_shot.setdefault(shot, []).append((name, level, x, y))
        feats, labels = [], []
        for shot, ms in by_shot.items():
            img = cv2.imread(str(self.shots_dir / shot))
            if img is None:
                continue
            h, w = img.shape[:2]
            badges = [b for b in find_badges(img) if b.structure]
            for name, level, x, y in ms:
                b = _nearest(badges, x * w, y * h)
                tile = crop(img, b) if b else None
                if tile is not None:
                    feats.append(feature(tile))
                    labels.append((name, level))
        with self._lock:
            self.features = np.stack(feats) if feats else np.zeros((0, 128 + 256), np.float32)
            self.labels = labels
        log.info("recogniser trained on %d labelled structures", len(labels))

    def recognize(self, img) -> list[Structure]:
        bgr = to_bgr(img)
        h, w = bgr.shape[:2]
        out = []
        for b in find_badges(bgr):
            if not b.structure:
                continue
            cx, cy = b.centre()
            name, level = UNKNOWN, 1
            tile = crop(bgr, b)
            if tile is not None:
                with self._lock:
                    if self.labels:
                        d = np.linalg.norm(self.features - feature(tile), axis=1)
                        i = int(d.argmin())
                        if d[i] < MATCH_DISTANCE:
                            name, level = self.labels[i]
            out.append(Structure(name, level, round(cx / w, 4), round(max(cy, 0) / h, 4), "auto"))
        return out


def _nearest(badges: list[Badge], x: float, y: float) -> Badge | None:
    """The structure a click at (x, y) refers to: nearest structure centre or badge."""
    best, best_d = None, None
    for b in badges:
        cx, cy = b.centre()
        d = min(np.hypot(cx - x, cy - y), np.hypot(b.x - x, b.y - y)) / b.gap
        if d < SNAP and (best_d is None or d < best_d):
            best, best_d = b, d
    return best


# Greyscale fingerprints (12x6) of the shop's LOCK and reroll buttons at the right edge of
# the screen. They are only on screen in the build phase; the battle view, which shows the
# opponent's village beside yours, has no shop. Boxes are (from right, top, to right, bottom)
# at 1080 pixels high.
_SHOP_BUTTONS = [
    ((135, 762, 62, 812), np.array([
        [67, 58, 58, 58, 57, 58, 58, 57, 57, 58, 58, 71], [66, 60, 60, 59, 60, 59, 59, 60, 60, 60, 59, 69],
        [65, 112, 73, 127, 115, 126, 139, 116, 109, 148, 70, 68], [63, 101, 115, 110, 114, 109, 123, 115, 99, 133, 70, 67],
        [61, 45, 46, 46, 46, 46, 46, 46, 46, 46, 46, 65], [59, 40, 41, 41, 41, 41, 41, 41, 41, 41, 41, 65]])),
    ((168, 832, 30, 886), np.array([
        [71, 68, 71, 74, 76, 76, 76, 76, 74, 71, 68, 72], [66, 60, 60, 74, 70, 59, 60, 60, 60, 60, 60, 66],
        [63, 56, 72, 138, 144, 56, 56, 134, 94, 88, 56, 63], [60, 50, 52, 145, 138, 54, 51, 129, 89, 75, 51, 60],
        [57, 45, 45, 62, 51, 45, 45, 45, 45, 46, 45, 57], [62, 55, 58, 61, 62, 63, 63, 62, 60, 57, 54, 62]])),
]
SHOP_MATCH = 15  # mean grey difference; build-phase frames measure ~0.3, the battle view ~60


def is_build_view(img) -> bool:
    """True when the shop is on screen, i.e. the view shows only your own village."""
    bgr = to_bgr(img)
    scale = REF_HEIGHT / bgr.shape[0]
    if abs(scale - 1) > 0.01:
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    w = gray.shape[1]
    for (right0, top, right1, bottom), ref in _SHOP_BUTTONS:
        region = gray[top:bottom, w - right0:w - right1]
        if region.size == 0:
            continue
        thumb = cv2.resize(region, (12, 6), interpolation=cv2.INTER_AREA).astype(int)
        if np.abs(thumb - ref).mean() < SHOP_MATCH:  # either button is enough (the mouse may hover one)
            return True
    return False


def frame_quality(img) -> int:
    """How much of the village a frame shows: number of structure badges found."""
    return sum(1 for b in find_badges(img) if b.structure)
