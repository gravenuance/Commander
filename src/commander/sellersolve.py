"""Picks which Cardmarket offers to buy: the cheapest package of cards plus shipping, from at most k sellers.

The choice is an integer programme solved exactly by scipy's HiGHS. Per offer, the number of copies bought;
per seller, whether they are used and which shipping method their parcel travels by. A method's weight and
value limits cap the cards and the worth a parcel may hold, so the model prices the seller-count against the
saving (50 cents more on a card can beat 3 € of shipping). The solver never touches the network: the
shipping methods of a seller's country come from a caller-supplied function.
"""

import importlib
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from commander.cmpages import Offer
from commander.cmshipping import ShippingMethod, ShippingQuote, cheapest, order_weight_g

log = logging.getLogger(__name__)

# One solve; past it the best package found so far is returned, flagged as not proven optimal.
SOLVE_TIME_LIMIT_S = 20.0
# All the solves of one curve together; each count gets an equal share of what is left, up to the limit above.
CURVE_BUDGET_S = 120.0

# HiGHS's status codes as scipy's milp reports them.
_HIGHS_OPTIMAL = 0
_HIGHS_LIMIT = 1
_HIGHS_INFEASIBLE = 2

# Cardmarket names a second printing "Captain James T. Kirk (V.1)"; the wants list does not.
_VERSION_SUFFIX = re.compile(r"\s*\(V\.\d+\)$", re.IGNORECASE)
# Wants are exported as "Front // Back", Cardmarket shows "Front / Back".
_FACE_SEPARATOR = re.compile(r"\s*/{1,2}\s*")
# Cardmarket's own letters for what NFKD does not split into letter and mark.
_LIGATURES = {"Æ": "Ae", "æ": "ae", "Œ": "Oe", "œ": "oe", "ß": "ss"}

Routes = Callable[[str], tuple[ShippingMethod, ...]]


class SolveTimeout(Exception):  # noqa: N818 - a condition the caller reports, not an error in the usual sense
    """The time limit passed before any package was found."""


class SolveError(Exception):
    """The solver's answer is inconsistent with the model, or HiGHS failed."""


class SolveCancelled(Exception):  # noqa: N818 - the caller's own cancel, not a failure
    """The caller's cancel check asked the curve to stop."""


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


def fold_marks(name: str) -> str:
    """The name with its accents dropped as Cardmarket drops them ("Fíli" is "Fili", "Æther" "Aether")."""
    for ligature, letters in _LIGATURES.items():
        name = name.replace(ligature, letters)
    return "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))


def card_key(name: str) -> str:
    """The key wants and offers are matched by: case-folded, without accents or a "(V.1)" suffix, and with
    " // " and " / " alike between the faces of a card."""
    unversioned = _VERSION_SUFFIX.sub("", " ".join(fold_marks(name).split()))
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


def merged_wants(wants: Iterable[WantedCard]) -> dict[str, WantedCard]:
    """The wants by `card_key`, a card wanted twice as one want of the summed quantity (first name kept)."""
    merged: dict[str, WantedCard] = {}
    for want in wants:
        key = card_key(want.name)
        earlier = merged.get(key)
        merged[key] = want if earlier is None else WantedCard(earlier.name, earlier.quantity + want.quantity)
    return merged


def _build_market(wants: Iterable[WantedCard], offers: Iterable[Offer], routes: Routes) -> _Market:
    wanted = merged_wants(wants)
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


