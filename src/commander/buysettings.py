"""What the Buy screen remembers: the offer filters, the scan's base sellers, and the decks and cards left out
of a purchase per account (ADR 0067).

One small versioned JSON file in the app's settings folder. A field that is missing or wrong falls back to its
default with a logged warning; a file that cannot be read at all is moved aside to `<name>.bak` and the
defaults used, so a later save never overwrites what the user had.
"""

import json
import logging
import re
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import cast

from commander.cmpages import Condition
from commander.cmscan import DEFAULT_BASE_SELLERS, ENGLISH, JAPANESE, Foil, ScanFilters
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_list, as_object, req_int

log = logging.getLogger(__name__)

BUY_FILE = "buy.json"
FORMAT_VERSION = 3
# The format that added the seller countries and the delivery time to the filters.
COUNTRIES_FORMAT = 2
# The format that added the price cap to the filters and the cards left out per deck.
PRICE_FORMAT = 3
MIN_BASE_SELLERS = 1
MAX_BASE_SELLERS = 40
# Cardmarket lists 35 countries; the bounds only keep nonsense out of the address.
MAX_COUNTRIES = 100
MAX_COUNTRY_ID = 9999
MAX_PRICE_CENTS = 1_000_000
NOT_AVAILABLE = "Those filters are not available; choose again."
BAD_PRICE = "Max price: 0.01 to 10000 euros, at most two decimals."
# A price as typed: whole euros, or euros and one or two decimals after a dot or a comma.
_PRICE_TEXT = re.compile(r"(\d{1,7})(?:[.,](\d{1,2}))?", re.ASCII)
# Condition choices in the file and on the page: a condition's code, or this for any.
ANY = "any"


class BuyError(Exception):
    """A request to the Buy screen that cannot be done; the message is for the user."""


class LanguageChoice(Enum):
    """Cardmarket offers many languages; the owner's decks take English, Japanese (the European market sells
    it cheaply), both, or any, which saves the most."""

    ENGLISH = "english"
    JAPANESE = "japanese"
    BOTH = "both"
    ANY = "any"

    @property
    def cardmarket_ids(self) -> tuple[int, ...]:
        """Cardmarket's language ids, sorted; empty for any, which Cardmarket reads as every language."""
        return _LANGUAGE_IDS[self]


_LANGUAGE_IDS = {
    LanguageChoice.ENGLISH: (ENGLISH,),
    LanguageChoice.JAPANESE: (JAPANESE,),
    LanguageChoice.BOTH: (ENGLISH, JAPANESE),
    LanguageChoice.ANY: (),
}


class DeliveryChoice(Enum):
    """The longest a seller may take to deliver, as the page offers it. The values are the days."""

    ANY = "any"
    FIVE_DAYS = "5"
    WEEK = "7"
    TEN_DAYS = "10"
    TWO_WEEKS = "14"

    @property
    def max_days(self) -> int | None:
        return None if self is DeliveryChoice.ANY else int(self.value)


@dataclass(slots=True, frozen=True)
class BuyFilters:
    """What a copy to buy must be. English, Near Mint or better, any finish, any seller country and any
    delivery time suit the owner's decks: a blank field is a catch-all, so only a choice narrows."""

    language: LanguageChoice = LanguageChoice.ENGLISH
    min_condition: Condition | None = Condition.NM
    foil: Foil = Foil.ANY
    # Cardmarket's country ids; empty means sellers from anywhere.
    countries: frozenset[int] = frozenset()
    delivery: DeliveryChoice = DeliveryChoice.ANY
    # The most one copy may cost, in cents; None for any price. Judged on the offers read, not by Cardmarket.
    max_price_cents: int | None = None

    def scan_filters(self) -> ScanFilters:
        return ScanFilters(
            self.language.cardmarket_ids,
            self.min_condition,
            self.foil,
            self.countries,
            self.delivery.max_days,
        )

    def to_json(self) -> dict[str, object]:
        condition = self.min_condition.value if self.min_condition is not None else ANY
        return {
            "language": self.language.value,
            "condition": condition,
            "foil": self.foil.value,
            "countries": sorted(self.countries),
            "delivery": self.delivery.value,
            "maxPriceCents": self.max_price_cents,
        }


