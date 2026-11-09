"""Picks which Cardmarket offers to buy: the cheapest package of cards plus shipping, from at most k sellers.

The choice is an integer programme solved exactly by scipy's HiGHS. Per offer, the number of copies bought;
per seller, whether they are used and which shipping method their parcel travels by. A method's weight and
value limits cap the cards and the worth a parcel may hold, so the model prices the seller-count against the
saving (50 cents more on a card can beat 3 € of shipping). The solver never touches the network: the
shipping methods of a seller's country come from a caller-supplied function.
"""

import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from commander.cmpages import Offer
from commander.cmshipping import ShippingMethod, ShippingQuote, cheapest, order_weight_g

log = logging.getLogger(__name__)

# One solve; past it the best package found so far is returned, flagged as not proven optimal.
SOLVE_TIME_LIMIT_S = 20.0

# Cardmarket names a second printing "Captain James T. Kirk (V.1)"; the wants list does not.
_VERSION_SUFFIX = re.compile(r"\s*\(V\.\d+\)$", re.IGNORECASE)
# Wants are exported as "Front // Back", Cardmarket shows "Front / Back".
_FACE_SEPARATOR = re.compile(r"\s*/{1,2}\s*")

Routes = Callable[[str], tuple[ShippingMethod, ...]]


class SolveTimeout(Exception):  # noqa: N818 - a condition the caller reports, not an error in the usual sense
    """The time limit passed before any package was found."""


class SolveError(Exception):
    """The solver's answer is inconsistent with the model, or HiGHS failed."""


@dataclass(slots=True, frozen=True)
class WantedCard:
    name: str
    quantity: int

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError(f"{self.name}: a want is for at least one copy, got {self.quantity}")


@dataclass(slots=True, frozen=True)
class Pick:
    offer: Offer
    copies: int


@dataclass(slots=True, frozen=True)
class Shipment:
    """One seller's parcel, with the cheapest valid method for what it holds."""

    seller: str
    country: str
    cards: int
    value_cents: int
    quote: ShippingQuote


@dataclass(slots=True, frozen=True)
class Shortfall:
    """A wanted card of which fewer copies than wanted are on offer."""

    card: str
    wanted: int
    available: int


@dataclass(slots=True, frozen=True)
class Package:
    picks: tuple[Pick, ...]
    shipments: tuple[Shipment, ...]
    # Wanted cards without a usable offer, by the name the want gave.
    missing: tuple[str, ...]
    short: tuple[Shortfall, ...]
    cards_cents: int
    shipping_cents: int
    # False when the time limit stopped the search before the package was proven the cheapest.
    optimal: bool = True

    @property
    def total_cents(self) -> int:
        return self.cards_cents + self.shipping_cents

    @property
    def seller_count(self) -> int:
        return len(self.shipments)


def card_key(name: str) -> str:
    """The key wants and offers are matched by: case-folded, without a "(V.1)" suffix, and with " // " and
    " / " alike between the faces of a card."""
    unversioned = _VERSION_SUFFIX.sub("", " ".join(name.split()))
    return " // ".join(_FACE_SEPARATOR.split(unversioned)).casefold()


# --- the market the model is built from -------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Tier:
    """A shipping method as the model uses it: how many cards it carries, besides its value limit."""

    method: ShippingMethod
    card_cap: int


@dataclass(slots=True, frozen=True)
class _Candidate:
    offer: Offer
    key: str
    # Most copies worth buying: the stock, the wanted copies, and what the seller's best method can carry.
    most: int


@dataclass(slots=True, frozen=True)
class _Seller:
    name: str
    country: str
    # Every method this seller may use; the pruned tiers are what the model chooses between.
    methods: tuple[ShippingMethod, ...]
    tiers: tuple[_Tier, ...]
    candidates: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class _Market:
    candidates: tuple[_Candidate, ...]
    sellers: tuple[_Seller, ...]
    # Copies to buy of each wanted card that has a usable offer, by card key.
    needs: Mapping[str, int]
    missing: tuple[str, ...]
    short: tuple[Shortfall, ...]


def _card_capacity(method: ShippingMethod, most_cards: int) -> int:
    """The most cards (up to `most_cards`) whose weight the method carries."""
    cards = 0
    while cards < most_cards and order_weight_g(cards + 1) <= method.max_weight_g:
        cards += 1
    return cards


