"""What the Buy screen remembers: the offer filters, the scan's base sellers, and the decks left out of a
purchase per account (ADR 0067).

One small versioned JSON file in the app's settings folder. A field that is missing or wrong falls back to its
default with a logged warning; a file that cannot be read at all is moved aside to `<name>.bak` and the
defaults used, so a later save never overwrites what the user had.
"""

import json
import logging
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import cast

from commander.cmpages import Condition
from commander.cmscan import DEFAULT_BASE_SELLERS, ENGLISH, Foil, ScanFilters
from commander.fsutil import write_text_atomic
from commander.jsonshape import ShapeError, as_list, as_object, req_int

log = logging.getLogger(__name__)

BUY_FILE = "buy.json"
FORMAT_VERSION = 2
# The format that added the seller countries and the delivery time to the filters.
COUNTRIES_FORMAT = 2
MIN_BASE_SELLERS = 1
MAX_BASE_SELLERS = 40
# Cardmarket lists 35 countries; the bounds only keep nonsense out of the address.
MAX_COUNTRIES = 100
MAX_COUNTRY_ID = 9999
NOT_AVAILABLE = "Those filters are not available; choose again."
# Condition choices in the file and on the page: a condition's code, or this for any.
ANY = "any"


class BuyError(Exception):
    """A request to the Buy screen that cannot be done; the message is for the user."""


class LanguageChoice(Enum):
    """Cardmarket offers many languages, but the scan filters on one: English, or any."""

    ENGLISH = "english"
    ANY = "any"

    @property
    def cardmarket_id(self) -> int | None:
        return ENGLISH if self is LanguageChoice.ENGLISH else None


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

    def scan_filters(self) -> ScanFilters:
        return ScanFilters(
            self.language.cardmarket_id, self.min_condition, self.foil, self.countries, self.delivery.max_days
        )

    def to_json(self) -> dict[str, object]:
        condition = self.min_condition.value if self.min_condition is not None else ANY
        return {
            "language": self.language.value,
            "condition": condition,
            "foil": self.foil.value,
            "countries": sorted(self.countries),
            "delivery": self.delivery.value,
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


def parse_filters(
    language: object, condition: object, foil: object, countries: object, delivery: object
) -> BuyFilters:
    """The filters the page sent; a value that is none of the choices is refused, not guessed."""
    try:
        chosen_language = LanguageChoice(language)
        chosen_condition = None if condition == ANY else Condition(condition)
        chosen_foil = Foil(foil)
        chosen_countries = _check_countries(countries)
        chosen_delivery = DeliveryChoice(delivery)
    except ValueError:
        raise BuyError(NOT_AVAILABLE) from None
    return BuyFilters(chosen_language, chosen_condition, chosen_foil, chosen_countries, chosen_delivery)


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
    # Deck ids left out of the purchase, by account (casefolded user name).
    left_out: Mapping[str, frozenset[int]] = field(default_factory=dict[str, frozenset[int]])


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

    def set_filters(self, filters: BuyFilters) -> None:
        self._change(lambda settings: replace(settings, filters=filters))

    def set_base_sellers(self, count: int) -> None:
        self._change(lambda settings: replace(settings, base_sellers=check_base_sellers(count)))

    def set_left_out(self, user: str, deck_ids: frozenset[int]) -> None:
        def change(settings: BuySettings) -> BuySettings:
            left = {name: ids for name, ids in settings.left_out.items() if name != _account(user)}
            if deck_ids:
                left[_account(user)] = deck_ids
            return replace(settings, left_out=left)

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
    if version < COUNTRIES_FORMAT:
        filters = raw.get("filters")
        if filters is not None:
            migrated["filters"] = {**as_object(filters, "filters"), "countries": [], "delivery": "any"}
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
    return BuySettings(filters, base_sellers, _left_out(raw.get("leftOut")))


def _filters(raw: object, defaults: BuyFilters) -> BuyFilters:
    if raw is None:
        return defaults
    fields = as_object(raw, "filters")
    return BuyFilters(
        _choice(LanguageChoice, fields, "language", defaults.language),
        _condition(fields.get("condition"), defaults.min_condition),
        _choice(Foil, fields, "foil", defaults.foil),
        _countries(fields.get("countries")),
        _choice(DeliveryChoice, fields, "delivery", defaults.delivery),
    )


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
    if raw is None:
        return {}
    left: dict[str, frozenset[int]] = {}
    for name, ids in as_object(raw, "leftOut").items():
        numbers = as_list(ids, f"leftOut.{name}")
        valid = [n for n in numbers if isinstance(n, int) and not isinstance(n, bool)]
        if len(valid) != len(numbers):
            log.warning("%s: leftOut.%s holds values that are no deck id; they are dropped", BUY_FILE, name)
        if valid:
            left[name] = frozenset(valid)
    return left


def _dump(settings: BuySettings) -> str:
    document: dict[str, object] = {
        "version": FORMAT_VERSION,
        "filters": settings.filters.to_json(),
        "baseSellers": settings.base_sellers,
        "leftOut": {name: sorted(ids) for name, ids in sorted(settings.left_out.items())},
    }
    return json.dumps(document, indent=1) + "\n"
