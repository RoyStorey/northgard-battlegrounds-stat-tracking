import json

from fastapi.testclient import TestClient

from nbg_tracker import stats
from nbg_tracker.capture import Recorder
from nbg_tracker.capture.logtail import JsonLinesParser, LogTailer
from nbg_tracker.db import Database
from nbg_tracker.models import Battle, Run, Structure
from nbg_tracker.server import create_app


def make_run(clan, results, structures, patch="0.80.47p"):
    return Run(clan=clan, patch=patch, battles=[
        Battle(round=i + 1, won=w, structures=[Structure(s) for s in structures])
        for i, w in enumerate(results)])


def test_db_round_trip(tmp_path):
    db = Database(tmp_path / "t.db")
    run = make_run("Stag", [True, False, True], ["Banner Hall", "Watchtower"])
    run.battles[0].cards = []
    rid = db.save_run(run)
    [back] = db.load_runs()
    assert back.id == rid and back.wins == 2 and back.losses == 1
    assert [s.name for s in back.battles[2].structures] == ["Banner Hall", "Watchtower"]
    assert db.delete_run(rid) and db.load_runs() == []


def test_impact_ranks_winning_piece_first():
    runs = [make_run("Wolf", [True] * 8, ["Wolf Den"]) for _ in range(3)]
    runs += [make_run("Wolf", [False] * 8, ["Sheepfold"]) for _ in range(3)]
    rep = stats.report(runs)
    names = [r["name"] for r in rep["structures"]]
    assert names[0] == "Wolf Den" and names[-1] == "Sheepfold"
    assert rep["structures"][0]["impact"] > 0 > rep["structures"][-1]["impact"]
    assert rep["structures"][0]["confidence"] == "high"


def test_impact_adjusts_for_season():
    # A late-game structure that wins at the usual late-game rate is neutral,
    # even though its raw win rate is lower than early pieces'.
    runs = []
    for _ in range(10):
        early = [Battle(round=r, won=True, structures=[Structure("Scout Camp")]) for r in range(1, 5)]
        late = [Battle(round=r, won=(r % 2 == 0), structures=[Structure("Yggdrasil")]) for r in range(5, 9)]
        runs.append(Run(clan="Raven", battles=early + late))
    rows = {r["name"]: r for r in stats.report(runs)["structures"]}
    assert abs(rows["Yggdrasil"]["impact"]) < 2
    assert rows["Yggdrasil"]["win_rate"] == 0.5


def test_small_samples_are_hidden_and_shrunk():
    runs = [make_run("Bear", [True, True], ["Bear Cave"])]
    assert stats.report(runs)["structures"] == []


def test_recorder_and_tailer(tmp_path):
    db = Database(tmp_path / "t.db")
    rec = Recorder(db)
    events = [
        {"type": "run_start", "clan": "Raven", "patch": "0.80.47p", "elo": 300},
        {"type": "structure_placed", "name": "Scout Camp"},
        {"type": "card_picked", "name": "Precise Strikes", "god": "Odin", "kind": "rune"},
        {"type": "battle_result", "round": 1, "won": True},
        {"type": "structure_upgraded", "name": "Scout Camp", "level": 2},
        {"type": "battle_result", "round": 2, "won": False},
        {"type": "run_end", "elo": 310},
    ]
    log = tmp_path / "events.log"
    log.write_text("noise line\n" + "\n".join(json.dumps(e) for e in events) + "\n")
    tailer = LogTailer(log, JsonLinesParser(), rec.handle)
    for line in log.read_text().splitlines():
        tailer.feed(line)
    [run] = db.load_runs()
    assert (run.clan, run.wins, run.losses, run.elo_after) == ("Raven", 1, 1, 310)
    assert run.battles[0].structures[0].level == 1 and run.battles[1].structures[0].level == 2
    assert run.battles[0].cards[0].god == "Odin"


def test_api(tmp_path):
    db = Database(tmp_path / "t.db")
    client = TestClient(create_app(db))
    assert "Battlegrounds Tracker" in client.get("/").text
    body = make_run("Stag", [True, False], ["Banner Hall"]).to_dict()
    assert client.post("/api/runs", json=body).status_code == 201
    rep = client.get("/api/report", params={"clan": "Stag"}).json()
    assert rep["summary"]["runs"] == 1 and rep["filters"]["clans"] == ["Stag"]
    assert client.post("/api/runs", json={"battles": []}).status_code == 422


