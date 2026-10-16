"""`cmdr meta` end to end, against the fake Archidekt, which also stands in for EDHTop16."""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from commander.board import Origin, Section, read_board
from commander.carddb import CARD_DB_FILE
from conftest import DECK_ID, FakeArchidekt, Json, card_entry, deck_page, load_fixture
from test_cli_apply import Run, run

FOLDER = "10004639-honestly-though"
MAGDA = "Magda, Brazen Outlaw"
PEER_ID = 777


def edge(standing: int, cards: list[tuple[str, int]], held_on: str = "2026-09-01T10:00:00.000Z") -> Json:
    return {
        "node": {
            "standing": standing,
            "wins": 4,
            "losses": 1,
            "draws": 0,
            "decklistUrl": f"/tournament/t{standing}/decklist/x",
            "tournament": {
                "TID": f"t{standing}",
                "name": "Test   Open",
                "tournamentDate": held_on,
                "size": 40,
                "topCut": 8,
            },
            "decklistItems": [{"count": 1, "spice": spice, "card": {"name": name}} for name, spice in cards],
        }
    }


def magda_page() -> Json:
    # Five top-cut lists and one that missed the cut; the last is older than the 60-day window.
    edges = [
        edge(1, [("Mind Stone", 0), ("Battered Golem", 1), ("Jeska's Will", 4)]),
        edge(2, [("Mind Stone", 0), ("Battered Golem", 1), ("Jeska's Will", 3)]),
        edge(3, [("Mind Stone", 0), ("Battered Golem", 1)]),
        edge(4, [("Mind Stone", 0), ("Battered Golem", 1)]),
        edge(5, [("Mind Stone", 0), ("Battered Golem", 1)]),
        edge(20, [("Arcane Signet", 0)]),
        edge(1, [("Mana Crypt", 4)], held_on="2026-08-01T10:00:00.000Z"),
    ]
    page: dict[str, Any] = {"pageInfo": {"hasNextPage": False, "endCursor": "7"}, "edges": edges}
    return {"data": {"commander": {"name": MAGDA, "entries": page}}}


def peer_row(deck_id: int, owner: str, views: int) -> Json:
    return {
        "id": deck_id,
        "name": f"Peer {deck_id}",
        "owner": {"id": deck_id, "username": owner},
        "viewCount": views,
        "updatedAt": "2026-10-01T10:00:00.000000Z",
        "edhBracket": 5,
        "deckFormat": 3,
    }


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    archidekt.peer_page = deck_page([peer_row(DECK_ID, "MAKKIS", 900), peer_row(PEER_ID, "someone", 120)])
    archidekt.edhtop16.pages[MAGDA] = [magda_page()]
    archidekt.spellbook.combos = load_fixture("spellbook_meta_kinnan.json")
    return archidekt


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt, raw_deck: Json) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code
        == 0
    )
    # Another player's deck, which the user's deck list leaves out.
    peer = copy.deepcopy(raw_deck)
    peer["id"] = PEER_ID
    peer["owner"] = {"id": 5, "username": "someone"}
    fake.put(peer)
    for name in ("board.json", "activity.jsonl"):
        (tmp_path / "decks" / FOLDER / name).unlink()
    fake.requests.clear()
    return tmp_path


