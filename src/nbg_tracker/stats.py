"""Statistics: which pieces win for you, adjusted for how hard each stage is.

Late rounds are harder, and rare structures only show up late, so a raw win
rate would make late-game pieces look bad. Each PvP battle therefore gets an
*expected* win chance from your own record in that season. A piece's impact
is how many more battles you win than expected while it is on your board,
shrunk toward zero for small samples so a 3-0 fluke does not top the list.
"""
from __future__ import annotations

import math
from collections import defaultdict
from itertools import combinations
from statistics import mean
from typing import Callable, Iterable

from .models import Battle, Run

SHRINK = 5.0  # pseudo-battles of "no effect" added to every feature
BASELINE_SHRINK = 2.0  # lighter: seasons quickly collect many battles
MIN_BATTLES = 3  # features seen in fewer battles are hidden
MIN_PAIR_BATTLES = 6


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def filter_runs(runs: Iterable[Run], patch: str | None = None, clan: str | None = None,
                mode: str | None = None) -> list[Run]:
    return [r for r in runs
            if (not patch or r.patch == patch)
            and (not clan or r.clan == clan)
            and (not mode or r.mode == mode)]


def _pvp(runs: list[Run]) -> list[tuple[Run, Battle]]:
    return [(r, b) for r in runs for b in r.battles if b.kind == "pvp"]


def season_baseline(battles: list[Battle]) -> Callable[[Battle], float]:
    """Expected win chance per season, shrunk toward your overall rate."""
    if not battles:
        return lambda b: 0.5
    overall = sum(b.won for b in battles) / len(battles)
    by_season: dict[int, list[int]] = defaultdict(list)
    for b in battles:
        by_season[b.season].append(int(b.won))
    rates = {s: (sum(v) + BASELINE_SHRINK * overall) / (len(v) + BASELINE_SHRINK) for s, v in by_season.items()}
    return lambda b: rates.get(b.season, overall)


def _confidence(residual: float, variance: float) -> str:
    if variance <= 0:
        return "low"
    z = abs(residual) / math.sqrt(variance)
    return "high" if z >= 2 else "medium" if z >= 1 else "low"


def feature_impacts(battles: list[Battle], features: Callable[[Battle], Iterable[str]],
                    min_battles: int = MIN_BATTLES) -> list[dict]:
    expected = season_baseline(battles)
    acc: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "exp": 0.0, "var": 0.0})
    for b in battles:
        p = expected(b)
        for f in set(features(b)):
            a = acc[f]
            a["n"] += 1
            a["wins"] += int(b.won)
            a["exp"] += p
            a["var"] += p * (1 - p)
    out = []
    for name, a in acc.items():
        if a["n"] < min_battles:
            continue
        residual = a["wins"] - a["exp"]
        lo, hi = wilson(a["wins"], a["n"])
        out.append({
            "name": name,
            "battles": a["n"],
            "wins": a["wins"],
            "win_rate": a["wins"] / a["n"],
            "win_rate_ci": [lo, hi],
            "expected_win_rate": a["exp"] / a["n"],
            # extra wins per 100 battles vs. your usual rate at that stage
            "impact": 100 * residual / (a["n"] + SHRINK),
            "confidence": _confidence(residual, a["var"]),
        })
    out.sort(key=lambda d: d["impact"], reverse=True)
    return out


def _structures(b: Battle) -> list[str]:
    return [s.name for s in b.structures]


def _cards(b: Battle) -> list[str]:
    return [c.name for c in b.cards]


def _gods(b: Battle) -> list[str]:
    return [c.god for c in b.cards if c.god]


def _pairs(b: Battle) -> list[str]:
    names = sorted(set(_structures(b)))
    return [f"{x} + {y}" for x, y in combinations(names, 2)]


def group_runs(runs: list[Run], key: Callable[[Run], str | None]) -> list[dict]:
    groups: dict[str, list[Run]] = defaultdict(list)
    for r in runs:
        groups[key(r) or "Unknown"].append(r)
    out = []
    for name, rs in groups.items():
        clears = sum(r.cleared for r in rs)
        deltas = [r.elo_after - r.elo_before for r in rs
                  if r.elo_after is not None and r.elo_before is not None]
        out.append({
            "name": name,
            "runs": len(rs),
            "avg_wins": mean(r.wins for r in rs),
            "clear_rate": clears / len(rs),
            "clear_rate_ci": list(wilson(clears, len(rs))),
            "avg_elo_delta": mean(deltas) if deltas else None,
        })
    out.sort(key=lambda d: (d["avg_wins"], d["runs"]), reverse=True)
    return out


def by_round(runs: list[Run]) -> list[dict]:
    acc: dict[int, list[int]] = defaultdict(list)
    for _, b in _pvp(runs):
        acc[b.round].append(int(b.won))
    return [{"round": k, "battles": len(v), "win_rate": sum(v) / len(v),
             "win_rate_ci": list(wilson(sum(v), len(v)))} for k, v in sorted(acc.items())]


def trend(runs: list[Run], window: int = 10) -> list[dict]:
    ordered = sorted(runs, key=lambda r: (r.started_at or "", r.id or 0))
    out = []
    for i, r in enumerate(ordered):
        recent = ordered[max(0, i - window + 1): i + 1]
        out.append({"run_id": r.id, "started_at": r.started_at, "wins": r.wins,
                    "rolling_avg_wins": mean(x.wins for x in recent)})
    return out


def summary(runs: list[Run]) -> dict:
    pvp = [b for _, b in _pvp(runs)]
    deltas = [r.elo_after - r.elo_before for r in runs
              if r.elo_after is not None and r.elo_before is not None]
    return {
        "runs": len(runs),
        "battles": len(pvp),
        "avg_wins": mean(r.wins for r in runs) if runs else None,
        "clear_rate": sum(r.cleared for r in runs) / len(runs) if runs else None,
        "battle_win_rate": sum(b.won for b in pvp) / len(pvp) if pvp else None,
        "avg_elo_delta": mean(deltas) if deltas else None,
    }


def report(runs: list[Run], patch: str | None = None, clan: str | None = None,
           mode: str | None = None) -> dict:
    """Everything the dashboard shows, for the filtered set of runs."""
    selected = filter_runs(runs, patch, clan, mode)
    battles = [b for _, b in _pvp(selected)]
    return {
        "filters": {
            "patches": sorted({r.patch for r in runs if r.patch}, reverse=True),
            "clans": sorted({r.clan for r in runs if r.clan}),
            "modes": sorted({r.mode for r in runs if r.mode}),
        },
        "summary": summary(selected),
        "structures": feature_impacts(battles, _structures),
        "cards": feature_impacts(battles, _cards),
        "gods": feature_impacts(battles, _gods),
        "synergies": feature_impacts(battles, _pairs, MIN_PAIR_BATTLES),
        "clans": group_runs(selected, lambda r: r.clan),
        "warchiefs": group_runs(selected, lambda r: r.warchief),
        "rounds": by_round(selected),
        "trend": trend(selected),
        "recent_runs": [
            {k: v for k, v in r.to_dict().items() if k != "battles"}
            | {"final_structures": sorted({s.name for s in (r.battles[-1].structures if r.battles else [])}),
               "to_review": sum(1 for b in r.battles if b.screenshot and not b.reviewed)}
            for r in sorted(selected, key=lambda r: (r.started_at or "", r.id or 0), reverse=True)[:25]
        ],
    }
