"""Upgrade ideas for a deck: cards that complete a combo, cards for roles it is short of, and cuts to weigh.

Combos come from Commander Spellbook; roles, prices and popularity ranks come from the local card data
(the ranks are EDHREC's, as Scryfall publishes them). EDHREC itself is never asked: its terms forbid
automated requests.
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from commander.analysis import ANALYZED_ROLES, game_changers, tagged_count
from commander.background import run_all
from commander.carddb import COLORS, CardDb, CardInfo, ResolvedCard, Role
from commander.deckcache import Answer, AnswerSource, Clock, DeckCache, cached_answer
from commander.jsonshape import as_object, opt_int, opt_str, req_list, req_str, str_list
from commander.models import COMMANDER_CATEGORY, Deck
from commander.net import NetworkError
from commander.spellbook import (
    FRESH_FOR,
    PARALLEL_REQUESTS,
    BracketTag,
    ComboCard,
    ComboMatches,
    SpellbookClient,
    SpellbookError,
    Variant,
    deck_request,
)

CACHE_FOLDER = Path("spellbook", "combos")
CACHE_VERSION = 1
DEFAULT_COUNT = 5
# Common deckbuilding guidance; the config file's [targets] table overrides any of them.
DEFAULT_TARGETS: Mapping[Role, int] = {Role.RAMP: 10, Role.DRAW: 10, Role.REMOVAL: 8, Role.BOARD_WIPE: 2}
# Official bracket rules (magic.wizards.com/en/formats/commander): game changers allowed per bracket,
# None meaning any number, and no mass land denial below bracket 4.
GAME_CHANGER_LIMITS: Mapping[int, int | None] = {1: 0, 2: 0, 3: 3, 4: None, 5: None}
LOWEST_BRACKET_FOR_MASS_LAND_DENIAL = 4

type ComboResult = Answer[ComboMatches]


@dataclass(slots=True, frozen=True)
class SuggestOptions:
    targets: Mapping[Role, int]
    # Ideas to list per section.
    count: int
    max_cents: int | None
    within_bracket: bool

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"count must be at least 1, got {self.count}")
        if self.max_cents is not None and self.max_cents < 0:
            raise ValueError(f"max_cents must not be negative, got {self.max_cents}")


@dataclass(slots=True, frozen=True)
class BracketRaise:
    """Adding the card would lift the deck to at least this bracket."""

    bracket: int
    reason: str


@dataclass(slots=True, frozen=True)
class ComboPiece:
    """The one card a deck lacks for each of these combos."""

    name: str
    info: CardInfo | None
    combos: tuple[Variant, ...]
    raises: BracketRaise | None

    @property
    def price_cents(self) -> int | None:
        return self.info.cheapest_cents if self.info else None


@dataclass(slots=True, frozen=True)
class RoleIdea:
    info: CardInfo
    raises: BracketRaise | None


@dataclass(slots=True, frozen=True)
class RoleTarget:
    role: Role
    have: int
    target: int
    # Empty when the role is not short.
    ideas: tuple[RoleIdea, ...]

    @property
    def short(self) -> bool:
        return self.have < self.target


@dataclass(slots=True, frozen=True)
class Suggestions:
    deck: Deck
    # None when a commander is missing from the card data, so the deck's colours are unknown.
    identity: str | None
    combos: ComboResult
    pieces: tuple[ComboPiece, ...]
    # Combo pieces beyond the count shown.
    more_pieces: int
    # Every role with a target that the card data can count, short or not.
    roles: tuple[RoleTarget, ...]
    cuts: tuple[CardInfo, ...]
    # Ideas skipped because they would lift the deck above its bracket (only with within_bracket).
    hidden: int


def suggest(
    deck: Deck, resolved: Sequence[ResolvedCard], db: CardDb, combos: ComboResult, options: SuggestOptions
) -> Suggestions:
    """Ideas for one deck; resolved must cover the deck's main cards."""
    main = [r for r in resolved if r.card.in_deck]
    bracket = deck.bracket
    changers = len(game_changers(main))
    identity = deck_identity(main)
    data = combos.data
    pieces = combo_pieces(main, data, db, bracket=bracket, changers=changers) if data is not None else []
    priced = [p for p in pieces if _affordable(p.price_cents, options.max_cents)]
    kept = [p for p in priced if not (options.within_bracket and p.raises)]
    roles, hidden = role_targets(main, db, identity, options, bracket=bracket, changers=changers)
    return Suggestions(
        deck=deck,
        identity=identity,
        combos=combos,
        pieces=tuple(kept[: options.count]),
        more_pieces=max(len(kept) - options.count, 0),
        roles=roles,
        cuts=cut_candidates(main, data, count=options.count),
        hidden=hidden + len(priced) - len(kept),
    )