# scipy ships no complete type stubs: every call into it stays in this function, through modules typed Any.
def _run_milp(lp: _Lp, time_limit_s: float) -> _LpResult:
    # Imported here so the app starts without loading scipy.
    np: Any = importlib.import_module("numpy")
    optimize: Any = importlib.import_module("scipy.optimize")
    sparse: Any = importlib.import_module("scipy.sparse")

    row_index = [r for r, row in enumerate(lp.rows) for _ in row.terms]
    column_index = [column for row in lp.rows for column, _ in row.terms]
    coefficients = [value for row in lp.rows for _, value in row.terms]
    matrix = sparse.coo_array((coefficients, (row_index, column_index)), shape=(len(lp.rows), len(lp.cost)))
    answer = optimize.milp(
        c=np.array(lp.cost),
        integrality=np.ones(len(lp.cost)),
        bounds=optimize.Bounds(0, np.array(lp.upper)),
        constraints=optimize.LinearConstraint(
            matrix,
            np.array([row.low for row in lp.rows]),
            np.array([row.high for row in lp.rows]),
        ),
        # A relative gap above 0 would accept packages a few cents short of the cheapest.
        options={"time_limit": time_limit_s, "mip_rel_gap": 0.0},
    )
    status = int(answer.status)
    if status == _HIGHS_INFEASIBLE:
        return _LpResult(_Status.INFEASIBLE, ())
    if status not in {_HIGHS_OPTIMAL, _HIGHS_LIMIT}:
        raise SolveError(f"HiGHS failed with status {status}: {answer.message}")
    if answer.x is None:
        return _LpResult(_Status.UNFINISHED, ())
    values = tuple(float(v) for v in answer.x)
    return _LpResult(_Status.OPTIMAL if status == _HIGHS_OPTIMAL else _Status.FEASIBLE, values)


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


@dataclass(slots=True, frozen=True)
class _Bounds:
    """Limits the model's objective is known to lie within, which lets the solver prune against them."""

    low: float = _NO_LOWER_LIMIT
    high: float = float("inf")


_NO_BOUNDS = _Bounds()
# Objectives are whole numbers; half a unit of slack keeps a bound from cutting off the package it came from.
_BOUND_SLACK = 0.5


def _cost_scale(market: _Market) -> int:
    """What a cent costs in the model's objective, which adds one unit per used seller as a tie-break; the
    scale keeps all of those units together below a cent, so fewer parcels win only between equal totals."""
    return len(market.sellers) + 1


def _objective(market: _Market, package: Package) -> float:
    """The model's objective for a package: the value of the solution the package was read from."""
    return float(package.total_cents * _cost_scale(market) + package.seller_count)


def _bounds(market: _Market, *, floor: Package | None, ceiling: Package | None) -> _Bounds:
    """Bounds from packages already found for the same market. The best package over every seller count
    (`floor`, when proven the cheapest) lies at or below any capped optimum, as a cap only removes packages.
    A package from fewer sellers (`ceiling`) is also a package for a higher cap, so the capped optimum lies at
    or below it."""
    low = _NO_LOWER_LIMIT
    if floor is not None and floor.optimal:
        low = _objective(market, floor) - _BOUND_SLACK
    high = float("inf") if ceiling is None else _objective(market, ceiling) + _BOUND_SLACK
    return _Bounds(low, high)


def _model(market: _Market, max_sellers: int | None, bounds: _Bounds = _NO_BOUNDS) -> tuple[_Lp, _Layout]:
    """The programme for a market, with the objective held within `bounds`. Costs are scaled so that the
    one-unit tie-break on each used seller is worth less than a cent: of two packages that cost the same, the
    one with fewer parcels wins."""
    candidates, sellers = market.candidates, market.sellers
    scale = _cost_scale(market)
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
    if bounds != _NO_BOUNDS:
        rows.append(_Row(tuple((v, c) for v, c in enumerate(cost) if c), bounds.low, bounds.high))
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


def _solve_market(
    market: _Market, max_sellers: int | None, time_limit_s: float, bounds: _Bounds = _NO_BOUNDS
) -> Package | None:
    if not market.needs:
        return Package((), (), market.missing, market.short, 0, 0)
    lp, layout = _model(market, max_sellers, bounds)
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


@dataclass(slots=True, frozen=True)
class CurveResult:
    packages: tuple[Package, ...]
    # Seller counts whose best package was not found within the time left and that no package settles:
    # nothing was found for them, or what was found is no better than the count before and not proven the
    # cheapest. A count between those shown may hold a better package than the rows suggest.
    timed_out: tuple[int, ...]


@dataclass(slots=True, frozen=True)
class _Budget:
    """The time all the solves of one curve share."""

    deadline_s: float
    per_solve_s: float
    clock: Callable[[], float]

    def share(self, solves_left: int) -> float:
        """The seconds for the next of `solves_left` solves: an equal part of the time left, so that time a
        quick solve leaves over goes to the later ones, and never more than one solve may take. 0 when the
        budget is spent."""
        left = self.deadline_s - self.clock()
        return min(self.per_solve_s, left / solves_left) if left > 0 else 0.0


