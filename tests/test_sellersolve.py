import itertools
import re

import pytest

from commander.cmpages import Condition, Offer
from commander.cmscan import card_slug
from commander.cmshipping import ShippingMethod
from commander.sellersolve import (
    Package,
    Routes,
    Shortfall,
    WantedCard,
    card_key,
    curve,
    solve,
)

LETTER = ShippingMethod(
    "Letter", tracked=False, letter=True, max_value_cents=2500, max_weight_g=20, price_cents=200
)
HEAVY_LETTER = ShippingMethod(
    "Heavy letter", tracked=False, letter=True, max_value_cents=2500, max_weight_g=50, price_cents=500
)
TRACKED = ShippingMethod(
    "Tracked", tracked=True, letter=False, max_value_cents=100_000, max_weight_g=2000, price_cents=1500
)
TRACKED_LETTER = ShippingMethod(
    "Tracked letter", tracked=True, letter=True, max_value_cents=10_000, max_weight_g=20, price_cents=450
)
_article_ids = itertools.count(1)


def offer(
    card: str,
    seller: str,
    price_cents: int,
    *,
    quantity: int = 1,
    country: str = "France",
    buyable: bool = True,
    tracked_only: bool = False,
) -> Offer:
    return Offer(
        article_id=next(_article_ids),
        card=card,
        seller=seller,
        seller_country=country,
        expansion="Any",
        condition=Condition.NM,
        language="English",
        foil=False,
        buyable=buyable,
        price_cents=price_cents,
        quantity=quantity,
        tracked_only=tracked_only,
    )


def everywhere(*methods: ShippingMethod) -> Routes:
    def routes(_country: str) -> tuple[ShippingMethod, ...]:
        return methods

    return routes


def wants(*cards: str) -> list[WantedCard]:
    return [WantedCard(card, 1) for card in cards]


def solved(
    wanted: list[WantedCard],
    offers: list[Offer],
    routes: Routes | None = None,
    *,
    max_sellers: int | None = None,
) -> Package:
    package = solve(wanted, offers, routes or everywhere(LETTER), max_sellers=max_sellers)
    assert package is not None
    return package


def sellers_of(package: Package) -> set[str]:
    return {s.seller for s in package.shipments}


def test_consolidating_beats_two_cheaper_sellers() -> None:
    offers = [
        offer("X", "A", 100),
        offer("Y", "A", 100),
        offer("X", "B", 50),
        offer("Y", "C", 50),
    ]

    package = solved(wants("X", "Y"), offers)

    assert sellers_of(package) == {"A"}
    assert package.cards_cents == 200
    assert package.shipping_cents == 200
    assert package.total_cents == 400
    assert package.optimal


def test_a_second_parcel_is_added_when_a_card_saves_more_than_the_shipping() -> None:
    offers = [offer("X", "A", 500), offer("Y", "A", 500), offer("X", "B", 100)]

    package = solved(wants("X", "Y"), offers)

    # B: 1,00 + 2,00 plus A's Y at 5,00 + 2,00 is 10,00; A alone is 10,00 + 2,00.
    assert sellers_of(package) == {"A", "B"}
    assert package.total_cents == 1000


@pytest.mark.parametrize(
    ("price_cents", "expected_parcels", "expected_total"),
    [
        (1200, 1, 2600),  # 24,00 fits the 25 € letter
        (1250, 1, 2700),  # exactly 25,00 still fits
        (1251, 2, 2902),  # 25,02 needs tracked at 15,00 (40,02 in all), so two letters win
        (1300, 2, 3000),
    ],
)
def test_an_untracked_parcel_may_not_be_worth_more_than_25_euros(
    price_cents: int, expected_parcels: int, expected_total: int
) -> None:
    offers = [
        offer("X", "A", price_cents),
        offer("Y", "A", price_cents),
        offer("X", "B", price_cents),
        offer("Y", "C", price_cents),
    ]

    package = solved(wants("X", "Y"), offers, everywhere(LETTER, TRACKED))

    assert package.seller_count == expected_parcels
    assert {s.quote.method_name for s in package.shipments} == {"Letter"}
    assert package.total_cents == expected_total