def _check_countries(countries: object) -> frozenset[int]:
    """The country ids the page sent; anything but a short list of ids is refused."""
    if not isinstance(countries, list | tuple):
        raise BuyError(NOT_AVAILABLE)
    items = cast("Sequence[object]", countries)
    ids = [_country_id(item) for item in items]
    if len(items) > MAX_COUNTRIES or None in ids:
        raise BuyError(NOT_AVAILABLE)
    return frozenset(i for i in ids if i is not None)


def _country_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_COUNTRY_ID:
        return None
    return value


def parse_max_price(text: object) -> int | None:
    """A price typed in euros ("5", "2,50", "2.5", "€ 5") as cents; blank means any price. Anything that is
    not 0,01 to 10 000 € in at most two decimals is refused."""
    if text is None:
        return None
    if not isinstance(text, str):
        raise BuyError(BAD_PRICE)
    typed = text.replace("€", "").strip()
    if not typed:
        return None
    found = _PRICE_TEXT.fullmatch(typed)
    if found is None:
        raise BuyError(BAD_PRICE)
    euros, decimals = found.group(1), found.group(2) or ""
    return check_max_price_cents(int(euros) * 100 + int(decimals.ljust(2, "0")))


def check_max_price_cents(cents: int) -> int:
    if not 1 <= cents <= MAX_PRICE_CENTS:
        raise BuyError(BAD_PRICE)
    return cents


def parse_filters(
    language: object,
    condition: object,
    foil: object,
    countries: object,
    delivery: object,
    *,
    max_price: object = None,
) -> BuyFilters:
    """The filters the page sent; a value that is none of the choices is refused, not guessed. The price cap
    comes as the text typed in euros."""
    try:
        chosen_language = LanguageChoice(language)
        chosen_condition = None if condition == ANY else Condition(condition)
        chosen_foil = Foil(foil)
        chosen_countries = _check_countries(countries)
        chosen_delivery = DeliveryChoice(delivery)
    except ValueError:
        raise BuyError(NOT_AVAILABLE) from None
    return BuyFilters(
        chosen_language,
        chosen_condition,
        chosen_foil,
        chosen_countries,
        chosen_delivery,
        parse_max_price(max_price),
    )


def check_base_sellers(count: object) -> int:
    if isinstance(count, bool) or not isinstance(count, int):
        raise BuyError(f"Base sellers must be {MIN_BASE_SELLERS} to {MAX_BASE_SELLERS}.")
    if not MIN_BASE_SELLERS <= count <= MAX_BASE_SELLERS:
        raise BuyError(f"Base sellers must be {MIN_BASE_SELLERS} to {MAX_BASE_SELLERS}.")
    return count


@dataclass(slots=True, frozen=True)
class BuySettings:
    filters: BuyFilters = field(default_factory=BuyFilters)
    base_sellers: int = DEFAULT_BASE_SELLERS
    # Deck ids left out of the purchase whole, by account (casefolded user name).
    left_out: Mapping[str, frozenset[int]] = field(default_factory=dict[str, frozenset[int]])
    # The cards left out of a deck that is otherwise bought, by account and then deck id.
    left_out_cards: Mapping[str, Mapping[int, frozenset[str]]] = field(
        default_factory=dict[str, Mapping[int, frozenset[str]]]
    )


