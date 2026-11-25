"""System tray icon: open dashboard, pause recording, quit."""
from __future__ import annotations

import webbrowser

from .capture import Recorder


def _icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(37, 125, 224, 255))
    d.polygon([(32, 14), (48, 46), (16, 46)], fill=(255, 255, 255, 255))
    return img


def run_tray(url: str, recorder: Recorder, status: dict) -> None:
    try:
        import pystray
    except ImportError as e:
        raise SystemExit("tray support needs: pip install nbg-tracker[tray]  (or use --no-tray)") from e

    def toggle_pause(icon, item):
        recorder.paused = not recorder.paused
        status["paused"] = recorder.paused

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda i, it: webbrowser.open(url), default=True),
        pystray.MenuItem("Pause recording", toggle_pause, checked=lambda it: recorder.paused),
        pystray.MenuItem("Quit", lambda i, it: i.stop()),
    )
    pystray.Icon("nbg-tracker", _icon_image(), "Northgard Battlegrounds Tracker", menu).run()