def test_a_parcel_over_the_value_limit_is_sent_tracked() -> None:
    package = solved(
        wants("X", "Y"), [offer("X", "A", 1300), offer("Y", "A", 1300)], everywhere(LETTER, TRACKED)
    )

    (shipment,) = package.shipments
    assert shipment.quote.method_name == "Tracked"
    assert shipment.quote.tracked
    assert shipment.value_cents == 2600
    assert package.total_cents == 4100


def test_a_tracked_only_seller_is_sent_tracked_whatever_the_value() -> None:
    package = solved(
        wants("X"), [offer("X", "A", 100, tracked_only=True)], everywhere(LETTER, TRACKED_LETTER)
    )

    (shipment,) = package.shipments
    assert shipment.quote.method_name == "Tracked letter"
    assert package.total_cents == 100 + 450


def test_a_tracked_only_seller_loses_to_a_dearer_card_that_ships_untracked() -> None:
    offers = [offer("X", "A", 100, tracked_only=True), offer("X", "B", 200)]

    package = solved(wants("X"), offers, everywhere(LETTER, TRACKED_LETTER))

    # A: 1,00 + 4,50 = 5,50; B: 2,00 + 2,00 = 4,00.
    assert sellers_of(package) == {"B"}
    assert package.total_cents == 400


def test_a_tracked_only_seller_is_tracked_for_all_the_cards_bought_from_them() -> None:
    offers = [offer("X", "A", 100, tracked_only=True), offer("Y", "A", 100)]

    package = solved(wants("X", "Y"), offers, everywhere(LETTER, TRACKED_LETTER))

    (shipment,) = package.shipments
    assert shipment.quote.method_name == "Tracked letter"
    assert package.total_cents == 200 + 450


def test_a_card_that_outweighs_the_cheap_letter_makes_a_second_parcel_pay() -> None:
    cards = ("C1", "C2", "C3", "C4", "C5")
    offers = [offer(card, "A", 100) for card in cards] + [offer("C1", "B", 100)]

    package = solved(wants(*cards), offers, everywhere(LETTER, HEAVY_LETTER))

    # Five cards from A weigh 50 g: 5,00 + 5,00 = 10,00. Four from A, one from B: 4 + 2 + 1 + 2 = 9,00.
    assert sellers_of(package) == {"A", "B"}
    assert package.total_cents == 900
    by_seller = {s.seller: s.cards for s in package.shipments}
    assert by_seller == {"A": 4, "B": 1}


def test_a_card_without_an_offer_is_missing_and_the_rest_is_bought() -> None:
    package = solved(wants("X", "Y"), [offer("X", "A", 100)])

    assert package.missing == ("Y",)
    assert package.short == ()
    assert [p.offer.card for p in package.picks] == ["X"]
    assert package.total_cents == 300


def test_two_copies_wanted_are_bought_from_two_sellers_with_one_each() -> None:
    offers = [offer("X", "A", 100), offer("X", "B", 150)]

    package = solved([WantedCard("X", 2)], offers)

    assert sellers_of(package) == {"A", "B"}
    assert package.missing == ()
    assert package.short == ()
    assert package.total_cents == 100 + 150 + 200 + 200


def test_two_copies_from_one_seller_beat_two_parcels() -> None:
    offers = [offer("X", "A", 100), offer("X", "B", 150), offer("X", "C", 160, quantity=2)]

    package = solved([WantedCard("X", 2)], offers)

    assert sellers_of(package) == {"C"}
    assert [(p.offer.seller, p.copies) for p in package.picks] == [("C", 2)]
    assert package.total_cents == 320 + 200


def test_fewer_copies_on_offer_than_wanted_are_bought_and_reported_short() -> None:
    package = solved([WantedCard("X", 2), WantedCard("Y", 1)], [offer("X", "A", 100), offer("Y", "A", 100)])

    assert package.short == (Shortfall("X", wanted=2, available=1),)
    assert package.missing == ()
    assert sum(p.copies for p in package.picks if p.offer.card == "X") == 1
    assert package.total_cents == 200 + 200


def test_a_quantity_wanted_on_two_lines_is_added_up() -> None:
    offers = [offer("X", "A", 100, quantity=3)]

    package = solved([WantedCard("X", 1), WantedCard("x", 2)], offers)

    assert [(p.offer.seller, p.copies) for p in package.picks] == [("A", 3)]
    assert package.short == ()


