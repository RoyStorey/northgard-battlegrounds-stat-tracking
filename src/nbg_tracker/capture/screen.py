"""Screenshots of the board, taken during each build phase.

Only the game's own window is captured, and only while it is the window in
front, so nothing else on your screen is ever recorded. Frames are taken
every couple of seconds during the build phase (frames of the battle view,
which shows the opponent's village too, are skipped), and the one showing the most
structures is kept as that battle's board when the log says a battle starts
(the camera moves, so the last frame may be zoomed in on one corner).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from PIL import Image

from ..vision import Recognizer, frame_quality, is_build_view

log = logging.getLogger(__name__)

WINDOW_TITLE = "Northgard Battlegrounds"


class GameWindow:
    """Finds the game window and grabs its client area (Windows only)."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("screen capture only works on Windows")
        import ctypes
        from ctypes import wintypes

        import mss

        self.ctypes, self.wintypes = ctypes, wintypes
        self.user32 = ctypes.windll.user32
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixels on scaled displays
        except Exception:
            pass
        self.sct = None
        self._mss = mss

    def _find(self) -> int | None:
        found: list[int] = []
        proc = self.ctypes.WINFUNCTYPE(self.wintypes.BOOL, self.wintypes.HWND, self.wintypes.LPARAM)

        def cb(hwnd, _):
            if self.user32.IsWindowVisible(hwnd):
                buf = self.ctypes.create_unicode_buffer(256)
                self.user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value.strip() == WINDOW_TITLE:
                    found.append(hwnd)
            return True

        self.user32.EnumWindows(proc(cb), 0)
        return found[0] if found else None

    def grab(self) -> Image.Image | None:
        """The game's client area, or None if the game is not the window in front."""
        hwnd = self.user32.GetForegroundWindow()
        if not hwnd or hwnd != self._find():
            return None
        rect = self.wintypes.RECT()
        self.user32.GetClientRect(hwnd, self.ctypes.byref(rect))
        pt = self.wintypes.POINT(0, 0)
        self.user32.ClientToScreen(hwnd, self.ctypes.byref(pt))
        if rect.right < 200 or rect.bottom < 200:
            return None  # minimised
        if self.sct is None:
            self.sct = self._mss.mss()  # mss handles are per thread; created in the capture thread
        shot = self.sct.grab({"left": pt.x, "top": pt.y, "width": rect.right, "height": rect.bottom})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class ScreenCapturer:
    """Event filter: passes events through and adds screenshot + board events before each battle."""

    def __init__(self, emit: Callable[[dict], None], shots_dir: str | Path,
                 recognizer: Recognizer | None = None, grab: Callable[[], Image.Image | None] | None = None,
                 interval: float = 1.5, view_check: Callable[[Image.Image], bool] = is_build_view,
                 reader=None, cards=None):
        """reader: reads the shop and clan off a frame (learning.Learner); optional.
        cards: follows Fate card screens and favour (cards.CardTracker); optional."""
        self.emit = emit
        self.shots_dir = Path(shots_dir)
        self.shots_dir.mkdir(parents=True, exist_ok=True)
        self.recognizer = recognizer
        self._grab = grab
        self.interval = interval
        self.view_check = view_check
        self.reader = reader
        self.cards = cards
        self.building = False
        self.latest: Image.Image | None = None
        self.latest_quality = -1
        self.round = 0
        self.live: dict | None = None  # the board right now, for the dashboard's current-run panel
        self._live_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._grab is None:
            window = GameWindow()
            self._grab = window.grab
        self._thread = threading.Thread(target=self._loop, name="screen", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.building:
                try:
                    img = self._grab()
                except Exception:
                    log.exception("screen grab failed")
                    img = None
                if img is not None and self.cards is not None:
                    try:
                        if self.cards.frame(img):
                            img = None  # the card choice screen, not the board
                    except Exception:
                        log.exception("card screen check failed")
                if img is not None and not self._is_build_view(img):
                    img = None  # battle view: the opponent's village is on screen too
                if img is not None:
                    try:
                        q = frame_quality(img)
                    except Exception:
                        log.exception("frame check failed")
                        q = 0
                    with self._lock:
                        better = q >= self.latest_quality  # ties go to the newer frame
                        if better:
                            self.latest, self.latest_quality = img, q
                    if better and time.monotonic() - self._live_at > 4:
                        self._update_live(img)
            self._stop.wait(self.interval)

    def _is_build_view(self, img: Image.Image) -> bool:
        try:
            return self.view_check(img)
        except Exception:
            log.exception("view check failed")
            return False

    def _update_live(self, img: Image.Image) -> None:
        self._live_at = time.monotonic()
        try:
            found = self.recognizer.recognize(img) if self.recognizer else []
            shop = self.reader.read_shop(img) if self.reader else []
            if self.cards is not None:
                self.cards.update_favor(img)
            clan = self.reader.read_clan(img) if self.reader else None
            if clan:
                self.emit({"type": "run_info", "clan": clan, "clan_source": "portrait"})
            tmp = self.shots_dir / "live.tmp.jpg"
            img.save(tmp, format="JPEG", quality=80)
            tmp.replace(self.shots_dir / "live.jpg")
        except Exception:
            log.exception("live board update failed")
            return
        self.live = {"file": "live.jpg", "updated": datetime.now().isoformat(timespec="seconds"),
                     "structures": [{"name": s.name, "level": s.level, "x": s.x, "y": s.y} for s in found],
                     "shop": shop, "clan_guess": clan}

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "run_start":
            self.round, self.live = 0, None
            if self.cards is not None:
                self.cards.reset()
                self.cards.favor = {}
            self._begin_build()
        elif kind == "battle_result":
            self.round = event.get("round", self.round + 1)
            self._begin_build()
        elif kind == "opponent" and self.building:
            self._battle_starting()
        elif kind == "run_end":
            self.building, self.live = False, None
        self.emit(event)

    def _begin_build(self) -> None:
        with self._lock:
            self.latest, self.latest_quality = None, -1
        self.building = True

    def _battle_starting(self) -> None:
        self.building = False
        if self.cards is not None:
            try:
                self.cards.battle_starting()
            except Exception:
                log.exception("settling the card offer failed")
        with self._lock:
            img, self.latest, self.latest_quality = self.latest, None, -1
        if img is None:
            return
        name = f"{datetime.now():%Y%m%d-%H%M%S}-r{self.round + 1:02d}.jpg"
        img.save(self.shots_dir / name, quality=85)
        self.emit({"type": "screenshot", "file": name})
        if self.recognizer is not None:
            found = self.recognizer.recognize(img)
            self.emit({"type": "board", "structures": [
                {"name": s.name, "level": s.level, "x": s.x, "y": s.y, "source": s.source} for s in found]})
