import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.cmshipping import (
    CardmarketShipping,
    CmShippingError,
    Country,
    ShippingMethod,
    ShippingQuote,
    cheapest,
    order_weight_g,
    parse_euros,
    parse_methods,
)
from commander.jsonshape import ShapeError
from commander.net import Fetcher, NetworkError
from conftest import CM_SWEDEN, FakeCardmarketHelp, Json

FRANCE = 12
ITALY = 17
JAPAN = 36
PRIORITY_LETTER = "Lettre Internationale (Priority Letter)"
TRACKED_LETTER = "Lettre Suivie Internationale (Tracked Letter)"
COLISSIMO = "Colissimo (Tracked Parcel)"
JAPAN_PARCEL = "Courrier Parcel with Full Insurance (Flat Fee)"
START = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.current = START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, by: timedelta) -> None:
        self.current += by


def run_with(fake: FakeCardmarketHelp, folder: Path, clock: Clock) -> CardmarketShipping:
    return CardmarketShipping(Fetcher(fake.client(), max_attempts=1), folder, now=clock)


def method(
    name: str, price: int, *, tracked: bool = False, weight: int = 100, value: int = 2500
) -> ShippingMethod:
    return ShippingMethod(name, tracked, False, value, weight, price)  # noqa: FBT003 — positional like the model


def france_methods(tmp_path: Path) -> tuple[ShippingMethod, ...]:
    return run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(FRANCE, CM_SWEDEN)


# --- parsing ---


def test_parses_prices_in_cents_with_commas_and_thousand_dots() -> None:
    assert parse_euros("1,30 €", "x") == 130
    assert parse_euros("0,00 €", "x") == 0
    assert parse_euros("2.500,00 €", "x") == 250_000
    assert parse_euros("1.000.000,00 €", "x") == 100_000_000
    assert parse_euros("1.000,50 €", "x") == 100_050


@pytest.mark.parametrize(
    "text", ["", "1,30", "1.30 €", "1,3 €", "6.361,18 ¥", "1,30 $", "abc €", "1.00,00 €"]
)
def test_refuses_text_that_is_not_an_amount_in_euros(text: str) -> None:
    with pytest.raises(ShapeError):
        parse_euros(text, "price")


def test_france_to_sweden_methods_come_with_cents_and_limits(tmp_path: Path) -> None:
    methods = france_methods(tmp_path)
    assert methods[:3] == (
        ShippingMethod(PRIORITY_LETTER, False, True, 2500, 20, 255),  # noqa: FBT003
        ShippingMethod(PRIORITY_LETTER, False, True, 2500, 100, 535),  # noqa: FBT003
        ShippingMethod(TRACKED_LETTER, True, True, 5000, 50, 815),  # noqa: FBT003
    )
    assert ShippingMethod(COLISSIMO, True, False, 5000, 500, 1599) in methods  # noqa: FBT003
    assert len(methods) == 38


def test_virtual_delivery_is_not_a_shipment(tmp_path: Path) -> None:
    # The saved France table ends with "Virtual Delivery" at 1,00 €.
    assert all("Virtual" not in m.name for m in france_methods(tmp_path))
    raw = [
        {
            "name": "Virtual Delivery",
            "isTracked": True,
            "maxValue": "10.000,00 €",
            "maxWeight": 0,
            "stampPrice": "0,00 €",
            "price": "1,00 €",
            "isLetter": False,
            "isVirtual": True,
        }
    ]
    assert parse_methods(raw) == ()


def test_japan_prices_are_read_in_euros_not_the_yen_stamp_price(tmp_path: Path) -> None:
    methods = run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(JAPAN, CM_SWEDEN)
    assert len(methods) == 9
    assert methods[0] == ShippingMethod(JAPAN_PARCEL, True, False, 250_000, 20_000, 3816)  # noqa: FBT003
    assert methods[-1] == ShippingMethod(JAPAN_PARCEL, True, False, 100_000_000, 20_000, 1_515_508)  # noqa: FBT003