def test_too_small_a_seller_cap_gives_no_package() -> None:
    offers = [offer("X", "A", 100), offer("Y", "B", 100)]

    assert solve(wants("X", "Y"), offers, everywhere(LETTER), max_sellers=1) is None
    assert solve(wants("X", "Y"), offers, everywhere(LETTER), max_sellers=0) is None

    package = solved(wants("X", "Y"), offers, max_sellers=2)
    assert sellers_of(package) == {"A", "B"}


def test_a_seller_cap_forces_the_dearer_consolidation() -> None:
    offers = [
        offer("X", "A", 500),
        offer("Y", "A", 500),
        offer("X", "B", 100),
        offer("Y", "C", 100),
    ]

    capped = solved(wants("X", "Y"), offers, max_sellers=1)
    free = solved(wants("X", "Y"), offers)

    assert sellers_of(capped) == {"A"}
    assert capped.total_cents == 1200
    assert sellers_of(free) == {"B", "C"}
    assert free.total_cents == 600


def test_a_negative_seller_cap_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="negative"):
        solve(wants("X"), [offer("X", "A", 100)], everywhere(LETTER), max_sellers=-1)


def test_a_want_is_for_at_least_one_copy() -> None:
    with pytest.raises(ValueError, match="at least one"):
        WantedCard("X", 0)


def test_unbuyable_offers_and_sellers_without_a_route_are_ignored() -> None:
    offers = [
        offer("X", "Grey", 10, buyable=False),
        offer("X", "Island", 20, country="Atlantis"),
        offer("X", "Real", 300),
    ]

    def routes(country: str) -> tuple[ShippingMethod, ...]:
        return () if country == "Atlantis" else (LETTER,)

    package = solved(wants("X"), offers, routes)

    assert sellers_of(package) == {"Real"}
    assert package.total_cents == 500


def test_a_card_only_unbuyable_offers_carry_is_missing() -> None:
    package = solved(wants("X"), [offer("X", "Grey", 10, buyable=False)])

    assert package.missing == ("X",)
    assert package.picks == ()
    assert package.total_cents == 0


def test_a_method_too_light_for_one_card_leaves_the_seller_unusable() -> None:
    feather = ShippingMethod(
        "Feather", tracked=False, letter=True, max_value_cents=2500, max_weight_g=10, price_cents=1
    )

    package = solved(wants("X"), [offer("X", "A", 100)], everywhere(feather))

    assert package.missing == ("X",)


def test_a_card_dearer_than_any_method_may_carry_is_missing() -> None:
    package = solved(wants("X"), [offer("X", "A", 2501)], everywhere(LETTER))

    assert package.missing == ("X",)


def test_offers_of_cards_nobody_wants_are_ignored() -> None:
    package = solved(wants("X"), [offer("X", "A", 100), offer("Z", "A", 1)])

    assert [p.offer.card for p in package.picks] == ["X"]


def test_a_two_faced_card_matches_across_the_slash_styles() -> None:
    package = solved(wants("Front // Back"), [offer("Front / Back", "A", 100)])

    assert package.missing == ()
    assert package.total_cents == 300


def test_a_second_printing_matches_its_card() -> None:
    package = solved(wants("Captain James T. Kirk"), [offer("Captain James T. Kirk (V.1)", "A", 100)])

    assert package.missing == ()


def test_nothing_wanted_is_an_empty_package() -> None:
    package = solved([], [offer("X", "A", 100)])

    assert package.picks == ()
    assert package.shipments == ()
    assert package.total_cents == 0


def test_the_chosen_package_does_not_depend_on_the_order_of_the_offers() -> None:
    offers = [offer("X", "A", 100), offer("Y", "A", 100), offer("X", "B", 100), offer("Y", "B", 100)]

    forward = solved(wants("X", "Y"), offers)
    backward = solved(wants("X", "Y"), offers[::-1])

    assert [(p.offer.article_id, p.copies) for p in forward.picks] == [
        (p.offer.article_id, p.copies) for p in backward.picks
    ]


