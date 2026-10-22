"""Upcoming cards that fit a deck: the sync's daily probe of Scryfall's previews, its cache and the ranking
of the cards for one deck (ADR 0055).

The probe keeps its cards in a small file of its own, beside the card database, which it never rebuilds.
"""

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path

from commander.carddb import CardDb, ResolvedCard, Role, name_keys
from commander.deckprofile import Profile, ThemeSpec
from commander.fsutil import write_text_atomic
from commander.jsonshape import (
    ShapeError,
    as_object,
    opt_str,
    req_bool,
    req_float,
    req_int,
    req_list,
    req_str,
    str_list,
)
from commander.models import Deck
from commander.net import NetworkError, Pacer
from commander.scryfall import ScryfallClient, ScryfallError, SetCard, SetInfo, is_scryfall_id
from commander.suggest import deck_identity, name_keys_of, short_roles
from commander.themereading import read_deck_themes
from commander.themewants import preview_facts, spec_wants, type_words

log = logging.getLogger(__name__)

PREVIEWS_FILE = "previews.json"
# Bump when the file's fields change; a file of another version is ignored until the next probe replaces it.
FORMAT_VERSION = 2
# Half Scryfall's asked-for 10 requests a second.
PROBE_INTERVAL_SECONDS = 0.2
# 1,750 cards a set, more than any set has had.
MAX_PAGES_PER_SET = 10
# The previews shown for a deck, best first.
SHOWN = 6


@dataclass(slots=True, frozen=True)
class PreviewCache:
    # The UTC day of the last successful probe; None before the first.
    probed_on: date | None
    cards: tuple[SetCard, ...]


NO_PREVIEWS = PreviewCache(None, ())


class ProbeState(Enum):
    # Already probed today, so nothing was asked.
    DONE_TODAY = "done today"
    NO_SEASON = "no upcoming set"
    UPDATED = "updated"
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class PreviewProbe:
    state: ProbeState
    # The upcoming sets searched, by code; empty unless UPDATED.
    sets: tuple[str, ...] = ()
    # The previews kept.
    cards: int = 0
    requests: int = 0
    seconds: float = 0.0
    # Why the probe failed; empty unless FAILED.
    problem: str = ""


@dataclass(slots=True, frozen=True)
class PreviewFit:
    card: SetCard
    # The deck's dominant tags the card matches, in the deck's order.
    tags: tuple[str, ...]
    fills_short_role: bool


def local_day(moment: datetime) -> date:
    """The viewer's calendar day at moment: release dates are calendar days, and a card is out on its day
    wherever the owner is, not on UTC's."""
    return moment.astimezone().date()


def upcoming_sets(sets: Iterable[SetInfo], today: date) -> list[SetInfo]:
    """Sets bringing new cards that are not out yet and already have cards on Scryfall, soonest first."""
    found = [s for s in sets if s.brings_new_cards and s.released_at > today and s.card_count > 0]
    return sorted(found, key=lambda s: (s.released_at, s.code))