MISSING = object()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", MISSING),
        ("price", "cheap"),
        ("maxWeight", "20"),
        ("isTracked", "yes"),
        ("isVirtual", None),
    ],
)
def test_a_method_of_the_wrong_shape_is_refused(field: str, value: object) -> None:
    entry: Json = {
        "name": "Letter",
        "isTracked": False,
        "maxValue": "25,00 €",
        "maxWeight": 20,
        "stampPrice": "1,00 €",
        "price": "1,30 €",
        "isLetter": True,
        "isVirtual": False,
    }
    if value is MISSING:
        del entry[field]
    else:
        entry[field] = value
    with pytest.raises(ShapeError):
        parse_methods([entry])


# --- weight ---


@pytest.mark.parametrize(
    ("cards", "grams"),
    [(1, 20), (4, 20), (5, 50), (17, 50), (18, 100), (40, 100), (41, 103), (60, 150)],
)
def test_order_weight_follows_cardmarkets_rule_of_thumb(cards: int, grams: int) -> None:
    # Beyond 40 cards each adds 2.5 g, rounded up: 41 is 100 + 2.5 -> 103; 60 is 100 + 50 -> 150.
    assert order_weight_g(cards) == grams


def test_an_order_without_cards_has_no_weight() -> None:
    with pytest.raises(ValueError, match="at least one card"):
        order_weight_g(0)


# --- cheapest ---


def test_one_cheap_card_from_france_goes_by_the_priority_letter(tmp_path: Path) -> None:
    quote = cheapest(france_methods(tmp_path), 1, 314)
    assert quote == ShippingQuote(PRIORITY_LETTER, 255, False)  # noqa: FBT003


def test_thirty_cards_under_25_euros_need_the_100_g_letter(tmp_path: Path) -> None:
    # 30 cards weigh 100 g; the 20 g letter and the 50 g tracked letter are too light.
    quote = cheapest(france_methods(tmp_path), 30, 2400)
    assert quote == ShippingQuote(PRIORITY_LETTER, 535, False)  # noqa: FBT003


def test_an_order_of_exactly_25_euros_may_still_go_untracked(tmp_path: Path) -> None:
    quote = cheapest(france_methods(tmp_path), 1, 2500)
    assert quote == ShippingQuote(PRIORITY_LETTER, 255, False)  # noqa: FBT003


def test_over_25_euros_the_cheapest_tracked_method_is_chosen(tmp_path: Path) -> None:
    quote = cheapest(france_methods(tmp_path), 1, 3000)
    assert quote == ShippingQuote(TRACKED_LETTER, 815, True)  # noqa: FBT003


def test_a_heavy_valuable_order_goes_by_parcel(tmp_path: Path) -> None:
    # 30 cards are 100 g: too heavy for the 50 g tracked letter, so the 500 g parcel at 15,99 €.
    assert cheapest(france_methods(tmp_path), 30, 3000) == ShippingQuote(COLISSIMO, 1599, True)  # noqa: FBT003


def test_italy_has_no_20_g_letter_so_one_card_takes_the_50_g_one(tmp_path: Path) -> None:
    methods = run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(ITALY, CM_SWEDEN)
    quote = cheapest(methods, 1, 500)
    assert quote == ShippingQuote("Postapriority Internazionale - Ufficio Postale", 488, False)  # noqa: FBT003


def test_japan_has_no_untracked_letter_so_its_cheapest_fitting_tracked_method_is_chosen(
    tmp_path: Path,
) -> None:
    methods = run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(JAPAN, CM_SWEDEN)
    assert cheapest(methods, 1, 1000) == ShippingQuote(JAPAN_PARCEL, 3816, True)  # noqa: FBT003
    # An order worth 3.000 € is over the first method's 2.500 € cover, so the 5.000 € one at 76,05 €.
    assert cheapest(methods, 1, 300_000) == ShippingQuote(JAPAN_PARCEL, 7605, True)  # noqa: FBT003