def _solve_within(
    market: _Market, max_sellers: int | None, budget: _Budget, solves_left: int, bounds: _Bounds
) -> Package | None:
    share = budget.share(solves_left)
    if share <= 0:
        raise SolveTimeout("the curve's time budget is spent")
    return _solve_market(market, max_sellers, share, bounds)


def _poll(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise SolveCancelled("the solving was cancelled")


def _solve_uncapped(market: _Market, budget: _Budget) -> Package | None:
    """The package for any number of sellers: empty when none covers the wants, None when it times out. It may
    take up to half the budget, as nothing can be shown without it and it bounds the counts."""
    try:
        return _solve_within(market, None, budget, 2, _NO_BOUNDS) or _empty_package(market)
    except SolveTimeout as error:
        log.warning("No package found for any number of sellers: %s", error)
        return None


def _empty_package(market: _Market) -> Package:
    return Package((), (), market.missing, market.short, 0, 0)


def solve_curve(
    wants: Iterable[WantedCard],
    offers: Iterable[Offer],
    routes: Routes,
    *,
    max_sellers: int,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
    budget_s: float = CURVE_BUDGET_S,
    cancelled: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CurveResult:
    """The best package for each seller count from the smallest that can cover the wants up to `max_sellers`,
    leaving out a count whose total is no lower than the one before it. No packages when nothing can be
    bought or the wants need more sellers than `max_sellers`.

    The solves share `budget_s`, each taking an equal part of what is left and at most `time_limit_s`. A solve
    that finds nothing in its time is logged and its count reported in `timed_out`; the other counts still
    run. That holds for the uncapped solve too, and without its package the curve runs every count up to
    `max_sellers`. `cancelled` is polled before each solve and raises SolveCancelled; a solve already running
    is not interrupted, so cancelling takes up to one share. Raises SolveError when HiGHS fails.

    Each solve is bounded by the ones before it. Any package of fewer sellers also fits a higher cap, so the
    best package so far caps the total of the next count from above; and the uncapped optimum, when proven,
    bounds every capped total from below. Counts therefore run upward, and a count that cannot beat the one
    before ends as soon as the solver proves it."""
    _check_cap(max_sellers)
    market = _build_market(wants, offers, routes)
    budget = _Budget(clock() + budget_s, time_limit_s, clock)
    _poll(cancelled)
    free = _solve_uncapped(market, budget)
    if free is not None and free.seller_count == 0:
        return CurveResult((), ())
    top = max_sellers if free is None else min(max_sellers, free.seller_count)
    to_solve = [count for count in range(1, top + 1) if free is None or count != free.seller_count]
    steps: list[Package] = []
    timed_out: list[int] = []
    for count in range(1, top + 1):
        _poll(cancelled)
        if free is not None and count == free.seller_count:
            package = free
        else:
            bounds = _bounds(market, floor=free, ceiling=steps[-1] if steps else None)
            try:
                package = _solve_within(market, count, budget, len(to_solve) - to_solve.index(count), bounds)
            except SolveTimeout as error:
                log.warning("No package found for %d sellers: %s", count, error)
                timed_out.append(count)
                continue
        if package is None:
            continue
        if steps and package.total_cents >= steps[-1].total_cents:
            if not package.optimal:
                timed_out.append(count)
            continue
        steps.append(package)
        if free is not None and package.total_cents <= free.total_cents:
            break
    shown = {p.seller_count for p in steps}
    return CurveResult(tuple(steps), tuple(count for count in timed_out if count not in shown))


def curve(
    wants: Iterable[WantedCard],
    offers: Iterable[Offer],
    routes: Routes,
    *,
    max_sellers: int,
    time_limit_s: float = SOLVE_TIME_LIMIT_S,
) -> tuple[Package, ...]:
    """The packages of `solve_curve` without its timed-out counts."""
    return solve_curve(wants, offers, routes, max_sellers=max_sellers, time_limit_s=time_limit_s).packages
