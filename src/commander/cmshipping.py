"""Cardmarket's shipping tables: what a package of cards costs to send from a seller's country to the owner's.

Cardmarket sets shipping, not the seller, by origin and destination country, weight and value, plus a
packaging fee. Its help page's calculator reads two public JSON endpoints, which robots.txt allows and which
need no login: the countries with their ids, and the methods of one route. The price of a method includes the
fee and is what the buyer pays. The answers change rarely, so they are kept in the cache folder: the country
list for 30 days, a route's methods for 7.
"""

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from commander.deckcache import Clock
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_list, as_object, req_bool, req_int, req_list, req_str
from commander.net import Fetcher

log = logging.getLogger(__name__)

API_BASE = "https://help.cardmarket.com/api"
COUNTRIES_URL = f"{API_BASE}/countries"
SHIPPING_URL = f"{API_BASE}/shippingCosts"
LOCALE = "en"
PAGE_SIZE = 50
# A country list of 35 takes one page; a server that never runs out of pages is not trusted for more.
MAX_COUNTRY_PAGES = 20
COUNTRIES_FRESH_FOR = timedelta(days=30)
METHODS_FRESH_FOR = timedelta(days=7)
# Bump when the stored shape or the way it is read changes, so older files are fetched again.
CACHE_FORMAT = 1
# Cardmarket lists this entry among the countries; it is not one.
INVALID_COUNTRY_ID = 0

# Cardmarket's rule of thumb for what a letter holds: up to 4 cards 20 g, 17 cards 50 g, 40 cards 100 g.
_WEIGHT_STEPS = ((4, 20), (17, 50), (40, 100))
_HEAVY_BASE_CARDS = 40
_HEAVY_BASE_GRAMS = 100
# Per card beyond the 40, in tenths of a gram: 2.5 g.
_HEAVY_TENTHS_PER_CARD = 25
# "1.000.000,00 €": dots between thousands, a comma before two decimals, the euro sign after.
_EUROS = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+),(\d{2})\s*€")


class CmShippingError(Exception):
    """Cardmarket's help site answered with something this tool cannot use."""


@dataclass(slots=True, frozen=True)
class Country:
    id: int
    name: str


@dataclass(slots=True, frozen=True)
class ShippingMethod:
    name: str
    tracked: bool
    letter: bool
    # The most an order sent this way may be worth.
    max_value_cents: int
    max_weight_g: int
    # What the buyer pays, packaging fee included.
    price_cents: int


@dataclass(slots=True, frozen=True)
class ShippingQuote:
    method_name: str
    price_cents: int
    tracked: bool


