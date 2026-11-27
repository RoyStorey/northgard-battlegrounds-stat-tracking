"""Fair credit for each piece: one model over all your battles.

A simple "win rate while it's on the board" gives too much credit to pieces
you only buy when a run is already going well. Each PvP battle is therefore
modelled (logistic regression) from:

* the season (late rounds are harder),
* your record so far in the run (wins minus losses before the battle),
* how big your board is (total structure levels),
* which pieces are on the board, all at once, so pieces that tend to appear
  together share the credit instead of each taking all of it.

Each piece's effect is pulled toward zero (an L2 penalty). The pull weighs
the same for every piece, so pieces seen in only a few battles, which carry
little evidence, stay close to zero, while the ones you build every game are
measured precisely.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np

from .models import Battle, Run

PIECE_PENALTY = 4.0  # strength of the pull toward "no effect"
CONTROL_PENALTY = 0.01


@dataclass
class Situation:
    run_id: int | None
    battle: Battle
    season: int
    record: int  # PvP wins minus losses earlier in the same run
    levels: int  # total level of named structures on the board


def situations(runs: Iterable[Run]) -> list[Situation]:
    out = []
    for r in runs:
        w = l = 0
        for b in sorted(r.battles, key=lambda b: b.round):
            if b.kind != "pvp":
                continue
            named = [s for s in b.structures if s.name and s.name != "?"]
            out.append(Situation(r.id, b, min(b.season, 3), w - l, sum(s.level for s in named)))
            w += b.won
            l += not b.won
    return out


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-np.clip(z, -30, 30)))


def adjusted_impacts(sits: list[Situation], features: Callable[[Battle], Iterable[str]],
                     has_info: Callable[[Battle], bool], min_battles: int = 3) -> list[dict]:
    """One row per piece: its fair effect in extra wins per 100 battles, plus plain facts."""
    sits = [s for s in sits if has_info(s.battle)]
    if len(sits) < 8:
        return []
    feats = [set(features(s.battle)) for s in sits]
    counts: dict[str, int] = defaultdict(int)
    for f in feats:
        for name in f:
            counts[name] += 1
    names = sorted(n for n, c in counts.items() if c >= min_battles)
    if not names:
        return []
    col = {n: i for i, n in enumerate(names)}

    n_ctrl = 6  # intercept, 3 seasons (Spring is the base), record, board size
    X = np.zeros((len(sits), n_ctrl + len(names)))
    y = np.array([float(s.battle.won) for s in sits])
    for i, (s, f) in enumerate(zip(sits, feats)):
        X[i, 0] = 1
        if s.season:
            X[i, s.season] = 1
        X[i, 4] = max(-5, min(10, s.record)) / 5
        X[i, 5] = s.levels / 10
        for name in f:
            if name in col:
                X[i, n_ctrl + col[name]] = 1
    penalty = np.r_[[CONTROL_PENALTY] * n_ctrl, [PIECE_PENALTY] * len(names)]

    beta = np.zeros(X.shape[1])
    for _ in range(50):  # Newton steps
        p = _sigmoid(X @ beta)
        w = p * (1 - p)
        hessian = (X * w[:, None]).T @ X + np.diag(penalty)
        step = np.linalg.solve(hessian, X.T @ (y - p) - penalty * beta)
        beta += step
        if np.abs(step).max() < 1e-6:
            break
    p = _sigmoid(X @ beta)
    w = p * (1 - p)
    hessian = (X * w[:, None]).T @ X + np.diag(penalty)
    se = np.sqrt(np.clip(np.diag(np.linalg.inv(hessian)), 1e-12, None))

    runs_total = len({s.run_id for s in sits})
    out = []
    for name, j in col.items():
        k = n_ctrl + j
        rows = X[:, k] == 1
        n = int(rows.sum())
        wins = int(y[rows].sum())
        without = _sigmoid(X[rows] @ beta - beta[k])  # the same battles, had the piece not been there
        z = abs(beta[k]) / se[k]
        runs_with = len({s.run_id for s, r in zip(sits, rows) if r})
        out.append({
            "name": name,
            "battles": n,
            "wins": wins,
            "win_rate": wins / n,
            "expected_win_rate": float(without.mean()),
            "impact": float(100 * (p[rows] - without).mean()),
            "confidence": "high" if z >= 2 else "medium" if z >= 1 else "low",
            "runs": runs_with,
            "usage": runs_with / runs_total if runs_total else 0.0,
            "avg_record": float(np.mean([s.record for s, r in zip(sits, rows) if r])),
        })
    out.sort(key=lambda d: d["impact"], reverse=True)
    return out
