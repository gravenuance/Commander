"""The cards a deck's plan stands on, each with why."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from commander.archidekt import PeerDeck
from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.keycards import KeyCard, key_cards
from commander.metadata import PeerList
from commander.models import Deck, DeckCard, plan_category
from commander.spellbook import ComboDetail, ComboPiece
from conftest import build_card_db, filler_card

COMMANDER = "Filler Card 100"
HULK = "Costly Beast"
PITCH = "Pitch Spell"
EXTRA_CARDS = [filler_card(20_001, HULK, 7.0), filler_card(20_002, PITCH, 5.0)]
# Made up for these tests: the pitch spell is cast for less than its mana value.
PITCH_TAG = json.dumps(
    {
        "object": "tag",
        "id": "cccccccc-0000-4000-8000-000000000101",
        "slug": "alternative-cost",
        "type": "oracle",
        "child_ids": [],
        "taggings": [{"oracle_id": "eeeeeeee-0000-4000-8000-000000020002", "weight": "median"}],
    }
)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    folder = tmp_path_factory.mktemp("keydb")
    with CardDb(build_card_db(folder, [PITCH_TAG], EXTRA_CARDS)) as opened:
        yield opened


def deck_card(name: str, *categories: str) -> DeckCard:
    return DeckCard(name, 1, categories or ("Uncategorized",), True, "sid", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model


def deck(*extra: DeckCard) -> Deck:
    cards = [
        deck_card(COMMANDER, "Commander"),
        *(deck_card(f"Filler Card {i}") for i in range(1, 21)),
        deck_card(HULK, "Tutor"),
        deck_card(PITCH, "Counters"),
        deck_card("Ancient Tomb", "Land"),
        *extra,
    ]
    return Deck(1, "test", "me", 5, "2026-10-16", tuple(cards))


def peer(index: int, planned: tuple[str, ...]) -> PeerList:
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return PeerList(PeerDeck(index, f"peer {index}", "someone", 10, when, 5), (COMMANDER, *planned), planned)


def combo(*names: str) -> ComboDetail:
    pieces = tuple(ComboPiece(name, "Artifact", ("B",)) for name in names)
    return ComboDetail("1-2", pieces, (), ("Infinite mana",), "")


def reasons(found: tuple[KeyCard, ...]) -> dict[str, tuple[str, ...]]:
    return {key.name: key.reasons for key in found}


def test_plan_categories_name_combos_and_wins_in_any_spelling() -> None:
    assert [plan_category(c) for c in ("Combo Pieces", "WinConEnablers", "Win Con", "Finisher")] == [True] * 4
    assert [plan_category(c) for c in ("Combat", "Ramp", "Copy", "Winter")] == [False] * 4


def test_key_cards_come_from_combos_categories_peers_cost_and_the_profile(db: CardDb) -> None:
    tested = deck(deck_card("Filler Card 30", "Combo Pieces"), deck_card("Filler Card 31"))
    peers = [peer(1, ("Filler Card 31",)), peer(2, ("Filler Card 31", "Filler Card 1")), peer(3, ())]
    profile = Profile(keep_cards=("Filler Card 2",))
    found = key_cards(tested, profile, [combo(COMMANDER, "Filler Card 3")], peers, db)
    assert found[0].name == COMMANDER
    assert reasons(found) == {
        COMMANDER: ("your commander", "in a combo with Filler Card 3"),
        "Filler Card 3": (f"in a combo with {COMMANDER}",),
        "Filler Card 30": ("you file it under Combo Pieces",),
        # One recent deck filing Filler Card 1 under a plan is not enough.
        "Filler Card 31": ("2 of 3 recent decks file it under a combo or win category",),
        HULK: ("costs 7, far above the deck's 1.4 average",),
        "Filler Card 2": ("kept by your profile",),
    }


def test_a_card_filed_as_two_entries_gives_each_reason_once(db: CardDb) -> None:
    # Two printings of one card, one of them filed under the plan.
    tested = deck(deck_card("Filler Card 30", "Combo Pieces"), deck_card("Filler Card 30", "Removal"))
    found = key_cards(tested, Profile(), [combo("Filler Card 30", "Filler Card 3")], [], db)
    assert reasons(found)["Filler Card 30"] == (
        "in a combo with Filler Card 3",
        "you file it under Combo Pieces",
    )


def test_pitch_spells_lands_and_ignored_cards_are_not_key(db: CardDb) -> None:
    found = key_cards(deck(), Profile(ignore_keys=(HULK.upper(),)), [], [], db)
    # The pitch spell costs 5 but is cast for less; Ancient Tomb is a land.
    assert [key.name for key in found] == [COMMANDER]
