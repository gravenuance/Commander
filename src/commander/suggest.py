"""Upgrade ideas for a deck: cards that complete a combo, cards for roles it is short of, and cuts to weigh.

Combos come from Commander Spellbook; roles, prices and popularity ranks come from the local card data
(the ranks are EDHREC's, as Scryfall publishes them). EDHREC itself is never asked: its terms forbid
automated requests.
"""

from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from itertools import islice
from pathlib import Path

from commander.analysis import ANALYZED_ROLES, CATEGORY_ALIASES, game_changers
from commander.background import run_all
from commander.budget import NO_BUDGET, Budget, resolved_budget
from commander.carddb import COLORS, NO_THEME, CardDb, CardInfo, ResolvedCard, Role, Theme, back_is_land
from commander.deckcache import Answer, AnswerSource, Clock, DeckCache, cached_answer
from commander.deckprofile import Profile, ResolvedThemes
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
# Candidates read per role before filtering, so ideas matching a theme can be ranked ahead of the rest.
IDEA_WINDOW = 200

type ComboResult = Answer[ComboMatches]


@dataclass(slots=True, frozen=True)
class SuggestOptions:
    targets: Mapping[Role, int]
    # Ideas to list per section.
    count: int
    max_cents: int | None
    # Ideas that would lift the deck above this bracket are left out; None keeps them, marked.
    ceiling: int | None = None
    # Ideas matching a theme come first, and deck cards matching one come last among cuts.
    themes: Theme = NO_THEME
    # The same themes one by one, by the name an idea says it fits.
    theme_labels: tuple[tuple[str, Theme], ...] = ()
    exclude: Theme = NO_THEME
    # Casefolded card names: never suggested, and never offered as cuts.
    exclude_names: frozenset[str] = frozenset()
    keep_names: frozenset[str] = frozenset()
    # Casefolded Archidekt categories whose cards are never offered as cuts.
    keep_categories: frozenset[str] = frozenset()
    # Below bracket 5, narrow max_cents to the deck's own budget and put ideas within it first (ADR 0025).
    deck_budget: bool = True

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError(f"count must be at least 1, got {self.count}")
        if self.max_cents is not None and self.max_cents < 0:
            raise ValueError(f"max_cents must not be negative, got {self.max_cents}")
        if self.ceiling is not None and not 1 <= self.ceiling <= 5:  # noqa: PLR2004 — brackets run 1..5
            raise ValueError(f"ceiling must be 1-5, got {self.ceiling}")


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
    # The card matches one of the deck's themes...
    themed: bool = False
    # ...and these are the ones, by name ("profile", a deck tag or a category).
    fits: tuple[str, ...] = ()
    # Within the deck's working budget (ADR 0025); every idea is at bracket 5.
    within_budget: bool = True


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
    # The most played cards matching the deck's themes; empty without themes.
    themed: tuple[RoleIdea, ...]
    cuts: tuple[CardInfo, ...]
    # Ideas skipped because they would lift the deck above the ceiling.
    hidden: int
    ceiling: int | None = None
    budget: Budget = NO_BUDGET


def suggest(
    deck: Deck, resolved: Sequence[ResolvedCard], db: CardDb, combos: ComboResult, options: SuggestOptions
) -> Suggestions:
    """Ideas for one deck; resolved must cover the deck's main cards."""
    main = [r for r in resolved if r.card.in_deck]
    # The flag's or profile's price cap is the budget's limit; below bracket 5 the deck's cards narrow it.
    budget = (
        resolved_budget(deck.bracket, main, options.max_cents)
        if options.deck_budget
        else Budget(None, options.max_cents)
    )
    picker = _Picker(db, options, deck.bracket, len(game_changers(main)), budget)
    identity = deck_identity(main)
    data = combos.data
    pieces = combo_pieces(main, data, db, bracket=deck.bracket, changers=picker.changers) if data else []
    kept = picker.pieces(pieces)
    roles = role_targets(main, db, identity, options, picker)
    themed: list[RoleIdea] = []
    if identity is not None:
        held = {r.info.oracle_id for r in main if r.info}
        candidates = db.theme_candidates(options.themes, identity, exclude=held, max_cents=budget.ceiling)
        themed = picker.pick(candidates, theme_first=False)
    return Suggestions(
        deck=deck,
        identity=identity,
        combos=combos,
        pieces=tuple(kept[: options.count]),
        more_pieces=max(len(kept) - options.count, 0),
        roles=roles,
        themed=tuple(themed),
        cuts=cut_candidates(
            main,
            data,
            count=options.count,
            protected=protected_cards(main, options.keep_names, options.keep_categories),
            themed=db.matching(options.themes, (r.info.oracle_id for r in main if r.info)),
        ),
        hidden=len(picker.hidden_ids),
        ceiling=options.ceiling,
        budget=budget,
    )


