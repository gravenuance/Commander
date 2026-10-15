import copy
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from commander.archidekt import parse_deck
from commander.carddb import CardDb, CardInfo, Role
from commander.deckcache import Answer, AnswerSource
from commander.models import Deck
from commander.net import Fetcher
from commander.spellbook import ComboMatches, SpellbookClient, parse_matches
from commander.suggest import (
    DEFAULT_TARGETS,
    BracketRaise,
    ComboResult,
    Suggestions,
    SuggestOptions,
    card_raise,
    combos_cache,
    find_combos,
    find_deck_combos,
    suggest,
)
from conftest import FakeSpellbook, Json, load_fixture

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
OPTIONS = SuggestOptions(DEFAULT_TARGETS, count=5, max_cents=None, within_bracket=False)


@pytest.fixture
def db(card_db_file: Path) -> Iterator[CardDb]:
    with CardDb(card_db_file) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    """Magda, Battered Golem, Disruptor Flute and six Mountains, set to bracket 5."""
    return parse_deck(raw_deck)


def combos_json() -> Json:
    return copy.deepcopy(load_fixture("spellbook_combos.json"))


def fetched(raw: Json | None = None) -> ComboResult:
    return Answer(parse_matches(raw or combos_json()), AnswerSource.FETCHED, NOW, None)


def run(
    deck: Deck, db: CardDb, combos: ComboResult | None = None, options: SuggestOptions = OPTIONS
) -> Suggestions:
    return suggest(deck, db.resolve(deck.main_cards), db, combos or fetched(), options)


def with_maybeboard_in_deck(deck: Deck) -> Deck:
    return replace(deck, cards=tuple(replace(c, in_deck=True) for c in deck.cards))


def pieces(result: Suggestions) -> list[tuple[str, int | None, int | None]]:
    return [(p.name, p.price_cents, p.raises.bracket if p.raises else None) for p in result.pieces]


def test_cards_one_short_of_a_combo_come_most_popular_combo_first(deck: Deck, db: CardDb) -> None:
    result = run(deck, db)
    # Reiterate + Jeska's Will and Time Vault + Clock of Omens each lack two cards here, so they are left out.
    assert pieces(result) == [
        ("Aggravated Assault", 1693, None),
        ("Maskwood Nexus", None, None),
        ("Splinter Twin", 668, None),
    ]
    assert result.more_pieces == 0
    nexus = result.pieces[1]
    assert nexus.info is None
    assert nexus.combos[0].requirements == ("Creature with Saddle 4 or less",)
    assert [v.id for v in result.pieces[0].combos] == ["1486-3750"]


def test_card_completing_several_combos_comes_first(deck: Deck, db: CardDb) -> None:
    raw = combos_json()
    twin = copy.deepcopy(raw["results"]["almostIncluded"][3])
    twin["id"], twin["popularity"] = "3459-4702-x", 50
    raw["results"]["almostIncluded"].append(twin)
    result = run(deck, db, fetched(raw))
    assert result.pieces[0].name == "Splinter Twin"
    assert [v.id for v in result.pieces[0].combos] == ["3459-4702", "3459-4702-x"]


def test_banned_combos_and_banned_cards_are_not_suggested(deck: Deck, db: CardDb) -> None:
    raw = combos_json()
    near = raw["results"]["almostIncluded"]
    near[1]["bracketTag"] = "B"
    near[3]["uses"][1]["card"] |= {"name": "Mana Crypt", "oracleId": "2c63e4e1-89d2-4bc6-a232-94e75c4b1c8a"}
    assert [p.name for p in run(deck, db, fetched(raw)).pieces] == ["Maskwood Nexus"]


def test_combo_card_without_an_oracle_id_is_matched_by_name(deck: Deck, db: CardDb) -> None:
    raw = combos_json()
    raw["results"]["almostIncluded"][1]["uses"][0]["card"]["oracleId"] = None  # Magda, already in the deck
    raw["results"]["almostIncluded"][1]["uses"][1]["card"]["oracleId"] = None  # Aggravated Assault
    assault = run(deck, db, fetched(raw)).pieces[0]
    assert assault.info is not None
    assert (assault.name, assault.price_cents) == ("Aggravated Assault", 1693)


def test_roles_below_target_get_the_most_played_cards_in_the_deck_colours(deck: Deck, db: CardDb) -> None:
    gaps = {g.role: g for g in run(deck, db).roles}
    # Magda is tagged ramp and Disruptor Flute removal and board wipe; red has no draw candidates here.
    assert {role: (g.have, g.target) for role, g in gaps.items()} == {
        Role.RAMP: (1, 10),
        Role.DRAW: (0, 10),
        Role.REMOVAL: (1, 8),
        Role.BOARD_WIPE: (1, 2),
    }
    assert [i.info.name for i in gaps[Role.RAMP].ideas] == ["Arcane Signet", "Mind Stone", "Jeska's Will"]
    assert [i.info.name for i in gaps[Role.REMOVAL].ideas] == ["Chaos Warp", "Lightning Bolt"]
    assert gaps[Role.DRAW].ideas == gaps[Role.BOARD_WIPE].ideas == ()