def test_northgard_log_parser(tmp_path):
    from pathlib import Path
    from nbg_tracker.capture.nbg_log import NorthgardLogParser

    db = Database(tmp_path / "t.db")
    rec = Recorder(db)
    tailer = LogTailer("unused", NorthgardLogParser(), rec.handle)
    for line in (Path(__file__).parent / "fixtures" / "player_log_two_runs.log").read_text().splitlines():
        tailer.feed(line)
    runs = db.load_runs()
    assert [(r.wins, r.losses, r.mode, r.elo_before, r.elo_after) for r in runs] == [
        (2, 6, "ranked", 101, 87),
        (10, 4, "ranked", 87, 115),
    ]
    assert runs[1].cleared and not runs[0].cleared
    assert len(runs[1].battles) == 18
    assert [b.round for b in runs[1].battles if b.kind == "pve"] == [1, 5, 9, 13]


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def test_describe_asset_paths():
    from nbg_tracker.gamedata import describe
    d = "Assets/Core/Data/"
    assert describe(d + "Items/ItemData/HealersHut.asset").name == "Healers Hut"
    assert describe(d + "Items/ItemData/ClanStructures/StagStructures/BannerHallStag.asset").name == "Banner Hall"
    assert describe(d + "Items/ItemData/Environment/Mountain.asset").kind == "other"
    assert describe(d + "Players/Clans/Clans/WolfClan.asset").name == "Wolf"
    card = describe(d + "Cards/Patrons/Odin/ExtraCards/Tier1/Odin_101.asset")
    assert (card.kind, card.name, card.god, card.tier) == ("card", "Odin 101", "Odin", "Tier1")
    assert describe(d + "Cards/Patrons/Thor/AffinityBlessings/Thor02.asset").name == "Thor favor 2"


def test_board_from_player_data():
    from nbg_tracker.capture.savefile import board_events
    from nbg_tracker.gamedata import GameData

    data = json.loads((FIXTURES / "player_data_stag_run.json").read_text())
    info, board = board_events(data, GameData())
    assert info == {"type": "run_info", "clan": "Stag"}
    structures = {s["name"]: s["level"] for s in board["structures"]}
    assert structures["Healers Hut"] == 3 and structures["Banner Hall"] == 1
    assert "Mountain" not in structures
    kinds = {c["name"]: c["kind"] for c in board["cards"]}
    assert kinds["Loki 102"] == "rune" and kinds["Baldr favor 1"] == "favor" and kinds["Baldr 103"] == "card"
    assert board_events({"CurrentRun": None}, GameData()) == []


def test_log_and_board_make_a_full_run(tmp_path):
    from nbg_tracker.capture.nbg_log import NorthgardLogParser
    from nbg_tracker.capture.savefile import Enricher
    from nbg_tracker.gamedata import GameData

    db = Database(tmp_path / "t.db")
    rec = Recorder(db)
    emit = Enricher(GameData(), rec.handle)
    parser = NorthgardLogParser()
    player_data = (FIXTURES / "player_data_stag_run.json").read_text()
    lines = ["Starting season: Spring", "Starting season: Spring", player_data,
             "PlayerUnit: True OpponentUnit: False", player_data,
             "PlayerUnit: True OpponentUnit: False",
             '{"PlayerName": "me", "CurrentRun": null, "_currentEra": {"RankedElo": 120}}']
    for line in lines:
        for e in parser.parse(line):
            emit(e)
    [run] = db.load_runs()
    assert run.clan == "Stag" and (run.wins, run.losses) == (1, 0) and run.elo_after == 120
    assert "Healers Hut" in {s.name for s in run.battles[1].structures}


def test_read_catalog_if_available():
    import os
    import pytest
    from nbg_tracker.gamedata import describe, read_catalog

    path = os.environ.get("NBG_CATALOG")
    if not path:
        pytest.skip("set NBG_CATALOG to the game's catalog.bin to run this")
    m = read_catalog(path)
    assert describe(m["e692742736efc0843acb2dd2084eaccf"]).name == "Stag"