def deck_identity(main: Sequence[ResolvedCard]) -> str | None:
    commanders = [r.info for r in main if COMMANDER_CATEGORY in r.card.categories]
    if not commanders:
        return None
    colors: set[str] = set()
    for info in commanders:
        if info is None:
            return None
        colors.update(info.color_identity)
    return "".join(color for color in COLORS if color in colors)


def card_raise(info: CardInfo, bracket: int | None, changers: int) -> BracketRaise | None:
    """The bracket the card alone would lift a deck to, given the game changers it already has."""
    if bracket is None:
        return None
    raises: list[BracketRaise] = []
    if info.game_changer:
        fits = next(b for b in range(bracket, 6) if _changers_fit(b, changers + 1))
        if fits > bracket:
            raises.append(BracketRaise(fits, "game changer"))
    if Role.MASS_LAND_DENIAL in info.roles and bracket < LOWEST_BRACKET_FOR_MASS_LAND_DENIAL:
        raises.append(BracketRaise(LOWEST_BRACKET_FOR_MASS_LAND_DENIAL, "mass land denial"))
    return _highest(raises)


def combo_pieces(
    main: Sequence[ResolvedCard], matches: ComboMatches, db: CardDb, *, bracket: int | None, changers: int
) -> list[ComboPiece]:
    """Every card that would complete a near-miss combo, the ones completing most combos first."""
    held_ids = {r.info.oracle_id for r in main if r.info}
    held_names = {r.card.name.casefold() for r in main}
    found: dict[str, tuple[ComboCard, list[Variant]]] = {}
    for variant in matches.almost_included:
        missing = [c for c in variant.cards if not _held(c, held_ids, held_names)]
        # No missing card means the missing piece is a described one, like "a sacrifice outlet".
        if variant.tag is BracketTag.BANNED or len(missing) != 1:
            continue
        card = missing[0]
        found.setdefault(card.oracle_id or card.name.casefold(), (card, []))[1].append(variant)
    by_id = db.cards(card.oracle_id for card, _ in found.values() if card.oracle_id)
    by_name = db.cards_named(card.name for card, _ in found.values() if not card.oracle_id)
    pieces: list[ComboPiece] = []
    for card, variants in found.values():
        info = by_id.get(card.oracle_id) if card.oracle_id else by_name.get(card.name.casefold())
        if info is not None and not info.commander_legal:
            continue
        own = card_raise(info, bracket, changers) if info else None
        raises = _highest([own, *(_combo_raise(v.tag, bracket) for v in variants)])
        ordered = tuple(sorted(variants, key=lambda v: -(v.popularity or 0)))
        pieces.append(ComboPiece(info.name if info else card.name, info, ordered, raises))
    return sorted(
        pieces, key=lambda p: (-len(p.combos), -max(v.popularity or 0 for v in p.combos), p.name.casefold())
    )


def role_targets(
    main: Sequence[ResolvedCard],
    db: CardDb,
    identity: str | None,
    options: SuggestOptions,
    *,
    bracket: int | None,
    changers: int,
) -> tuple[tuple[RoleTarget, ...], int]:
    """Each role's count against its target, with the most played cards that fit for a short one.

    Also returns how many ideas were hidden. Without a colour identity nothing can be suggested.
    """
    if identity is None:
        return (), 0
    exclude = {r.info.oracle_id for r in main if r.info}
    # Ideas are never lands, so lands that happen to carry a role tag do not count towards a target.
    spells = [r for r in main if r.info and not r.info.is_land]
    targets: list[RoleTarget] = []
    hidden = 0
    for role in ANALYZED_ROLES:
        target = options.targets.get(role)
        if target is None or role not in db.available_roles:
            continue
        have = tagged_count(spells, role)
        ideas: list[RoleIdea] = []
        if have >= target:
            targets.append(RoleTarget(role, have, target, ()))
            continue
        for info in db.candidates(role, identity, exclude=exclude, max_cents=options.max_cents):
            raises = card_raise(info, bracket, changers)
            if raises and options.within_bracket:
                hidden += 1
                continue
            ideas.append(RoleIdea(info, raises))
            if len(ideas) == options.count:
                break
        targets.append(RoleTarget(role, have, target, tuple(ideas)))
    return tuple(targets), hidden


