"""Upcoming cards that fit a deck: the sync's daily probe of Scryfall's previews, its cache and the ranking
of the cards for one deck (ADR 0055).

The probe keeps its cards in a small file of its own, beside the card database, which it never rebuilds.
"""

import json
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from statistics import fmean

from commander.carddb import CardDb, ResolvedCard, Role, Theme, name_keys
from commander.deckprofile import Profile
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
from commander.models import COMMANDER_CATEGORY, Deck
from commander.net import NetworkError, Pacer
from commander.scryfall import (
    ScryfallClient,
    ScryfallError,
    SetCard,
    SetInfo,
    card_roles,
    is_scryfall_id,
    tag_lineages,
)
from commander.suggest import deck_identity, name_keys_of, short_roles
from commander.themereading import DeckReading, read_deck_themes
from commander.themewants import (
    Trait,
    named_traits,
    preview_facts,
    theme_wants,
    traits,
    want_sizes,
    want_trait,
)

log = logging.getLogger(__name__)

PREVIEWS_FILE = "previews.json"
# Bump when the file's fields change; a file of another version is ignored until the next probe replaces it.
FORMAT_VERSION = 4
# Half Scryfall's asked-for 10 requests a second.
PROBE_INTERVAL_SECONDS = 0.2
# 1,750 cards a set, more than any set has had.
MAX_PAGES_PER_SET = 10
# The previews shown for a deck, best first.
SHOWN = 12
# What the commander wants weighs this many times what the deck's themes alone ask for: the commander is
# always there to use it.
COMMANDER_WEIGHT = 2.0
# The least a match weighs, so a feature nearly every card has still counts for something.
MIN_WEIGHT = 0.1


@dataclass(slots=True, frozen=True)
class PreviewCache:
    # The UTC day of the last successful probe; None before the first.
    probed_on: date | None
    cards: tuple[SetCard, ...]
    # The cards' Tagger tags (with every tag above them) and roles, by oracle id, from the tags of the probe's
    # day: the card data's are up to a week older, too old for a card revealed since.
    tags: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])
    roles: Mapping[str, frozenset[Role]] = field(default_factory=dict[str, frozenset[Role]])


NO_PREVIEWS = PreviewCache(None, ())
_ROLE_VALUES = frozenset(role.value for role in Role)


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
    # Why the previews' tags were not refreshed, when the cards were; the last tags are kept.
    tag_problem: str = ""


