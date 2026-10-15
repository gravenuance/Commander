import copy
from dataclasses import replace

import pytest

from commander.archidekt import parse_deck
from commander.jsonshape import ShapeError
from commander.net import Fetcher
from commander.spellbook import (
    BracketTag,
    SpellbookClient,
    SpellbookError,
    TwoCard,
    deck_request,
    parse_estimate,
    request_key,
)
from conftest import FakeSpellbook, Json, card_entry, load_fixture


def estimate_fixture() -> Json:
    return copy.deepcopy(load_fixture("spellbook_estimate.json"))


def test_parses_the_deck_tag_and_every_combo() -> None:
    estimate = parse_estimate(estimate_fixture())
    assert estimate.tag is BracketTag.RUTHLESS
    assert [(c.id, c.tag, c.two_card) for c in estimate.combos] == [
        ("1486-1651-7298", BracketTag.SPICY, TwoCard.YES),
        ("1486-3459-7223--67", BracketTag.EXHIBITION, TwoCard.NO),
        ("4477-6556-7182", BracketTag.EXHIBITION, TwoCard.ARGUABLY),
    ]
    first = estimate.combos[0]
    assert first.cards == ("Magda, Brazen Outlaw", "Clock of Omens", "Firdoch Core")
    assert first.results[0] == "Infinite artifact ETB"
    assert first.url == "https://commanderspellbook.com/combo/1486-1651-7298/"


def test_tags_map_to_the_lowest_bracket_they_fit() -> None:
    assert [(t.code, t.minimum) for t in BracketTag] == [
        ("R", 4),
        ("S", 3),
        ("P", 3),
        ("O", 2),
        ("C", 2),
        ("E", 1),
        ("B", None),
    ]


def test_unknown_deck_tag_is_refused() -> None:
    raw = estimate_fixture()
    raw["bracketTag"] = "X"
    with pytest.raises(ShapeError, match="unknown bracket tag 'X'"):
        parse_estimate(raw)


def test_combo_without_cards_is_refused() -> None:
    raw = estimate_fixture()
    raw["combos"][1]["combo"]["uses"] = []
    with pytest.raises(ShapeError, match=r"combos\[1\]\.combo\.uses: a combo needs at least one card"):
        parse_estimate(raw)


def test_missing_two_card_flag_is_refused() -> None:
    raw = estimate_fixture()
    del raw["combos"][0]["definitelyTwoCard"]
    with pytest.raises(ShapeError, match=r"combos\[0\]\.definitelyTwoCard"):
        parse_estimate(raw)


def test_card_without_a_name_is_refused() -> None:
    raw = estimate_fixture()
    del raw["combos"][2]["combo"]["uses"][1]["card"]["name"]
    with pytest.raises(ShapeError, match=r"uses\[1\]\.card\.name"):
        parse_estimate(raw)


def test_request_puts_commanders_apart_and_leaves_the_maybeboard_out(raw_deck: Json) -> None:
    request = deck_request(parse_deck(raw_deck))
    assert request == {
        "commanders": [{"card": "Magda, Brazen Outlaw", "quantity": 1}],
        "main": [
            {"card": "Battered Golem", "quantity": 1},
            {"card": "Disruptor Flute", "quantity": 1},
            {"card": "Mountain", "quantity": 6},
        ],
    }


def test_fingerprint_ignores_card_order_but_not_quantities(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    reordered = replace(deck, cards=tuple(reversed(deck.cards)))
    assert request_key(deck_request(reordered)) == request_key(deck_request(deck))
    more = replace(
        deck, cards=tuple(replace(c, quantity=7) if c.name == "Mountain" else c for c in deck.cards)
    )
    assert request_key(deck_request(more)) != request_key(deck_request(deck))


def test_deck_too_large_for_spellbook_is_refused(raw_deck: Json) -> None:
    raw_deck["cards"] = [card_entry(i, f"Card {i}", ["Main"]) for i in range(1, 602)]
    with pytest.raises(SpellbookError, match="at most 600 cards"):
        deck_request(parse_deck(raw_deck))


def test_client_posts_the_deck_and_parses_the_answer(raw_deck: Json) -> None:
    fake = FakeSpellbook()
    request = deck_request(parse_deck(raw_deck))
    with fake.client() as http:
        estimate = SpellbookClient(Fetcher(http)).estimate_bracket(request)
    assert fake.bodies == [request]
    assert estimate.tag is BracketTag.RUTHLESS


def test_client_reports_a_malformed_answer_as_a_spellbook_error(raw_deck: Json) -> None:
    fake = FakeSpellbook()
    fake.answer = {"bracketTag": "R"}
    with fake.client() as http, pytest.raises(SpellbookError, match="unexpected bracket estimate"):
        SpellbookClient(Fetcher(http)).estimate_bracket(deck_request(parse_deck(raw_deck)))