def _tiers(methods: Iterable[ShippingMethod], *, tracked_only: bool, most_cards: int) -> tuple[_Tier, ...]:
    """The methods worth choosing between: without those that carry no card, and without any that another
    method beats on cards, value and price."""
    candidates = [
        _Tier(method, _card_capacity(method, most_cards))
        for method in methods
        if method.tracked or not tracked_only
    ]
    candidates = [tier for tier in candidates if tier.card_cap > 0]
    candidates.sort(
        key=lambda t: (
            t.method.price_cents,
            -t.card_cap,
            -t.method.max_value_cents,
            not t.method.tracked,
            t.method.name,
        )
    )
    kept: list[_Tier] = []
    for tier in candidates:
        beaten = any(
            k.card_cap >= tier.card_cap and k.method.max_value_cents >= tier.method.max_value_cents
            for k in kept
        )
        if not beaten:
            kept.append(tier)
    return tuple(kept)


def _merged_wants(wants: Iterable[WantedCard]) -> dict[str, WantedCard]:
    merged: dict[str, WantedCard] = {}
    for want in wants:
        key = card_key(want.name)
        earlier = merged.get(key)
        merged[key] = want if earlier is None else WantedCard(earlier.name, earlier.quantity + want.quantity)
    return merged


def _build_market(wants: Iterable[WantedCard], offers: Iterable[Offer], routes: Routes) -> _Market:
    wanted = _merged_wants(wants)
    most_cards = sum(w.quantity for w in wanted.values())
    stocked = sorted(
        (o for o in offers if o.buyable and o.quantity > 0 and card_key(o.card) in wanted),
        key=lambda o: (o.seller, card_key(o.card), o.price_cents, o.article_id),
    )
    tracked_sellers = {o.seller for o in stocked if o.tracked_only}
    methods_by_country: dict[str, tuple[ShippingMethod, ...]] = {}
    country_of: dict[str, str] = {}
    tiers_of: dict[str, tuple[_Tier, ...]] = {}
    candidates: list[_Candidate] = []
    members: dict[str, list[int]] = {}
    for offer in stocked:
        country = country_of.setdefault(offer.seller, offer.seller_country)
        if offer.seller not in tiers_of:
            if country not in methods_by_country:
                methods_by_country[country] = routes(country)
            tiers_of[offer.seller] = _tiers(
                methods_by_country[country],
                tracked_only=offer.seller in tracked_sellers,
                most_cards=most_cards,
            )
        tiers = tiers_of[offer.seller]
        if not tiers:
            continue
        key = card_key(offer.card)
        most = min(offer.quantity, wanted[key].quantity)
        if offer.price_cents > 0:
            most = min(most, max(t.method.max_value_cents for t in tiers) // offer.price_cents)
        if most < 1:
            continue
        members.setdefault(offer.seller, []).append(len(candidates))
        candidates.append(_Candidate(offer, key, most))
    sellers = tuple(
        _Seller(
            name=name,
            country=country_of[name],
            methods=tuple(t.method for t in tiers_of[name]),
            tiers=tiers_of[name],
            candidates=tuple(indices),
        )
        for name, indices in members.items()
    )
    available = dict.fromkeys(wanted, 0)
    for candidate in candidates:
        available[candidate.key] += candidate.most
    return _Market(
        candidates=tuple(candidates),
        sellers=sellers,
        needs={key: min(w.quantity, available[key]) for key, w in wanted.items() if available[key] > 0},
        missing=tuple(w.name for key, w in wanted.items() if available[key] == 0),
        short=tuple(
            Shortfall(w.name, w.quantity, available[key])
            for key, w in wanted.items()
            if 0 < available[key] < w.quantity
        ),
    )


# --- the integer programme --------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _Row:
    """One linear constraint: low <= sum of coefficient * variable <= high."""

    terms: tuple[tuple[int, float], ...]
    low: float
    high: float


@dataclass(slots=True, frozen=True)
class _Lp:
    """Minimise sum(cost * x) over integer x in [0, upper] subject to the rows."""

    cost: tuple[float, ...]
    upper: tuple[float, ...]
    rows: tuple[_Row, ...]


class _Status(Enum):
    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    UNFINISHED = "unfinished"


@dataclass(slots=True, frozen=True)
class _LpResult:
    status: _Status
    values: tuple[float, ...]


# scipy ships no complete type stubs: every call into it stays in this one function.
def _run_milp(lp: _Lp, time_limit_s: float) -> _LpResult:
    import numpy as np  # noqa: PLC0415 - deferred so the app starts without loading scipy
    from scipy.optimize import Bounds, LinearConstraint, milp  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs, reportUnknownVariableType] # no scipy stubs
    from scipy.sparse import coo_array  # noqa: PLC0415  # pyright: ignore[reportMissingTypeStubs] # no scipy stubs

    row_index = [r for r, row in enumerate(lp.rows) for _ in row.terms]
    column_index = [column for row in lp.rows for column, _ in row.terms]
    coefficients = [value for row in lp.rows for _, value in row.terms]
    matrix = coo_array((coefficients, (row_index, column_index)), shape=(len(lp.rows), len(lp.cost)))  # pyright: ignore[reportUnknownVariableType] # untyped scipy.sparse
    answer = milp(  # pyright: ignore[reportUnknownVariableType] # untyped scipy.optimize
        c=np.array(lp.cost),
        integrality=np.ones(len(lp.cost)),
        bounds=Bounds(0, np.array(lp.upper)),  # pyright: ignore[reportArgumentType] # takes arrays, typed float
        constraints=LinearConstraint(  # pyright: ignore[reportUnknownArgumentType] # untyped scipy.sparse
            matrix,
            np.array([row.low for row in lp.rows]),  # pyright: ignore[reportArgumentType] # takes arrays
            np.array([row.high for row in lp.rows]),  # pyright: ignore[reportArgumentType] # takes arrays
        ),
        # A relative gap above 0 would accept packages a few cents short of the cheapest.
        options={"time_limit": time_limit_s, "mip_rel_gap": 0.0},
    )
    status = int(answer.status)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType] # untyped result
    solution = answer.x  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] # untyped result
    if status == 2:  # noqa: PLR2004 - HiGHS's code for infeasible
        return _LpResult(_Status.INFEASIBLE, ())
    if status not in {0, 1}:
        raise SolveError(f"HiGHS failed with status {status}: {answer.message}")  # pyright: ignore[reportUnknownMemberType] # untyped result
    if solution is None:
        return _LpResult(_Status.UNFINISHED, ())
    values = tuple(float(v) for v in solution)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType] # untyped result
    return _LpResult(_Status.OPTIMAL if status == 0 else _Status.FEASIBLE, values)


