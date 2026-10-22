from dataclasses import replace

import pytest

from commander.archidekt import parse_deck
from commander.models import Deck, DeckCard, short_commander_name
from conftest import Json


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    return parse_deck(raw_deck)


def named(deck: Deck, *commanders: str) -> Deck:
    template = next(c for c in deck.cards if "Commander" in c.categories)
    cards = tuple(c for c in deck.cards if "Commander" not in c.categories)
    return replace(deck, cards=(*cards, *(replace(template, name=n) for n in commanders)))


@pytest.mark.parametrize(
    ("commanders", "short"),
    [
        (("Magda, Brazen Outlaw",), "Magda"),
        (("Ellivere of the Wild Court",), "Ellivere"),
        (("Edgar Markov",), "Edgar"),
        (("Etali, Primal Conqueror // Etali, Primal Sickness",), "Etali"),
        (("Tymna the Weaver", "Kraum, Ludevic's Opus"), "Tymna & Kraum"),
    ],
)
def test_short_name_is_the_commanders_first_name(deck: Deck, commanders: tuple[str, ...], short: str) -> None:
    assert named(deck, *commanders).short_name == short


def test_a_deck_without_a_commander_goes_by_its_name(deck: Deck) -> None:
    no_commander = replace(deck, cards=tuple(c for c in deck.cards if "Commander" not in c.categories))
    assert no_commander.short_name == deck.name


@pytest.mark.parametrize(
    ("name", "short"),
    [
        ("Magda, Brazen Outlaw", "Magda"),
        ("Ellivere of the Wild Court", "Ellivere"),
        ("Kraum, Ludevic's Opus / Tymna the Weaver", "Kraum/Tymna"),
        ("Esika, God of the Tree // The Prismatic Bridge", "Esika"),
        ("Glarb", "Glarb"),
        ("The Gitrog Monster", "Gitrog"),
        ("Tevesh Szat, Doom of Fools", "Tevesh"),
        ("The Ur-Dragon", "Ur-Dragon"),
    ],
)
def test_commanders_go_by_the_name_players_use(name: str, short: str) -> None:
    assert short_commander_name(name) == short


def test_main_counts_sum_a_card_filed_as_several_entries_and_leave_out_the_cards_outside_the_deck() -> None:
    template = DeckCard("Plains", 1, ("Land",), True, "sid", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — as the model
    cards = (
        replace(template, quantity=10),
        replace(template, quantity=9, entry_id=2),
        replace(template, name="Island", quantity=3),
        replace(template, name="Wastes", quantity=4, in_deck=False),
    )
    deck = Deck(1, "test", "me", 3, "2026-10-16", cards)
    assert deck.main_counts == {"Plains": 19, "Island": 3}


def test_main_cards_and_commanders_are_the_deck_cards_in_order_and_follow_a_replaced_card_list() -> None:
    template = DeckCard("Plains", 1, ("Land",), True, "sid", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — as the model
    leader = replace(template, name="Magda, Brazen Outlaw", categories=("Commander",), entry_id=2)
    out = replace(template, name="Wastes", in_deck=False, entry_id=3)
    benched = replace(template, name="Sol Ring", categories=("Commander",), in_deck=False, entry_id=4)
    deck = Deck(1, "test", "me", 3, "2026-10-16", (template, leader, out, benched))
    assert deck.main_cards == (template, leader)
    assert deck.commanders == ("Magda, Brazen Outlaw",)
    assert deck.main_cards is deck.main_cards
    other = replace(deck, cards=(replace(template, name="Island"), out))
    assert other.main_cards == (replace(template, name="Island"),)
    assert other.commanders == ()
    # The cached lists are no part of what makes two decks equal or of how a deck prints.
    assert replace(deck, name="test") == deck
    assert "_main_cards" not in repr(deck)