@dataclass(slots=True, frozen=True)
class PreviewFit:
    card: SetCard
    # The deck's dominant tags the card matches, in the deck's order.
    tags: tuple[str, ...]
    # The roles the deck runs short of that the card fills, in the order of Role.
    roles: tuple[Role, ...]
    # Whether the card is one the deck's commander wants (themewants.commander_wants).
    fits_commander: bool = False
    # Whether the card works against the deck's commander (themewants.commander_conflicts).
    against_commander: bool = False


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
    with the previews among them, and their tags and roles from that day's Tagger tags; with no upcoming set
    the cache is emptied. A failure keeps the last cache and comes back as FAILED, already logged; a failure
    of the tags alone keeps the cards found and the last tags of those still previews."""
    moment = now()
    # Once a UTC day, like the card data's check; what is out yet goes by the local day.
    today, local = moment.astimezone(UTC).date(), local_day(moment)
    last = read_previews(folder)
    if not force and last.probed_on == today:
        return PreviewProbe(ProbeState.DONE_TODAY)
    started = monotonic()
    requests = 0

    def before_each() -> None:
        nonlocal requests
        pacer.wait()
        requests += 1

    try:
        # The set list is shared with the card data's check, so it may come from that check's copy.
        sets, requested = client.read_sets()
        requests += requested
        upcoming = upcoming_sets(sets, local)
        cards: dict[str, SetCard] = {}
        for found in upcoming:
            for card in client.set_cards(found.code, before_each=before_each, max_pages=MAX_PAGES_PER_SET):
                if is_preview(card, local):
                    # A card in a set and its Commander decks alike keeps its first printing.
                    cards.setdefault(card.oracle_id, card)
        tags, roles, tag_problem = _preview_tags(client, folder, cards, last, before_each)
        write_previews(folder, PreviewCache(today, tuple(cards.values()), tags, roles))
    except (NetworkError, ScryfallError, OSError) as error:
        log.warning("card previews not refreshed, the last ones are kept: %s", error)
        return PreviewProbe(ProbeState.FAILED, requests=requests, problem=str(error))
    seconds = monotonic() - started
    codes = tuple(s.code for s in upcoming)
    log.info(
        "card previews: %d from sets %s, %d with tags, in %d requests, %.1f s",
        len(cards),
        ", ".join(codes) or "none",
        len(tags),
        requests,
        seconds,
    )
    state = ProbeState.UPDATED if upcoming else ProbeState.NO_SEASON
    return PreviewProbe(state, codes, len(cards), requests, seconds, tag_problem=tag_problem)


def _preview_tags(
    client: ScryfallClient,
    folder: Path,
    cards: Mapping[str, SetCard],
    last: PreviewCache,
    before_each: Callable[[], None],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[Role]], str]:
    """The previews' tags and roles from today's Tagger tags, and why they are the last ones instead (logged),
    or "". With no previews no tags are fetched."""
    if not cards:
        return {}, {}, ""
    try:
        # A rebuild of the card data in the same sync may have downloaded them already.
        _, tags = client.oracle_tags(folder, before_request=before_each)
    except (NetworkError, ScryfallError, OSError) as error:
        log.warning("tags of the card previews not refreshed, the last ones are kept: %s", error)
        kept_tags = {o: slugs for o, slugs in last.tags.items() if o in cards}
        return kept_tags, {o: r for o, r in last.roles.items() if o in cards}, str(error)
    return tag_lineages(tags, cards), card_roles(tags, {o: c.oracle_text for o, c in cards.items()}), ""


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
    tags: Mapping[str, frozenset[str]] | None = None,
    roles: Mapping[str, frozenset[Role]] | None = None,
    on_board: Iterable[str] = (),
) -> list[PreviewFit]:
    """The previews in the deck's colour identity that match its dominant tags, that its commander wants or
    that fill a role it runs short of. Those working against the commander go last, still shown. The rest
    go by the weight of the distinct features they match (`_feature_weights`: the types, keywords, tags and
    wants of the themes, the commander's wants and the short roles, each once, a rare one weighing more, and
    what the commander wants twice), then those the commander wants, then those filling a short role, then
    the coloured before the colourless, then those sharing more Tagger tags with the deck's cards and nearer
    its average mana value (`_like_the_deck`), then by name; at most SHOWN. today is the local day; tags and
    roles are the probe's (`PreviewCache`), beside the card data's.
    Like every suggestion, a card the profile excludes, by name or by its [exclude] types, keywords or tags,
    is never one, nor is a card the deck already has or one of the names on_board (the deck's board, in any
    state), left out before the cut to SHOWN so the next fit takes its place."""
    identity = deck_identity(main)
    if identity is None:
        log.debug("deck %d: no previews, its commander is missing or not in the card data", deck.id)
        return []
    # The deck's own cards too: one just added on Archidekt may be too new for the card data.
    excluded = (
        name_keys_of(profile.exclude_cards) | name_keys_of(r.card.name for r in main) | name_keys_of(on_board)
    )
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
    reading = read_deck_themes(deck, profile, db)
    short = short_roles(main, db, targets)
    if not reading.themes and not reading.commander and not short:
        return []
    ids = [c.oracle_id for c in live]
    lineage = _merged(db.tag_lineage(ids), tags or {})
    held_roles = _merged(db.roles(ids), roles or {})
    like = _like_the_deck(main, db, lineage)
    weight = _feature_weights(db, reading)
    ranked: list[tuple[tuple[bool, float, bool, bool, bool, float, float, str], PreviewFit]] = []
    for card in live:
        facts = preview_facts(card, lineage.get(card.oracle_id, frozenset()))
        if named_traits(profile.exclude, facts):
            continue
        by_theme = [(theme, traits(theme.spec, facts)) for theme in reading.themes]
        matched = tuple(theme.name for theme, found in by_theme if found)
        wanted = {want_trait(want) for want in reading.commander if want.matches(facts)}
        fills = [role for role in Role if role in held_roles.get(card.oracle_id, frozenset()) & short]
        filled = {("role", role.value) for role in fills}
        if not matched and not wanted and not filled:
            continue
        # Tags asking for the same feature, like Tokens and Go-wide for repeatable tokens, count it once, and
        # so does a theme's feeder the commander wants too.
        features = {trait for _, found in by_theme for trait in found} | wanted | filled
        score = sum(weight(trait) * (COMMANDER_WEIGHT if trait in wanted else 1.0) for trait in features)
        against = any(want.matches(facts) for want in reading.against)
        affinity, distance = like(card)
        colourless = not card.color_identity
        key = (
            against,
            -round(score, 6),
            not wanted,
            not filled,
            colourless,
            -affinity,
            distance,
            card.name.casefold(),
        )
        ranked.append((key, PreviewFit(card, matched, tuple(fills), bool(wanted), against)))
    ranked.sort(key=lambda item: item[0])
    return [fit for _, fit in ranked[:SHOWN]]


def _merged[T](
    first: Mapping[str, frozenset[T]], second: Mapping[str, frozenset[T]]
) -> dict[str, frozenset[T]]:
    return {key: first.get(key, frozenset()) | second.get(key, frozenset()) for key in first.keys() | second}


def _feature_weights(db: CardDb, reading: DeckReading) -> Callable[[Trait], float]:
    """How much matching a feature says a card fits: the rarer the feature among all Commander-legal cards,
    the more (the log of all of them over those with it), and never under MIN_WEIGHT. Dwarf or Landfall
    weighs far more than a creature with power 2 or less."""
    total = max(db.legal_cards(), 1)
    wants = {want_trait(w): w for theme in reading.themes for w in theme_wants(theme.spec)}
    wants.update((want_trait(w), w) for w in reading.commander)
    want_size = want_sizes(db, wants.values())
    # Legal cards only, as the other counts: a tag's own size counts every card in it.
    tag_ids = db.tag_ids({slug for theme in reading.themes for slug in theme.spec.tags})
    tag_size = {slug: db.theme_size(Theme(tag_ids=(tag_id,))) for slug, tag_id in tag_ids.items()}
    role_size = db.role_sizes()
    known: dict[Trait, float] = {}

    def size(trait: Trait) -> int:
        kind, value = trait
        if kind == "type":
            return db.theme_size(Theme(types=(value,)))
        if kind == "keyword":
            return db.theme_size(Theme(keywords=(value,)))
        if kind == "tag":
            return tag_size.get(value, 0)
        if kind == "want":
            return want_size.get(wants[trait], 0) if trait in wants else 0
        return role_size.get(Role(value), 0)

    def weight(trait: Trait) -> float:
        if trait not in known:
            known[trait] = max(MIN_WEIGHT, math.log(total / max(size(trait), 1)))
        return known[trait]

    return weight


def _like_the_deck(
    main: Sequence[ResolvedCard], db: CardDb, lineage: Mapping[str, frozenset[str]]
) -> Callable[[SetCard], tuple[float, float]]:
    """Breaks a tie between previews that fit the deck alike: how many of the deck's cards share each of the
    preview's Tagger tags, each tag's count over all the cards carrying it so a rare tag the deck plays
    outweighs a common one; then how near its mana value is to the deck's average. A preview with no tag
    the deck's cards carry has none of the first and goes by the second."""
    spells = [
        r.info
        for r in main
        if r.info is not None and not r.info.is_land and COMMANDER_CATEGORY not in r.card.categories
    ]
    held = Counter(
        slug for tags in db.tag_lineage(info.oracle_id for info in spells).values() for slug in tags
    )
    average = fmean(info.cmc for info in spells) if spells else 0.0
    sizes = db.tag_sizes({slug for tags in lineage.values() for slug in tags} & held.keys())

    def like(card: SetCard) -> tuple[float, float]:
        tags = lineage.get(card.oracle_id, frozenset())
        affinity = sum(held[slug] / sizes[slug] for slug in tags if slug in held and sizes.get(slug))
        return round(affinity, 6), abs(card.mana_value - average)

    return like