def order_weight_g(cards: int) -> int:
    """The weight in grams Cardmarket's rule of thumb gives an order of `cards` cards: up to 4 cards fit a
    20 g letter, 17 a 50 g letter and 40 a 100 g letter; each card beyond 40 adds 2.5 g, rounded up."""
    if cards < 1:
        raise ValueError("an order holds at least one card")
    for most_cards, grams in _WEIGHT_STEPS:
        if cards <= most_cards:
            return grams
    extra_tenths = (cards - _HEAVY_BASE_CARDS) * _HEAVY_TENTHS_PER_CARD
    return _HEAVY_BASE_GRAMS + -(-extra_tenths // 10)


def cheapest(methods: Sequence[ShippingMethod], cards: int, value_cents: int) -> ShippingQuote | None:
    """The cheapest method that carries `cards` cards worth `value_cents`, or None when none does (no route,
    too heavy, too valuable). At equal prices the tracked method wins, as it insures the order, then the
    name, so the answer does not depend on the order of the table."""
    weight = order_weight_g(cards)
    fitting = [m for m in methods if m.max_weight_g >= weight and m.max_value_cents >= value_cents]
    if not fitting:
        return None
    best = min(fitting, key=lambda m: (m.price_cents, not m.tracked, m.name))
    return ShippingQuote(best.name, best.price_cents, best.tracked)


def parse_euros(text: str, where: str) -> int:
    """Cents from Cardmarket's price text: "1,30 €" is 130 and "1.000.000,00 €" is 100000000."""
    match = _EUROS.fullmatch(text.strip())
    if match is None:
        raise ShapeError(f"{where}: expected an amount in euros such as '1,30 €', got {text[:40]!r}")
    return int(match.group(1).replace(".", "")) * 100 + int(match.group(2))


def parse_countries(raw: object) -> tuple[Country, ...]:
    """The countries of an answer without Cardmarket's placeholder entry."""
    items = req_list(as_object(raw, "answer"), "items", "answer")
    countries: list[Country] = []
    for index, item in enumerate(items):
        where = f"answer.items[{index}]"
        entry = as_object(item, where)
        country_id = req_int(entry, "externalId", where)
        name = req_str(entry, "name", where)
        if country_id != INVALID_COUNTRY_ID and name:
            countries.append(Country(country_id, name))
    if not countries:
        # Cardmarket serves dozens; an empty list is a wrong answer, never kept as the truth.
        raise ShapeError("answer: no countries")
    return tuple(countries)


def parse_methods(raw: object) -> tuple[ShippingMethod, ...]:
    """The postal methods of an answer; virtual delivery is no shipment and is left out."""
    methods: list[ShippingMethod] = []
    for index, item in enumerate(as_list(raw, "answer")):
        where = f"answer[{index}]"
        entry = as_object(item, where)
        if req_bool(entry, "isVirtual", where):
            continue
        methods.append(
            ShippingMethod(
                name=req_str(entry, "name", where),
                tracked=req_bool(entry, "isTracked", where),
                letter=req_bool(entry, "isLetter", where),
                max_value_cents=parse_euros(req_str(entry, "maxValue", where), f"{where}.maxValue"),
                max_weight_g=req_int(entry, "maxWeight", where),
                price_cents=parse_euros(req_str(entry, "price", where), f"{where}.price"),
            )
        )
    return tuple(methods)


class CardmarketShipping:
    """Reads Cardmarket's countries and shipping tables, from the cache folder while they are fresh."""

    def __init__(self, fetcher: Fetcher, cache_dir: Path, *, now: Clock) -> None:
        self._fetcher = fetcher
        self._cache_dir = cache_dir
        self._now = now

    def countries(self) -> tuple[Country, ...]:
        return self._cached(
            self._cache_dir / "cm-countries.json",
            COUNTRIES_FRESH_FOR,
            self._fetch_countries,
            parse_countries,
        )

    def methods(self, origin_id: int, destination_id: int) -> tuple[ShippingMethod, ...]:
        """The methods from one country to another; empty when Cardmarket serves no such route."""
        path = self._cache_dir / f"cm-shipping-{origin_id}-{destination_id}.json"
        return self._cached(
            path,
            METHODS_FRESH_FOR,
            lambda: self._fetcher.get_json(
                SHIPPING_URL, {"locale": LOCALE, "fromCountry": origin_id, "toCountry": destination_id}
            ),
            parse_methods,
        )

    def country_id(self, name: str) -> int | None:
        """The id of the country with this name, ignoring case; None for a name Cardmarket does not list."""
        wanted = name.strip().casefold()
        return next((c.id for c in self.countries() if c.name.casefold() == wanted), None)

    def _fetch_countries(self) -> object:
        items: list[object] = []
        total = 0
        # Pages count from 0, as the help page's own script asks; page 1 of a 35-country list is empty.
        for page in range(MAX_COUNTRY_PAGES):
            answer = as_object(
                self._fetcher.get_json(COUNTRIES_URL, {"page": page, "limit": PAGE_SIZE, "locale": LOCALE}),
                "answer",
            )
            fresh = req_list(answer, "items", "answer")
            if not fresh:
                # A page past the end answers a total of 0, so it says nothing about the list's size.
                break
            total = req_int(answer, "total", "answer")
            items.extend(fresh)
            if len(items) >= total:
                return {"total": total, "items": items}
        if not items:
            raise ShapeError("answer: no countries")
        raise ShapeError(f"answer: read {len(items)} of {total} countries")

    def _cached[T](
        self,
        path: Path,
        fresh_for: timedelta,
        fetch: Callable[[], object],
        parse: Callable[[object], T],
    ) -> T:
        stored = self._read(path, fresh_for, parse)
        if stored is not None:
            return stored
        try:
            raw = fetch()
            parsed = parse(raw)
        except ShapeError as error:
            raise CmShippingError(f"unexpected answer from Cardmarket's help site: {error}") from error
        self._write(path, raw)
        return parsed

    def _read[T](self, path: Path, fresh_for: timedelta, parse: Callable[[object], T]) -> T | None:
        """The stored answer when it is current and sound; otherwise None, and it will be fetched again."""
        try:
            document = as_object(json.loads(path.read_text(encoding="utf-8")), str(path))
            if req_int(document, "format", "cache") != CACHE_FORMAT:
                return None
            age = self._now() - datetime.fromisoformat(req_str(document, "fetched_at", "cache"))
            if not timedelta(0) <= age < fresh_for:
                return None
            return parse(document.get("answer"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as error:  # unreadable, not JSON, a wrong shape, a naive time
            log.warning("Ignoring the Cardmarket shipping cache %s: %s", path.name, error)
            return None

    def _write(self, path: Path, answer: object) -> None:
        document: Mapping[str, object] = {
            "format": CACHE_FORMAT,
            "fetched_at": self._now().isoformat(),
            "answer": answer,
        }
        try:
            write_text_atomic(path, json.dumps(document, ensure_ascii=False))
        except OSError as error:
            log.warning("Cardmarket shipping answer not cached in %s: %s", path.name, error)
