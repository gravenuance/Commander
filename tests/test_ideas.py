"""Compiling Considering from the meta and recent Archidekt decks (ADR 0014)."""

from dataclasses import replace
from pathlib import Path

import pytest

from commander import ideas as ideas_module
from commander.board import Actor, BoardService, Origin, Status, read_board
from commander.carddb import CardDb
from commander.ideas import Idea, compile_ideas, ideas_summary, staple_ideas
from commander.meta import Basis, CardShare, Staples
from commander.metadata import PeerList
from commander.models import Deck
from commander.store import StoredDeck
from test_meta import NOW, SINCE, db, deck, entry, meta_data, peer

__all__ = ["db", "deck"]  # fixtures shared with test_meta


def share(name: str, lists: int, of_lists: int, peers: int, of_peers: int) -> CardShare:
    return CardShare(
        name, lists, lists / of_lists if of_lists else 0.0, peers, peers / of_peers, spice=None, flagged=False
    )


def test_staples_become_ideas_of_the_source_playing_them_more_most_played_first(deck: Deck) -> None:
    rows = (
        share("Mind Stone", 4, 5, 1, 4),
        share("Lotus Petal", 1, 5, 3, 4),
        share("Jeska's Will", 5, 5, 4, 4),
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


def test_each_source_keeps_its_top_ideas_and_settles(
    deck: Deck, db: CardDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ideas_module, "MAX_PER_SOURCE", 1)
    stored = StoredDeck(tmp_path, deck)
    lists = [
        entry(i, 40, 8, [("Mind Stone", 0), ("Jeska's Will", 0), *([("Arcane Signet", 0)] if i <= 4 else [])])
        for i in range(1, 11)
    ]
    # Mana Crypt is as played as any, but banned: the board refuses it, so it takes no place.
    peers = [peer(i, ["Mana Crypt", "Arcane Signet", "Mind Stone", "Jeska's Will"]) for i in range(1, 11)]
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


def test_an_idea_with_no_deck_card_to_replace_still_goes_on_the_board_and_takes_a_place(
    deck: Deck, db: CardDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ancient Tomb is ramp, and the deck's only ramp is its commander, who is never offered as a cut.
    peers = [peer(i, ["Ancient Tomb", "Arcane Signet"]) for i in range(1, 4)]
    stored = StoredDeck(tmp_path, deck)
    added = compile_ideas(stored, meta_data([], peers), db, now=lambda: NOW)
    assert sorted(c.name for c in added[Origin.DECKS]) == ["Ancient Tomb", "Arcane Signet"]
    tomb = read_board(tmp_path).find("Ancient Tomb")
    assert tomb is not None
    assert (tomb.status, tomb.origin, tomb.replace) == (Status.OPEN, Origin.DECKS, ())

    # Both are in 3 of 3 decks, so the one place goes to the first by name: the idea with no target.
    monkeypatch.setattr(ideas_module, "MAX_PER_SOURCE", 1)
    other = tmp_path / "limited"
    other.mkdir()
    [only] = compile_ideas(StoredDeck(other, deck), meta_data([], peers), db, now=lambda: NOW)[Origin.DECKS]
    assert only.name == "Ancient Tomb"


def test_below_bracket_5_a_turned_down_idea_comes_back_when_recent_decks_take_to_it(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    stored = StoredDeck(tmp_path, deck)
    service = BoardService(tmp_path, deck, db, now=lambda: NOW)

    [signet] = compile_ideas(stored, meta_data([], decks_playing(7, 10)), db, now=lambda: NOW)[Origin.DECKS]
    # Turned down at 7 of 10 decks, so it only returns at 90%.
    service.decide(signet.id, Status.REJECTED, Actor.YOU, share=0.7)
    assert compile_ideas(stored, meta_data([], decks_playing(8, 10)), db, now=lambda: NOW)[Origin.DECKS] == []
    [back] = compile_ideas(stored, meta_data([], decks_playing(10, 10)), db, now=lambda: NOW)[Origin.DECKS]
    assert (back.name, back.status) == ("Arcane Signet", Status.OPEN)
    assert back.reason.startswith("back: in 100% of the lists read, up from 70% when turned down")


def test_a_turned_down_idea_with_no_share_noted_gets_the_decks_as_its_baseline(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    stored = StoredDeck(tmp_path, deck)
    peers = [peer(i, ["Arcane Signet"]) for i in range(1, 4)]
    [signet] = compile_ideas(stored, meta_data([], peers), db, now=lambda: NOW)[Origin.DECKS]
    BoardService(tmp_path, deck, db, now=lambda: NOW).decide(signet.id, Status.REJECTED, Actor.YOU)
    assert compile_ideas(stored, meta_data([], peers), db, now=lambda: NOW)[Origin.DECKS] == []
    turned_down = read_board(tmp_path).find("Arcane Signet")
    assert turned_down is not None
    assert turned_down.rejected_share == pytest.approx(1.0)


def compiled_names(folder: Path, deck: Deck, db: CardDb, profile: str) -> dict[Origin, list[str]]:
    """What one compile adds with this profile.toml, from ten lists playing Mind Stone and Jeska's Will (four
    also Arcane Signet) and ten Archidekt decks playing all three, so Arcane Signet is the decks' idea."""
    folder.mkdir()
    (folder / "profile.toml").write_text(profile, encoding="utf-8")
    lists = [
        entry(i, 40, 8, [("Mind Stone", 0), ("Jeska's Will", 0), *([("Arcane Signet", 0)] if i <= 4 else [])])
        for i in range(1, 11)
    ]
    peers = [peer(i, ["Arcane Signet", "Mind Stone", "Jeska's Will"]) for i in range(1, 11)]
    added = compile_ideas(StoredDeck(folder, deck), meta_data(lists, peers), db, now=lambda: NOW)
    return {origin: sorted(card.name for card in cards) for origin, cards in added.items()}


def test_cards_the_profile_excludes_by_name_or_tag_are_no_ideas(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    by_name = compiled_names(
        tmp_path / "name", deck, db, 'version = 1\n[exclude]\ncards = ["Jeska\'s Will"]\n'
    )
    assert by_name == {Origin.META: ["Mind Stone"], Origin.DECKS: ["Arcane Signet"], Origin.ADJACENT: []}
    by_tag = compiled_names(tmp_path / "tag", deck, db, 'version = 1\n[exclude]\ntags = ["ramp"]\n')
    assert by_tag == {Origin.META: [], Origin.DECKS: [], Origin.ADJACENT: []}


def test_ideas_over_the_profiles_ceiling_or_price_cap_are_left_out(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    # Jeska's Will is a game changer, which lifts a bracket 2 deck to 3.
    capped = compiled_names(
        tmp_path / "ceiling", replace(deck, bracket=2), db, "version = 1\nbracket_ceiling = 2\n"
    )
    # Below bracket 5 the deck's budget (ADR 0025) also leaves out Mind Stone, which has no price.
    assert capped == {Origin.META: [], Origin.DECKS: ["Arcane Signet"], Origin.ADJACENT: []}
    # Jeska's Will costs EUR 33.80 and Mind Stone has no price; Arcane Signet costs EUR 0.30.
    cheap = compiled_names(tmp_path / "price", deck, db, "version = 1\nmax_card_eur = 1\n")
    assert cheap == {Origin.META: [], Origin.DECKS: ["Arcane Signet"], Origin.ADJACENT: []}


ALIKE = (
    "version = 1\n[ideas]\n"
    'commanders = ["The Gitrog Monster", ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]]\n'
)


def test_the_adjacent_commanders_pooled_decks_give_ideas_named_after_them(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    (tmp_path / "profile.toml").write_text(ALIKE, encoding="utf-8")
    # Arcane Signet is in half the pool, the least a staple needs; Lightning Bolt in a quarter.
    pool = [
        peer(1, ["Arcane Signet"]),
        peer(2, ["Arcane Signet", "Lightning Bolt"]),
        peer(3, []),
        peer(4, []),
    ]
    added = compile_ideas(StoredDeck(tmp_path, deck), meta_data([], []), db, now=lambda: NOW, adjacent=pool)
    [signet] = added[Origin.ADJACENT]
    assert (signet.name, signet.origin) == ("Arcane Signet", Origin.ADJACENT)
    assert signet.reason == "in 2 of 4 recent Gitrog and Tevesh + Kodama decks"
    assert ideas_summary(added) == "+1 from adjacent decks"


def test_a_turbo_deck_gets_no_off_plan_adjacent_ideas(deck: Deck, db: CardDb, tmp_path: Path) -> None:
    (tmp_path / "profile.toml").write_text(ALIKE, encoding="utf-8")
    # Both in every pooled deck; Lightning Bolt is removal, off a Turbo deck's plan.
    pool = [peer(i, ["Arcane Signet", "Lightning Bolt"]) for i in range(1, 5)]
    turbo = replace(deck, tags=("Turbo",))
    added = compile_ideas(StoredDeck(tmp_path, turbo), meta_data([], []), db, now=lambda: NOW, adjacent=pool)
    assert [card.name for card in added[Origin.ADJACENT]] == ["Arcane Signet"]


def test_a_pool_under_three_decks_or_a_profile_naming_none_gives_no_adjacent_ideas(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    small = tmp_path / "small"
    small.mkdir()
    (small / "profile.toml").write_text(ALIKE, encoding="utf-8")
    pool = [peer(1, ["Arcane Signet"]), peer(2, ["Arcane Signet"])]
    added = compile_ideas(StoredDeck(small, deck), meta_data([], []), db, now=lambda: NOW, adjacent=pool)
    assert added[Origin.ADJACENT] == []
    unnamed = tmp_path / "unnamed"
    unnamed.mkdir()
    pool.append(peer(3, ["Arcane Signet"]))
    added = compile_ideas(StoredDeck(unnamed, deck), meta_data([], []), db, now=lambda: NOW, adjacent=pool)
    assert added[Origin.ADJACENT] == []


def decks_playing(playing: int, total: int) -> list[PeerList]:
    return [peer(i, ["Arcane Signet"] if i <= playing else ["Unknown Card"]) for i in range(1, total + 1)]


def test_a_card_is_an_idea_when_the_wilson_lower_bound_of_its_share_reaches_32_percent(
    deck: Deck, db: CardDb, tmp_path: Path
) -> None:
    # Ten equal decks: 7 playing it have a lower bound of 0.397, 6 of 0.313.
    for playing, expected in ((7, ["Arcane Signet"]), (6, [])):
        folder = tmp_path / f"playing-{playing}"
        folder.mkdir()
        added = compile_ideas(
            StoredDeck(folder, deck), meta_data([], decks_playing(playing, 10)), db, now=lambda: NOW
        )
        assert [c.name for c in added[Origin.DECKS]] == expected
