from dataclasses import replace

import pytest

from commander.archidekt import parse_deck
from commander.models import Deck, short_commander_name
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
