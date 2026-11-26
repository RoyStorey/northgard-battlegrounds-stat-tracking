# Northgard Battlegrounds Tracker

Records your Northgard Battlegrounds runs and shows which structures, Fate cards,
gods and clans are winning for you.

- **Tray app** watches the game in the background and saves each run.
- **Dashboard** at http://127.0.0.1:8765/ shows the stats.
- Data stays on your PC in `%LOCALAPPDATA%\nbg-tracker\tracker.db`.

## How it records

- **Player.log** gives each run, battle results (PvE vs PvP), ranked/unranked and ELO.
- **Screenshots** of the game window are taken during each build phase (only while
  the game is the window in front). The last one before each battle is kept in
  `%LOCALAPPDATA%\nbg-tracker\shots`.
- **Finding structures**: every tile has a hex badge that stays the same size at any
  zoom, and tiles with a structure have a number box beside it. The tracker finds
  badges by template matching, so it works wherever the camera is. Of the frames
  taken during a build phase, it keeps the one showing the most structures.
- **Review page**: after a run, open it from the dashboard's run list. Each round's
  screenshot shows the structures found, named where the tracker recognises them
  ("?" where it doesn't yet). Your names train the recogniser, so it fills boards in
  by itself after a run or two. Levels are read from the number in each badge.
- **Current run**: while a run is in progress, the top of the dashboard shows the
  round, wins, losses and lives left, each battle so far, and your board right now
  with your past record for every structure on it.
- **Clan** is worked out from clan-only structures on your board (e.g. Raven Banner
  Hall), including for runs recorded before.
- **Insights**: takeaways in plain words (only once the evidence holds up), each
  structure's impact, upgrade value by level, tempo (board size in battles won vs
  lost), clans, and where runs stall. All battles can be exported as CSV.
- **Names** come from the game's `StreamingAssets/aa/catalog.bin`, found through the
  install path in the log, with a bundled copy as fallback. The save file is
  encrypted, so it is not read.

Import an old log with `nbg-tracker replay "%USERPROFILE%\AppData\LocalLow\...\Player-prev.log"`.

## Install and run (Windows)

```
pip install .[tray]
nbg-tracker run
```

- `nbg-tracker run --no-tray` runs in a console window instead of the tray.
- `nbg-tracker run --log PATH` points at the game log if it isn't found automatically.
- `nbg-tracker run --no-screenshots` turns board capture off.
- `nbg-tracker serve` opens the dashboard without watching the game.
- `nbg-tracker replay FILE` imports runs from an old Player.log or a file of JSON events.

## How the stats work

Late rounds are harder, and rare structures only appear late, so raw win rates
make late-game pieces look bad. Each PvP battle gets an expected win chance from
your own record in that season (seasons change every 4 rounds). A piece's
**impact** is extra wins per 100 battles above that expectation while it's on
your board. Small samples get pulled toward zero, and each result is tagged
low/medium/high confidence.

## Events

The recorder builds runs from these events (one JSON object per line for `replay`):

| type | fields |
|---|---|
| `run_start` | `clan`, `warchief?`, `patch?`, `mode?`, `elo?`, `time?` |
| `board` | `structures: [{name, level}]`, `cards?: [{name, god, kind}]` |
| `structure_placed` / `structure_upgraded` / `structure_removed` | `name`, `level?` |
| `card_picked` | `name`, `god?`, `kind?` (gift/blessing/rune) |
| `battle_result` | `round`, `won`, `kind?` (pvp/pve), `opponent_clan?` |
| `run_end` | `elo?`, `time?` |

## Development

```
pip install -e .[dev]
pytest
```