def test_the_curve_shows_the_total_falling_as_sellers_are_added() -> None:
    offers = [
        offer("X", "A", 300),
        offer("Y", "A", 300),
        offer("Z", "A", 300),
        offer("X", "B", 100),
        offer("Y", "B", 100),
        offer("Z", "C", 100),
        offer("Y", "D", 50),
    ]

    steps = curve(wants("X", "Y", "Z"), offers, everywhere(LETTER), max_sellers=5)

    # One seller: A, 9,00 + 2,00. Two: B and C, 3,00 + 4,00. Three (B, C, D) costs 8,50 and is not shown.
    assert [(p.seller_count, p.total_cents) for p in steps] == [(1, 1100), (2, 700)]


def test_the_curve_leaves_out_a_count_that_saves_nothing_but_shows_a_later_one() -> None:
    offers = [
        offer("X", "Far", 100, country="Japan"),
        offer("Y", "Far", 100, country="Japan"),
        offer("Z", "Far", 100, country="Japan"),
        offer("X", "B", 50),
        offer("Y", "C", 50),
        offer("Z", "D", 50),
    ]
    parcel = ShippingMethod(
        "Parcel", tracked=True, letter=False, max_value_cents=10_000, max_weight_g=2000, price_cents=1000
    )

    def routes(country: str) -> tuple[ShippingMethod, ...]:
        return (parcel,) if country == "Japan" else (LETTER,)

    steps = curve(wants("X", "Y", "Z"), offers, routes, max_sellers=5)

    # Far alone is 3,00 + 10,00. With two sellers the best is still that (Far's two cards, 2,00 + 10,00, plus
    # one 0,50 card and its 2,00 letter is 14,50). Three letters cost 1,50 + 6,00.
    assert [(p.seller_count, p.total_cents) for p in steps] == [(1, 1300), (3, 750)]


def test_the_curve_starts_at_the_fewest_sellers_that_cover_the_wants() -> None:
    offers = [offer("X", "A", 100), offer("Y", "B", 100), offer("Z", "C", 100)]

    steps = curve(wants("X", "Y", "Z"), offers, everywhere(LETTER), max_sellers=5)

    assert [(p.seller_count, p.total_cents) for p in steps] == [(3, 900)]


def test_the_curve_stops_at_the_seller_cap() -> None:
    offers = [
        offer("X", "A", 300),
        offer("Y", "A", 300),
        offer("X", "B", 100),
        offer("Y", "C", 100),
    ]

    steps = curve(wants("X", "Y"), offers, everywhere(LETTER), max_sellers=1)

    assert [(p.seller_count, p.total_cents) for p in steps] == [(1, 800)]


def test_the_curve_is_empty_when_the_cap_is_below_what_the_wants_need() -> None:
    offers = [offer("X", "A", 100), offer("Y", "B", 100)]

    assert curve(wants("X", "Y"), offers, everywhere(LETTER), max_sellers=1) == ()


def test_the_curve_is_empty_when_nothing_can_be_bought() -> None:
    assert curve(wants("X"), [], everywhere(LETTER), max_sellers=3) == ()


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Sol Ring", "sol ring"),
        ("  SOL   Ring ", "sol ring"),
        ("Captain James T. Kirk (V.1)", "captain james t. kirk"),
        ("Captain James T. Kirk (v.12)", "captain james t. kirk"),
        ("Fire // Ice", "fire // ice"),
        ("Fire / Ice", "fire // ice"),
        ("Fire/Ice", "fire // ice"),
        ("Front // Back (V.2)", "front // back"),
        ("K'Vort-Class Raider", "k'vort-class raider"),
        ("Jace, the Mind Sculptor", "jace, the mind sculptor"),
        ("Fíli and Kíli, Joyous", "fili and kili, joyous"),
        ("Lim-Dûl's Vault", "lim-dul's vault"),
        ("Æther Vial", "aether vial"),
        ("Jötun Grunt", "jotun grunt"),
    ],
)
def test_card_key(name: str, key: str) -> None:
    assert card_key(name) == key


@pytest.mark.parametrize(
    "name", ["Fíli and Kíli, Joyous", "Lim-Dûl's Vault", "Æther Vial", "Jötun Grunt", "Fire // Ice"]
)
def test_card_key_and_card_slug_fold_a_name_alike(name: str) -> None:
    assert card_slug(name).lower() == re.sub(r"[^a-z0-9]+", "-", card_key(name).replace("'", "")).strip("-")


def test_card_key_leaves_a_parenthesis_inside_the_name_alone() -> None:
    assert card_key("Gilded Lotus (Showcase)") == "gilded lotus (showcase)"