class BuyStore:
    """The settings, read from the file once and written through on every change."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._settings: BuySettings | None = None

    def filters(self) -> BuyFilters:
        return self._current().filters

    def base_sellers(self) -> int:
        return self._current().base_sellers

    def left_out(self, user: str) -> frozenset[int]:
        return self._current().left_out.get(_account(user), frozenset())

    def left_out_cards(self, user: str) -> Mapping[int, frozenset[str]]:
        return self._current().left_out_cards.get(_account(user), {})

    def set_filters(self, filters: BuyFilters) -> None:
        self._change(lambda settings: replace(settings, filters=filters))

    def set_base_sellers(self, count: int) -> None:
        self._change(lambda settings: replace(settings, base_sellers=check_base_sellers(count)))

    def set_left_out(self, user: str, deck_ids: frozenset[int], cards: Mapping[int, frozenset[str]]) -> None:
        """Replace the account's left-out decks and, by deck, its left-out cards; empty sets are dropped."""
        account = _account(user)

        def change(settings: BuySettings) -> BuySettings:
            left = {name: ids for name, ids in settings.left_out.items() if name != account}
            if deck_ids:
                left[account] = deck_ids
            kept = {deck: names for deck, names in cards.items() if names}
            left_cards = {n: by_deck for n, by_deck in settings.left_out_cards.items() if n != account}
            if kept:
                left_cards[account] = kept
            return replace(settings, left_out=left, left_out_cards=left_cards)

        self._change(change)

    def _current(self) -> BuySettings:
        with self._lock:
            if self._settings is None:
                self._settings = self._read()
            return self._settings

    def _change(self, change: Callable[[BuySettings], BuySettings]) -> None:
        with self._lock:
            if self._settings is None:
                self._settings = self._read()
            changed = change(self._settings)
            if changed == self._settings:
                return
            try:
                write_text_atomic(self._path, _dump(changed))
            except OSError as error:
                log.warning("could not save the Buy settings to %s: %s", self._path, error)
                raise BuyError("Could not save your choices; see the log.") from error
            self._settings = changed

    def _read(self) -> BuySettings:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return BuySettings()
        except (OSError, UnicodeDecodeError) as error:
            log.warning("could not read the Buy settings in %s: %s; using the defaults", self._path, error)
            return BuySettings()
        try:
            settings, migrated_from = _parse(text)
        except (json.JSONDecodeError, ShapeError) as error:
            backup = self._path.with_name(self._path.name + ".bak")
            log.warning(
                "the Buy settings in %s are unusable (%s); kept as %s, using the defaults",
                self._path,
                error,
                backup.name,
            )
            try:
                self._path.replace(backup)
            except OSError as move_error:
                log.warning("could not move %s aside: %s", self._path, move_error)
            return BuySettings()
        if migrated_from is not None:
            self._back_up(migrated_from)
        return settings

    def _back_up(self, version: int) -> None:
        """Keep the file as it was before its migration, and only the latest such copy."""
        backup = self._path.with_name(f"{self._path.name}.v{version}.bak")
        try:
            for old in self._path.parent.glob(f"{self._path.name}.v*.bak"):
                if old != backup:
                    old.unlink()
            shutil.copy2(self._path, backup)
        except OSError as error:
            log.warning("could not keep %s as %s: %s", self._path, backup.name, error)
            return
        log.info("the Buy settings were format %d; kept as %s", version, backup.name)


def _account(user: str) -> str:
    return user.casefold()


def _migrate(raw: Mapping[str, object]) -> tuple[Mapping[str, object], int]:
    """Bring an older file to the current format, and say which format it was."""
    version = req_int(raw, "version", BUY_FILE)
    if not 1 <= version <= FORMAT_VERSION:
        raise ShapeError(f"{BUY_FILE}: format version {version}, this build reads 1 to {FORMAT_VERSION}")
    migrated = dict(raw)
    # A filters value that is no object is left as it is; `_filters` warns about it and uses the defaults.
    if version < COUNTRIES_FORMAT and isinstance(filters := raw.get("filters"), dict):
        migrated["filters"] = {**cast("dict[str, object]", filters), "countries": [], "delivery": "any"}
    if version < PRICE_FORMAT:
        if isinstance(filters := migrated.get("filters"), dict):
            migrated["filters"] = {**cast("dict[str, object]", filters), "maxPriceCents": None}
        migrated["leftOutCards"] = {}
    migrated["version"] = FORMAT_VERSION
    return migrated, version


def _parse(text: str) -> tuple[BuySettings, int | None]:
    """The settings, and the format the file was in when that was older than this build's."""
    raw, version = _migrate(as_object(json.loads(text), BUY_FILE))
    return _bind(raw), None if version == FORMAT_VERSION else version


def _bind(raw: Mapping[str, object]) -> BuySettings:
    defaults = BuySettings()
    filters = _filters(raw.get("filters"), defaults.filters)
    base = raw.get("baseSellers", defaults.base_sellers)
    try:
        base_sellers = check_base_sellers(base)
    except BuyError:
        log.warning("%s: base sellers %r is out of range; using %d", BUY_FILE, base, defaults.base_sellers)
        base_sellers = defaults.base_sellers
    return BuySettings(
        filters, base_sellers, _left_out(raw.get("leftOut")), _left_out_cards(raw.get("leftOutCards"))
    )


