import re
from collections.abc import Callable

import httpx
import pytest

from commander.archidekt import ArchidektClient, ArchidektError, parse_deck
from commander.jsonshape import ShapeError
from commander.net import Fetcher
from conftest import Handler, Json, card_entry, deck_page, summary_row


def no_sleep(_: float) -> None:
    pass


def client_for(handler: Handler) -> ArchidektClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ArchidektClient(Fetcher(http, sleep=no_sleep))


def test_parse_deck_reads_header_fields(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    assert deck.id == 10004639
    assert deck.name == "Honestly, though.."
    assert deck.owner == "MAKKIS"
    assert deck.bracket == 5
    assert deck.updated_at == "2026-10-11T20:56:43.155670Z"
    assert deck.url == "https://archidekt.com/decks/10004639"


def test_parse_deck_splits_main_deck_from_maybeboard(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    assert deck.commanders == ("Magda, Brazen Outlaw",)
    assert deck.main_count == 9
    assert [c.name for c in deck.cards if not c.in_deck] == [
        "High-Speed Hoverbike",
        "Tempestra, Dame of Games",
    ]


def test_parse_deck_keeps_card_identity_fields(raw_deck: Json) -> None:
    mountain = next(c for c in parse_deck(raw_deck).cards if c.name == "Mountain")
    assert mountain.quantity == 6
    assert mountain.categories == ("Land",)
    assert mountain.entry_id == 2081750497
    assert mountain.set_code == "j25"
    assert mountain.collector_number == "90"
    assert mountain.finish == "Normal"


def test_card_is_in_deck_when_only_a_secondary_category_is_excluded(raw_deck: Json) -> None:
    raw_deck["cards"].append(card_entry(1, "Arcane Signet", ["Ramp", "TO ADD"]))
    signet = next(c for c in parse_deck(raw_deck).cards if c.name == "Arcane Signet")
    assert signet.in_deck


def test_uncategorised_card_counts_as_in_deck(raw_deck: Json) -> None:
    raw_deck["cards"].append(card_entry(1, "Sol Ring", []))
    sol_ring = next(c for c in parse_deck(raw_deck).cards if c.name == "Sol Ring")
    assert sol_ring.in_deck
    assert sol_ring.primary_category == "Uncategorized"


def test_deleted_entries_are_dropped(raw_deck: Json) -> None:
    raw_deck["cards"].append(card_entry(1, "Sol Ring", ["Ramp"], deleted=True))
    assert "Sol Ring" not in [c.name for c in parse_deck(raw_deck).cards]


def test_cards_are_sorted_by_name(raw_deck: Json) -> None:
    names = [c.name for c in parse_deck(raw_deck).cards]
    assert names == [
        "Battered Golem",
        "Disruptor Flute",
        "High-Speed Hoverbike",
        "Magda, Brazen Outlaw",
        "Mountain",
        "Tempestra, Dame of Games",
    ]


def drop_cards(deck: Json) -> None:
    del deck["cards"]


def quantity_as_text(deck: Json) -> None:
    deck["cards"][0]["quantity"] = "1"


def drop_owner_name(deck: Json) -> None:
    del deck["owner"]["username"]


def included_as_number(deck: Json) -> None:
    deck["categories"][0]["includedInDeck"] = 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (drop_cards, "deck.cards"),
        (quantity_as_text, "deck.cards[0].quantity"),
        (drop_owner_name, "deck.owner.username"),
        (included_as_number, "includedInDeck"),
    ],
)
def test_parse_deck_rejects_wrong_shapes(
    raw_deck: Json, mutate: Callable[[Json], None], message: str
) -> None:
    mutate(raw_deck)
    with pytest.raises(ShapeError, match=re.escape(message)):
        parse_deck(raw_deck)


def test_get_deck_wraps_bad_data_in_archidekt_error(raw_deck: Json) -> None:
    raw_deck["cards"][0]["quantity"] = 0

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw_deck)

    with pytest.raises(ArchidektError, match="deck 10004639"):
        client_for(handler).get_deck(10004639)


def test_list_follows_pagination_and_keeps_only_commander_decks() -> None:
    second = "https://archidekt.com/api/decks/v3/?ownerUsername=MAKKIS&page=2"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=deck_page([summary_row(3, "Third", "t3")]))
        rows = [summary_row(1, "First", "t1"), summary_row(2, "Standard deck", "t2", deck_format=1)]
        return httpx.Response(200, json=deck_page(rows, next_url=second))

    decks = client_for(handler).list_commander_decks("MAKKIS")
    assert [(d.id, d.name, d.updated_at) for d in decks] == [(1, "First", "t1"), (3, "Third", "t3")]
    assert seen[1] == second


def test_list_rejects_next_link_to_another_host() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=deck_page([], next_url="https://evil.example/api/decks/v3/"))

    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        client_for(handler).list_commander_decks("MAKKIS")
