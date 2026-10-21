"""`cmdr meta` end to end, against the fake Archidekt, which also stands in for TopDeck."""

import copy
import io
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from commander.board import Origin, Status, read_board
from commander.carddb import CARD_DB_FILE
from commander.cli_meta import print_report
from commander.meta import Basis, MetaReport, Sample
from commander.metacombos import AnswerRole, ComboAnswer, MetaCombo
from commander.tournaments import CommanderStats
from conftest import (
    DECK_ID,
    TOPDECK_SERVICE,
    FakeArchidekt,
    Json,
    MemoryKeyring,
    card_entry,
    deck_page,
    load_fixture,
    topdeck_event,
    topdeck_fixture_events,
)
from test_cli_apply import Run, run

FOLDER = "10000001-sample-deck"
MAGDA = "Magda, Brazen Outlaw"
KINNAN = "Kinnan, Bonder Prodigy"
PEER_ID = 777


def magda_events() -> list[Json]:
    """Magda decks in two events of 64: on 1 September five top-cut lists (top 8) and one that missed the
    cut, and on 1 August, before the 60-day window, a winner."""
    return [
        topdeck_event(
            "magda-open",
            "2026-09-01T10:00:00+00:00",
            64,
            8,
            [
                (1, [MAGDA], ["Mind Stone", "Battered Golem", "Jeska's Will"]),
                (2, [MAGDA], ["Mind Stone", "Battered Golem", "Jeska's Will"]),
                (3, [MAGDA], ["Mind Stone", "Battered Golem"]),
                (4, [MAGDA], ["Mind Stone", "Battered Golem"]),
                (5, [MAGDA], ["Mind Stone", "Battered Golem"]),
                (20, [MAGDA], ["Arcane Signet"]),
            ],
        ),
        topdeck_event("magda-old", "2026-08-01T10:00:00+00:00", 64, 8, [(1, [MAGDA], ["Mana Crypt"])]),
    ]


def kinnan_events() -> list[Json]:
    """Kinnan's two winning lists of the shared fake events: 5 September (120 players) and 7 October (80)."""
    return [e for e in topdeck_fixture_events() if e["TID"] in ("test-open-5", "test-open-6")]


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
    archidekt.peer_page = deck_page([peer_row(DECK_ID, "TESTER", 900), peer_row(PEER_ID, "someone", 600)])
    archidekt.topdeck.events = [*magda_events(), *kinnan_events()]
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
    assert lines[:2] == [
        "Magda: Magda, Brazen Outlaw, events since 2026-08-16",
        "  6 entries, 6 with decklists, 5 top cuts; reading 5 top-cut lists",
    ]
    # Six lists are under the ten it takes to judge a card.
    assert lines[2] == "  thin data (few lists): read shares as hints, not evidence"
    assert section(result.out, "Staples you don't play") == [
        "  100% · 0/1  Mind Stone",
        "   40% · 0/1  Jeska's Will",
    ]
    assert section(result.out, "Your own picks") == ["    0% · 1/1  Disruptor Flute"]
    assert section(result.out, "Most-viewed Archidekt decks") == [
        "     600 views  2026-10-01  Peer 777  https://archidekt.com/decks/777"
    ]
    assert section(result.out, "Your key cards") == [
        "  Magda, Brazen Outlaw: your commander",
        "  Battered Golem: you file it under Combo Pieces",
        "  (add one under [keep] in the profile; leave one out under [key_cards] ignore)",
    ]
    assert section(result.out, "Meta answers to your key cards") == ["  none found"]
    assert section(result.out, "Meta combos and your answers") == [
        # Kinnan's two lists weigh 1.55 (45 days old, 120 players) and 2.76 (8 days, 80 players), so the one
        # with the combo is 36% of them, not half; Kinnan is 2 of the 8 decks played, so 36% of 25% is 9%.
        "    9%  Kinnan, Bonder Prodigy + Basalt Monolith: Infinite colorless mana  (Kinnan 36%)",
        "        stax: Disruptor Flute (stops activated abilities)",
    ]
    assert lines[-1] == (
        "Tournament data from TopDeck.gg, https://topdeck.gg; also Archidekt (archidekt.com) and "
        "Commander Spellbook (commanderspellbook.com); none of them endorses this tool"
    )