@dataclass(slots=True, frozen=True)
class _Layout:
    """Where the variables sit: copies per candidate first, then "seller used", then each seller's methods."""

    used_at: int
    first_tier: tuple[int, ...]


_NO_LOWER_LIMIT = float("-inf")


def _seller_rows(market: _Market, j: int, layout: _Layout) -> list[_Row]:
    seller = market.sellers[j]
    candidates = market.candidates
    used = layout.used_at + j
    tiers = list(enumerate(seller.tiers, start=layout.first_tier[j]))
    # Copies come only from a used seller.
    rows = [
        _Row(((i, 1.0), (used, -float(candidates[i].most))), _NO_LOWER_LIMIT, 0) for i in seller.candidates
    ]
    # A used seller ships by exactly one method.
    rows.append(_Row(((used, -1.0), *((t, 1.0) for t, _ in tiers)), 0, 0))
    # The parcel holds no more cards and no more worth than that method allows.
    cards = tuple((i, 1.0) for i in seller.candidates)
    worth = tuple((i, float(candidates[i].offer.price_cents)) for i in seller.candidates)
    rows.append(_Row((*cards, *((t, -float(tier.card_cap)) for t, tier in tiers)), _NO_LOWER_LIMIT, 0))
    limits = ((t, -float(tier.method.max_value_cents)) for t, tier in tiers)
    rows.append(_Row((*worth, *limits), _NO_LOWER_LIMIT, 0))
    return rows


def _model(market: _Market, max_sellers: int | None) -> tuple[_Lp, _Layout]:
    """The programme for a market. Costs are scaled so that the one-unit tie-break on each used seller is
    worth less than a cent: of two packages that cost the same, the one with fewer parcels wins."""
    candidates, sellers = market.candidates, market.sellers
    scale = len(sellers) + 1
    cost: list[float] = [c.offer.price_cents * scale for c in candidates]
    upper: list[float] = [c.most for c in candidates]
    used_at = len(cost)
    cost.extend(1 for _ in sellers)
    upper.extend(1 for _ in sellers)
    first_tier: list[int] = []
    for seller in sellers:
        first_tier.append(len(cost))
        cost.extend(t.method.price_cents * scale for t in seller.tiers)
        upper.extend(1 for _ in seller.tiers)
    layout = _Layout(used_at, tuple(first_tier))

    by_key: dict[str, list[int]] = {}
    for i, candidate in enumerate(candidates):
        by_key.setdefault(candidate.key, []).append(i)
    rows = [_Row(tuple((i, 1.0) for i in by_key[key]), need, need) for key, need in market.needs.items()]
    for j in range(len(sellers)):
        rows.extend(_seller_rows(market, j, layout))
    if max_sellers is not None:
        rows.append(_Row(tuple((used_at + j, 1.0) for j in range(len(sellers))), 0, max_sellers))
    return _Lp(tuple(cost), tuple(upper), tuple(rows)), layout


