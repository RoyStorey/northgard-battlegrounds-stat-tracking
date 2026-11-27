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
  whose favour went up, by how much, must match exactly one card on offer.
  Anything uncertain (a misread counter, a neutral card, two matching cards)
  is not recorded rather than guessed.
* Cards are found by their type word (GIFT / RUNE / BLESSING) wherever they're
  drawn, and only read once the screen has stopped moving.

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

FAVOR_SLOTS = (51, 113, 176, 238)  # x of the favour counters, left to right
GOD_ICON_MATCH = 25  # mean grey difference for a favour icon to count as that god
CARD_ICON_MATCH = 0.5  # correlation for a god icon on a card (true icons score ~0.75-0.8)


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

    def boxes(self, img: np.ndarray) -> list[tuple[float, float, str]]:
        """(x, y, text) of every piece of text found in an image."""
        engine = self._engine()
        if engine is None:
            return []
        return [((b[0][0] + b[2][0]) / 2, (b[0][1] + b[2][1]) / 2, str(t).strip())
                for b, t, _ in (engine(img)[0] or [])]

    def read_offers(self, img) -> list[dict]:
        """The cards on offer, found by their type word (GIFT / RUNE / BLESSING) wherever they're
        drawn: the name sits just above it, the description below. Returns [] unless every card
        reads cleanly, so a half-drawn or animating screen is simply tried again."""
        bgr = _at_1080(img)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        w = bgr.shape[1]
        x0, y0 = max(0, w // 2 - 600), 200
        region = bgr[y0:850, x0:w // 2 + 600]
        found = [(x + x0, y + y0, t) for x, y, t in self.boxes(region)]
        anchors = [(x, y, KINDS[_letters(t)]) for x, y, t in found if _letters(t) in KINDS]
        anchors.sort()
        if len(anchors) != 3:
            return []
        offers = []
        for ax, ay, kind in anchors:
            above = [(ay - y, x, t) for x, y, t in found if 25 < ay - y < 90 and abs(x - ax) < 130]
            if not above:
                return []
            gap, _, title = min(above)
            title = clean_title(title)
            if not title:
                return []
            below = sorted((y, x, t) for x, y, t in found if 15 < y - ay < 170 and abs(x - ax) < 175
                           and _letters(t) not in KINDS)
            lines: list[list[tuple[float, str]]] = []
            for y, x, t in below:  # group into lines, then read each left to right
                if lines and abs(lines[-1][0][0] - y) < 12:
                    lines[-1].append((y, x, t))
                else:
                    lines.append([(y, x, t)])
            text = " ".join(" ".join(t for _, _, t in sorted(line, key=lambda b: b[1])) for line in lines)
            scale = gap / 50  # name-to-type distance is 50 px at full size
            offers.append({"name": title, "kind": kind, "text": text.strip(),
                           "gods": self._card_gods(gray, ax, ay - gap, scale)})
        return offers

    @staticmethod
    def _card_gods(gray: np.ndarray, x: float, title_y: float, scale: float) -> dict[str, int]:
        """God icons stacked in a card's top-left corner (one per point of favour). The spot is
        estimated from the text, so a few pixels around it are searched for the best match."""
        if abs(scale - 1) < 0.05:
            scale = 1.0  # measured from text boxes: small differences are noise
        templates = {g: _norm(t).ravel() for g, t in _GOD_TEMPLATES.items()}
        gods: dict[str, int] = {}
        r = max(8, int(round(18 * scale)))
        step = max(1, int(round(70 * scale)))
        ix0, iy0 = int(round(x - 124 * scale)), int(round(title_y - 277 * scale))
        for k in range(4):
            best_god, best = None, CARD_ICON_MATCH
            for dy in range(-6, 7, 2):
                for dx in range(-6, 7, 2):
                    ix, iy = ix0 + dx, iy0 + k * step + dy
                    patch = gray[iy - r:iy + r, ix - r:ix + r]
                    if patch.shape != (2 * r, 2 * r) or float((patch > 150).mean()) < 0.1:
                        continue
                    icon = _norm(cv2.resize(patch, (16, 16), interpolation=cv2.INTER_AREA).astype(np.float32)).ravel()
                    for g, t in templates.items():
                        score = float(np.dot(icon, t))
                        if score > best:
                            best_god, best = g, score
            if best_god is None:
                break
            gods[best_god] = gods.get(best_god, 0) + 1
        return gods


KINDS = {"gift": "Gift", "rune": "Rune", "blessing": "Blessing"}


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def card_key(name: str) -> str:
    """Identity of a card name, ignoring spaces and case ("BeseechThor" = "Beseech Thor")."""
    return _letters(name)


def clean_title(text: str) -> str:
    """Drop the banner's ornaments (read as X or symbols) and restore missing spaces."""
    words = [w for w in re.split(r"\s+", text) if w]
    while words and (len(_letters(words[0])) <= 1 or not re.search(r"[A-Za-z]{2}", words[0])):
        words.pop(0)
    while words and (len(_letters(words[-1])) <= 1 or not re.search(r"[A-Za-z]{2}", words[-1])):
        words.pop()
    title = " ".join(re.sub(r"[^A-Za-z'\-]", "", w) for w in words)
    title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title)  # BeseechThor -> Beseech Thor
    return re.sub(r"\s+", " ", title).strip()


def favor_tier(value: int, threshold: int) -> int:
    """Favour tiers reached, from a counter like 5/8 (tiers at 3, 5 and 8)."""
    base = {3: 0, 5: 1, 7: 2, 8: 2}.get(threshold, 0)
    return base + (1 if value >= threshold else 0)


class CardTracker:
    """Follows card screens across frames and reports offers, picks and favour as events."""

    def __init__(self, reader: CardReader, emit: Callable[[dict], None], keep_dir=None, keep: int = 10):
        """keep_dir: where to keep copies of the last few card screens read, for troubleshooting."""
        self.reader = reader
        self.emit = emit
        self.keep_dir = keep_dir
        self.keep = keep
        self.offers: list[dict] | None = None
        self.favor_before: dict | None = None
        self._offer_names: tuple | None = None
        self._last_sig = None
        self._read_sig = None
        self._last_after: dict | None = None
        self.favor: dict = {}

    def reset(self) -> None:
        self.offers, self.favor_before, self._offer_names = None, None, None
        self._last_after = None

    def frame(self, img) -> bool:
        """Look at a frame. Returns True if it was the card screen (not a board frame)."""
        if not CardReader.is_card_screen(img):
            self._last_sig = None
            if self.offers is not None:
                self._closed_since_offer = True
                self._settle(img, final=False)
            return False
        bgr = _at_1080(img)
        w = bgr.shape[1]
        sig = cv2.resize(cv2.cvtColor(bgr[200:850, max(0, w // 2 - 600):w // 2 + 600], cv2.COLOR_BGR2GRAY),
                         (60, 32), interpolation=cv2.INTER_AREA).astype(np.int16)
        steady = self._last_sig is not None and float(np.abs(sig - self._last_sig).mean()) < 4
        self._last_sig = sig
        if not steady:
            return True  # cards still moving in (or a hover changed them): read on the next frame
        if self._read_sig is not None and float(np.abs(sig - self._read_sig).mean()) < 4:
            self._closed_since_offer = False
            return True  # same cards as already read
        offers = self.reader.read_offers(bgr)
        if len(offers) != 3:
            return True  # couldn't read every card cleanly; try again
        self._read_sig = sig
        names = tuple(card_key(c["name"]) for c in offers)
        if names != self._offer_names:  # a new offer, or a reroll
            if self.offers is not None and self._closed_since_offer:
                self._settle(bgr, final=True)
            if self.favor_before is None:
                self.favor_before = self.reader.read_favor(bgr)
            self.offers, self._offer_names = offers, names
            self.emit({"type": "card_offer", "cards": offers})
            self._keep_copy(bgr)
        self._closed_since_offer = False
        return True

    def _keep_copy(self, bgr) -> None:
        if not self.keep_dir:
            return
        try:
            from datetime import datetime
            from pathlib import Path
            folder = Path(self.keep_dir)
            folder.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(folder / f"cards-{datetime.now():%Y%m%d-%H%M%S}.jpg"), bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
            for old in sorted(folder.glob("cards-*.jpg"))[:-self.keep]:
                old.unlink()
        except OSError:
            log.exception("couldn't keep a copy of the card screen")

    def battle_starting(self, img=None) -> None:
        """The build phase is over: settle any offer still open."""
        if self.offers is not None:
            self._settle(img, final=True)

    _closed_since_offer = False

    def _settle(self, img, final: bool) -> None:
        """Work out which card was taken from the change in favour. Only records a pick when the
        favour readings are complete and exactly one card on offer matches the change; anything
        uncertain is left unrecorded rather than guessed. final=False: the screen may only be
        hidden for a peek at the board, so wait unless favour has changed."""
        if img is not None:
            after = self.reader.read_favor(img)
            self._last_after = after
        else:
            after = self._last_after
        before = self.favor_before or {}
        if not after or not before or set(after) != set(before) or None in after.values() or None in before.values():
            if final:
                self.reset()
            return
        change = {g: after[g][0] - before[g][0] for g in after}
        if any(d < 0 for d in change.values()):  # a misread: don't trust either reading
            if final:
                self.reset()
            return
        gained = {g: d for g, d in change.items() if d > 0}
        if not gained:
            if final:
                self.reset()
            return
        matches = [c for c in self.offers if c["gods"] == gained]
        if len(matches) == 1 and sum(gained.values()) <= 4:
            pick = matches[0]
            self.emit({"type": "card_picked", "name": pick["name"], "god": next(iter(pick["gods"])),
                       "kind": (pick["kind"] or "").lower() or None})
        self.favor = after
        self.emit({"type": "favor", "values": {g: list(v) for g, v in after.items()}})
        self.reset()

    def update_favor(self, img) -> None:
        values = self.reader.read_favor(img)
        if values and values != self.favor:
            self.favor = values
            self.emit({"type": "favor", "values": {g: list(v) for g, v in values.items() if v}})