def options_for(
    deck: Deck,
    profile: Profile,
    themes: ResolvedThemes,
    config_targets: Mapping[Role, int],
    *,
    count: int,
    max_cents: int | None,
    within_bracket: bool,
    deck_budget: bool = True,
    rejected: Collection[str] = (),
) -> SuggestOptions:
    """One deck's options: flags first, then its profile, then the config file, then the defaults.

    rejected holds card names already turned down for the deck; they are never suggested again.
    """
    ceiling = deck.bracket if within_bracket else profile.bracket_ceiling
    excluded = {key for name in (*profile.exclude_cards, *rejected) for key in name_keys(name)}
    return SuggestOptions(
        targets={**DEFAULT_TARGETS, **config_targets, **profile.targets},
        count=count,
        max_cents=max_cents if max_cents is not None else profile.max_card_cents,
        ceiling=ceiling,
        themes=themes.themes,
        theme_labels=themes.labelled,
        exclude=themes.exclude,
        exclude_names=frozenset(excluded),
        keep_names=frozenset(key for name in profile.keep_cards for key in name_keys(name)),
        keep_categories=frozenset(category.casefold() for category in profile.keep_categories),
        deck_budget=deck_budget,
    )


@dataclass(slots=True)
class _Picker:
    """Filters ideas by the options (exclusions, price, ceiling) and counts what the ceiling hides."""

    db: CardDb
    options: SuggestOptions
    bracket: int | None
    changers: int
    budget: Budget = NO_BUDGET
    # The cards the ceiling hid, once each however many sections skipped them.
    hidden_ids: set[str] = field(default_factory=set[str])

    def pick(self, candidates: Iterator[CardInfo], *, theme_first: bool) -> list[RoleIdea]:
        # Ranked candidates come lazily; a window this deep leaves room for themed ones to move up.
        window = list(islice(candidates, IDEA_WINDOW))
        ids = [info.oracle_id for info in window]
        banned = self.db.matching(self.options.exclude, ids)
        themed = self.db.matching(self.options.themes, ids)
        labelled = [(label, self.db.matching(theme, ids)) for label, theme in self.options.theme_labels]
        if theme_first:
            window.sort(key=lambda info: info.oracle_id not in themed)
        else:
            # By popularity alone, staples of a broad theme (Tokens) crowd out the deck's own (Dinosaurs);
            # a card that fits more of the deck's themes comes first, the most played first among equals.
            window.sort(key=lambda info: -len({label for label, held in labelled if info.oracle_id in held}))
        # Ideas within the deck's working budget come before dearer ones, each kept in the order above.
        window.sort(key=lambda info: not self.budget.within(info.cheapest_cents))
        ideas: list[RoleIdea] = []
        for info in window:
            if info.oracle_id in banned or name_keys(info.name) & self.options.exclude_names:
                continue
            raises = card_raise(info, self.bracket, self.changers)
            if self._too_high(raises, info.oracle_id):
                continue
            fits = tuple(dict.fromkeys(label for label, held in labelled if info.oracle_id in held))
            within = self.budget.within(info.cheapest_cents)
            ideas.append(
                RoleIdea(info, raises, themed=info.oracle_id in themed, fits=fits, within_budget=within)
            )
            if len(ideas) == self.options.count:
                break
        return ideas

    def pieces(self, pieces: Sequence[ComboPiece]) -> list[ComboPiece]:
        ids = [p.info.oracle_id for p in pieces if p.info]
        banned = self.db.matching(self.options.exclude, ids)
        kept: list[ComboPiece] = []
        for piece in pieces:
            if (piece.info and piece.info.oracle_id in banned) or name_keys(
                piece.name
            ) & self.options.exclude_names:
                continue
            key = piece.info.oracle_id if piece.info else piece.name.casefold()
            if not self.budget.allows(piece.price_cents) or self._too_high(piece.raises, key):
                continue
            kept.append(piece)
        # Within the deck's working budget first, in the order given within each half.
        kept.sort(key=lambda piece: not self.budget.within(piece.price_cents))
        return kept

    def _too_high(self, raises: BracketRaise | None, key: str) -> bool:
        too_high = (
            raises is not None and self.options.ceiling is not None and raises.bracket > self.options.ceiling
        )
        if too_high:
            self.hidden_ids.add(key)
        return too_high


