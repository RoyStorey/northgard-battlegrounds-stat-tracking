# Northgard Battlegrounds Tracker

Records your Northgard Battlegrounds runs and shows which structures, Fate cards,
gods and clans are winning for you.

- **Tray app** watches the game in the background and saves each run.
- **Dashboard** at http://127.0.0.1:8765/ shows the stats.
- Data stays on your PC in `%LOCALAPPDATA%\nbg-tracker\tracker.db`.

## Status

Storage, statistics, the dashboard and the game-log parser work. The log gives
runs, battle results (PvE vs PvP), ranked/unranked and ELO. Structures, cards
and clan are not in the log; reading them from the save file is next. Runs can
also be added through the API or replayed from an event file.

## Install and run (Windows)

```
pip install .[tray]
nbg-tracker run
```

- `nbg-tracker run --no-tray` runs in a console window instead of the tray.
- `nbg-tracker run --log PATH` points at the game log if it isn't found automatically.
- `nbg-tracker serve` opens the dashboard without watching the game.
- `nbg-tracker replay events.jsonl` records runs from a file of JSON events.

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