def test_lands_with_a_role_tag_do_not_count_towards_its_target(deck: Deck, db: CardDb) -> None:
    golem = next(c for c in deck.cards if c.name == "Battered Golem")
    tomb = replace(golem, name="Ancient Tomb", scryfall_id="bd3d4b4b-cf31-4f89-8140-9650edb03c7b")
    with_tomb = replace(deck, cards=(*deck.cards, tomb))
    ramp = next(r for r in run(with_tomb, db).roles if r.role is Role.RAMP)
    assert ramp.have == 1


def test_targets_decide_which_roles_count_as_short(deck: Deck, db: CardDb) -> None:
    met = replace(OPTIONS, targets={Role.RAMP: 1, Role.REMOVAL: 2})
    roles = run(deck, db, options=met).roles
    assert [(r.role, r.have, r.target, r.short) for r in roles] == [
        (Role.RAMP, 1, 1, False),
        (Role.REMOVAL, 1, 2, True),
    ]
    assert roles[0].ideas == ()
    assert run(deck, db, options=replace(OPTIONS, targets={})).roles == ()


def test_count_limits_each_list(deck: Deck, db: CardDb) -> None:
    result = run(with_maybeboard_in_deck(deck), db, options=replace(OPTIONS, count=1))
    assert [p.name for p in result.pieces] == ["Aggravated Assault"]
    assert result.more_pieces == 2
    assert [i.info.name for i in result.roles[0].ideas] == ["Arcane Signet"]
    assert [c.name for c in result.cuts] == ["Tempestra, Dame of Games"]


def test_price_limit_drops_dearer_and_unpriced_cards(deck: Deck, db: CardDb) -> None:
    result = run(deck, db, options=replace(OPTIONS, max_cents=1000))
    # Aggravated Assault costs EUR 16.93 and Maskwood Nexus has no price; Splinter Twin costs EUR 6.68.
    assert [p.name for p in result.pieces] == ["Splinter Twin"]
    assert [i.info.name for i in result.roles[0].ideas] == ["Arcane Signet"]


def test_ideas_that_raise_the_bracket_are_marked(deck: Deck, db: CardDb) -> None:
    result = run(replace(deck, bracket=2), db)
    assert pieces(result) == [
        ("Aggravated Assault", 1693, 3),
        ("Maskwood Nexus", None, None),
        ("Splinter Twin", 668, 3),
    ]
    assert result.pieces[0].raises == BracketRaise(3, "Spicy combo")
    will = result.roles[0].ideas[2]
    assert (will.info.name, will.raises) == ("Jeska's Will", BracketRaise(3, "game changer"))
    assert result.hidden == 0


def test_combo_whose_lowest_bracket_is_the_decks_own_raises_nothing(deck: Deck, db: CardDb) -> None:
    # Spicy fits brackets 3-4, so a bracket 3 deck stays where it is.
    assert pieces(run(replace(deck, bracket=3), db)) == [
        ("Aggravated Assault", 1693, None),
        ("Maskwood Nexus", None, None),
        ("Splinter Twin", 668, None),
    ]


def test_within_bracket_hides_ideas_that_raise_it(deck: Deck, db: CardDb) -> None:
    result = run(replace(deck, bracket=2), db, options=replace(OPTIONS, within_bracket=True))
    assert [p.name for p in result.pieces] == ["Maskwood Nexus"]
    assert [i.info.name for i in result.roles[0].ideas] == ["Arcane Signet", "Mind Stone"]
    assert result.hidden == 3


def test_cuts_are_cards_with_no_role_and_no_combo_least_played_first(deck: Deck, db: CardDb) -> None:
    # Tempestra ranks 11271 and Hoverbike 9733; Battered Golem is in an included combo, Flute has roles.
    assert [c.name for c in run(with_maybeboard_in_deck(deck), db).cuts] == [
        "Tempestra, Dame of Games",
        "High-Speed Hoverbike",
    ]


def test_combo_cards_are_known_by_oracle_id_whatever_their_name(deck: Deck, db: CardDb) -> None:
    raw = combos_json()
    raw["results"]["included"][0]["uses"][1]["card"]["name"] = "Battered Golem (renamed)"
    cuts = run(with_maybeboard_in_deck(deck), db, fetched(raw)).cuts
    assert "Battered Golem" not in [c.name for c in cuts]


def test_the_commander_is_never_a_cut_candidate(deck: Deck, db: CardDb) -> None:
    # Tempestra has no role and is in no combo, so only being the commander keeps it off the list.
    swapped = tuple(
        replace(
            c, in_deck=True, categories=("Commander",) if c.name.startswith("Tempestra") else ("Creature",)
        )
        for c in deck.cards
    )
    cuts = run(replace(deck, cards=swapped), db).cuts
    assert [c.name for c in cuts] == ["High-Speed Hoverbike"]


