"""The TopDeck event store: what a saved event gives back, and what a damaged file does."""

import json
import logging
from datetime import date
from pathlib import Path

import pytest

from commander.topdeckstore import FOLDER, STORE_VERSION, DeckEntry, TournamentRecord, TournamentStore
from commander.tournaments import DecklistCard, Tournament

SINCE = date(2026, 10, 1)
UNTIL = date(2026, 10, 31)


def record(tid: str, *decks: DeckEntry, held_on: date = date(2026, 10, 5)) -> TournamentRecord:
    return TournamentRecord(Tournament(tid, held_on, 64, 8), decks)


def deck(commander: str, standing: int, *cards: tuple[str, int]) -> DeckEntry:
    return DeckEntry(commander, standing, tuple(DecklistCard(name, count) for name, count in cards))


def only_file(root: Path) -> Path:
    [path] = (root / FOLDER / "events").glob("*.json")
    return path


def write_event(root: Path, tid: str, decks: list[object], names: list[str]) -> None:
    document = {
        "version": STORE_VERSION,
        "tid": tid,
        "date": "2026-10-05",
        "size": 64,
        "topCut": 8,
        "names": names,
        "decks": decks,
    }
    folder = root / FOLDER / "events"
    folder.mkdir(parents=True)
    (folder / f"{tid}.json").write_text(json.dumps(document), encoding="utf-8")


def test_a_saved_event_comes_back_with_the_same_decks_and_cards(tmp_path: Path) -> None:
    saved = record(
        "e1",
        deck("Ellivere", 1, ("Sol Ring", 1), ("Forest", 12)),
        deck("Kinnan", 2, ("Sol Ring", 1)),
        deck("Ellivere", 3),
    )
    store = TournamentStore(tmp_path)
    store.save(saved)
    [loaded] = store.load_window(SINCE, UNTIL)
    assert loaded == saved
    standings = [(d.commander, d.standing) for d in loaded.decks]
    assert standings == [("Ellivere", 1), ("Kinnan", 2), ("Ellivere", 3)]
    assert loaded.decks[0].cards == (DecklistCard("Sol Ring", 1), DecklistCard("Forest", 12))
    assert loaded.decks[2].cards == ()
    assert [e.cards for e in loaded.entries("ellivere")] == [loaded.decks[0].cards, ()]


def test_the_cards_of_a_loaded_deck_are_the_same_tuple_each_time_they_are_read(tmp_path: Path) -> None:
    store = TournamentStore(tmp_path)
    store.save(record("e1", deck("Ellivere", 1, ("Sol Ring", 1))))
    [loaded] = store.load_window(SINCE, UNTIL)
    first = loaded.decks[0].cards
    assert loaded.decks[0].cards is first
    assert hash(loaded.decks[0]) == hash(deck("Ellivere", 1, ("Sol Ring", 1)))


def test_an_event_file_that_changed_is_read_again(tmp_path: Path) -> None:
    store = TournamentStore(tmp_path)
    store.save(record("e1", deck("Ellivere", 1, ("Sol Ring", 1))))
    [before] = store.load_window(SINCE, UNTIL)
    assert before.decks[0].cards == (DecklistCard("Sol Ring", 1),)
    store.save(record("e1", deck("Ellivere", 1, ("Arcane Signet", 1)), deck("Kinnan", 2)))
    [after] = store.load_window(SINCE, UNTIL)
    assert [(d.commander, d.cards) for d in after.decks] == [
        ("Ellivere", (DecklistCard("Arcane Signet", 1),)),
        ("Kinnan", ()),
    ]


@pytest.mark.parametrize(
    "run",
    [
        pytest.param([0, 1, 5], id="odd length"),
        pytest.param([2, 1], id="index past the names"),
        pytest.param([-1, 1], id="negative index"),
        pytest.param([0, "1"], id="count that is text"),
        pytest.param([0.0, 1], id="index that is a float"),
        pytest.param([0, None], id="count that is null"),
    ],
)
def test_an_event_with_a_misshapen_deck_is_skipped_and_logged_when_it_is_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, run: list[object]
) -> None:
    write_event(tmp_path, "bad", [{"c": "Ellivere", "s": 1, "k": run}], ["Sol Ring", "Forest"])
    with caplog.at_level(logging.WARNING):
        assert TournamentStore(tmp_path).load_window(SINCE, UNTIL) == []
    assert "ignoring unreadable cached TopDeck event bad.json" in caplog.text


def test_a_deck_that_is_not_a_list_of_cards_is_skipped(tmp_path: Path) -> None:
    write_event(tmp_path, "bad", [{"c": "Ellivere", "s": 1, "k": "nonsense"}], ["Sol Ring"])
    assert TournamentStore(tmp_path).load_window(SINCE, UNTIL) == []


def test_a_good_event_beside_a_bad_one_still_loads(tmp_path: Path) -> None:
    store = TournamentStore(tmp_path)
    store.save(record("good", deck("Ellivere", 1, ("Sol Ring", 1))))
    only_file(tmp_path).with_name("bad.json").write_text("{not json", encoding="utf-8")
    [loaded] = store.load_window(SINCE, UNTIL)
    assert loaded.tournament.tid == "good"
