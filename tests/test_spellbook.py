import copy
from dataclasses import replace

import pytest

from commander.archidekt import parse_deck
from commander.deckcache import request_key
from commander.jsonshape import ShapeError
from commander.net import Fetcher
from commander.spellbook import (
    BracketTag,
    ComboCard,
    SpellbookClient,
    SpellbookError,
    TwoCard,
    deck_request,
    parse_estimate,
    parse_matches,
)
from conftest import FakeSpellbook, Json, card_entry, load_fixture


def estimate_fixture() -> Json:
    return copy.deepcopy(load_fixture("spellbook_estimate.json"))


def combos_fixture() -> Json:
    return copy.deepcopy(load_fixture("spellbook_combos.json"))


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


def test_parses_included_and_near_miss_combos() -> None:
    matches = parse_matches(combos_fixture())
    assert [v.id for v in matches.included] == ["643-1486-3459--67"]
    assert [(v.id, v.tag) for v in matches.almost_included] == [
        ("1878-3368", BracketTag.POWERFUL),
        ("1486-3750", BracketTag.SPICY),
        ("643-1486-3459--68", BracketTag.EXHIBITION),
        ("3459-4702", BracketTag.SPICY),
        ("1261-1651", BracketTag.BANNED),
    ]
    assault = matches.almost_included[1]
    assert assault.cards == (
        ComboCard("Magda, Brazen Outlaw", "3d268e48-3004-4384-bf52-e63243fb5e02"),
        ComboCard("Aggravated Assault", "20129459-a386-41eb-899d-1aede3427300"),
    )
    assert (assault.results, assault.popularity) == (("Infinite combat phases",), 10497)
    assert assault.url == "https://commanderspellbook.com/combo/1486-3750/"
    assert matches.almost_included[2].requirements == ("Creature with Saddle 4 or less",)
    assert assault.requirements == ()


def test_card_without_an_oracle_id_keeps_its_name() -> None:
    raw = combos_fixture()
    raw["results"]["almostIncluded"][1]["uses"][1]["card"]["oracleId"] = None
    del raw["results"]["almostIncluded"][1]["popularity"]
    assault = parse_matches(raw).almost_included[1]
    assert assault.cards[1] == ComboCard("Aggravated Assault", None)
    assert assault.popularity is None


def test_near_miss_without_cards_is_refused() -> None:
    raw = combos_fixture()
    raw["results"]["almostIncluded"][0]["uses"] = []
    with pytest.raises(ShapeError, match=r"almostIncluded\[0\]\.uses: a combo needs at least one card"):
        parse_matches(raw)


def test_combo_list_without_near_misses_is_refused() -> None:
    raw = combos_fixture()
    del raw["results"]["almostIncluded"]
    with pytest.raises(ShapeError, match="almostIncluded"):
        parse_matches(raw)


def test_client_asks_for_the_combos_of_a_deck(raw_deck: Json) -> None:
    fake = FakeSpellbook()
    request = deck_request(parse_deck(raw_deck))
    with fake.client() as http:
        matches = SpellbookClient(Fetcher(http)).find_my_combos(request)
    assert (fake.combo_bodies, fake.bodies) == ([request], [])
    assert len(matches.almost_included) == 5


def test_client_reports_a_malformed_combo_list_as_a_spellbook_error(raw_deck: Json) -> None:
    fake = FakeSpellbook()
    fake.combos = {"results": {"included": [], "almostIncluded": [{"id": "1", "bracketTag": "X"}]}}
    with fake.client() as http, pytest.raises(SpellbookError, match="unexpected combo list"):
        SpellbookClient(Fetcher(http)).find_my_combos(deck_request(parse_deck(raw_deck)))