def probe_previews(
    client: ScryfallClient,
    folder: Path,
    *,
    now: Callable[[], datetime],
    pacer: Pacer,
    force: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> PreviewProbe:
    """Search the cards of each upcoming set, once a UTC day (or again, with force), and replace the cache
    with the previews among them; with no upcoming set the cache is emptied. A failure keeps the last cache
    and comes back as FAILED, already logged."""
    moment = now()
    # Once a UTC day, like the card data's check; what is out yet goes by the local day.
    today, local = moment.astimezone(UTC).date(), local_day(moment)
    if not force and read_previews(folder).probed_on == today:
        return PreviewProbe(ProbeState.DONE_TODAY)
    started = monotonic()
    requests = 0

    def before_each() -> None:
        nonlocal requests
        pacer.wait()
        requests += 1

    try:
        # The set list is shared with the card data's check, so this request may be answered from its copy.
        requests += 1
        upcoming = upcoming_sets(client.sets(), local)
        cards: dict[str, SetCard] = {}
        for found in upcoming:
            for card in client.set_cards(found.code, before_each=before_each, max_pages=MAX_PAGES_PER_SET):
                if is_preview(card, local):
                    # A card in a set and its Commander decks alike keeps its first printing.
                    cards.setdefault(card.oracle_id, card)
        write_previews(folder, PreviewCache(today, tuple(cards.values())))
    except (NetworkError, ScryfallError, OSError) as error:
        log.warning("card previews not refreshed, the last ones are kept: %s", error)
        return PreviewProbe(ProbeState.FAILED, requests=requests, problem=str(error))
    seconds = monotonic() - started
    codes = tuple(s.code for s in upcoming)
    log.info(
        "card previews: %d from sets %s in %d requests, %.1f s",
        len(cards),
        ", ".join(codes) or "none",
        requests,
        seconds,
    )
    state = ProbeState.UPDATED if upcoming else ProbeState.NO_SEASON
    return PreviewProbe(state, codes, len(cards), requests, seconds)


def is_preview(card: SetCard, today: date) -> bool:
    """Not out yet: a reprint already legal is no preview, and a card stops being one on its release day."""
    return not card.already_out and card.released_at > today


def read_previews(folder: Path) -> PreviewCache:
    """The cached previews; none when there is no cache yet or it cannot be read."""
    path = folder / PREVIEWS_FILE
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return NO_PREVIEWS
    except (OSError, ValueError) as error:
        log.warning("cannot read %s, so no previews until the next sync: %s", path, error)
        return NO_PREVIEWS
    try:
        return _cache_from_json(raw)
    except ShapeError as error:
        log.warning("%s is not a previews file this version reads (%s); a sync replaces it", path, error)
        return NO_PREVIEWS


def write_previews(folder: Path, cache: PreviewCache) -> None:
    write_text_atomic(folder / PREVIEWS_FILE, json.dumps(_cache_json(cache), ensure_ascii=False, indent=1))


def fitting_previews(
    deck: Deck,
    main: Sequence[ResolvedCard],
    profile: Profile,
    db: CardDb,
    cards: Iterable[SetCard],
    *,
    today: date,
    targets: Mapping[Role, int],
) -> list[PreviewFit]:
    """The previews in the deck's colour identity that match its dominant tags, the most tags first, then
    those filling a role the deck runs short of, then by name; at most SHOWN. today is the local day. Like
    every suggestion, a card the profile excludes, by name or by its [exclude] types, keywords or tags, is
    never one, nor is a card the deck already has."""
    identity = deck_identity(main)
    if identity is None:
        log.debug("deck %d: no previews, its commander is missing or not in the card data", deck.id)
        return []
    # The deck's own cards too: one just added on Archidekt may be too new for the card data.
    excluded = name_keys_of(profile.exclude_cards) | name_keys_of(r.card.name for r in main)
    live = [
        c
        for c in cards
        if is_preview(c, today)
        and set(c.color_identity) <= set(identity)
        and not name_keys(c.name) & excluded
    ]
    # The theme work only when there is something to match it against.
    if not live:
        return []
    themes = read_deck_themes(deck, profile, db).themes
    if not themes:
        return []
    ids = [c.oracle_id for c in live]
    lineage = db.tag_lineage(ids)
    roles = db.roles(ids)
    short = short_roles(main, db, targets)
    fits: list[PreviewFit] = []
    for card in live:
        slugs = lineage.get(card.oracle_id, frozenset())
        if _matches(profile.exclude, card, slugs):
            continue
        matched = tuple(theme.name for theme in themes if _matches(theme.spec, card, slugs))
        if matched:
            fills = bool(roles.get(card.oracle_id, frozenset()) & short)
            fits.append(PreviewFit(card, matched, fills))
    fits.sort(key=lambda f: (-len(f.tags), not f.fills_short_role, f.card.name.casefold()))
    return fits[:SHOWN]


def _matches(spec: ThemeSpec, card: SetCard, slugs: frozenset[str]) -> bool:
    """Whether the card has one of the spec's types, keywords or tags (sub-tags included), as the card data's
    theme search would judge it."""
    words = type_words(card.type_line)
    if any(f" {word.casefold()} " in words for word in spec.types):
        return True
    keywords = {keyword.casefold() for keyword in card.keywords}
    if any(keyword.casefold() in keywords for keyword in spec.keywords):
        return True
    if any(tag in slugs for tag in spec.tags):
        return True
    facts = preview_facts(card, slugs)
    return any(want.matches(facts) for want in spec_wants(spec))


def _cache_json(cache: PreviewCache) -> dict[str, object]:
    return {
        "version": FORMAT_VERSION,
        "probedOn": cache.probed_on.isoformat() if cache.probed_on else None,
        "cards": [
            {
                "scryfallId": c.scryfall_id,
                "oracleId": c.oracle_id,
                "name": c.name,
                "manaCost": c.mana_cost,
                "manaValue": c.mana_value,
                "typeLine": c.type_line,
                "text": c.oracle_text,
                "power": c.power,
                "keywords": list(c.keywords),
                "identity": c.color_identity,
                "set": c.set_code,
                "setName": c.set_name,
                "releasedAt": c.released_at.isoformat(),
                "alreadyOut": c.already_out,
            }
            for c in cache.cards
        ],
    }


def _cache_from_json(raw: object) -> PreviewCache:
    obj = as_object(_migrate(raw), "previews")
    probed = obj.get("probedOn")
    cards: list[SetCard] = []
    for index, item in enumerate(req_list(obj, "cards", "previews")):
        where = f"previews.cards[{index}]"
        card = as_object(item, where)
        if not is_scryfall_id(req_str(card, "scryfallId", where)):
            log.warning("%s (%s) skipped: not a Scryfall id", where, card.get("name"))
            continue
        cards.append(
            SetCard(
                scryfall_id=req_str(card, "scryfallId", where),
                oracle_id=req_str(card, "oracleId", where),
                name=req_str(card, "name", where),
                mana_cost=req_str(card, "manaCost", where),
                mana_value=req_float(card, "manaValue", where),
                type_line=req_str(card, "typeLine", where),
                oracle_text=req_str(card, "text", where),
                power=opt_str(card, "power", where),
                keywords=str_list(card, "keywords", where),
                color_identity=req_str(card, "identity", where),
                set_code=req_str(card, "set", where),
                set_name=req_str(card, "setName", where),
                released_at=_date(req_str(card, "releasedAt", where), where),
                already_out=req_bool(card, "alreadyOut", where),
            )
        )
    return PreviewCache(_date(probed, "previews.probedOn") if isinstance(probed, str) else None, tuple(cards))


def _migrate(raw: object) -> object:
    # Migrations from older formats go here, on the raw tree; a cache of another version is simply refetched.
    version = req_int(as_object(raw, "previews"), "version", "previews")
    if version != FORMAT_VERSION:
        raise ShapeError(f"previews.version: {version}, expected {FORMAT_VERSION}")
    return raw


def _date(text: str, where: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ShapeError(f"{where}: not a date: {text!r}") from None