def _cache_json(cache: PreviewCache) -> dict[str, object]:
    return {
        "version": FORMAT_VERSION,
        "probedOn": cache.probed_on.isoformat() if cache.probed_on else None,
        "cards": [
            {
                **set_card_json(c),
                "tags": sorted(cache.tags.get(c.oracle_id, ())),
                "roles": sorted(role.value for role in cache.roles.get(c.oracle_id, ())),
            }
            for c in cache.cards
        ],
    }


def set_card_json(c: SetCard) -> dict[str, object]:
    """A card as the previews cache and a board card's snapshot of it (ADR 0055) store it."""
    return {
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
        "collectorNumber": c.collector_number,
        "releasedAt": c.released_at.isoformat(),
        "alreadyOut": c.already_out,
    }


def set_card_from_json(card: Mapping[str, object], where: str) -> SetCard:
    """The card `set_card_json` wrote; a ShapeError when a field is missing or wrong, or the id is not a
    Scryfall one (its image address and its printing on Archidekt are made from it)."""
    scryfall_id = req_str(card, "scryfallId", where)
    if not is_scryfall_id(scryfall_id):
        raise ShapeError(f"{where}.scryfallId: not a Scryfall id: {scryfall_id!r}")
    return SetCard(
        scryfall_id=scryfall_id,
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
        collector_number=req_str(card, "collectorNumber", where),
        released_at=_date(req_str(card, "releasedAt", where), where),
        already_out=req_bool(card, "alreadyOut", where),
    )


def _cache_from_json(raw: object) -> PreviewCache:
    obj = as_object(_migrate(raw), "previews")
    probed = obj.get("probedOn")
    cards: list[SetCard] = []
    tags: dict[str, frozenset[str]] = {}
    roles: dict[str, frozenset[Role]] = {}
    for index, item in enumerate(req_list(obj, "cards", "previews")):
        where = f"previews.cards[{index}]"
        card = as_object(item, where)
        if not is_scryfall_id(req_str(card, "scryfallId", where)):
            log.warning("%s (%s) skipped: not a Scryfall id", where, card.get("name"))
            continue
        cards.append(set_card_from_json(card, where))
        oracle_id = cards[-1].oracle_id
        if slugs := str_list(card, "tags", where):
            tags[oracle_id] = frozenset(slugs)
        # A role this version does not know is left out, as the card data's are.
        if held := {Role(r) for r in str_list(card, "roles", where) if r in _ROLE_VALUES}:
            roles[oracle_id] = frozenset(held)
    day = _date(probed, "previews.probedOn") if isinstance(probed, str) else None
    return PreviewCache(day, tuple(cards), tags, roles)


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
