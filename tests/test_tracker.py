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


def mixed_runs(spec, n_runs=12, rounds=10, seed=1):
    """Runs with realistic mixed results: spec maps a structure to its win chance."""
    import random
    rng = random.Random(seed)
    runs = []
    for i in range(n_runs):
        for name, p in spec.items():
            runs.append(make_run("Wolf", [rng.random() < p for _ in range(rounds)], [name]))
    return runs


def test_impact_ranks_winning_piece_first():
    runs = mixed_runs({"Wolf Den": 0.8, "Sheepfold": 0.25})
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
    assert describe(d + "Items/ItemData/ClanStructures/StagStructures/BannerHallStag.asset").name == "Stag Banner Hall"
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
    assert structures["Healers Hut"] == 3 and structures["Stag Banner Hall"] == 1
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
    cap = ScreenCapturer(out.append, tmp_path, Recognizer(tmp_path), grab=lambda: next(frames), interval=0.005,
                         view_check=lambda img: True)
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
    for run in mixed_runs({"Wolf Den": 0.7, "Mine": 0.4}, n_runs=3):
        db.save_run(run)
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
    assert den["history"]["battles"] == 30 and unknown["history"] is None


def test_tray_stops_on_ctrl_c(monkeypatch):
    import sys
    import threading
    import types
    from nbg_tracker import tray

    stopped = threading.Event()

    class FakeIcon:
        def __init__(self, *a, **k):
            pass

        def run(self):
            stopped.wait(5)

        def stop(self):
            stopped.set()

    fake = types.SimpleNamespace(Icon=FakeIcon, Menu=lambda *a: None, MenuItem=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "pystray", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    calls = iter([False, KeyboardInterrupt()])

    class Done(threading.Event):
        def wait(self, timeout=None):
            v = next(calls, True)
            if isinstance(v, BaseException):
                raise v
            return v or super().wait(0)

    monkeypatch.setattr(tray.threading, "Event", Done)
    tray.run_tray("http://x/", types.SimpleNamespace(paused=False), {})
    assert stopped.is_set()


def test_tracker_started_mid_run_catches_up(tmp_path):
    import time
    from nbg_tracker.capture.nbg_log import NorthgardLogParser

    lines = (FIXTURES / "player_log_two_runs.log").read_text().splitlines()
    # cut the log in the middle of the second run: the first run is over, the second is under way
    cut = [i for i, l in enumerate(lines) if l.startswith("Starting season: Summer")][1]
    log_file = tmp_path / "Player.log"
    log_file.write_text("\n".join(lines[:cut]) + "\n")

    db = Database(tmp_path / "t.db")
    rec = Recorder(db)
    tailer = LogTailer(log_file, NorthgardLogParser(), rec.handle, poll=0.01,
                       catch_up=lambda on: setattr(rec, "saving", not on))
    tailer.start()
    for _ in range(200):
        if rec.live().get("in_run") and rec.saving:
            break
        time.sleep(0.01)
    live = rec.live()
    assert live["in_run"] and (live["wins"], live["losses"], live["round"]) == (2, 1, 5)
    assert db.load_runs() == []  # the run that finished before startup is not saved again

    with open(log_file, "a") as f:  # the game carries on
        f.write("\n".join(lines[cut:]) + "\n")
    for _ in range(200):
        if db.load_runs():
            break
        time.sleep(0.01)
    tailer.stop()
    [run] = db.load_runs()
    assert (run.wins, run.losses, run.elo_after) == (10, 4, 115)


def test_battle_view_frames_are_skipped(tmp_path):
    import time
    import cv2
    import numpy as np
    from PIL import Image
    from nbg_tracker.capture.screen import ScreenCapturer
    from nbg_tracker.vision import _SHOP_BUTTONS, Recognizer, is_build_view

    def with_shop(img):
        img = img.copy()
        w = img.shape[1]
        for (r0, top, r1, bottom), ref in _SHOP_BUTTONS:
            patch = cv2.resize(ref.astype("uint8"), (r0 - r1, bottom - top), interpolation=cv2.INTER_NEAREST)
            img[top:bottom, w - r0:w - r1] = patch[:, :, None]
        return img

    mine = [("Mine", *GRID[0])]
    both = mine + [("Farm", *GRID[1]), ("Wolf Den", *GRID[2]), ("Healers Hut", *GRID[3])]
    build = with_shop(_board_image(mine))
    battle = _board_image(both)  # more structures, but no shop: the battle view
    assert is_build_view(build) and not is_build_view(battle)

    frames = iter([Image.fromarray(build[:, :, ::-1])] + [Image.fromarray(battle[:, :, ::-1])] * 1000)
    out = []
    cap = ScreenCapturer(out.append, tmp_path, Recognizer(tmp_path), grab=lambda: next(frames), interval=0.005)
    cap.start()
    cap({"type": "run_start", "clan": "Unknown"})
    time.sleep(0.3)
    cap({"type": "opponent", "name": "x"})
    cap.stop()
    board = next(e for e in out if e["type"] == "board")
    assert len(board["structures"]) == 1  # only your own village


def test_levels_read_from_badges():
    from nbg_tracker.vision import _DIGIT_TEMPLATES, find_badges
    img = _board_image([("Mine", *GRID[0]), ("Farm", *GRID[1]), ("Wolf Den", *GRID[2])], empty=[GRID[3]])
    for (x, y), level in zip(GRID[:3], (1, 2, 3)):
        t = _DIGIT_TEMPLATES[level]
        img[y - t.shape[0] // 2:y - t.shape[0] // 2 + t.shape[0], x + 40:x + 40 + t.shape[1]] = t[:, :, None]
    found = {(round(b.x), round(b.y)): b.level for b in find_badges(img)}
    assert found == {GRID[0]: 1, GRID[1]: 2, GRID[2]: 3, GRID[3]: None}


def test_clan_worked_out_from_clan_structures(tmp_path):
    from nbg_tracker.gamedata import GameData, clan_from_structures
    clans = GameData().structure_clans()
    assert clans["Raven Banner Hall"] == "Raven" and clans["Hall Of Skalds"] == "Stag"
    assert clan_from_structures(["Mine", "Berserker Camp"], clans) == "Wolf"
    assert clan_from_structures(["Mine"], clans) is None

    db = Database(tmp_path / "t.db")
    rec = Recorder(db, structure_clans=clans)
    rec.handle({"type": "run_start", "clan": "Unknown"})
    rec.handle({"type": "board", "structures": [{"name": "Mine"}, {"name": "Raven Banner Hall"}]})
    assert rec.live()["clan"] == "Raven"

    old = make_run("Unknown", [True, False], ["Mine"])
    db.save_run(old)
    client = TestClient(create_app(db, structure_clans=clans))
    bid = client.get(f"/api/runs/{old.id}").json()["battles"][1]["id"]
    client.put(f"/api/battles/{bid}/structures", json=[{"name": "Stag Banner Hall", "x": 0.5, "y": 0.5}])
    assert client.get(f"/api/runs/{old.id}").json()["clan"] == "Stag"


def _run_with(levels_by_round, results, name="Mine"):
    return Run(clan="Wolf", battles=[
        Battle(round=i + 1, won=w, structures=[Structure(name, lv)] + [Structure("Farm")] * extra)
        for i, (w, (lv, extra)) in enumerate(zip(results, levels_by_round))])


def test_upgrade_value_and_tempo():
    # Mine wins at level 3 and loses at level 1; winners also have bigger boards
    runs = [_run_with([(1, 0), (1, 0), (3, 2), (3, 2)], [False, False, True, True]) for _ in range(6)]
    rep = stats.report(runs)
    [up] = [u for u in rep["upgrades"] if u["name"] == "Mine"]
    assert set(up["levels"]) == {1, 3} and up["upgrade_gain"] > 0
    t = {row["round"]: row for row in rep["tempo"]}
    assert t[3]["won"]["structures"] == 3 and t[1]["lost"]["structures"] == 1


def test_takeaways_only_with_evidence():
    few = stats.report([make_run("Wolf", [True, False], ["Mine"])])
    assert all(t["kind"] == "info" for t in few["takeaways"])  # just the "review more" hint
    many = mixed_runs({"Wolf Den": 0.8, "Sheepfold": 0.25})
    texts = " ".join(t["text"] for t in stats.report(many)["takeaways"])
    assert "Wolf Den is your strongest" in texts and "Sheepfold is costing you" in texts


def test_csv_export(tmp_path):
    db = Database(tmp_path / "t.db")
    db.save_run(make_run("Wolf", [True, False], ["Mine", "Farm"]))
    text = TestClient(create_app(db)).get("/api/export.csv").text
    lines = text.strip().splitlines()
    assert lines[0].startswith("run_id,") and len(lines) == 3 and "Mine 1; Farm 1" in lines[1]


def test_naming_once_fills_later_rounds(tmp_path):
    import cv2
    from nbg_tracker.learning import Learner
    from nbg_tracker.vision import Recognizer, find_badges

    shots = tmp_path / "shots"
    shots.mkdir()
    db = Database(tmp_path / "t.db")
    rec = Recognizer(shots)
    run = Run(clan="Bear", battles=[])
    for i in range(3):  # the camera moves between rounds
        img = _board_image([("Mine", *GRID[0]), ("Wolf Den", *GRID[1])], offset=(i * 60, -i * 40), seed=i)
        cv2.imwrite(str(shots / f"r{i}.jpg"), img)
        h, w = img.shape[:2]
        found = rec.recognize(img)
        assert {s.name for s in found} == {"?"}
        run.battles.append(Battle(round=i + 1, won=True, structures=found, screenshot=f"r{i}.jpg"))
    db.save_run(run)
    client = TestClient(create_app(db, learner=Learner(db, rec, shots)))

    # name both structures in round 1 only
    first = client.get(f"/api/runs/{run.id}").json()["battles"][0]
    body = []
    for s in first["structures"]:
        name = "Mine" if s["x"] < 0.4 else "Wolf Den"
        body.append({"name": name, "level": 1, "x": s["x"], "y": s["y"]})
    client.put(f"/api/battles/{first['id']}/structures", json=body)

    later = client.get(f"/api/runs/{run.id}").json()["battles"][1:]
    for b in later:
        assert sorted(s["name"] for s in b["structures"]) == ["Mine", "Wolf Den"]
        assert not b["reviewed"] and all(s["source"] == "auto" for s in b["structures"])


def test_piece_bought_only_when_ahead_gets_no_free_credit():
    """A piece added only once a run is going well should not look like the best piece."""
    import random
    rng = random.Random(3)
    runs = []
    for i in range(60):
        skill = rng.random()  # some runs simply go better than others
        battles, w, l = [], 0, 0
        for rnd in range(1, 13):
            board = [Structure("Mine")]
            if w - l >= 3:
                board.append(Structure("Draugr Tombs"))  # only built when far ahead; does nothing itself
            won = rng.random() < 0.3 + 0.5 * skill
            battles.append(Battle(round=rnd, won=won, structures=board))
            w, l = w + won, l + (not won)
        runs.append(Run(clan="Bear", battles=battles))
    rows = {r["name"]: r for r in stats.report(runs)["structures"]}
    tombs = rows["Draugr Tombs"]
    assert tombs["win_rate"] > tombs["expected_win_rate"] - 0.05  # raw numbers look great...
    assert tombs["win_rate"] > 0.6
    assert abs(tombs["impact"]) < 6  # ...but the model gives it little credit


def test_medals_and_clan_cards():
    from nbg_tracker.models import medal
    assert [medal(w) for w in (4, 5, 7, 8, 9, 10)] == [None, "bronze", "bronze", "silver", "silver", "gold"]
    runs = [make_run("Bear", [True] * 10 + [False] * 2, ["Mine"]), make_run("Bear", [True] * 5 + [False] * 5, ["Mine"]),
            make_run("Raven", [True] * 8 + [False] * 5, ["Mine"])]
    rep = stats.report(runs)
    cards = {c["name"]: c for c in rep["clan_cards"]}
    assert list(cards)[:4] == ["Stag", "Raven", "Wolf", "Bear"]
    assert cards["Bear"]["medals"] == {"gold": 1, "silver": 0, "bronze": 1} and cards["Stag"]["runs"] == 0
    assert rep["summary"]["medals"] == {"gold": 1, "silver": 1, "bronze": 1}


def test_live_panel_editing(tmp_path):
    import cv2
    from nbg_tracker.learning import Learner
    from nbg_tracker.vision import Recognizer, shop_slots

    shots = tmp_path / "shots"
    shots.mkdir()
    db = Database(tmp_path / "t.db")
    rec = Recognizer(shots)
    learner = Learner(db, rec, shots)
    recorder = Recorder(db)
    recorder.handle({"type": "run_start", "clan": "Unknown"})
    img = _board_image([("Mine", *GRID[0]), ("Farm", *GRID[1])])
    cv2.imwrite(str(shots / "live.jpg"), img)
    found = rec.recognize(img)

    class Cap:
        live = {"file": "live.jpg", "updated": "now", "shop": [None] * 4,
                "structures": [{"name": s.name, "level": s.level, "x": s.x, "y": s.y} for s in found]}

    learner.recorder, learner.capturer = recorder, Cap()
    client = TestClient(create_app(db, shots_dir=shots, learner=learner, structure_names=["Mine", "Farm"],
                                   live=lambda: dict(recorder.live(), board=Cap.live)))
    marks = [dict(m, name="Mine" if m["x"] < 0.4 else "Farm") for m in Cap.live["structures"]]
    assert client.put("/api/live/structures", json=marks).json() == {"learned": 2}
    assert sorted(s.name for s in rec.recognize(img)) == ["Farm", "Mine"]  # learned at once
    assert sorted(s.name for s in recorder.structures) == ["Farm", "Mine"]  # next battle uses your names

    assert client.put("/api/live/shop/1", json={"name": "Mine"}).status_code == 200
    assert learner.read_shop(img)[1] == "Mine"
    live = client.get("/api/live").json()
    assert live["board"]["shop"][1]["name"] == "Mine" and live["board"]["shop"][1]["owned"]

    assert client.put("/api/live/clan", json={"clan": "Bear"}).status_code == 200
    recorder.handle({"type": "run_info", "clan": "Raven", "clan_source": "portrait"})
    assert recorder.live()["clan"] == "Bear"  # your pick isn't overwritten


class _FakeCardReader:
    """Stands in for screen reading: frames are dicts saying what's on screen."""

    def read_favor(self, img):
        return {g: tuple(v) for g, v in img["favor"].items()}

    def read_offers(self, img):
        return img["offers"]


def _card_frames(monkeypatch):
    from nbg_tracker.capture import cards
    monkeypatch.setattr(cards.CardReader, "is_card_screen", staticmethod(lambda img: img["screen"] == "cards"))
    monkeypatch.setattr(cards, "_at_1080", lambda img: img)
    monkeypatch.setattr(cards.cv2, "resize", lambda a, *k, **kw: a)
    monkeypatch.setattr(cards.cv2, "cvtColor", lambda a, *k: a)
    return cards


class _Frame(dict):
    def __getitem__(self, k):
        if isinstance(k, tuple):  # the title strip, used to notice rerolls
            return _TitleKey(dict.__getitem__(self, "offers"))
        return dict.__getitem__(self, k)


class _TitleKey:
    def __init__(self, offers):
        self.v = "|".join(o["name"] for o in offers)

    def tobytes(self):
        return self.v.encode()


def test_card_pick_from_favour_change(monkeypatch):
    cards = _card_frames(monkeypatch)
    out = []
    t = cards.CardTracker(_FakeCardReader(), out.append)
    offer1 = [{"name": "Lost Wanderer", "kind": "Gift", "gods": {}}, {"name": "Flurry of Blows", "kind": "Rune", "gods": {"Thor": 1}}]
    offer2 = [{"name": "Call to Peace", "kind": "Blessing", "gods": {"Baldr": 1}},
              {"name": "Beseech Thor", "kind": "Gift", "gods": {"Thor": 2}}, {"name": "Lost Ruins", "kind": "Gift", "gods": {}}]
    fav0 = {"Thor": (0, 3), "Baldr": (1, 3)}
    assert t.frame(_Frame(screen="cards", offers=offer1, favor=fav0))
    assert t.frame(_Frame(screen="cards", offers=offer2, favor=fav0))  # rerolled
    assert not t.frame(_Frame(screen="board", offers=offer2, favor=fav0))  # peeking: nothing changed yet
    assert [e["type"] for e in out] == ["card_offer", "card_offer"]
    t.frame(_Frame(screen="board", offers=offer2, favor={"Thor": (0, 3), "Baldr": (2, 3)}))
    picked = [e for e in out if e["type"] == "card_picked"]
    assert picked == [{"type": "card_picked", "name": "Call to Peace", "god": "Baldr", "kind": "blessing"}]

    out.clear()  # a neutral card: settled when the battle starts
    fav1 = {"Thor": (0, 3), "Baldr": (2, 3)}
    t.frame(_Frame(screen="cards", offers=offer2, favor=fav1))
    t.frame(_Frame(screen="board", offers=offer2, favor=fav1))
    t.battle_starting()
    assert [e["name"] for e in out if e["type"] == "card_picked"] == ["Lost Ruins"]


def test_favour_becomes_cards():
    from nbg_tracker.capture.cards import favor_tier
    assert [favor_tier(v, t) for v, t in ((0, 3), (2, 3), (3, 5), (6, 8), (8, 8))] == [0, 0, 1, 2, 3]
    rec = Recorder(Database(":memory:"))
    rec.handle({"type": "run_start", "clan": "Bear"})
    rec.handle({"type": "card_picked", "name": "Call to Peace", "god": "Baldr", "kind": "blessing"})
    rec.handle({"type": "favor", "values": {"Baldr": [3, 5], "Thor": [1, 3]}})
    rec.handle({"type": "favor", "values": {"Baldr": [5, 8], "Thor": [1, 3]}})
    names = sorted(c.name for c in rec.cards)
    assert names == ["Baldr favor 2", "Call to Peace"]
    rec.handle({"type": "card_offer", "cards": [{"name": "Lost Ruins", "gods": {}}]})
    assert rec.live()["offer"][0]["name"] == "Lost Ruins" and rec.live()["favor"]["Baldr"] == [5, 8]


def test_card_library_builds_itself(tmp_path):
    db = Database(tmp_path / "t.db")
    rec = Recorder(db)
    rec.handle({"type": "run_start", "clan": "Bear"})
    offer = [{"name": "Flurry of Blows", "kind": "Rune", "text": "Target MELEE.", "gods": {"Thor": 1}},
             {"name": "Beseech Thor", "kind": "Gift", "text": "Gain 2 favor with Thor.", "gods": {"Thor": 2}}]
    rec.handle({"type": "card_offer", "cards": offer})
    offer[0]["text"] = "Target MELEE. Gain 10 Crit Chance."  # read more fully the next time
    rec.handle({"type": "card_offer", "cards": offer})
    rec.handle({"type": "card_picked", "name": "Flurry of Blows", "god": "Thor", "kind": "rune"})
    rec.saving = False  # catching up on an old log: don't count twice
    rec.handle({"type": "card_offer", "cards": offer})

    cards = {c["name"]: c for c in TestClient(create_app(db)).get("/api/cards").json()}
    flurry = cards["Flurry of Blows"]
    assert (flurry["offered"], flurry["picked"], flurry["god"], flurry["favor"]) == (2, 1, "Thor", 1)
    assert flurry["text"] == "Target MELEE. Gain 10 Crit Chance." and flurry["pick_rate"] == 0.5
    assert cards["Beseech Thor"]["favor"] == 2 and cards["Beseech Thor"]["picked"] == 0