def test_a_combo_held_by_more_of_the_recent_lists_is_marked_rising(home: Path, fake: FakeArchidekt) -> None:
    # Three lists from 10 days ago hold the combo, none of the three from 40 days ago do.
    recent = [
        topdeck_event(
            f"recent-{n}",
            "2026-10-05T10:00:00+00:00",
            64,
            8,
            [(1, [KINNAN], ["Sol Ring", "Basalt Monolith"])],
        )
        for n in (1, 2, 3)
    ]
    earlier = [
        topdeck_event(f"earlier-{n}", "2026-09-05T10:00:00+00:00", 64, 8, [(1, [KINNAN], ["Sol Ring"])])
        for n in (1, 2, 3)
    ]
    fake.topdeck.events = [*magda_events(), *recent, *earlier]
    result = meta(home, fake, "magda")
    assert result.code == 0, result.err
    # The recent lists weigh twice the earlier ones, so the combo is 2 of 3 parts of Kinnan's lists.
    [line, _] = section(result.out, "Meta combos and your answers")
    assert line.endswith("(Kinnan 67%)  rising")
    assert "rising" not in section(result.out, "Meta combos and your answers")[1]


def test_queries_follow_the_config_and_leave_out_the_users_own_deck(home: Path, fake: FakeArchidekt) -> None:
    config = home / "appdata" / "Commander" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version = 1\n[meta]\nmin_event_size = 100\nmeta_commanders = 1\n", encoding="utf-8")
    result = meta(home, fake, "magda")
    assert result.code == 0, result.err
    # TopDeck is asked for events of 16 players or more whatever the config says; the config's 100 is
    # applied to what was stored, so Magda's two events of 64 do not count and only Kinnan's 120 does.
    assert {r["participantMin"] for r in fake.topdeck.requests} == {16}
    assert "  TopDeck has no lists for this commander" in result.out.splitlines()
    assert [s.split()[0] for s in section(result.out, "Most-played commanders")] == ["1"]
    assert f"/api/decks/{DECK_ID}/" not in fake.requests
    assert f"/api/decks/{PEER_ID}/" in fake.requests


