"""The seller-location and delivery-time limits on scanned offers (ADR 0067)."""

import itertools
from dataclasses import replace

import pytest

from commander.cmpages import Condition, Offer
from commander.offerlimits import (
    DeliveryBasis,
    DeliveryEstimate,
    OfferLimits,
    apply_limits,
    estimate_deliveries,
    middle_days,
)

_ids = itertools.count(1)


def offer(
    seller: str,
    country: str,
    days: int | None,
    *,
    route: int | None = None,
    card: str = "Sol Ring",
    buyable: bool = True,
    price: int = 100,
) -> Offer:
    return Offer(
        article_id=next(_ids),
        card=card,
        seller=seller,
        seller_country=country,
        expansion="Any",
        condition=Condition.NM,
        language="English",
        foil=False,
        buyable=buyable,
        price_cents=price,
        quantity=1,
        delivery_days=days,
        route_days=route,
    )


def sellers(eligible_offers: tuple[Offer, ...]) -> set[str]:
    return {o.seller for o in eligible_offers}


class TestMiddleDays:
    @pytest.mark.parametrize(
        ("days", "middle"),
        [
            ([11], 11),
            ([7, 3, 9], 7),
            ([9, 1, 5, 20, 6], 6),
            ([6, 18], 12),
            ([4, 4], 4),
            # An even count takes the mean of the two middle values, rounded up to a whole day.
            ([6, 9], 8),
            ([5, 6, 7, 20], 7),
            ([20, 5, 7, 6], 7),
        ],
    )
    def test_the_median_and_for_an_even_count_the_mean_of_the_middle_two_rounded_up(
        self, days: list[int], middle: int
    ) -> None:
        assert middle_days(days) == middle

    def test_no_days_have_no_middle(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            middle_days([])


class TestEstimates:
    def test_a_seller_with_an_estimate_keeps_it(self) -> None:
        estimates = estimate_deliveries([offer("A", "Germany", 6), offer("B", "Germany", 18)])

        assert estimates == {
            "A": DeliveryEstimate(6, DeliveryBasis.OWN, "Germany"),
            "B": DeliveryEstimate(18, DeliveryBasis.OWN, "Germany"),
        }

    def test_a_seller_whose_offers_differ_has_the_slowest(self) -> None:
        estimates = estimate_deliveries([offer("A", "Germany", 6), offer("A", "Germany", 9)])

        assert estimates["A"].days == 9

    def test_a_seller_without_an_estimate_takes_the_median_of_the_sellers_from_its_country(self) -> None:
        offers = [offer("A", "Germany", 6), offer("B", "Germany", 9), offer("C", "Germany", 18)]

        estimates = estimate_deliveries([*offers, offer("D", "Germany", None)])

        assert estimates["D"] == DeliveryEstimate(9, DeliveryBasis.COUNTRY_MEDIAN, "Germany")

    def test_an_even_number_of_sellers_gives_the_rounded_up_mean_of_the_middle_two(self) -> None:
        estimates = estimate_deliveries(
            [offer("A", "Germany", 6), offer("B", "Germany", 9), offer("C", "Germany", None)]
        )

        assert estimates["C"] == DeliveryEstimate(8, DeliveryBasis.COUNTRY_MEDIAN, "Germany")

    def test_each_seller_counts_once_however_many_offers_it_has(self) -> None:
        offers = [offer("A", "Germany", 6, card=f"Card {n}") for n in range(5)]

        estimates = estimate_deliveries([*offers, offer("B", "Germany", 18), offer("C", "Germany", None)])

        # Per offer the median would be 6; per seller it is the mean of 6 and 18.
        assert estimates["C"].days == 12

    def test_only_sellers_from_the_same_country_count_whatever_its_case(self) -> None:
        offers = [offer("A", "Germany", 6), offer("B", "Austria", 14), offer("C", "GERMANY", None)]

        estimates = estimate_deliveries(offers)

        assert estimates["C"] == DeliveryEstimate(6, DeliveryBasis.COUNTRY_MEDIAN, "GERMANY")

    def test_without_peers_the_route_average_stands_in(self) -> None:
        offers = [offer("A", "Italy", None, route=14), offer("B", "Germany", 6, route=7)]

        estimates = estimate_deliveries(offers)

        assert estimates["A"] == DeliveryEstimate(14, DeliveryBasis.ROUTE_AVERAGE, "Italy")

    def test_the_route_average_of_a_country_is_shared_by_its_sellers(self) -> None:
        offers = [offer("A", "Italy", None, route=14), offer("B", "Italy", None)]

        assert estimate_deliveries(offers)["B"].days == 14

    def test_peers_come_before_the_route_average(self) -> None:
        offers = [offer("A", "Italy", None, route=14), offer("B", "Italy", 6)]

        assert estimate_deliveries(offers)["A"] == DeliveryEstimate(6, DeliveryBasis.COUNTRY_MEDIAN, "Italy")

    def test_a_seller_with_neither_has_no_estimate(self) -> None:
        offers = [offer("A", "Italy", None), offer("B", "Germany", 6, route=7)]

        assert "A" not in estimate_deliveries(offers)


class TestApplyingTheLimits:
    def test_without_limits_every_buyable_offer_stays(self) -> None:
        offers = [offer("A", "Germany", 30), offer("B", "Italy", None), offer("C", "Spain", 2, buyable=False)]

        eligible = apply_limits(offers, OfferLimits())

        assert sellers(eligible.offers) == {"A", "B"}
        assert (eligible.other_countries, eligible.too_slow, eligible.no_estimate) == (0, 0, 0)
        assert eligible.delivery["A"].days == 30
        assert "B" not in eligible.delivery

    def test_offers_from_countries_not_chosen_are_left_out_and_counted(self) -> None:
        offers = [offer("A", "Germany", 6), offer("B", "Austria", 14), offer("C", "Netherlands", 7)]

        eligible = apply_limits(offers, OfferLimits(frozenset({"germany", "netherlands"})))

        assert sellers(eligible.offers) == {"A", "C"}
        assert eligible.other_countries == 1

    def test_a_seller_arriving_in_exactly_the_limit_stays_and_a_slower_one_goes(self) -> None:
        offers = [offer("A", "Germany", 7), offer("B", "Germany", 8), offer("C", "Germany", 6)]

        eligible = apply_limits(offers, OfferLimits(max_days=7))

        assert sellers(eligible.offers) == {"A", "C"}
        assert (eligible.too_slow, eligible.no_estimate) == (1, 0)

    def test_a_seller_without_an_estimate_is_judged_by_the_median_of_its_country(self) -> None:
        offers = [
            offer("A", "Germany", 6),
            offer("B", "Germany", 18),
            offer("C", "Germany", None),
            offer("D", "Austria", 14),
            offer("E", "Austria", None),
        ]

        eligible = apply_limits(offers, OfferLimits(max_days=12))

        # C is judged at 12 days (the mean of 6 and 18) and stays; B (18) and D (14) are slower, and E,
        # judged at D's 14, is too.
        assert sellers(eligible.offers) == {"A", "C"}
        assert eligible.too_slow == 3
        assert eligible.no_estimate == 0
        assert eligible.delivery["C"].basis is DeliveryBasis.COUNTRY_MEDIAN

    def test_without_peers_a_seller_is_judged_by_the_route_average(self) -> None:
        offers = [offer("A", "Sweden", None, route=4), offer("B", "Italy", None, route=14)]

        eligible = apply_limits(offers, OfferLimits(max_days=10))

        assert sellers(eligible.offers) == {"A"}
        assert (eligible.too_slow, eligible.no_estimate) == (1, 0)

    def test_a_seller_with_neither_estimate_is_left_out_and_counted_by_offer(self) -> None:
        offers = [
            offer("A", "Germany", 6),
            offer("B", "Italy", None, card="Sol Ring"),
            offer("B", "Italy", None, card="Mind Stone"),
        ]

        eligible = apply_limits(offers, OfferLimits(max_days=14))

        assert sellers(eligible.offers) == {"A"}
        assert (eligible.too_slow, eligible.no_estimate) == (0, 2)

    def test_the_same_seller_without_a_limit_stays_with_no_estimate(self) -> None:
        eligible = apply_limits([offer("B", "Italy", None)], OfferLimits())

        assert sellers(eligible.offers) == {"B"}
        assert eligible.delivery == {}

    def test_an_offer_that_cannot_be_bought_is_no_peer(self) -> None:
        offers = [offer("A", "Germany", 30, buyable=False), offer("B", "Germany", None)]

        eligible = apply_limits(offers, OfferLimits(max_days=14))

        assert eligible.offers == ()
        assert eligible.no_estimate == 1

    def test_limits_work_together(self) -> None:
        offers = [
            offer("A", "Germany", 6),
            offer("B", "Austria", 6),
            offer("C", "Germany", 15),
            offer("D", "Germany", None),
        ]

        eligible = apply_limits(offers, OfferLimits(frozenset({"germany"}), 10))

        # D's country median is the mean of 6 and 15 rounded up, 11 days.
        assert sellers(eligible.offers) == {"A"}
        assert (eligible.other_countries, eligible.too_slow, eligible.no_estimate) == (1, 2, 0)


class TestThePriceCap:
    def test_an_offer_priced_at_exactly_the_cap_stays_and_one_cent_more_goes(self) -> None:
        offers = [
            offer("A", "Germany", 6, price=500),
            offer("B", "Germany", 6, price=501),
            offer("C", "Germany", 6, price=499),
        ]

        eligible = apply_limits(offers, OfferLimits(max_price_cents=500))

        assert sellers(eligible.offers) == {"A", "C"}
        assert [o.seller for o in eligible.over_price] == ["B"]

    def test_without_a_cap_nothing_is_over_the_price(self) -> None:
        eligible = apply_limits([offer("A", "Germany", 6, price=99_999)], OfferLimits())

        assert sellers(eligible.offers) == {"A"}
        assert eligible.over_price == ()

    def test_the_price_is_judged_per_copy_whatever_the_stock(self) -> None:
        stocked = replace(offer("A", "Germany", 6, price=300), quantity=40)

        eligible = apply_limits([stocked], OfferLimits(max_price_cents=300))

        assert sellers(eligible.offers) == {"A"}

    def test_an_offer_left_out_for_its_country_or_speed_is_not_counted_as_over_the_price(self) -> None:
        offers = [
            offer("A", "Austria", 6, price=900),
            offer("B", "Germany", 18, price=900),
            offer("C", "Germany", 6, price=900),
            offer("D", "Germany", 6, price=100),
        ]

        eligible = apply_limits(offers, OfferLimits(frozenset({"germany"}), 10, 500))

        assert sellers(eligible.offers) == {"D"}
        assert [o.seller for o in eligible.over_price] == ["C"]
        assert (eligible.other_countries, eligible.too_slow, eligible.no_estimate) == (1, 1, 0)

    def test_a_seller_that_is_too_dear_still_counts_toward_its_countrys_delivery_median(self) -> None:
        offers = [
            offer("A", "Germany", 6, price=900),
            offer("B", "Germany", 18, price=900),
            offer("C", "Germany", None, price=100),
        ]

        eligible = apply_limits(offers, OfferLimits(max_days=12, max_price_cents=500))

        # C is judged at 12 days, the mean of A's 6 and B's 18, though both of them cost too much.
        assert sellers(eligible.offers) == {"C"}
        assert eligible.delivery["C"].days == 12

    def test_an_offer_that_cannot_be_bought_is_not_over_the_price(self) -> None:
        eligible = apply_limits(
            [offer("A", "Germany", 6, price=900, buyable=False)], OfferLimits(max_price_cents=1)
        )

        assert eligible.offers == ()
        assert eligible.over_price == ()
