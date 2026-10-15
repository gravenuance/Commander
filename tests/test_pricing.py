from dataclasses import replace
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.carddb import CardDb
from commander.models import Deck
from commander.pricing import DeckPrice, PriceSource, format_eur, price_deck
from conftest import Json


def priced(deck: Deck, card_db_file: Path) -> DeckPrice:
    with CardDb(card_db_file) as db:
        return price_deck(deck, db.resolve(deck.main_cards))


def with_card(deck: Deck, target: str, /, **changes: str) -> Deck:
    cards = tuple(replace(c, **changes) if c.name == target else c for c in deck.cards)
    return replace(deck, cards=cards)


def test_deck_value_uses_the_printings_in_the_deck(raw_deck: Json, card_db_file: Path) -> None:
    price = priced(parse_deck(raw_deck), card_db_file)
    # Magda plst 7.63 + Flute mh3 2.17 + 6 x Mountain j25 0.12 + Golem 5dn 0.35; maybeboard excluded.
    assert price.total_cents == 1087
    assert [(c.name, c.total_cents) for c in price.cards] == [
        ("Magda, Brazen Outlaw", 763),
        ("Disruptor Flute", 217),
        ("Mountain", 72),
        ("Battered Golem", 35),
    ]


def test_cheapest_total_uses_each_cards_cheapest_printing(raw_deck: Json, card_db_file: Path) -> None:
    # Magda khm 6.79, Flute mh3 2.17, 6 x Mountain clb 0.07, Golem 0.35.
    assert priced(parse_deck(raw_deck), card_db_file).cheapest_total_cents == 973


def test_foil_finish_uses_the_foil_price(raw_deck: Json, card_db_file: Path) -> None:
    deck = with_card(parse_deck(raw_deck), "Disruptor Flute", finish="Foil")
    flute = next(c for c in priced(deck, card_db_file).cards if c.name == "Disruptor Flute")
    assert (flute.unit_cents, flute.source) == (381, PriceSource.PRINTING)


def test_missing_finish_price_falls_back_to_the_cheapest_printing(raw_deck: Json, card_db_file: Path) -> None:
    # Magda's plst printing has no foil price.
    deck = with_card(parse_deck(raw_deck), "Magda, Brazen Outlaw", finish="Foil")
    price = priced(deck, card_db_file)
    magda = next(c for c in price.cards if c.name == "Magda, Brazen Outlaw")
    assert (magda.unit_cents, magda.source) == (679, PriceSource.CHEAPEST)
    assert price.estimated == ("Magda, Brazen Outlaw",)


def test_unknown_printing_is_estimated_from_the_card_name(raw_deck: Json, card_db_file: Path) -> None:
    deck = with_card(
        parse_deck(raw_deck), "Battered Golem", scryfall_id="00000000-0000-0000-0000-000000000000"
    )
    golem = next(c for c in priced(deck, card_db_file).cards if c.name == "Battered Golem")
    assert (golem.unit_cents, golem.source) == (35, PriceSource.CHEAPEST)


def test_card_without_any_price_is_listed_and_counts_zero(raw_deck: Json, card_db_file: Path) -> None:
    deck = with_card(
        parse_deck(raw_deck),
        "Battered Golem",
        scryfall_id="00000000-0000-0000-0000-000000000000",
        name="Nope",
    )
    price = priced(deck, card_db_file)
    assert price.unpriced == ("Nope",)
    assert price.total_cents == 1087 - 35


@pytest.mark.parametrize(
    ("cents", "text"), [(0, "€0.00"), (5, "€0.05"), (1087, "€10.87"), (123456789, "€1,234,567.89")]
)
def test_format_eur(cents: int, text: str) -> None:
    assert format_eur(cents) == text