def test_no_method_fits_a_too_heavy_or_too_valuable_order(tmp_path: Path) -> None:
    methods = run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(JAPAN, CM_SWEDEN)
    # 9000 cards weigh 100 + 22400 g, over the 20 kg of every Japanese method.
    assert cheapest(methods, 9000, 1000) is None
    # The top method covers 1.000.000,00 €.
    assert cheapest(methods, 1, 100_000_000) is not None
    assert cheapest(methods, 1, 100_000_001) is None


def test_no_methods_means_no_quote() -> None:
    assert cheapest((), 1, 100) is None


def test_equal_prices_prefer_the_tracked_method_then_the_name() -> None:
    methods = (
        method("B plain", 300),
        method("A plain", 300),
        method("Z tracked", 300, tracked=True),
        method("Y tracked", 300, tracked=True),
    )
    assert cheapest(methods, 1, 100) == ShippingQuote("Y tracked", 300, True)  # noqa: FBT003
    assert cheapest(methods[:2], 1, 100) == ShippingQuote("A plain", 300, False)  # noqa: FBT003
    assert cheapest(methods[::-1], 1, 100) == ShippingQuote("Y tracked", 300, True)  # noqa: FBT003


def test_a_cheaper_untracked_method_beats_a_dearer_tracked_one() -> None:
    methods = (method("Tracked", 400, tracked=True), method("Plain", 399))
    assert cheapest(methods, 1, 100) == ShippingQuote("Plain", 399, False)  # noqa: FBT003


# --- the service ---


def test_countries_are_read_across_pages_without_the_placeholder(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp(per_page=10)
    countries = run_with(fake, tmp_path, Clock()).countries()
    assert len(countries) == 34
    assert countries[0] == Country(1, "Austria")
    assert countries[-1] == Country(13, "United Kingdom")
    assert all(c.id != 0 and c.name != "INVALID_ID" for c in countries)
    # 35 entries in pages of 10 take four requests, each asking for the English names.
    assert [params["page"] for _, params in fake.requests] == ["1", "2", "3", "4"]
    assert {(path, params["limit"], params["locale"]) for path, params in fake.requests} == {
        ("/api/countries", "50", "en")
    }


def test_a_server_that_stops_short_of_the_total_is_an_error(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp(per_page=10)
    fake.countries = fake.countries[:5]
    fake.total = 35
    with pytest.raises(CmShippingError, match="read 5 of 35"):
        run_with(fake, tmp_path, Clock()).countries()


def test_country_ids_are_found_by_name_ignoring_case(tmp_path: Path) -> None:
    shipping = run_with(FakeCardmarketHelp(), tmp_path, Clock())
    assert shipping.country_id("Italy") == ITALY
    assert shipping.country_id("italy") == ITALY
    assert shipping.country_id("United Kingdom") == 13
    assert shipping.country_id("CZECH REPUBLIC") == 6
    assert shipping.country_id("Sweden") == CM_SWEDEN
    assert shipping.country_id("Atlantis") is None
    assert shipping.country_id("INVALID_ID") is None
    assert shipping.country_id("Ital") is None


def test_the_countries_are_kept_for_30_days(tmp_path: Path) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    first = run_with(fake, tmp_path, clock).countries()
    assert len(fake.requests) == 1
    clock.advance(timedelta(days=29, hours=23))
    assert run_with(fake, tmp_path, clock).countries() == first
    assert len(fake.requests) == 1
    clock.advance(timedelta(hours=1))
    assert run_with(fake, tmp_path, clock).countries() == first
    assert len(fake.requests) == 2


def test_a_route_is_asked_for_once_within_7_days_and_again_after(tmp_path: Path) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    first = run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)
    assert fake.requests == [("/api/shippingCosts", {"locale": "en", "fromCountry": "12", "toCountry": "28"})]
    clock.advance(timedelta(days=6, hours=23))
    assert run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN) == first
    assert len(fake.requests) == 1
    clock.advance(timedelta(hours=1))
    assert run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN) == first
    assert len(fake.requests) == 2