def test_without_spellbook_there_are_no_combo_pieces_and_combo_cards_may_be_cut(
    deck: Deck, db: CardDb
) -> None:
    down: ComboResult = Answer(None, AnswerSource.UNAVAILABLE, None, "HTTP 503")
    result = run(with_maybeboard_in_deck(deck), db, down)
    assert result.pieces == ()
    assert [c.name for c in result.cuts] == [
        "Tempestra, Dame of Games",
        "High-Speed Hoverbike",
        "Battered Golem",
    ]
    assert result.roles


def test_deck_without_a_commander_gets_no_role_ideas(deck: Deck, db: CardDb) -> None:
    no_commander = replace(deck, cards=tuple(replace(c, categories=("Creature",)) for c in deck.cards))
    result = run(no_commander, db)
    assert (result.identity, result.roles) == (None, ())


def changer(*, game_changer: bool = True, roles: frozenset[Role] = frozenset()) -> CardInfo:
    return CardInfo(
        oracle_id="id",
        name="Card",
        mana_cost="{1}",
        cmc=1.0,
        type_line="Sorcery",
        color_identity="R",
        game_changer=game_changer,
        commander_legal=True,
        cheapest_cents=100,
        edhrec_rank=1,
        roles=roles,
    )


@pytest.mark.parametrize(
    ("card", "bracket", "changers", "expected"),
    [
        (changer(), 1, 0, BracketRaise(3, "game changer")),
        (changer(), 2, 0, BracketRaise(3, "game changer")),
        (changer(), 3, 2, None),
        (changer(), 3, 3, BracketRaise(4, "game changer")),
        (changer(), 4, 9, None),
        (changer(), None, 0, None),
        (changer(game_changer=False), 1, 0, None),
        (
            changer(game_changer=False, roles=frozenset({Role.MASS_LAND_DENIAL})),
            3,
            0,
            BracketRaise(4, "mass land denial"),
        ),
        (changer(game_changer=False, roles=frozenset({Role.MASS_LAND_DENIAL})), 4, 0, None),
        (changer(roles=frozenset({Role.MASS_LAND_DENIAL})), 2, 0, BracketRaise(4, "mass land denial")),
    ],
)
def test_card_raise_follows_the_official_bracket_limits(
    card: CardInfo, bracket: int | None, changers: int, expected: BracketRaise | None
) -> None:
    assert card_raise(card, bracket, changers) == expected


@pytest.mark.parametrize(("count", "max_cents"), [(0, None), (1, -1)])
def test_options_are_checked(count: int, max_cents: int | None) -> None:
    with pytest.raises(ValueError, match="must"):
        SuggestOptions(DEFAULT_TARGETS, count=count, max_cents=max_cents, within_bracket=False)


class CombosEnv:
    def __init__(self, deck: Deck, tmp_path: Path) -> None:
        self.deck = deck
        self.fake = FakeSpellbook()
        self.cache = combos_cache(tmp_path / "combos")
        self.now = NOW

    def find(self, *, refresh: bool = False) -> ComboResult:
        with self.fake.client() as http:
            client = SpellbookClient(Fetcher(http, max_attempts=1))
            return find_deck_combos(self.deck, client, self.cache, now=lambda: self.now, refresh=refresh)


def test_combo_list_is_fetched_once_and_then_read_back_unchanged(deck: Deck, tmp_path: Path) -> None:
    env = CombosEnv(deck, tmp_path)
    first = env.find()
    env.now = NOW + timedelta(days=6)
    second = env.find()
    assert (first.source, second.source) == (AnswerSource.FETCHED, AnswerSource.CACHED)
    assert len(env.fake.combo_bodies) == 1
    assert isinstance(second.data, ComboMatches)
    assert second.data == first.data


def test_week_old_combo_list_is_fetched_again_and_kept_when_that_fails(deck: Deck, tmp_path: Path) -> None:
    env = CombosEnv(deck, tmp_path)
    env.find()
    env.now = NOW + timedelta(days=7)
    env.fake.combos_status = 503
    stale = env.find()
    assert (stale.source, stale.fetched_at) == (AnswerSource.STALE, NOW)
    assert stale.error is not None
    assert "HTTP 503" in stale.error


def test_combo_list_unavailable_without_a_cached_one(deck: Deck, tmp_path: Path) -> None:
    env = CombosEnv(deck, tmp_path)
    env.fake.combos_status = 500
    assert env.find().source is AnswerSource.UNAVAILABLE


def test_several_decks_get_their_combos_in_deck_order(deck: Deck, tmp_path: Path) -> None:
    decks = [replace(deck, id=deck_id) for deck_id in (4, 2, 8)]
    fake = FakeSpellbook()
    with fake.client() as http:
        results = find_combos(
            decks, SpellbookClient(Fetcher(http)), combos_cache(tmp_path), now=lambda: NOW, refresh=False
        )
    assert [r.source for r in results] == [AnswerSource.FETCHED] * 3
    assert sorted(p.stem for p in tmp_path.iterdir()) == ["2", "4", "8"]
