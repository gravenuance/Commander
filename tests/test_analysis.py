from dataclasses import replace
from pathlib import Path

import pytest

from commander.analysis import DeckAnalysis, RoleCount, analyze, count_pips
from commander.archidekt import parse_deck
from commander.carddb import CardDb, CardInfo, ResolvedCard, Role
from commander.models import Deck
from conftest import Json


def analysis_of(deck: Deck, card_db_file: Path) -> DeckAnalysis:
    with CardDb(card_db_file) as db:
        return analyze(deck, db.resolve(deck.main_cards), db.available_roles)


def tweak(resolved: list[ResolvedCard], name: str, /, **changes: float | bool) -> list[ResolvedCard]:
    """A copy of resolved with the named card's facts changed."""
    return [
        replace(r, info=replace(r.info, **changes)) if r.card.name == name and r.info is not None else r
        for r in resolved
    ]


def role(analysis: DeckAnalysis, wanted: Role) -> RoleCount:
    return next(r for r in analysis.roles if r.role is wanted)


@pytest.fixture
def fixture_analysis(raw_deck: Json, card_db_file: Path) -> DeckAnalysis:
    # Main deck: Magda {1}{R} creature, Battered Golem {3} artifact creature, Disruptor Flute {2} artifact,
    # 6 Mountain. The maybeboard (Hoverbike, Tempestra) must not count anywhere.
    return analysis_of(parse_deck(raw_deck), card_db_file)


def test_counts_cards_and_lands(fixture_analysis: DeckAnalysis) -> None:
    assert (fixture_analysis.cards, fixture_analysis.lands) == (9, 6)


def test_curve_and_average_cover_nonland_cards(fixture_analysis: DeckAnalysis) -> None:
    assert fixture_analysis.curve == (0, 0, 2, 1, 0, 0, 0, 0)
    assert fixture_analysis.average_mana_value == pytest.approx(7 / 3)


def test_types_count_each_type_a_card_has(fixture_analysis: DeckAnalysis) -> None:
    assert fixture_analysis.types == {
        "Creature": 2,
        "Artifact": 2,
        "Enchantment": 0,
        "Planeswalker": 0,
        "Battle": 0,
        "Instant": 0,
        "Sorcery": 0,
        "Land": 6,
    }


def test_pips_come_from_mana_costs(fixture_analysis: DeckAnalysis) -> None:
    assert fixture_analysis.pips == {"W": 0, "U": 0, "B": 0, "R": 1, "G": 0, "C": 0}


def test_role_with_your_category_shows_both_sides(fixture_analysis: DeckAnalysis) -> None:
    ramp = role(fixture_analysis, Role.RAMP)
    assert (ramp.yours, ramp.tagged) == (1, 1)
    assert ramp.only_yours == ("Battered Golem",)
    assert ramp.only_tagged == ("Magda, Brazen Outlaw",)


def test_role_without_your_category_shows_only_tags(fixture_analysis: DeckAnalysis) -> None:
    removal = role(fixture_analysis, Role.REMOVAL)
    assert (removal.yours, removal.tagged) == (None, 1)
    assert (removal.only_yours, removal.only_tagged) == ((), ())
    assert role(fixture_analysis, Role.BOARD_WIPE).tagged == 1
    assert role(fixture_analysis, Role.TUTOR).tagged == 1
    assert role(fixture_analysis, Role.DRAW).tagged == 0


def test_category_names_match_ignoring_case(raw_deck: Json, card_db_file: Path) -> None:
    deck = parse_deck(raw_deck)
    flute = next(c for c in deck.cards if c.name == "Disruptor Flute")
    cards = tuple(replace(c, categories=("REMOVAL",)) if c is flute else c for c in deck.cards)
    removal = role(analysis_of(replace(deck, cards=cards), card_db_file), Role.REMOVAL)
    assert (removal.yours, removal.tagged) == (1, 1)


def test_unavailable_role_is_none(raw_deck: Json, card_db_file: Path) -> None:
    deck = parse_deck(raw_deck)
    with CardDb(card_db_file) as db:
        result = analyze(deck, db.resolve(deck.main_cards), frozenset())
    assert all(r.tagged is None for r in result.roles)


def test_flags_game_changers_illegal_and_unknown_cards(raw_deck: Json, card_db_file: Path) -> None:
    deck = parse_deck(raw_deck)
    with CardDb(card_db_file) as db:
        resolved = db.resolve(deck.main_cards)
    resolved = tweak(resolved, "Battered Golem", game_changer=True)
    resolved = tweak(resolved, "Disruptor Flute", commander_legal=False)
    resolved = [
        replace(r, info=None, printing=None) if r.card.name.startswith("Magda") else r for r in resolved
    ]
    result = analyze(deck, resolved, frozenset())
    assert result.game_changers == ("Battered Golem",)
    assert result.not_legal == ("Disruptor Flute",)
    assert result.unknown == ("Magda, Brazen Outlaw",)
    # An unknown card still counts toward the deck size but not the curve.
    assert result.cards == 9
    assert sum(result.curve) == 2


def test_high_mana_values_share_the_top_bucket(raw_deck: Json, card_db_file: Path) -> None:
    deck = parse_deck(raw_deck)
    with CardDb(card_db_file) as db:
        resolved = db.resolve(deck.main_cards)
    resolved = tweak(tweak(resolved, "Battered Golem", cmc=9.0), "Disruptor Flute", cmc=7.0)
    assert analyze(deck, resolved, frozenset()).curve == (0, 0, 1, 0, 0, 0, 0, 2)


@pytest.mark.parametrize(
    ("cost", "pips"),
    [
        ("", {}),
        ("{X}{2}{R}{R}", {"R": 2}),
        ("{W/U}{2/B}{G/P}{C}", {"W": 1, "U": 1, "B": 1, "G": 1, "C": 1}),
        ("{R/G}{R}", {"R": 2, "G": 1}),
        ("{2}{R} // {1}{U}", {"R": 1, "U": 1}),
    ],
)
def test_count_pips(cost: str, pips: dict[str, int]) -> None:
    assert count_pips(cost) == pips


def test_front_face_decides_types(raw_deck: Json) -> None:
    deck = parse_deck(raw_deck)
    golem = next(c for c in deck.cards if c.name == "Battered Golem")
    info = CardInfo(
        oracle_id="c95309e9-5c2f-4518-b2fd-825d3d0a4ae0",
        name="Sundering Eruption // Volcanic Fissure",
        mana_cost="{2}{R}",
        cmc=3.0,
        type_line="Sorcery // Land",
        color_identity="R",
        game_changer=False,
        commander_legal=True,
        cheapest_cents=None,
        roles=frozenset(),
    )
    one = replace(deck, cards=(golem,))
    result = analyze(one, [ResolvedCard(golem, info, None)], frozenset())
    assert (result.types["Sorcery"], result.types["Land"], result.lands) == (1, 0, 0)
