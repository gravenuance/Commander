import logging
import re
from collections.abc import Callable

import httpx
import pytest

from commander.archidekt import (
    ArchidektClient,
    ArchidektError,
    AuthError,
    SignInRequiredError,
    auth_headers,
    category_flags,
    parse_deck,
    raise_if_unauthorized,
)
from commander.jsonshape import ShapeError
from commander.net import Fetcher, HttpStatusError
from conftest import Handler, Json, card_entry, deck_page, summary_row


def no_sleep(_: float) -> None:
    pass


def client_for(handler: Handler) -> ArchidektClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ArchidektClient(Fetcher(http, sleep=no_sleep))


def test_parse_deck_reads_header_fields(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    assert deck.id == 10000001
    assert deck.name == "Sample, deck.."
    assert deck.owner == "TESTER"
    assert deck.bracket == 5
    assert deck.updated_at == "2026-10-11T20:56:43.155670Z"
    assert deck.url == "https://archidekt.com/decks/10000001"


def test_parse_deck_reads_the_deck_tags_in_order(raw_deck: Json) -> None:
    assert parse_deck(raw_deck).tags == ("Dwarves", "Vehicles", "cEDH")


def test_a_deck_without_deck_tags_has_none(raw_deck: Json) -> None:
    del raw_deck["deckTags"]
    assert parse_deck(raw_deck).tags == ()
    raw_deck["deckTags"] = [{"name": "Stax"}, {"name": "Stax"}]
    assert parse_deck(raw_deck).tags == ("Stax",)


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


def test_parse_deck_reads_each_card_label_by_name(raw_deck: Json) -> None:
    for index, label in enumerate(("Getting,#2ccce4", ",#656565", None)):
        raw_deck["cards"].append({**card_entry(90 + index, f"Card {index}", ["Ramp"]), "label": label})
    labels = {
        c.name: (c.label, c.label_colour) for c in parse_deck(raw_deck).cards if c.name.startswith("Card ")
    }
    assert labels == {"Card 0": ("Getting", "#2ccce4"), "Card 1": ("", ""), "Card 2": ("", "")}
    assert parse_deck(raw_deck).labels_known


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


def tag_without_name(deck: Json) -> None:
    del deck["deckTags"][1]["name"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (drop_cards, "deck.cards"),
        (quantity_as_text, "deck.cards[0].quantity"),
        (drop_owner_name, "deck.owner.username"),
        (included_as_number, "includedInDeck"),
        (tag_without_name, "deck.deckTags[1].name"),
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

    with pytest.raises(ArchidektError, match="deck 10000001"):
        client_for(handler).get_deck(10000001)


def test_list_follows_pagination_and_keeps_only_commander_decks() -> None:
    second = "https://archidekt.com/api/decks/v3/?ownerUsername=TESTER&page=2"
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=deck_page([summary_row(3, "Third", "t3")]))
        rows = [summary_row(1, "First", "t1"), summary_row(2, "Standard deck", "t2", deck_format=1)]
        return httpx.Response(200, json=deck_page(rows, next_url=second))

    decks = client_for(handler).list_commander_decks("TESTER")
    assert [(d.id, d.name, d.updated_at) for d in decks] == [(1, "First", "t1"), (3, "Third", "t3")]
    assert seen[1] == second


SECOND_PAGE = "https://archidekt.com/api/decks/v3/?ownerUsername=TESTER&page=2"


def drifting_listing(reads: list[list[list[int]]]) -> tuple[Handler, list[str]]:
    """A two-page listing; the n-th read from page 1 answers with the ids in `reads[n]` (page 1, page 2)."""
    requests: list[str] = []
    state = {"read": -1}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        second = request.url.params.get("page") == "2"
        if not second:
            state["read"] += 1
        ids = reads[min(state["read"], len(reads) - 1)][1 if second else 0]
        rows = [summary_row(i, f"Deck {i}", f"t{i}") for i in ids]
        return httpx.Response(200, json=deck_page(rows, next_url=None if second else SECOND_PAGE))

    return handler, requests


def test_a_listing_that_shows_a_deck_twice_is_read_again_from_page_one() -> None:
    # The first read sees deck 2 on both pages and misses deck 4, which an edit moved to page 1 meanwhile.
    handler, requests = drifting_listing([[[1, 2], [2, 3]], [[4, 1], [2, 3]]])

    decks = client_for(handler).list_commander_decks("TESTER")

    assert [d.id for d in decks] == [4, 1, 2, 3]
    assert sum(1 for url in requests if "page=2" not in url) == 2


def test_a_listing_that_drifts_again_keeps_the_first_row_of_a_repeated_deck_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler, requests = drifting_listing([[[1, 2], [2, 3]]])

    with caplog.at_level(logging.INFO, logger="commander.archidekt"):
        decks = client_for(handler).list_commander_decks("TESTER")

    assert [d.id for d in decks] == [1, 2, 3]
    assert sum(1 for url in requests if "page=2" not in url) == 2
    assert [r.levelno for r in caplog.records if "drift" in r.getMessage()] == [logging.INFO, logging.INFO]


def test_list_rows_carry_the_deck_tags_when_archidekt_sends_them() -> None:
    tagged = {**summary_row(1, "First", "t1"), "tags": [{"name": "Stax"}, {"name": "cEDH"}]}

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=deck_page([tagged, summary_row(2, "Second", "t2")]))

    first, second = client_for(handler).list_commander_decks("TESTER")
    assert (first.tags, second.tags) == (("Stax", "cEDH"), None)