def meta(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    return run(["meta", *args, "--workspace", str(home / "decks")], home, fake)


def section(out: str, title: str) -> list[str]:
    """The lines under a report heading, up to the next blank line."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(title))
    rows: list[str] = []
    for line in lines[start + 1 :]:
        if not line:
            break
        rows.append(line)
    return rows


def test_the_report_compares_the_deck_with_performing_lists(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda")
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[:3] == [
        "Magda: Magda, Brazen Outlaw, events since 2026-08-16",
        "  EDHTop16: https://edhtop16.com/commander/Magda%2C%20Brazen%20Outlaw",
        "  6 entries, 6 with decklists, 5 top cuts; reading 5 top-cut lists",
    ]
    # Six lists are under the ten it takes to judge a card.
    assert lines[3] == "  thin data (few lists): read shares as hints, not evidence"
    assert section(result.out, "Staples you don't play") == [
        "  100% · 0/1  Mind Stone",
        "   40% · 0/1  Jeska's Will",
    ]
    assert section(result.out, "Spicy picks") == [
        "  spice 4  2 list(s)  Jeska's Will  best: 1st of 40, Test Open (2026-09-01)"
    ]
    assert section(result.out, "Your own picks") == ["    0% · 1/1  Disruptor Flute"]
    assert section(result.out, "Most-viewed Archidekt decks") == [
        "     120 views  2026-10-01  Peer 777  https://archidekt.com/decks/777"
    ]
    assert section(result.out, "Your key cards") == [
        "  Magda, Brazen Outlaw: your commander",
        "  Battered Golem: you file it under Combo Pieces",
        "  (add one under [keep] in the profile; leave one out under [key_cards] ignore)",
    ]
    assert section(result.out, "Meta answers to your key cards") == ["  none found"]
    assert section(result.out, "Meta combos and your answers") == [
        "   15%  Kinnan, Bonder Prodigy + Basalt Monolith: Infinite colorless mana  (Kinnan 50%)",
        "        stax: Disruptor Flute (stops activated abilities)",
    ]
    assert lines[-1].startswith(
        "data from EDHTop16 (edhtop16.com), Archidekt (archidekt.com) and Commander Spellbook"
    )


def test_queries_follow_the_config_and_leave_out_the_users_own_deck(home: Path, fake: FakeArchidekt) -> None:
    config = home / "appdata" / "Commander" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version = 1\n[meta]\nmin_event_size = 32\nmeta_commanders = 1\n", encoding="utf-8")
    assert meta(home, fake, "magda").code == 0
    sizes = {r.get("minEventSize", r.get("minSize")) for r in fake.edhtop16.requests}
    assert sizes == {32}
    # One meta commander asked for, with room to re-rank: two.
    assert {"first": 2, "minDate": "2026-08-16", "minSize": 32} in fake.edhtop16.requests
    assert f"/api/decks/{DECK_ID}/" not in fake.requests
    assert f"/api/decks/{PEER_ID}/" in fake.requests


def test_a_card_check_counts_lists_with_and_without_it(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda", "--card", "mind stone")
    assert result.code == 0, result.err
    assert result.out.splitlines()[:8] == [
        "mind stone for Magda (events since 2026-08-16)",
        "  in your deck: no",
        "  EDHTop16: in 5 of 5 top-cut lists, 5 of 6 lists in all",
        "  top cuts: 5 with it, 0 without; average finish top 5% with it, top 48% without",
        "  spice: up to 0 of 4",
        "  Archidekt: in 0 of 1 recent decks",
        "  meta: in 0 of 2 top commanders' lists",
        "  thin data: read these counts as hints, not evidence",
    ]


def test_json_carries_the_report_and_card_checks(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda", "--json", "--card", "Battered Golem")
    assert result.code == 0, result.err
    data = json.loads(result.out)
    [report] = data["reports"]
    assert report["sample"] == {
        "known": True,
        "entries": 6,
        "withLists": 6,
        "topCuts": 5,
        "read": 5,
        "basis": "top_cut",
        "thin": True,
    }
    assert [(m["name"], m["lists"], m["share"]) for m in report["missing"]] == [
        ("Mind Stone", 5, 1.0),
        ("Jeska's Will", 2, 0.4),
    ]
    assert report["spicy"][0]["best"]["decklist"] == "https://edhtop16.com/tournament/t1/decklist/x"
    assert [s["source"] for s in report["sources"]] == ["fetched", "fetched", "fetched", "fetched", "fetched"]
    assert data["cards"][0]["inDeck"] is True
    assert data["cards"][0]["readWith"] == 5


def test_json_combos_carry_short_commander_names_and_answer_roles(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda", "--json")
    assert result.code == 0, result.err
    [combo] = json.loads(result.out)["reports"][0]["combos"]
    assert combo["pieces"] == ["Kinnan, Bonder Prodigy", "Basalt Monolith"]
    assert combo["results"] == ["Infinite colorless mana"]
    assert combo["playedBy"] == [{"commander": "Kinnan, Bonder Prodigy", "short": "Kinnan", "share": 0.5}]
    assert combo["answers"] == [
        {"card": "Disruptor Flute", "how": "stops activated abilities", "role": "stax", "catch": ""}
    ]


def test_to_board_suggests_the_missing_staples_once(home: Path, fake: FakeArchidekt) -> None:
    first = meta(home, fake, "magda", "--to-board")
    assert first.code == 0, first.err
    assert "Magda board: +2 from meta\n" in first.err
    board = read_board(home / "decks" / FOLDER)
    card = board.find("Mind Stone")
    assert card is not None
    assert (card.section, card.origin) == (Section.SUGGESTED, Origin.META)
    assert card.reason == (
        "in 5 of 5 top-cut Magda lists on EDHTop16 since 2026-08-16;"
        " in 0 of 1 most-viewed recent Archidekt decks"
    )
    # Replacement targets come from the card's role in the app, not from one cut list for every idea.
    assert card.replace == ()
    again = meta(home, fake, "magda", "--to-board")
    assert "Magda board: no new ideas\n" in again.err


def test_every_bracket_5_deck_side_by_side(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake)
    assert result.code == 0, result.err
    assert result.out.splitlines()[:2] == [
        "deck        lists  top cuts  read  missing  threats  unanswered combos",
        "Magda           6         5     5        2        0                  0 thin",
    ]


def test_card_and_to_board_need_one_deck(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "--card", "Mind Stone")
    assert (result.code, result.err) == (2, "cmdr: --to-board and --card need one deck\n")


def test_a_second_run_reads_the_cache_and_an_outage_keeps_the_last_answer(
    home: Path, fake: FakeArchidekt
) -> None:
    assert meta(home, fake, "magda").code == 0
    asked = len(fake.edhtop16.requests)
    fake.edhtop16.status = 403
    cached = meta(home, fake, "magda")
    assert cached.code == 0
    assert len(fake.edhtop16.requests) == asked
    refreshed = meta(home, fake, "magda", "--refresh")
    assert refreshed.code == 0
    assert "EDHTop16 lists failed" in refreshed.err
    assert "showing the copy from 2026-10-15 12:00 UTC" in refreshed.err


def test_an_outage_with_nothing_cached_fails(home: Path, fake: FakeArchidekt) -> None:
    fake.edhtop16.status = 403
    result = meta(home, fake, "magda")
    assert result.code == 1
    assert "cmdr: Magda: EDHTop16 lists unavailable:" in result.err
    assert "  6 entries" not in result.out


def sync(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    return run(["sync", "--no-cards", *args, "--workspace", str(home / "decks")], home, fake)


def test_sync_compiles_the_meta_staples_into_considered_once(home: Path, fake: FakeArchidekt) -> None:
    first = sync(home, fake)
    assert first.code == 0, first.err
    assert "Magda ideas: +2 from meta\n" in first.out
    card = read_board(home / "decks" / FOLDER).find("Jeska's Will")
    assert card is not None
    assert (card.section, card.origin) == (Section.SUGGESTED, Origin.META)
    again = sync(home, fake)
    assert again.code == 0, again.err
    assert "ideas:" not in again.out


def test_sync_json_lists_each_decks_new_ideas_by_source(home: Path, fake: FakeArchidekt) -> None:
    result = sync(home, fake, "--json")
    assert result.code == 0, result.err
    ideas = json.loads(result.out)["ideas"]
    assert ideas.keys() == {"Magda"}
    assert sorted(ideas["Magda"]["meta"]) == ["Jeska's Will", "Mind Stone"]
    assert ideas["Magda"]["archidekt decks"] == []


def test_sync_no_ideas_leaves_the_board_and_the_meta_alone(home: Path, fake: FakeArchidekt) -> None:
    result = sync(home, fake, "--no-ideas")
    assert result.code == 0, result.err
    assert "ideas" not in result.out
    assert fake.edhtop16.requests == []
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


def test_sync_below_bracket_5_learns_from_recent_archidekt_decks_alone(
    home: Path, fake: FakeArchidekt
) -> None:
    fake.decks[DECK_ID]["edhBracket"] = 3
    fake.decks[DECK_ID]["updatedAt"] = "2026-10-14T10:00:00.000000Z"
    # Three peers, the fewest that make staples; two of them play Arcane Signet.
    peers = (PEER_ID, PEER_ID + 1, PEER_ID + 2)
    for peer_id in peers[1:]:
        fake.put({**fake.decks[PEER_ID], "id": peer_id})
    for peer_id in peers[:2]:
        fake.decks[peer_id]["cards"].append(card_entry(9_100, "Arcane Signet", ["Ramp"]))
    fake.peer_page = deck_page([{**peer_row(p, "someone", 120), "edhBracket": 3} for p in peers])
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Magda ideas: +1 from archidekt decks\n" in result.out, result.err
    assert fake.edhtop16.requests == []
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert card.origin is Origin.DECKS


def test_sync_with_every_source_down_keeps_the_board_and_succeeds(home: Path, fake: FakeArchidekt) -> None:
    fake.edhtop16.status = 403
    fake.peer_page = {"unexpected": True}
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "cmdr: Magda: no meta or Archidekt decks to learn from (" in result.err
    assert "ideas:" not in result.out
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


YGRA = "Ygra, Eater of All"


def test_sync_learns_from_the_commanders_the_profile_names_as_alike(home: Path, fake: FakeArchidekt) -> None:
    profile = 'version = 1\n[ideas]\ncommanders = ["Ygra, Eater of All"]\n'
    (home / "decks" / FOLDER / "profile.toml").write_text(profile, encoding="utf-8")
    # Three Ygra decks, the fewest that make staples; two of them play Arcane Signet.
    ygra = (PEER_ID + 10, PEER_ID + 11, PEER_ID + 12)
    for deck_id in ygra:
        fake.put({**fake.decks[PEER_ID], "id": deck_id})
    for deck_id in ygra[:2]:
        fake.decks[deck_id]["cards"].append(card_entry(9_100, "Arcane Signet", ["Ramp"]))
    fake.peer_pages[YGRA] = deck_page([peer_row(d, "someone", 50) for d in ygra])
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Magda ideas: +2 from meta, +1 from adjacent decks\n" in result.out
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert (card.origin, card.reason) == (Origin.ADJACENT, "in 2 of 3 recent Ygra decks")


def test_sync_names_an_alike_commander_it_cannot_search_and_carries_on(
    home: Path, fake: FakeArchidekt
) -> None:
    profile = 'version = 1\n[ideas]\ncommanders = ["Ygra, Eater of All"]\n'
    (home / "decks" / FOLDER / "profile.toml").write_text(profile, encoding="utf-8")
    fake.peer_pages[YGRA] = {"unexpected": True}
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "cmdr: Magda: no recent Ygra, Eater of All decks to learn from (" in result.err
    assert "Magda ideas: +2 from meta\n" in result.out