def cut_candidates(
    main: Sequence[ResolvedCard], matches: ComboMatches | None, *, count: int
) -> tuple[CardInfo, ...]:
    """Nonland cards that fill no role and sit in no combo, the least played first; unranked ones last.

    Without matches, combo pieces cannot be told apart and may be listed.
    """
    combo_cards = [card for v in matches.included for card in v.cards] if matches is not None else []
    combo_ids = {c.oracle_id for c in combo_cards if c.oracle_id}
    combo_names = {c.name.casefold() for c in combo_cards}
    pool: dict[str, CardInfo] = {}
    for r in main:
        info = r.info
        if (
            info is None
            or COMMANDER_CATEGORY in r.card.categories
            or info.is_land
            or info.roles & set(ANALYZED_ROLES)
            or info.oracle_id in combo_ids
            or r.card.name.casefold() in combo_names
        ):
            continue
        pool[info.oracle_id] = info
    ranked = sorted(
        pool.values(), key=lambda i: (i.edhrec_rank is None, -(i.edhrec_rank or 0), i.name.casefold())
    )
    return tuple(ranked[:count])


def combos_cache(folder: Path) -> DeckCache[ComboMatches]:
    return DeckCache(
        folder, version=CACHE_VERSION, what="combo list", encode=_matches_to_json, decode=_matches_from_json
    )


def find_combos(
    decks: Sequence[Deck],
    client: SpellbookClient,
    cache: DeckCache[ComboMatches],
    *,
    now: Clock,
    refresh: bool,
) -> list[ComboResult]:
    """The combos of each deck and those one card short, in deck order; failures come back as results."""
    jobs = [partial(find_deck_combos, deck, client, cache, now=now, refresh=refresh) for deck in decks]
    return [future.result() for future in run_all(jobs, limit=PARALLEL_REQUESTS, name="spellbook")]


def find_deck_combos(
    deck: Deck, client: SpellbookClient, cache: DeckCache[ComboMatches], *, now: Clock, refresh: bool
) -> ComboResult:
    try:
        request = deck_request(deck)
    except SpellbookError as error:
        return Answer(None, AnswerSource.UNAVAILABLE, None, str(error))
    return cached_answer(
        deck.id,
        request,
        client.find_my_combos,
        cache,
        now=now,
        refresh=refresh,
        fresh_for=FRESH_FOR,
        errors=(NetworkError, SpellbookError),
    )


def _changers_fit(bracket: int, changers: int) -> bool:
    limit = GAME_CHANGER_LIMITS[bracket]
    return limit is None or changers <= limit


def _combo_raise(tag: BracketTag, bracket: int | None) -> BracketRaise | None:
    if bracket is None or tag.minimum is None or tag.minimum <= bracket:
        return None
    return BracketRaise(tag.minimum, f"{tag.label} combo")


def _highest(raises: Collection[BracketRaise | None]) -> BracketRaise | None:
    known = [r for r in raises if r is not None]
    return max(known, key=lambda r: r.bracket) if known else None


def _held(card: ComboCard, held_ids: set[str], held_names: set[str]) -> bool:
    return (card.oracle_id is not None and card.oracle_id in held_ids) or card.name.casefold() in held_names


def _affordable(price_cents: int | None, max_cents: int | None) -> bool:
    return max_cents is None or (price_cents is not None and price_cents <= max_cents)


def _matches_to_json(matches: ComboMatches) -> dict[str, object]:
    return {
        "included": [_variant_to_json(v) for v in matches.included],
        "almostIncluded": [_variant_to_json(v) for v in matches.almost_included],
    }


def _variant_to_json(variant: Variant) -> dict[str, object]:
    return {
        "id": variant.id,
        "tag": variant.tag.code,
        "cards": [{"name": c.name, "oracleId": c.oracle_id} for c in variant.cards],
        "requirements": list(variant.requirements),
        "results": list(variant.results),
        "popularity": variant.popularity,
    }


def _matches_from_json(raw: object) -> ComboMatches:
    obj = as_object(raw, "cache.answer")
    return ComboMatches(_variants_from_json(obj, "included"), _variants_from_json(obj, "almostIncluded"))


def _variants_from_json(obj: Mapping[str, object], key: str) -> tuple[Variant, ...]:
    variants: list[Variant] = []
    for index, item in enumerate(req_list(obj, key, "cache.answer")):
        where = f"cache.answer.{key}[{index}]"
        variant = as_object(item, where)
        cards = tuple(
            ComboCard(req_str(card, "name", where), opt_str(card, "oracleId", where))
            for card in (as_object(c, where) for c in req_list(variant, "cards", where))
        )
        variants.append(
            Variant(
                id=req_str(variant, "id", where),
                tag=BracketTag.from_code(req_str(variant, "tag", where)),
                cards=cards,
                requirements=str_list(variant, "requirements", where),
                results=str_list(variant, "results", where),
                popularity=opt_int(variant, "popularity", where),
            )
        )
    return tuple(variants)
