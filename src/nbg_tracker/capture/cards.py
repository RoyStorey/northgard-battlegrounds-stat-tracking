"""Fate cards and god favour, read off the screen.

Nothing here needs you to type anything in:

* The card choice screen is recognised by its "peek" (eye) button. Each of
  the three cards' name, type (Gift / Rune / Blessing) and description are
  read with text recognition (rapidocr, installed with the tracker), and the
  god icons in a card's top-left corner tell which god it gives favour to,
  and how much (two Thor icons = 2 Thor favour).
* The favour counters at the top left ("2/3" under each god's icon) are read
  the same way, and which god each one is comes from its icon.
* Which card you took is worked out when the choice screen closes: the god
  whose favour went up, by how much, matches one card. A neutral card (no
  god icons) is recorded when it was the only neutral card on offer.

Positions are for a 1080-pixel-high screen; other sizes are scaled to it.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from typing import Callable

import cv2
import numpy as np

from ..vision import to_bgr, _at_1080

log = logging.getLogger(__name__)

GODS = ["Thor", "Odin", "Baldr", "Loki"]
# 16x16 greyscale fingerprints of the gods' favour icons (top left of the screen).
_GOD_ICONS = {
    "Thor": "PVM+LS4tLi0uLi0uLzRLUFU6Li0uLlKPkGwuLi4uMks7LiwuLi6DxcWnMS4uLi4wLi0tLi4uTIKCay8vLy8uLSwtLS4uLi6otVMvLy8vLy4sLS0uLi4ttMRaLy8vLy8vLC0uLS0tLbTFXi4uLy8vLywtLi0tLS20xl8uLi8vLy8uLi4tLS0utcZhLi4vLy8vLi4uLS4tLpiiVS0uLy8vLy0tLi0uLUCGimIuLi4uLi0tLy5NeX2txcW8gntsLi4tLS0uqcXGx8bGxsbGvUUuLS0sUcLGxsbGxsbGxsh+Li0vLTl3r8bGxsbGxr2JSy0tRS4tLTZfn8bGsXA8Li0uMg==",
    "Odin": "LlJCMC0vLy4tLi0sLTFEVFdBLi4tLy8uLS4uLS0tLkRFLS0tQWeDg4JcLy4tLS4wMC4ucpefnZuLm3hbOS0tLy4wcZ2enp6ObY2en6CQYDItNmebnp6enp6enp6Zjo5tLTmHnp6enp6enpJRQjY2PC5tnp6enp6enpdGLywsLC03f5Oenp6enZxtMi0tLi4uME6cnp6enp6fmGg1Li0tLi1wn56enp6enZ6fikktLS02k56enp6fjmhCOzo3Li0tPp6enoldOy8tLi4uLi0tLTmbnHAyLi4uLS4uLi4uLi45ilcwLi4uLi4tLS0tLS4uTjguLy4vLy8vLy8vLy8uLw==",
    "Baldr": "P1E+LiwsLS4vLy4uLTNKTlU6LSwtQ12Nlm5PMC8tMEk7LSw2jcvMqKXS1axJLi8wLy1Ct8dpPC9k0XOpy2YvLi02sbs3MC00zK0zLIfMUSwuedvBPHeFMGbJozYxoLIyNruQusPNzYd/taY0Lk3TUEHaVFGzVLqwldhZMJZlu4hBzkYuLlm+Oi6RjWbYy9qMQNBJLo68x0MwoM+/mze6jj7WVi20o5e+uKllml8yuoIyspEuk7IwhMc2Li0uUthILGrS1s+jOnnY19I+OLefMSw0n9xcLS5FYX3VS53CRS4uLjqi0IpNLS5b2Ma9Vi4vQywtMWrA1cjH0NSRNy0tMQ==",
    "Loki": "SmFGLy4uMz49MS4tLjhXX2Y/LS0ybKrKx6JfMSwtMlZCLS0tjdDOq6rQzpItLi0xLy4sW87AUVo/W8TOZC0tLjAvLovPbpjPyYBszX0tLSwvLy6Rzmq3pmW/cXOILi0tLi8uks92pMM/lpcuLy8vLS4vL2bOtl/HvLeULi8vLy0tLy4untGuXavQzn4zLS0tLi8vLkWhz7Sex8nPdy0tLi4vLkZxNYnO0KeMzsE9LS4tLy90dS6dpnrNcMbNXy0uLS8ucJkupqd80HW/zV8tLi4vLj/IYU+szbJqyMM9Li0wLy4tisV4Sk9jr9CGLi0tUS4uLjmdzsW+yc+kNS4tNw==",
}
_GOD_TEMPLATES = {g: np.frombuffer(base64.b64decode(v), np.uint8).reshape(16, 16).astype(np.float32)
                  for g, v in _GOD_ICONS.items()}
# 12x10 greyscale fingerprint of the card screen's "peek" (eye) button, centred at (960, 955).
_EYE = np.frombuffer(base64.b64decode(
    "TEVGR0hTUklGRUZLUDY+Pj4+Pj4+PjdRUDQ8PDw8PDw8PDRPTzM6PGmWlGQ6OjNNTTFIuovWzo62QTFMTC5Uvm3YzW6/Sy9LSiwwO4SurH04"
    "MS1KSCosLS4uLi4tLipJRycpKioqKioqKidHPz89P0FKSUA+PT4+"), np.uint8).reshape(10, 12).astype(int)

CARD_CENTRES = (572, 960, 1348)  # x of the three offered cards (relative to the screen centre at 960)
FAVOR_SLOTS = (51, 113, 176, 238)  # x of the favour counters, left to right
GOD_ICON_MATCH = 25  # mean grey difference for a favour icon to count as that god
CARD_ICON_MATCH = 0.3  # correlation for a god icon on a card


def _norm(a: np.ndarray) -> np.ndarray:
    a = a - a.mean()
    return a / (np.linalg.norm(a) + 1e-6)


def _patch(gray: np.ndarray, cx: int, cy: int, r: int = 18) -> np.ndarray:
    return cv2.resize(gray[cy - r:cy + r, cx - r:cx + r], (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)


def _ocr_ready(img: np.ndarray, threshold: int | None = None, scale: int = 1) -> np.ndarray:
    if threshold is None:
        return img
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    g = cv2.resize(g, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    _, b = cv2.threshold(g, threshold, 255, cv2.THRESH_BINARY_INV)
    return cv2.cvtColor(cv2.copyMakeBorder(b, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=255), cv2.COLOR_GRAY2BGR)


class CardReader:
    def __init__(self, ocr: Callable | None = None):
        self._ocr = ocr
        self._ocr_failed = False

    def _engine(self):
        if self._ocr is None and not self._ocr_failed:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self._ocr = RapidOCR()
            except Exception as e:  # missing or broken: fall back to telling cards apart by look
                log.warning("text recognition unavailable (%s); cards will be named by look", e)
                self._ocr_failed = True
        return self._ocr

    def text(self, img: np.ndarray) -> str:
        engine = self._engine()
        if engine is None:
            return ""
        result = engine(img)[0] or []
        # read in lines, top to bottom then left to right
        boxes = sorted(result, key=lambda r: (round(min(p[1] for p in r[0]) / 12), min(p[0] for p in r[0])))
        return " ".join(r[1] for r in boxes).strip()

    # ---- what's on screen
    @staticmethod
    def is_card_screen(img) -> bool:
        bgr = _at_1080(img)
        cx = bgr.shape[1] // 2
        region = cv2.cvtColor(bgr[928:982, cx - 30:cx + 30], cv2.COLOR_BGR2GRAY)
        if region.size == 0:
            return False
        thumb = cv2.resize(region, (12, 10), interpolation=cv2.INTER_AREA).astype(int)
        return float(np.abs(thumb - _EYE).mean()) < 12

    def read_favor(self, img) -> dict[str, tuple[int, int] | None]:
        """{god: (favour, next threshold)} for each favour counter shown; None if unreadable."""
        bgr = _at_1080(img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        out: dict[str, tuple[int, int] | None] = {}
        for cx in FAVOR_SLOTS:
            icon = _patch(gray, cx, 188)
            dist = {g: float(np.abs(icon - t).mean()) for g, t in _GOD_TEMPLATES.items()}
            god = min(dist, key=dist.get)
            if dist[god] > GOD_ICON_MATCH:
                continue
            m = re.search(r"(\d+)\s*/\s*(\d+)", self.text(_ocr_ready(bgr[205:230, cx - 26:cx + 26], 170, 4)))
            out[god] = (int(m.group(1)), int(m.group(2))) if m else None
        return out

    def read_offers(self, img) -> list[dict]:
        bgr = _at_1080(img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        shift = bgr.shape[1] // 2 - 960  # cards are centred on the screen
        offers = []
        for cx in CARD_CENTRES:
            cx += shift
            title = self.text(bgr[555:597, cx - 170:cx + 170])
            title = re.sub(r"^[^A-Za-z]+", "", title).strip()
            if not title:  # no text reader: name the card by how its title looks
                t = cv2.resize(gray[560:592, cx - 150:cx + 150], (24, 6), interpolation=cv2.INTER_AREA)
                title = "Card " + hashlib.md5((t // 16).tobytes()).hexdigest()[:6]
            kind = self.text(bgr[608:642, cx - 140:cx + 140]).title() or None
            desc = self.text(bgr[650:760, cx - 165:cx + 165])
            gods: dict[str, int] = {}
            for k in range(4):  # favour icons stack down the card's top-left corner
                ix, iy = cx - 124, 298 + 70 * k
                if float((gray[iy - 20:iy + 20, ix - 20:ix + 20] > 150).mean()) < 0.1:
                    break
                icon = _norm(_patch(gray, ix, iy))
                score = {g: float(np.dot(icon.ravel(), _norm(t).ravel())) for g, t in _GOD_TEMPLATES.items()}
                god = max(score, key=score.get)
                if score[god] < CARD_ICON_MATCH:
                    break
                gods[god] = gods.get(god, 0) + 1
            offers.append({"name": title, "kind": kind if kind in ("Gift", "Rune", "Blessing") else kind,
                           "text": desc, "gods": gods})
        return offers


def favor_tier(value: int, threshold: int) -> int:
    """Favour tiers reached, from a counter like 5/8 (tiers at 3, 5 and 8)."""
    base = {3: 0, 5: 1, 7: 2, 8: 2}.get(threshold, 0)
    return base + (1 if value >= threshold else 0)


class CardTracker:
    """Follows card screens across frames and reports offers, picks and favour as events."""

    def __init__(self, reader: CardReader, emit: Callable[[dict], None]):
        self.reader = reader
        self.emit = emit
        self.offers: list[dict] | None = None
        self.favor_before: dict | None = None
        self._title_key: bytes | None = None
        self._last_after: dict | None = None
        self.favor: dict = {}

    def reset(self) -> None:
        self.offers, self.favor_before, self._title_key = None, None, None
        self._last_after = None

    def frame(self, img) -> bool:
        """Look at a frame. Returns True if it was the card screen (not a board frame)."""
        if CardReader.is_card_screen(img):
            bgr = _at_1080(img)
            key = cv2.resize(cv2.cvtColor(bgr[555:597, :], cv2.COLOR_BGR2GRAY), (96, 4)).tobytes()
            if self.offers is None or key != self._title_key:  # new offer, or rerolled
                if self.offers is not None and self._closed_since_offer:
                    self._settle(bgr, final=True)  # the last offer closed without a favour change
                self._title_key = key
                if self.favor_before is None:
                    self.favor_before = self.reader.read_favor(bgr)
                self.offers = self.reader.read_offers(bgr)
                self.emit({"type": "card_offer", "cards": self.offers})
            self._closed_since_offer = False
            return True
        if self.offers is not None:
            self._closed_since_offer = True
            self._settle(img, final=False)
        return False

    def battle_starting(self, img=None) -> None:
        """The build phase is over: settle any offer still open."""
        if self.offers is not None:
            self._settle(img, final=True)

    _closed_since_offer = False

    def _settle(self, img, final: bool) -> None:
        """final=False: only settle if favour changed (the screen may just be peeked at)."""
        if img is not None:
            after = self.reader.read_favor(img)
            self._last_after = after
        else:  # no frame: use the latest board reading since the offer
            after = self._last_after if self._last_after is not None else dict(self.favor_before or {})
        before = self.favor_before or {}
        gained = {g: v[0] - before[g][0] for g, v in after.items()
                  if v and before.get(g) and v[0] > before[g][0]}
        gained.update({g: v[0] for g, v in after.items() if v and g not in before})  # a god newly shown
        pick = None
        if gained:
            pick = next((c for c in self.offers if c["gods"] == gained), None)
            if pick is None:  # favour counters can wrap at a new tier: match on the god alone
                pick = next((c for c in self.offers if set(c["gods"]) == set(gained)), None)
        elif not final:
            return  # nothing changed yet: maybe just peeking at the board
        else:
            neutral = [c for c in self.offers if not c["gods"]]
            pick = neutral[0] if len(neutral) == 1 else None
        if pick is not None:
            god = next(iter(pick["gods"]), None)
            self.emit({"type": "card_picked", "name": pick["name"], "god": god,
                       "kind": (pick["kind"] or "").lower() or None})
        if after:
            self.favor = after
            self.emit({"type": "favor", "values": {g: list(v) for g, v in after.items() if v}})
        self.reset()

    def update_favor(self, img) -> None:
        values = self.reader.read_favor(img)
        if values and values != self.favor:
            self.favor = values
            self.emit({"type": "favor", "values": {g: list(v) for g, v in values.items() if v}})