def test_each_route_has_its_own_cache(tmp_path: Path) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    shipping = run_with(fake, tmp_path, clock)
    france = shipping.methods(FRANCE, CM_SWEDEN)
    italy = shipping.methods(ITALY, CM_SWEDEN)
    assert france != italy
    assert shipping.methods(FRANCE, CM_SWEDEN) == france
    assert shipping.methods(ITALY, CM_SWEDEN) == italy
    assert len(fake.requests) == 2
    # The other way round is another route.
    assert shipping.methods(CM_SWEDEN, FRANCE) == ()
    assert len(fake.requests) == 3


def test_a_route_cardmarket_does_not_serve_has_no_methods_and_is_remembered(tmp_path: Path) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    shipping = run_with(fake, tmp_path, clock)
    assert shipping.methods(JAPAN, FRANCE) == ()
    assert shipping.methods(JAPAN, FRANCE) == ()
    assert len(fake.requests) == 1


def test_the_cache_file_carries_a_format_version_and_the_time(tmp_path: Path) -> None:
    run_with(FakeCardmarketHelp(), tmp_path, Clock()).methods(FRANCE, CM_SWEDEN)
    (path,) = tmp_path.glob("*.json")
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["format"] == 1
    assert stored["fetched_at"] == "2026-11-09T12:00:00+00:00"
    assert len(stored["answer"]) == 39
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    "content",
    [
        "",
        "{not json",
        "[]",
        '{"format": 1, "fetched_at": "2026-11-09T12:00:00+00:00"}',
        '{"format": 1, "fetched_at": "2026-11-09T12:00:00+00:00", "answer": [{"name": "x"}]}',
        '{"format": 1, "fetched_at": "yesterday", "answer": []}',
        '{"format": 1, "fetched_at": "2026-11-09T12:00:00", "answer": []}',
        '{"format": 2, "fetched_at": "2026-11-09T12:00:00+00:00", "answer": []}',
        '{"fetched_at": "2026-11-09T12:00:00+00:00", "answer": []}',
    ],
)
def test_a_cache_file_that_cannot_be_used_is_fetched_again(tmp_path: Path, content: str) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)
    (path,) = tmp_path.glob("*.json")
    path.write_text(content, encoding="utf-8")
    methods = run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)
    assert len(methods) == 38
    assert len(fake.requests) == 2
    # The fresh answer replaced the unusable file, so the next call is served from it.
    assert len(run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)) == 38
    assert len(fake.requests) == 2


def test_a_cache_dated_in_the_future_is_fetched_again(tmp_path: Path) -> None:
    fake, clock = FakeCardmarketHelp(), Clock()
    run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)
    clock.advance(timedelta(days=-1))
    run_with(fake, tmp_path, clock).methods(FRANCE, CM_SWEDEN)
    assert len(fake.requests) == 2


def test_an_unexpected_answer_raises_and_is_not_cached(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp()
    fake.routes[(FRANCE, CM_SWEDEN)] = {"error": "no such route"}
    with pytest.raises(CmShippingError, match="unexpected answer"):
        run_with(fake, tmp_path, Clock()).methods(FRANCE, CM_SWEDEN)
    assert list(tmp_path.glob("*")) == []


def test_a_method_with_a_missing_field_raises_and_is_not_cached(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp()
    del fake.routes[(FRANCE, CM_SWEDEN)][4]["price"]
    with pytest.raises(CmShippingError, match=r"answer\[4\]\.price"):
        run_with(fake, tmp_path, Clock()).methods(FRANCE, CM_SWEDEN)
    assert list(tmp_path.glob("*")) == []


def test_a_countries_answer_of_the_wrong_shape_raises(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp()
    fake.countries = [{"name": "Austria"}]
    with pytest.raises(CmShippingError, match="externalId"):
        run_with(fake, tmp_path, Clock()).countries()


def test_a_failing_server_raises_the_network_error_and_costs_nothing_silently(tmp_path: Path) -> None:
    fake = FakeCardmarketHelp()
    fake.status = 503
    with pytest.raises(NetworkError):
        run_with(fake, tmp_path, Clock()).methods(FRANCE, CM_SWEDEN)
    assert list(tmp_path.glob("*")) == []