def _filters(raw: object, defaults: BuyFilters) -> BuyFilters:
    fields = _object_field(raw, "filters")
    if fields is None:
        return defaults
    return BuyFilters(
        _choice(LanguageChoice, fields, "language", defaults.language),
        _condition(fields.get("condition"), defaults.min_condition),
        _choice(Foil, fields, "foil", defaults.foil),
        _countries(fields.get("countries")),
        _choice(DeliveryChoice, fields, "delivery", defaults.delivery),
        _max_price(fields.get("maxPriceCents")),
    )


def _object_field(raw: object, where: str) -> Mapping[str, object] | None:
    """The object at `where`; None, with a warning, when it is missing or misshaped."""
    if raw is None:
        return None
    try:
        return as_object(raw, where)
    except ShapeError as error:
        log.warning("%s: %s; it is dropped", BUY_FILE, error)
        return None


def _list_field(raw: object, where: str) -> Sequence[object] | None:
    """The list at `where`; None, with a warning, when it is misshaped."""
    try:
        return as_list(raw, where)
    except ShapeError as error:
        log.warning("%s: %s; it is dropped", BUY_FILE, error)
        return None


def _max_price(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_PRICE_CENTS:
        return value
    log.warning("%s: filters.maxPriceCents %r is no price; using any price", BUY_FILE, value)
    return None


def _countries(raw: object) -> frozenset[int]:
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        log.warning("%s: filters.countries %r is no list; using any country", BUY_FILE, raw)
        return frozenset()
    ids = [_country_id(item) for item in cast("list[object]", raw)[:MAX_COUNTRIES]]
    if None in ids:
        log.warning("%s: filters.countries holds values that are no country id; they are dropped", BUY_FILE)
    return frozenset(i for i in ids if i is not None)


def _choice[E: Enum](kind: type[E], fields: Mapping[str, object], key: str, default: E) -> E:
    value = fields.get(key)
    if value is None:
        return default
    try:
        return kind(value)
    except ValueError:
        log.warning("%s: filters.%s %r is not a choice; using %s", BUY_FILE, key, value, default.value)
        return default


def _condition(value: object, default: Condition | None) -> Condition | None:
    if value is None:
        return default
    if value == ANY:
        return None
    try:
        return Condition(value)
    except ValueError:
        log.warning("%s: filters.condition %r is not a condition; using the default", BUY_FILE, value)
        return default


def _left_out(raw: object) -> dict[str, frozenset[int]]:
    accounts = _object_field(raw, "leftOut")
    left: dict[str, frozenset[int]] = {}
    for name, ids in (accounts or {}).items():
        numbers = _list_field(ids, f"leftOut.{name}")
        if numbers is None:
            continue
        valid = [n for n in numbers if isinstance(n, int) and not isinstance(n, bool)]
        if len(valid) != len(numbers):
            log.warning("%s: leftOut.%s holds values that are no deck id; they are dropped", BUY_FILE, name)
        if valid:
            left[name] = frozenset(valid)
    return left


def _left_out_cards(raw: object) -> dict[str, Mapping[int, frozenset[str]]]:
    accounts = _object_field(raw, "leftOutCards")
    left: dict[str, Mapping[int, frozenset[str]]] = {}
    for account, decks in (accounts or {}).items():
        by_deck: dict[int, frozenset[str]] = {}
        for deck, names in (_object_field(decks, f"leftOutCards.{account}") or {}).items():
            where = f"leftOutCards.{account}.{deck}"
            cards = _list_field(names, where)
            if cards is None:
                continue
            valid = frozenset(n for n in cards if isinstance(n, str) and n)
            if len(valid) != len(cards):
                log.warning("%s: %s holds values that are no card name; they are dropped", BUY_FILE, where)
            if not (deck.isascii() and deck.isdigit()):
                log.warning("%s: %s is no deck id; it is dropped", BUY_FILE, where)
            elif valid:
                by_deck[int(deck)] = valid
        if by_deck:
            left[account] = by_deck
    return left


def _dump(settings: BuySettings) -> str:
    document: dict[str, object] = {
        "version": FORMAT_VERSION,
        "filters": settings.filters.to_json(),
        "baseSellers": settings.base_sellers,
        "leftOut": {name: sorted(ids) for name, ids in sorted(settings.left_out.items())},
        "leftOutCards": {
            account: {str(deck): sorted(names) for deck, names in sorted(by_deck.items())}
            for account, by_deck in sorted(settings.left_out_cards.items())
        },
    }
    return json.dumps(document, indent=1) + "\n"