def name_keys(name: str) -> frozenset[str]:
    """The casefolded names a card answers to: its full name and, for two faces, the front face's."""
    key = name.casefold()
    return frozenset({key, key.split(" // ")[0]})


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
    main: Sequence[ResolvedCard], db: CardDb, identity: str | None, options: SuggestOptions, picker: _Picker
) -> tuple[RoleTarget, ...]:
    """Each role's count against its target, with the most played cards that fit for a short one.

    A card counts for a role when Scryfall's tags give it the role or the deck files it under a category of
    that name. Without a colour identity nothing can be suggested.
    """
    if identity is None:
        return ()
    exclude = {r.info.oracle_id for r in main if r.info}
    # Ideas are never lands, so lands that happen to carry a role do not count towards a target.
    spells = [r for r in main if r.info and not r.info.is_land]
    targets: list[RoleTarget] = []
    for role in ANALYZED_ROLES:
        target = options.targets.get(role)
        if target is None or role not in db.available_roles:
            continue
        have = sum(r.card.quantity for r in spells if fills_role(r, role))
        if have >= target:
            targets.append(RoleTarget(role, have, target, ()))
            continue
        candidates = db.candidates(role, identity, exclude=exclude, max_cents=picker.budget.ceiling)
        targets.append(RoleTarget(role, have, target, tuple(picker.pick(candidates, theme_first=True))))
    return tuple(targets)


def fills_role(card: ResolvedCard, role: Role) -> bool:
    """Whether the deck card fills the role, by its tags or by the deck's own category for it."""
    if card.info is not None and role in card.info.roles:
        return True
    aliases = CATEGORY_ALIASES.get(role, frozenset())
    return any(category.casefold() in aliases for category in card.card.categories)


def protected_cards(
    main: Sequence[ResolvedCard], keep_names: Collection[str], keep_categories: Collection[str]
) -> frozenset[str]:
    """Oracle ids of deck cards kept by name key or casefolded category, so never offered as cuts."""
    protected: set[str] = set()
    for r in main:
        if r.info is None:
            continue
        categories = {category.casefold() for category in r.card.categories}
        if categories & set(keep_categories) or name_keys(r.card.name) & set(keep_names):
            protected.add(r.info.oracle_id)
    return frozenset(protected)


def cut_candidates(
    main: Sequence[ResolvedCard],
    matches: ComboMatches | None,
    *,
    count: int,
    protected: Collection[str] = frozenset(),
    themed: Collection[str] = frozenset(),
) -> tuple[CardInfo, ...]:
    """Nonland cards that fill no role and sit in no combo, the least played first; unranked ones last.

    A role counts whether it comes from the tags or the deck's own categories. protected holds oracle ids
    never to list; themed ones come after the rest, since a typal deck matches its theme almost everywhere.
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
            or back_is_land(info.type_line)
            or any(fills_role(r, role) for role in ANALYZED_ROLES)
            or info.oracle_id in protected
            or info.oracle_id in combo_ids
            or r.card.name.casefold() in combo_names
        ):
            continue
        pool[info.oracle_id] = info
    ranked = sorted(
        pool.values(),
        key=lambda i: (
            i.oracle_id in themed,
            i.edhrec_rank is None,
            -(i.edhrec_rank or 0),
            i.name.casefold(),
        ),
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


def affordable(price_cents: int | None, max_cents: int | None) -> bool:
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