def test_a_card_check_counts_lists_with_and_without_it(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda", "--card", "mind stone")
    assert result.code == 0, result.err
    assert result.out.splitlines()[:7] == [
        "mind stone for Magda (events since 2026-08-16)",
        "  in your deck: no",
        "  tournaments: in 5 of 5 top-cut lists, 5 of 6 lists in all",
        "  top cuts: 5 with it, 0 without; average finish top 3% with it, top 30% without",
        "  Archidekt: in 0 of 1 recent decks",
        "  meta: in 5 of 8 top commanders' lists",
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
    assert [s["source"] for s in report["sources"]] == ["fetched", "fetched", "fetched", "fetched", "fetched"]
    assert data["cards"][0]["inDeck"] is True
    assert data["cards"][0]["readWith"] == 5


def test_json_combos_carry_short_commander_names_and_answer_roles(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "magda", "--json")
    assert result.code == 0, result.err
    [combo] = json.loads(result.out)["reports"][0]["combos"]
    assert combo["pieces"] == ["Kinnan, Bonder Prodigy", "Basalt Monolith"]
    assert combo["results"] == ["Infinite colorless mana"]
    [played] = combo["playedBy"]
    assert (played["commander"], played["short"]) == ("Kinnan, Bonder Prodigy", "Kinnan")
    assert played["share"] == pytest.approx(0.36, abs=0.001)
    assert combo["rising"] is False
    assert combo["answers"] == [
        {"card": "Disruptor Flute", "how": "stops activated abilities", "role": "stax", "catch": ""}
    ]


def test_to_board_suggests_the_missing_staples_once(home: Path, fake: FakeArchidekt) -> None:
    first = meta(home, fake, "magda", "--to-board")
    assert first.code == 0, first.err
    assert "Magda board: +1 from meta\n" in first.err
    board = read_board(home / "decks" / FOLDER)
    card = board.find("Mind Stone")
    assert card is not None
    assert card.origin is Origin.META
    assert card.reason == (
        "in 5 of 6 Magda lists on TopDeck since 2026-08-16; in 0 of 1 most-viewed recent Archidekt decks"
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


def set_the_bracket(home: Path, fake: FakeArchidekt, bracket: int | None) -> None:
    """Change the deck's bracket on Archidekt (None takes it off) and pull it."""
    if bracket is None:
        del fake.decks[DECK_ID]["edhBracket"]
    else:
        fake.decks[DECK_ID]["edhBracket"] = bracket
    fake.decks[DECK_ID]["updatedAt"] = "2026-10-16T10:00:00.000000Z"
    assert sync(home, fake, "--no-ideas").code == 0


def test_every_bracket_4_and_5_deck_side_by_side_includes_an_unset_deck_estimated_4(
    home: Path, fake: FakeArchidekt
) -> None:
    set_the_bracket(home, fake, None)
    result = meta(home, fake)
    # The fake Spellbook estimates Ruthless 4+, so the deck with no bracket is read as bracket 4.
    assert result.code == 0, result.err
    assert result.out.splitlines()[1].split()[0] == "Magda"
    assert len(fake.spellbook.bodies) == 1


def test_a_bracket_4_deck_reads_its_lists_and_recent_decks_but_not_the_most_played_commanders(
    home: Path, fake: FakeArchidekt
) -> None:
    set_the_bracket(home, fake, None)
    result = meta(home, fake, "magda", "--json")
    # Bracket 4 reads no field and no combos, which is no failure: nothing is reported as unavailable.
    assert result.code == 0, result.err
    assert result.err == ""
    [report] = json.loads(result.out)["reports"]
    assert report["field"] == []
    assert report["combos"] == []
    assert [m["name"] for m in report["missing"]] == ["Mind Stone", "Jeska's Will"]
    assert [p["id"] for p in report["peers"]] == [PEER_ID]


def test_an_unset_deck_estimated_below_4_is_left_out_of_the_side_by_side_view(
    home: Path, fake: FakeArchidekt
) -> None:
    set_the_bracket(home, fake, None)
    fake.spellbook.answer["bracketTag"] = "P"
    result = meta(home, fake)
    assert (result.code, result.err) == (2, "cmdr: no bracket 4 or 5 decks; name a deck to check it anyway\n")


def test_an_unset_deck_without_an_estimate_is_named_as_having_no_bracket(
    home: Path, fake: FakeArchidekt
) -> None:
    set_the_bracket(home, fake, None)
    fake.spellbook.status = 403
    result = meta(home, fake, "magda")
    assert "cmdr: Magda has no bracket; the tournament lists in this report are cEDH\n" in result.err


def test_a_turbo_deck_races_the_meta_combos_instead_of_answering_them(
    home: Path, fake: FakeArchidekt
) -> None:
    snapshot = next((home / "decks").glob("10000001-*")) / "deck.json"
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    saved["tags"] = [*saved["tags"], "Turbo"]
    # Without Disruptor Flute, its stax lock, the deck answers none of the meta's combos.
    saved["cards"] = [card for card in saved["cards"] if card["name"] != "Disruptor Flute"]
    snapshot.write_text(json.dumps(saved), encoding="utf-8")
    table = meta(home, fake).out.splitlines()
    assert table[1].split()[-2:] == ["racing", "thin"]
    lines = meta(home, fake, "magda").out.splitlines()
    assert any(
        line.startswith("Staples you don't play (") and line.endswith("; weighed for turbo)")
        for line in lines
    )
    unanswered = [line.strip() for line in lines if line.strip().startswith("no answer in the deck")]
    assert unanswered
    assert set(unanswered) == {"no answer in the deck (turbo: racing, not answering)"}


def test_card_and_to_board_need_one_deck(home: Path, fake: FakeArchidekt) -> None:
    result = meta(home, fake, "--card", "Mind Stone")
    assert (result.code, result.err) == (2, "cmdr: --to-board and --card need one deck\n")


def test_a_second_run_reads_the_cache_and_an_outage_keeps_the_last_answer(
    home: Path, fake: FakeArchidekt
) -> None:
    assert meta(home, fake, "magda").code == 0
    asked = len(fake.topdeck.requests)
    fake.topdeck.status = 403
    cached = meta(home, fake, "magda")
    assert cached.code == 0
    assert len(fake.topdeck.requests) == asked
    # A refresh works the answers out again from the stored events, which need no request.
    refreshed = meta(home, fake, "magda", "--refresh")
    assert refreshed.code == 0
    assert len(fake.topdeck.requests) == asked
    # With the stored events gone, a refresh has to ask, and the outage leaves the last answer standing.
    shutil.rmtree(home / "cache" / "topdeck")
    gone = meta(home, fake, "magda", "--refresh")
    assert gone.code == 0
    assert "TopDeck lists failed" in gone.err
    assert "showing the copy from 2026-10-15 12:00 UTC" in gone.err


def test_an_outage_with_nothing_cached_fails(home: Path, fake: FakeArchidekt) -> None:
    fake.topdeck.status = 500
    result = meta(home, fake, "magda")
    assert result.code == 1
    assert "cmdr: Magda: TopDeck lists unavailable:" in result.err
    assert "  6 entries" not in result.out


def test_without_a_key_the_report_says_why_the_tournament_lists_are_missing(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets.clear()
    result = meta(home, fake, "magda")
    assert result.code == 1
    assert fake.topdeck.requests == []
    assert (
        "cmdr: Magda: TopDeck lists unavailable: no TopDeck key saved; add one in the app's TopDeck panel "
        "or with `cmdr topdeck`\n"
    ) in result.err
    # The Archidekt side of the report still comes out, with the credit.
    assert "Most-viewed Archidekt decks" in result.out
    assert result.out.splitlines()[-1].startswith("Tournament data from TopDeck.gg, https://topdeck.gg")


def test_a_refused_key_is_named_without_being_shown(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets[(TOPDECK_SERVICE, "api-key")] = "a-wrong-key"
    result = meta(home, fake, "magda")
    assert result.code == 1
    assert "TopDeck refused the key (HTTP 401)" in result.err
    assert "a-wrong-key" not in result.out + result.err


def test_json_carries_the_credit(home: Path, fake: FakeArchidekt) -> None:
    credit = json.loads(meta(home, fake, "magda", "--json").out)["credit"]
    assert credit.startswith("Tournament data from TopDeck.gg, https://topdeck.gg")


def sync(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    return run(["sync", "--no-cards", *args, "--workspace", str(home / "decks")], home, fake)


def test_sync_compiles_the_meta_staples_into_considering_once(home: Path, fake: FakeArchidekt) -> None:
    first = sync(home, fake)
    assert first.code == 0, first.err
    assert "Magda ideas: +1 from meta\n" in first.out
    card = read_board(home / "decks" / FOLDER).find("Mind Stone")
    assert card is not None
    assert card.origin is Origin.META
    # In 2 of 5 lists, Jeska's Will is under the Wilson bar that 5 of 5 clears.
    assert read_board(home / "decks" / FOLDER).find("Jeska's Will") is None
    again = sync(home, fake)
    assert again.code == 0, again.err
    assert "ideas:" not in again.out


def test_sync_json_lists_each_decks_new_ideas_by_source(home: Path, fake: FakeArchidekt) -> None:
    result = sync(home, fake, "--json")
    assert result.code == 0, result.err
    ideas = json.loads(result.out)["ideas"]
    assert ideas.keys() == {"Magda"}
    assert ideas["Magda"]["meta"] == ["Mind Stone"]
    assert ideas["Magda"]["archidekt decks"] == []


def test_sync_no_ideas_leaves_the_board_and_the_meta_alone(home: Path, fake: FakeArchidekt) -> None:
    result = sync(home, fake, "--no-ideas")
    assert result.code == 0, result.err
    assert "ideas" not in result.out
    assert fake.topdeck.requests == []
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


def test_sync_below_bracket_5_learns_from_recent_archidekt_decks_alone(
    home: Path, fake: FakeArchidekt
) -> None:
    fake.decks[DECK_ID]["edhBracket"] = 3
    fake.decks[DECK_ID]["updatedAt"] = "2026-10-14T10:00:00.000000Z"
    # Three peers, all of which play Arcane Signet: a lower bound of 0.44.
    peers = (PEER_ID, PEER_ID + 1, PEER_ID + 2)
    for peer_id in peers[1:]:
        fake.put({**fake.decks[PEER_ID], "id": peer_id})
    for peer_id in peers:
        fake.decks[peer_id]["cards"].append(card_entry(9_100, "Arcane Signet", ["Ramp"]))
    fake.peer_page = deck_page([{**peer_row(p, "someone", 600), "edhBracket": 3} for p in peers])
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Magda ideas: +1 from archidekt decks\n" in result.out, result.err
    assert fake.topdeck.requests == []
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert card.origin is Origin.DECKS


def test_sync_with_every_source_down_keeps_the_board_and_succeeds(home: Path, fake: FakeArchidekt) -> None:
    fake.topdeck.status = 403
    fake.peer_page = {"unexpected": True}
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "cmdr: Magda: no meta or Archidekt decks to learn from (" in result.err
    assert "ideas:" not in result.out
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


def test_sync_without_a_topdeck_key_says_so_once_and_asks_nothing(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets.clear()
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert result.err.count("tournament lists skipped: missing or refused TopDeck key") == 1
    assert fake.topdeck.requests == []
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


def test_sync_with_a_refused_topdeck_key_says_so_once_and_asks_once(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    memory_keyring.secrets[(TOPDECK_SERVICE, "api-key")] = "a-wrong-key"
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert result.err.count("tournament lists skipped: missing or refused TopDeck key") == 1
    assert "a-wrong-key" not in result.out + result.err
    assert len(fake.topdeck.requests) == 1
    assert read_board(home / "decks" / FOLDER).find("Mind Stone") is None


YGRA = "Ygra, Eater of All"


ALIKE_PROFILE = 'version = 1\n[ideas]\ncommanders = ["Ygra, Eater of All"]\n'


def put_ygra_decks(fake: FakeArchidekt) -> None:
    """Three recent Ygra decks, the fewest that make staples; two of them play Arcane Signet."""
    ygra = (PEER_ID + 10, PEER_ID + 11, PEER_ID + 12)
    for deck_id in ygra:
        led_by_ygra = copy.deepcopy(fake.decks[DECK_ID])
        led_by_ygra["owner"] = {"id": 5, "username": "someone"}
        for card in led_by_ygra["cards"]:
            if card["card"]["oracleCard"]["name"] == MAGDA:
                card["card"]["oracleCard"]["name"] = YGRA
        fake.put({**led_by_ygra, "id": deck_id})
    for deck_id in ygra[:2]:
        fake.decks[deck_id]["cards"].append(card_entry(9_100, "Arcane Signet", ["Ramp"]))
    fake.peer_pages[YGRA] = deck_page([peer_row(d, "someone", 600) for d in ygra])


TAG_PROFILE = "version = 1\n[ideas]\ntag_search = true\n"


def search_by_tag(home: Path) -> None:
    (home / "decks" / FOLDER / "profile.toml").write_text(TAG_PROFILE, encoding="utf-8")


def put_tag_decks(fake: FakeArchidekt, tag: str, commander: str, *, first_id: int) -> list[int]:
    """Three recent decks of the commander found by the deck tag, two of them playing Arcane Signet."""
    ids = [first_id, first_id + 1, first_id + 2]
    for deck_id in ids:
        led = copy.deepcopy(fake.decks[DECK_ID])
        led["owner"] = {"id": 5, "username": "someone"}
        for card in led["cards"]:
            if card["card"]["oracleCard"]["name"] == MAGDA:
                card["card"]["oracleCard"]["name"] = commander
        fake.put({**led, "id": deck_id})
    for deck_id in ids[:2]:
        fake.decks[deck_id]["cards"].append(card_entry(9_100, "Arcane Signet", ["Ramp"]))
    fake.tag_pages[tag.casefold()] = deck_page([peer_row(d, "someone", 600) for d in ids])
    return ids


def test_sync_learns_from_other_commanders_decks_carrying_the_decks_theme_tag(
    home: Path, fake: FakeArchidekt
) -> None:
    search_by_tag(home)
    put_tag_decks(fake, "Dwarves", YGRA, first_id=PEER_ID + 20)
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Magda ideas: +1 from meta, +1 from tagged decks\n" in result.out
    assert "Dwarves" in fake.tag_searches
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert (card.origin, card.origin_tag) == (Origin.TAGGED, "Dwarves")
    assert card.reason == "in 2 of 3 recent decks tagged Dwarves"


def test_sync_ignores_the_play_style_role_and_card_type_deck_tags(home: Path, fake: FakeArchidekt) -> None:
    fake.decks[DECK_ID]["deckTags"] = [
        {"id": i, "tag": i, "deck": DECK_ID, "name": name, "position": f"M-{i}"}
        for i, name in enumerate(("cEDH", "Combo", "Casual", "Ramp", "Creatures", "Spaghetti"), 1)
    ]
    search_by_tag(home)
    for tag in ("cEDH", "Combo", "Casual", "Ramp", "Creatures", "Spaghetti"):
        put_tag_decks(fake, tag, YGRA, first_id=PEER_ID + 20)
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert fake.tag_searches == []
    assert "tagged decks" not in result.out


def test_sync_makes_no_tag_search_for_a_deck_without_tags_or_with_its_themes_ignored(
    home: Path, fake: FakeArchidekt
) -> None:
    put_tag_decks(fake, "Dwarves", YGRA, first_id=PEER_ID + 20)
    (home / "decks" / FOLDER / "profile.toml").write_text(
        TAG_PROFILE + '[themes]\nignore = ["Dwarves", "Vehicles"]\n', encoding="utf-8"
    )
    assert sync(home, fake).code == 0
    assert fake.tag_searches == []
    fake.decks[DECK_ID]["deckTags"] = []
    search_by_tag(home)
    assert sync(home, fake).code == 0
    assert fake.tag_searches == []


def test_sync_makes_no_tag_search_unless_the_profile_turns_it_on(home: Path, fake: FakeArchidekt) -> None:
    put_tag_decks(fake, "Dwarves", YGRA, first_id=PEER_ID + 20)
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert fake.tag_searches == []
    assert "tagged decks" not in result.out
    assert read_board(home / "decks" / FOLDER).find("Arcane Signet") is None
    (home / "decks" / FOLDER / "profile.toml").write_text(
        "version = 1\n[ideas]\ntag_search = false\n", encoding="utf-8"
    )
    assert sync(home, fake).code == 0
    assert fake.tag_searches == []


def test_to_board_follows_the_tag_search_setting(home: Path, fake: FakeArchidekt) -> None:
    put_tag_decks(fake, "Dwarves", YGRA, first_id=PEER_ID + 20)
    off = meta(home, fake, "magda", "--to-board")
    assert off.code == 0, off.err
    assert fake.tag_searches == []
    search_by_tag(home)
    on = meta(home, fake, "magda", "--to-board")
    assert on.code == 0, on.err
    assert "Dwarves" in fake.tag_searches
    assert "+1 from tagged decks" in on.err


def test_turning_the_tag_search_off_turns_down_tagged_ideas_the_commanders_lists_do_not_support(
    home: Path, fake: FakeArchidekt
) -> None:
    search_by_tag(home)
    put_tag_decks(fake, "Dwarves", YGRA, first_id=PEER_ID + 20)
    assert sync(home, fake).code == 0
    before = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert before is not None
    assert before.origin is Origin.TAGGED
    (home / "decks" / FOLDER / "profile.toml").write_text("version = 1\n", encoding="utf-8")
    fake.tag_searches.clear()
    assert sync(home, fake).code == 0
    assert fake.tag_searches == []
    after = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert after is not None
    assert (after.status, after.note) == (Status.REJECTED, "no longer makes the cut")


def test_sync_leaves_the_decks_own_commanders_decks_out_of_the_tag_search(
    home: Path, fake: FakeArchidekt
) -> None:
    search_by_tag(home)
    put_tag_decks(fake, "Dwarves", MAGDA, first_id=PEER_ID + 20)
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Dwarves" in fake.tag_searches
    assert "tagged decks" not in result.out
    assert read_board(home / "decks" / FOLDER).find("Arcane Signet") is None


def test_a_tag_search_that_fails_is_named_and_the_sync_carries_on(home: Path, fake: FakeArchidekt) -> None:
    search_by_tag(home)
    fake.tag_pages["dwarves"] = {"unexpected": True}
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "cmdr: Magda: no recent Dwarves-tagged decks to learn from (" in result.err
    assert "Magda ideas: +1 from meta\n" in result.out


def test_sync_learns_from_the_commanders_the_profile_names_as_alike(home: Path, fake: FakeArchidekt) -> None:
    (home / "decks" / FOLDER / "profile.toml").write_text(ALIKE_PROFILE, encoding="utf-8")
    put_ygra_decks(fake)
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "Magda ideas: +1 from meta, +1 from adjacent decks\n" in result.out
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert (card.origin, card.reason) == (Origin.ADJACENT, "in 2 of 3 recent Ygra decks")


def test_to_board_learns_from_the_commanders_the_profile_names_as_alike(
    home: Path, fake: FakeArchidekt
) -> None:
    (home / "decks" / FOLDER / "profile.toml").write_text(ALIKE_PROFILE, encoding="utf-8")
    put_ygra_decks(fake)
    result = meta(home, fake, "magda", "--to-board")
    assert result.code == 0, result.err
    assert "Magda board: +1 from meta, +1 from adjacent decks\n" in result.err
    card = read_board(home / "decks" / FOLDER).find("Arcane Signet")
    assert card is not None
    assert (card.origin, card.reason) == (Origin.ADJACENT, "in 2 of 3 recent Ygra decks")


def test_to_board_names_an_alike_commander_it_cannot_search_and_carries_on(
    home: Path, fake: FakeArchidekt
) -> None:
    (home / "decks" / FOLDER / "profile.toml").write_text(ALIKE_PROFILE, encoding="utf-8")
    fake.peer_pages[YGRA] = {"unexpected": True}
    result = meta(home, fake, "magda", "--to-board")
    assert result.code == 0, result.err
    assert "cmdr: Magda: no recent Ygra, Eater of All decks to learn from (" in result.err
    assert "Magda board: +1 from meta\n" in result.err


def test_sync_names_an_alike_commander_it_cannot_search_and_carries_on(
    home: Path, fake: FakeArchidekt
) -> None:
    profile = 'version = 1\n[ideas]\ncommanders = ["Ygra, Eater of All"]\n'
    (home / "decks" / FOLDER / "profile.toml").write_text(profile, encoding="utf-8")
    fake.peer_pages[YGRA] = {"unexpected": True}
    result = sync(home, fake)
    assert result.code == 0, result.err
    assert "cmdr: Magda: no recent Ygra, Eater of All decks to learn from (" in result.err
    assert "Magda ideas: +1 from meta\n" in result.out


def made_up(combo_id: str, exposure: float, *pieces: str, answers: tuple[ComboAnswer, ...] = ()) -> MetaCombo:
    return MetaCombo(
        combo_id, f"https://example.test/{combo_id}", pieces, (), ("Infinite mana",), exposure, (), answers
    )


def report_of(combos: tuple[MetaCombo, ...]) -> MetaReport:
    sample = Sample(known=True, entries=5, with_lists=5, top_cuts=5, performing=5, basis=Basis.TOP_CUT)
    field = (CommanderStats("Kinnan", 10, 5, 0.5, 1.0),)
    return MetaReport(
        deck="Magda",
        commander=MAGDA,
        since=date(2026, 10, 19),
        sample=sample,
        missing=(),
        outliers=(),
        threats=(),
        combos=combos,
        own_combos=(),
        field=field,
        peers=(),
        peers_skipped=0,
        key_cards=(),
        sources=(),
    )


def test_combos_that_share_pieces_print_under_a_shared_line_with_only_their_other_pieces() -> None:
    answer = ComboAnswer("Grafdigger's Cage", "locks the graveyard", AnswerRole.STAX)
    combos = (
        made_up("1-2", 0.3, "Kinnan", "Thassa's Oracle", "Walking Ballista", answers=(answer,)),
        made_up("5-6", 0.25, "Sol Ring", "Mana Vault"),
        made_up("3-4", 0.2, "Kinnan", "Thassa's Oracle", "Basalt Monolith"),
    )
    report = report_of(combos)
    out = io.StringIO()
    print_report(report, out)
    assert section(out.getvalue(), "Meta combos and your answers") == [
        "  Shared: Kinnan, Thassa's Oracle",
        "       30%  Walking Ballista: Infinite mana  ()",
        "            stax: Grafdigger's Cage (locks the graveyard)",
        "       20%  Basalt Monolith: Infinite mana  ()",
        "            no answer in the deck",
        "   25%  Sol Ring + Mana Vault: Infinite mana  ()",
        "        no answer in the deck",
    ]


def test_a_grouped_combo_made_of_the_shared_pieces_alone_says_so() -> None:
    combos = (
        made_up("1-2", 0.3, "Kinnan", "Thassa's Oracle", "Walking Ballista"),
        made_up("3-4", 0.2, "Kinnan", "Thassa's Oracle"),
    )
    out = io.StringIO()
    print_report(report_of(combos), out)
    assert section(out.getvalue(), "Meta combos and your answers")[:4] == [
        "  Shared: Kinnan, Thassa's Oracle",
        "       30%  Walking Ballista: Infinite mana  ()",
        "            no answer in the deck",
        "       20%  only the shared pieces: Infinite mana  ()",
    ]
