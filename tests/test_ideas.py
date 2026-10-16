"""Compiling Considered from the meta and recent Archidekt decks (ADR 0014)."""

from pathlib import Path

import pytest

from commander import ideas as ideas_module
from commander.board import Actor, BoardService, Origin, Status, read_board
from commander.carddb import CardDb
from commander.ideas import Idea, compile_ideas, ideas_summary, staple_ideas
from commander.meta import Basis, CardShare, Staples
from commander.models import Deck
from commander.store import StoredDeck
from test_meta import NOW, SINCE, db, deck, entry, meta_data, peer

__all__ = ["db", "deck"]  # fixtures shared with test_meta


def share(name: str, lists: int, of_lists: int, peers: int, of_peers: int) -> CardShare:
    return CardShare(
        name, lists, lists / of_lists if of_lists else 0.0, peers, peers / of_peers, spice=None, flagged=False
    )


def test_staples_become_meta_ideas_else_archidekt_deck_ideas_most_played_first(deck: Deck) -> None:
    rows = (
        share("Mind Stone", 4, 5, 1, 4),
        share("Lotus Petal", 1, 5, 3, 4),
        share("Jeska's Will", 5, 5, 4, 4),
        # Too few lists and too few decks: no idea.
        share("Fury", 1, 5, 1, 4),
    )
    found = staple_ideas(deck, Staples(rows, 5, Basis.TOP_CUT, 4), SINCE)
    assert [(i.name, i.origin) for i in found] == [
        ("Jeska's Will", Origin.META),
        ("Mind Stone", Origin.META),
        ("Lotus Petal", Origin.DECKS),
    ]
    assert found[2].reason == (
        "in 1 of 5 top-cut Magda lists on EDHTop16 since 2026-08-16;"
        " in 3 of 4 most-viewed recent Archidekt decks"
    )


def test_below_bracket_5_only_archidekt_decks_speak(deck: Deck) -> None:
    found = staple_ideas(deck, Staples((share("Lotus Petal", 0, 0, 3, 4),), 0, Basis.ALL, 4), SINCE)
    assert found == [Idea("Lotus Petal", Origin.DECKS, "in 3 of 4 most-viewed recent Archidekt decks", 0.75)]


def test_each_source_keeps_its_top_ideas_with_a_target_and_settles(
    deck: Deck, db: CardDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ideas_module, "MAX_PER_SOURCE", 1)
    stored = StoredDeck(tmp_path, deck)
    lists = [entry(i, 40, 8, [("Mind Stone", 0), ("Jeska's Will", 0)]) for i in range(1, 6)]
    # Mana Crypt is the most played, but banned: the board refuses it, so it takes no place.
    peers = [
        peer(1, ["Mana Crypt", "Arcane Signet"]),
        peer(2, ["Mana Crypt", "Arcane Signet"]),
        peer(3, ["Mana Crypt"]),
    ]
    data = meta_data(lists, peers)

    first = compile_ideas(stored, data, db, now=lambda: NOW)
    [meta] = first[Origin.META]
    assert (meta.source, meta.origin, meta.status) == (Actor.CMDR, Origin.META, Status.OPEN)
    assert [c.name for c in first[Origin.DECKS]] == ["Arcane Signet"]
    assert ideas_summary(first) == "+1 from meta, +1 from archidekt decks"

    # The ideas on the board fill each source's place: nothing new on the next sync.
    assert ideas_summary(compile_ideas(stored, data, db, now=lambda: NOW)) == "no new ideas"

    # Turned down, an idea frees its place for the next one.
    BoardService(tmp_path, deck, db, now=lambda: NOW).decide(meta.name, Status.REJECTED, Actor.YOU)
    [following] = compile_ideas(stored, data, db, now=lambda: NOW)[Origin.META]
    assert following.name != meta.name
    turned_down = read_board(tmp_path).find(meta.name)
    assert turned_down is not None
    assert turned_down.status is Status.REJECTED
