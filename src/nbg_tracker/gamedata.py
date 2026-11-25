"""Turns the game's ID codes into names.

Save data refers to structures, cards and clans by Unity asset GUIDs. The
game's Addressables catalog (StreamingAssets/aa/catalog.bin) maps each GUID
to an asset path such as Assets/Core/Data/Items/ItemData/HealersHut.asset,
and the path tells us both the name and what kind of thing it is.

A copy of the mapping ships with the app (known_ids.json); the live catalog
is read on top of it so new pieces from patches get names too.
"""
from __future__ import annotations

import json
import logging
import re
import struct
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

MAGIC = 0x0DE38942
_MASK = 0x3FFFFFFF
_DYNAMIC = 0x40000000
_UNICODE = 0x80000000
_NONE = 0xFFFFFFFF
_GUID = re.compile(r"[0-9a-f]{32}")


def read_catalog(path: str | Path) -> dict[str, str]:
    """GUID -> asset path, from an Addressables 2.x binary catalog."""
    d = Path(path).read_bytes()

    def u(o: int) -> int:
        return struct.unpack_from("<I", d, o)[0]

    def plain(v: int) -> str:
        o = v & _MASK
        b = d[o:o + u(o - 4)]
        return b.decode("utf-16-le" if v & _UNICODE else "latin-1")

    def string(v: int) -> str | None:
        if v == _NONE:
            return None
        if not v & _DYNAMIC:
            return plain(v)
        parts, o = [], v & _MASK  # stored as a chain of (part, next) pairs, last part first
        for _ in range(256):
            parts.append(string(u(o)) or "")
            nxt = u(o + 4)
            if nxt == _NONE:
                break
            o = nxt & _MASK
        return "/".join(reversed(parts))

    if u(0) != MAGIC:
        raise ValueError("not an Addressables binary catalog")
    keys_offset = u(8)
    out: dict[str, str] = {}
    for k in range(u(keys_offset - 4) // 8):
        key_obj, location_set = u(keys_offset + 8 * k), u(keys_offset + 8 * k + 4)
        try:
            key = string(u(u(key_obj + 4)))
            if not key or not _GUID.fullmatch(key):
                continue
            first_location = u(location_set)
            asset = string(u(first_location + 4))  # the location's internal id
        except (struct.error, UnicodeDecodeError, RecursionError):
            continue
        if asset and asset.startswith("Assets/"):
            out[key] = asset
    return out


@dataclass
class Piece:
    kind: str  # structure, clan, card, favor, clan_skill, other
    name: str
    god: str | None = None
    tier: str | None = None


def _words(s: str) -> str:
    s = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"\s+", " ", s.replace("_", " ")).strip()


def describe(asset: str) -> Piece:
    stem = Path(asset).stem
    if "/Items/ItemData/" in asset:
        if "/Environment/" in asset or "/UpgradeItems/" in asset:
            return Piece("other", _words(stem))
        if stem.startswith("BannerHall"):
            return Piece("structure", "Banner Hall")
        return Piece("structure", _words(stem))
    if "/Players/Clans/Clans/" in asset:
        return Piece("clan", stem.removesuffix("Clan"))
    if m := re.search(r"/Cards/Patrons/(\w+)/ExtraCards/(\w+)/", asset):
        return Piece("card", stem.replace("_", " "), god=m.group(1), tier=m.group(2))
    if m := re.search(r"/Cards/Patrons/(\w+)/AffinityBlessings/", asset):
        level = re.search(r"(\d+)$", stem)
        return Piece("favor", f"{m.group(1)} favor {int(level.group(1)) if level else ''}".strip(), god=m.group(1))
    if m := re.match(r"ClanSkill_(\w+?)_(\d+)_Lvl(\d)", stem):
        return Piece("clan_skill", f"{m.group(1)} skill {int(m.group(2))}")
    return Piece("other", _words(stem))


class GameData:
    def __init__(self, catalog: str | Path | None = None):
        self.assets: dict[str, str] = json.loads(
            resources.files("nbg_tracker").joinpath("known_ids.json").read_text(encoding="utf-8"))
        if catalog and Path(catalog).exists():
            try:
                self.assets.update(read_catalog(catalog))
            except Exception:
                log.exception("could not read %s; using the bundled names", catalog)

    def piece(self, guid: str) -> Piece:
        asset = self.assets.get(guid)
        return describe(asset) if asset else Piece("other", f"Unknown {guid[:8]}")


def catalog_from_log(player_log: str | Path) -> Path | None:
    """The log's first lines name the install folder; the catalog sits under it."""
    try:
        with open(player_log, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(50), f):
                if m := re.search(r"Discovering subsystems at path (.+?)[/\\]UnitySubsystems", line):
                    return Path(m.group(1)) / "StreamingAssets" / "aa" / "catalog.bin"
    except OSError:
        pass
    return None
