"""Which scanned offers the owner's seller-location and delivery-time filters allow (ADR 0067).

Cardmarket's own filters cannot say everything the owner wants: a delivery limit of 10 or 14 days has no
choice in its form, and a seller's country is not checked on the wants-filtered stock pages. So the scan and
the solver both judge the offers it read with `apply_limits`.

A seller shows "-" instead of an estimate when it has too few deliveries. Under a delivery limit such a seller
is judged by its country: the median of the other sellers' estimates from that country in the same scan, else
Cardmarket's average for the route that its card-page rows carry. Only an offer with neither is left out.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum

from commander.cmpages import Offer


class DeliveryBasis(Enum):
    """Where a seller's days come from."""

    OWN = "own"
    COUNTRY_MEDIAN = "country"
    ROUTE_AVERAGE = "route"


@dataclass(slots=True, frozen=True)
class DeliveryEstimate:
    days: int
    basis: DeliveryBasis
    # The seller's country as Cardmarket spells it; the name the country-based estimates are made for.
    country: str


@dataclass(slots=True, frozen=True)
class OfferLimits:
    """Empty `countries` and no `max_days` allow everything. Country names are case-folded."""

    countries: frozenset[str] = frozenset()
    max_days: int | None = None


@dataclass(slots=True, frozen=True)
class Eligible:
    """The offers the limits allow, with what was left out and why."""

    offers: tuple[Offer, ...]
    # The estimate of every seller that has one, whether or not its offers stayed.
    delivery: Mapping[str, DeliveryEstimate] = field(default_factory=dict[str, DeliveryEstimate])
    other_countries: int = 0
    too_slow: int = 0
    no_estimate: int = 0


def middle_days(days: Iterable[int]) -> int:
    """The median of the days; of an even count, the mean of the two middle values rounded up to a whole day,
    so the estimate never promises sooner than the sellers do."""
    ordered = sorted(days)
    if not ordered:
        raise ValueError("a median needs at least one value")
    half = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[half]
    return (ordered[half - 1] + ordered[half] + 1) // 2


def estimate_deliveries(offers: Iterable[Offer]) -> dict[str, DeliveryEstimate]:
    """Each seller's days to the buyer, from the offers read: its own estimate (the largest, should its
    offers differ), else the median of the other sellers from its country, else the route average."""
    country_of: dict[str, str] = {}
    own: dict[str, int] = {}
    route: dict[str, int] = {}
    for offer in offers:
        country = country_of.setdefault(offer.seller, offer.seller_country)
        if offer.delivery_days is not None:
            own[offer.seller] = max(own.get(offer.seller, 0), offer.delivery_days)
        if offer.route_days is not None:
            route[country.casefold()] = max(route.get(country.casefold(), 0), offer.route_days)
    peers: dict[str, list[int]] = defaultdict(list)
    for seller, days in own.items():
        peers[country_of[seller].casefold()].append(days)
    estimates: dict[str, DeliveryEstimate] = {}
    for seller, country in country_of.items():
        key = country.casefold()
        if seller in own:
            estimates[seller] = DeliveryEstimate(own[seller], DeliveryBasis.OWN, country)
        elif peers[key]:
            median = middle_days(peers[key])
            estimates[seller] = DeliveryEstimate(median, DeliveryBasis.COUNTRY_MEDIAN, country)
        elif key in route:
            estimates[seller] = DeliveryEstimate(route[key], DeliveryBasis.ROUTE_AVERAGE, country)
    return estimates


def apply_limits(offers: Iterable[Offer], limits: OfferLimits) -> Eligible:
    """The buyable offers from the allowed countries whose seller arrives within `limits.max_days`."""
    buyable = [o for o in offers if o.buyable]
    in_country = buyable
    if limits.countries:
        in_country = [o for o in buyable if o.seller_country.casefold() in limits.countries]
    other_countries = len(buyable) - len(in_country)
    delivery = estimate_deliveries(in_country)
    if limits.max_days is None:
        return Eligible(tuple(in_country), delivery, other_countries)
    kept: list[Offer] = []
    too_slow = no_estimate = 0
    for offer in in_country:
        estimate = delivery.get(offer.seller)
        if estimate is None:
            no_estimate += 1
        elif estimate.days > limits.max_days:
            too_slow += 1
        else:
            kept.append(offer)
    return Eligible(tuple(kept), delivery, other_countries, too_slow, no_estimate)