def _shipment(seller: _Seller, picks: Sequence[Pick], tier_price_cents: int) -> Shipment:
    cards = sum(p.copies for p in picks)
    value = sum(p.copies * p.offer.price_cents for p in picks)
    quote = cheapest(seller.methods, cards, value)
    if quote is None or quote.price_cents > tier_price_cents:
        raise SolveError(
            f"{seller.name}: {cards} cards worth {value} cents have no method at the {tier_price_cents} "
            f"cents the model chose, the cheapest being {quote}"
        )
    return Shipment(seller.name, seller.country, cards, value, quote)


def _package(market: _Market, layout: _Layout, values: Sequence[float], *, optimal: bool) -> Package:
    picks: list[Pick] = []
    shipments: list[Shipment] = []
    for j, seller in enumerate(market.sellers):
        copies = [(i, round(values[i])) for i in seller.candidates]
        bought = [Pick(market.candidates[i].offer, n) for i, n in copies if n > 0]
        if not bought:
            continue
        first = layout.first_tier[j]
        chosen = max(range(len(seller.tiers)), key=lambda t, first=first: values[first + t])
        shipments.append(_shipment(seller, bought, seller.tiers[chosen].method.price_cents))
        picks.extend(bought)
    return Package(
        picks=tuple(picks),
        shipments=tuple(shipments),
        missing=market.missing,
        short=market.short,
        cards_cents=sum(p.copies * p.offer.price_cents for p in picks),
        shipping_cents=sum(s.quote.price_cents for s in shipments),
        optimal=optimal,
    )


def _solve_market(market: _Market, max_sellers: int | None, time_limit_s: float) -> Package | None:
    if not market.needs:
        return Package((), (), market.missing, market.short, 0, 0)
    lp, layout = _model(market, max_sellers)
    result = _run_milp(lp, time_limit_s)
    match result.status:
        case _Status.INFEASIBLE:
            return None
        case _Status.UNFINISHED:
            raise SolveTimeout(f"no package found within {time_limit_s:g} s")
        case _Status.OPTIMAL | _Status.FEASIBLE:
            return _package(market, layout, result.values, optimal=result.status is _Status.OPTIMAL)


def _check_cap(max_sellers: int | None) -> None:
    if max_sellers is not None and max_sellers < 0:
        raise ValueError(f"max_sellers cannot be negative, got {max_sellers}")


def solve(
    wants: Iterable[WantedCard],
    offers: Iterable[Offer],
    routes: Routes,
    *,
    max_sellers: int | None,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
) -> Package | None:
    """The cheapest package covering every wanted card that has a usable offer, from at most `max_sellers`
    sellers (any number when None). None when no package fits the cap. Only buyable offers count, and a seller
    whose country has no route to the buyer is unusable. Raises SolveTimeout when the time limit passes before
    any package is found; a package found but not proven cheapest comes back with `optimal` False."""
    _check_cap(max_sellers)
    return _solve_market(_build_market(wants, offers, routes), max_sellers, time_limit_s)


def curve(
    wants: Iterable[WantedCard],
    offers: Iterable[Offer],
    routes: Routes,
    *,
    max_sellers: int,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
) -> tuple[Package, ...]:
    """The best package for each seller count from the smallest that can cover the wants up to `max_sellers`,
    leaving out a count whose total is no lower than the one before it. Empty when nothing can be bought or
    the wants need more sellers than `max_sellers`. A count whose solve times out is left out."""
    _check_cap(max_sellers)
    market = _build_market(wants, offers, routes)
    free = _solve_market(market, None, time_limit_s)
    if free is None or free.seller_count == 0:
        return ()
    steps: list[Package] = []
    for count in range(1, min(max_sellers, free.seller_count) + 1):
        try:
            package = free if count == free.seller_count else _solve_market(market, count, time_limit_s)
        except SolveTimeout:
            log.warning("No package found for %d sellers within %g s", count, time_limit_s)
            continue
        if package is None or (steps and package.total_cents >= steps[-1].total_cents):
            continue
        steps.append(package)
        if package.total_cents <= free.total_cents:
            break
    return tuple(steps)