def test_list_rejects_next_link_to_another_host() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=deck_page([], next_url="https://evil.example/api/decks/v3/"))

    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        client_for(handler).list_commander_decks("TESTER")


def test_list_follows_archidekts_plain_http_next_link_over_https() -> None:
    # Archidekt writes its next links with http://, as seen live on 2026-10-15; its own site swaps in https.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json=deck_page([summary_row(2, "Second", "t2")]))
        next_url = "http://archidekt.com/api/decks/v3/?ownerUsername=TESTER&page=2"
        return httpx.Response(200, json=deck_page([summary_row(1, "First", "t1")], next_url=next_url))

    decks = client_for(handler).list_commander_decks("TESTER")

    assert [d.id for d in decks] == [1, 2]
    assert seen[1] == "https://archidekt.com/api/decks/v3/?ownerUsername=TESTER&page=2"


@pytest.mark.parametrize(
    "next_url",
    [
        "http://archidekt.com.evil.example/api/decks/v3/",
        "http://evil.example/archidekt.com/api/decks/v3/",
        "http://archidekt.com@evil.example/api/decks/v3/",
        "ftp://archidekt.com/api/decks/v3/",
    ],
)
def test_list_rejects_lookalike_next_links(next_url: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=deck_page([], next_url=next_url))

    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        client_for(handler).list_commander_decks("TESTER")


def test_category_flags_say_which_categories_count_in_the_deck(raw_deck: Json) -> None:
    flags = category_flags(raw_deck)

    assert flags["Maybeboard"] is False
    assert flags["Sideboard"] is True
    assert flags["Commander"] is True


@pytest.mark.parametrize("status", [401, 403])
def test_a_401_or_403_becomes_a_sign_in_required_error_naming_the_status(status: int) -> None:
    with pytest.raises(
        SignInRequiredError, match=rf"^Archidekt refused the login \(HTTP {status}\)$"
    ) as raised:
        raise_if_unauthorized(HttpStatusError("refused", status))

    assert isinstance(raised.value, AuthError)


def test_an_unauthorized_status_uses_the_message_the_caller_gives() -> None:
    with pytest.raises(SignInRequiredError) as raised:
        raise_if_unauthorized(HttpStatusError("refused", 401), "the saved login has expired")

    assert str(raised.value) == "the saved login has expired"


@pytest.mark.parametrize("status", [400, 404, 429, 500])
def test_any_other_status_passes_through_the_helper_unchanged(status: int) -> None:
    raise_if_unauthorized(HttpStatusError("failed", status))


def test_the_auth_header_carries_the_token_and_an_anonymous_call_has_none() -> None:
    assert auth_headers("abc") == {"Authorization": "JWT abc"}
    assert auth_headers(None) is None
    assert auth_headers("") is None


@pytest.mark.parametrize("status", [401, 403])
def test_a_signed_in_client_maps_a_refused_read_to_sign_in_required(status: int) -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as http:
        client = ArchidektClient(Fetcher(http), token="stale")
        with pytest.raises(SignInRequiredError, match=rf"HTTP {status}"):
            client.get_deck(1)


def test_a_signed_in_client_leaves_other_failures_as_they_are() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as http:
        client = ArchidektClient(Fetcher(http), token="good")
        with pytest.raises(HttpStatusError) as raised:
            client.get_deck(1)

    assert raised.value.status == 404
