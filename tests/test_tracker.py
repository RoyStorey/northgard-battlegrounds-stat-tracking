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


def _board_image(pieces, size=(1920, 1080), offset=(0, 0), seed=0, empty=()):
    """A fake board in the game's style: hex badges (with a number box under each
    structure) on textured ground, and a distinct 'building' above-right of each badge."""
    import cv2
    import numpy as np
    from nbg_tracker.vision import _hexagon
    rng = np.random.default_rng(seed)
    img = (rng.normal(0, 8, (size[1], size[0], 3)) + [70, 130, 110]).clip(0, 255).astype("uint8")
    looks = {"Healers Hut": ((230, 230, 240), "circle"), "Wolf Den": ((40, 60, 120), "rect"),
             "Mine": ((160, 150, 150), "tri"), "Farm": ((40, 200, 230), "rect")}

    def badge(x, y, box):
        for r, colour in ((21.5, (30, 25, 20)), (19.5, (230, 200, 150)), (17.0, (60, 50, 40)), (15.5, (45, 37, 27))):
            cv2.fillPoly(img, [(_hexagon(0, r) + (x, y)).astype(np.int32)], colour, lineType=cv2.LINE_AA)
        if box:
            cv2.rectangle(img, (x + 22, y - 10), (x + 70, y + 10), (0xA1, 0x9B, 0x7E), -1)

    for name, x, y in pieces:
        x, y = int(x + offset[0]), int(y + offset[1])
        badge(x, y, True)
        colour, shape = looks[name]
        cx, cy = x + 33, y - 92
        if shape == "circle":
            cv2.circle(img, (cx, cy), 45, colour, -1)
        elif shape == "rect":
            cv2.rectangle(img, (cx - 50, cy - 30), (cx + 50, cy + 40), colour, -1)
        else:
            cv2.fillPoly(img, [np.array([(cx, cy - 50), (cx + 50, cy + 40), (cx - 50, cy + 40)], np.int32)], colour)
    for x, y in empty:
        badge(int(x + offset[0]), int(y + offset[1]), False)
    return img


GRID = [(600, 400), (820, 400), (1040, 400), (710, 600), (930, 600)]


def test_badges_found_and_classified():
    from nbg_tracker.vision import find_badges
    img = _board_image([("Mine", *GRID[0]), ("Farm", *GRID[1])], empty=[GRID[3]])
    badges = sorted(find_badges(img), key=lambda b: (b.y, b.x))
    assert [(round(b.x), round(b.y), b.structure) for b in badges] == [
        (600, 400, True), (820, 400, True), (710, 600, False)]


def test_recognizer_learns_from_reviewed_boards(tmp_path):
    import cv2
    from nbg_tracker.vision import Recognizer, find_badges

    rec = Recognizer(tmp_path)
    first = _board_image([("Healers Hut", *GRID[0]), ("Wolf Den", *GRID[1]), ("Mine", *GRID[2]), ("Farm", *GRID[3])])
    cv2.imwrite(str(tmp_path / "a.jpg"), first)
    assert {s.name for s in rec.recognize(first)} == {"?"}  # found, but not named yet

    h, w = first.shape[:2]
    names = {GRID[0]: "Healers Hut", GRID[1]: "Wolf Den", GRID[2]: "Mine", GRID[3]: "Farm"}
    marks = []
    for b in find_badges(first):
        cx, cy = b.centre()
        marks.append(("a.jpg", names[(round(b.x), round(b.y))], 1, (cx + 8) / w, (cy - 5) / h))
    rec.train(marks)

    # the camera moved: everything shifted, and the buildings sit in different tiles
    moved = _board_image([("Farm", *GRID[0]), ("Mine", *GRID[1]), ("Healers Hut", *GRID[4])], offset=(-230, 140), seed=5)
    found = sorted((round(s.x * w), s.name) for s in rec.recognize(moved))
    assert [n for _, n in found] == ["Farm", "Mine", "Healers Hut"]


def test_screen_capturer_keeps_best_build_frame(tmp_path):
    import time
    from PIL import Image
    from nbg_tracker.capture.screen import ScreenCapturer
    from nbg_tracker.vision import Recognizer

    full = Image.fromarray(_board_image([("Mine", *GRID[0]), ("Farm", *GRID[1]), ("Wolf Den", *GRID[2])])[:, :, ::-1])
    zoomed = Image.fromarray(_board_image([("Mine", *GRID[0])])[:, :, ::-1])
    frames = iter([full, full] + [zoomed] * 1000)  # the camera zooms in late in the build phase
    out = []
    cap = ScreenCapturer(out.append, tmp_path, Recognizer(tmp_path), grab=lambda: next(frames), interval=0.005)
    cap.start()
    cap({"type": "run_start", "clan": "Unknown"})
    time.sleep(0.3)
    cap({"type": "opponent", "name": "x"})
    cap.stop()
    assert [e["type"] for e in out] == ["run_start", "screenshot", "board", "opponent"]
    assert (tmp_path / out[1]["file"]).is_file()
    assert len(out[2]["structures"]) == 3  # the full view, not the last zoomed frame


def test_review_api_saves_labels(tmp_path):
    called = []
    db = Database(tmp_path / "t.db")
    run = make_run("Wolf", [True, False], [])
    run.battles[0].screenshot = "a.jpg"
    db.save_run(run)
    client = TestClient(create_app(db, shots_dir=tmp_path, structure_names=["Mine", "Wolf Den"],
                                   on_reviewed=lambda: called.append(1)))
    assert "Review" in client.get("/review").text
    full = client.get(f"/api/runs/{run.id}").json()
    bid = full["battles"][0]["id"]
    body = [{"name": "Wolf Den", "level": 2, "x": 0.4, "y": 0.5}, {"name": "  ", "x": 0.1, "y": 0.1}]
    assert client.put(f"/api/battles/{bid}/structures", json=body).json() == {"saved": 1}
    assert called == [1]
    assert db.labelled_structures() == [("a.jpg", "Wolf Den", 2, 0.4, 0.5)]
    assert client.get("/api/pieces").json() == {"structures": ["Mine", "Wolf Den"]}
    assert client.get("/shots/../t.db").status_code == 404
    assert client.put("/api/battles/999/structures", json=[]).status_code == 404


def test_live_run_state(tmp_path):
    db = Database(tmp_path / "t.db")
    db.save_run(make_run("Wolf", [True] * 4, ["Wolf Den"]))
    rec = Recorder(db)
    assert rec.live() == {"in_run": False}
    for e in [{"type": "run_start", "clan": "Wolf", "mode": "ranked"},
              {"type": "battle_result", "round": 1, "won": True, "kind": "pve"},
              {"type": "battle_result", "round": 2, "won": False},
              {"type": "battle_result", "round": 3, "won": True},
              {"type": "battle_result", "round": 4, "won": True},
              {"type": "battle_result", "round": 5, "won": False, "kind": "pve"}]:
        rec.handle(e)
    live = rec.live()
    assert (live["round"], live["season"], live["wins"], live["losses"], live["lives_left"]) == (6, "Summer", 2, 1, 4)

    board = {"file": "live.jpg", "updated": "now", "structures": [
        {"name": "Wolf Den", "level": 1, "x": 0.5, "y": 0.5}, {"name": "?", "level": 1, "x": 0.2, "y": 0.2}]}
    client = TestClient(create_app(db, live=lambda: dict(rec.live(), board=board)))
    got = client.get("/api/live").json()
    assert got["in_run"] and got["wins"] == 2
    den, unknown = got["board"]["structures"]
    assert den["history"]["battles"] == 4 and unknown["history"] is None
