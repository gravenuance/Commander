"""A deck's play-style deck tags weigh the staples the meta says it misses (ADR 0022)."""

import json
from collections.abc import Iterator
from dataclasses import replace

import pytest

from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.ideas import staple_ideas
from commander.meta import Basis, Staples, analyse
from commander.models import Deck
from commander.playstyle import Fit, PlayStyle, Style, deck_play_style, staple_fits
from conftest import CHAOS_WARP, build_card_db
from test_ideas import share
from test_meta import REMOVAL_TAGS, SINCE, TOP_CUT_LISTS, deck, meta_data, peer

__all__ = ["deck"]  # fixture shared with test_meta

GOLEM = "066dfefd-eef5-4c81-8e8a-310bac01674f"


def tag(index: int, slug: str, members: list[str]) -> str:
    return json.dumps(
        {
            "object": "tag",
            "id": f"bbbbbbbb-0000-4000-8000-{index:012d}",
            "slug": slug,
            "type": "oracle",
            "child_ids": [],
            "taggings": [{"oracle_id": member, "weight": "median"} for member in members],
        }
    )


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    # Made up: Battered Golem as a hatebear; Lightning Bolt and Chaos Warp as removal (REMOVAL_TAGS).
    tags = [*REMOVAL_TAGS, tag(1, "hatebear", [GOLEM])]
    with CardDb(build_card_db(tmp_path_factory.mktemp("styledb"), tags)) as opened:
        yield opened


@pytest.fixture(scope="module")
def free_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    # The same, with Chaos Warp castable for free.
    tags = [*REMOVAL_TAGS, tag(2, "alternative-cost", [CHAOS_WARP])]
    with CardDb(build_card_db(tmp_path_factory.mktemp("freedb"), tags)) as opened:
        yield opened


CARDS = [
    "Arcane Signet",
    "Rhystic Study",
    "Lightning Bolt",
    "Chaos Warp",
    "Battered Golem",
    "Disruptor Flute",
]


def fits(style: PlayStyle, db: CardDb) -> dict[str, Fit]:
    return staple_fits(CARDS, style, db)


def test_play_styles_come_from_the_deck_tags(deck: Deck) -> None:
    tagged = replace(deck, tags=("Combo", "TURBO", " stax ", "cEDH"))
    assert deck_play_style(tagged) == PlayStyle(frozenset({Style.TURBO, Style.STAX}))
    # The fixture deck's tags (Dwarves, Vehicles, cEDH) weigh nothing.
    assert deck_play_style(deck) == PlayStyle()


def test_a_turbo_deck_favours_speed_and_finds_interaction_off_plan(db: CardDb) -> None:
    turbo = PlayStyle(frozenset({Style.TURBO}))
    assert turbo.races
    assert fits(turbo, db) == {
        "arcane signet": Fit.FAVOURED,  # ramp
        "rhystic study": Fit.FAVOURED,  # draw
        "lightning bolt": Fit.OFF_PLAN,
        "chaos warp": Fit.OFF_PLAN,
        "battered golem": Fit.NEUTRAL,
        # Fixture tags file Disruptor Flute as removal too.
        "disruptor flute": Fit.OFF_PLAN,
    }


def test_a_stax_deck_favours_stax_pieces_and_cheap_interaction(db: CardDb, free_db: CardDb) -> None:
    stax = PlayStyle(frozenset({Style.STAX}))
    assert not stax.races
    assert fits(stax, db) == {
        "arcane signet": Fit.NEUTRAL,
        "rhystic study": Fit.NEUTRAL,
        "lightning bolt": Fit.FAVOURED,  # mana value 1
        "chaos warp": Fit.NEUTRAL,  # mana value 3
        "battered golem": Fit.FAVOURED,  # a hatebear
        "disruptor flute": Fit.FAVOURED,  # its rules text stops activated abilities
    }
    assert fits(stax, free_db)["chaos warp"] is Fit.FAVOURED


def test_a_control_deck_favours_interaction_and_does_not_race(db: CardDb) -> None:
    both = PlayStyle(frozenset({Style.TURBO, Style.CONTROL}))
    assert not both.races
    found = fits(both, db)
    assert (found["chaos warp"], found["arcane signet"], found["battered golem"]) == (
        Fit.FAVOURED,
        Fit.FAVOURED,
        Fit.NEUTRAL,
    )
    assert fits(PlayStyle(), db) == {}


def test_a_turbo_decks_missing_staples_put_its_plan_first_and_interaction_last(
    deck: Deck, db: CardDb
) -> None:
    peers = [peer(1, ["Chaos Warp", "Mind Stone"]), peer(2, ["Chaos Warp"]), peer(3, ["Lightning Bolt"])]
    report = analyse(replace(deck, tags=("Turbo",)), meta_data(TOP_CUT_LISTS, peers), Profile(), db)
    # Unweighted: Mind Stone, Arcane Signet, High-Speed Hoverbike, Jeska's Will, Chaos Warp.
    assert [(c.name, c.fit) for c in report.missing] == [
        ("Mind Stone", Fit.FAVOURED),
        ("Arcane Signet", Fit.FAVOURED),
        ("Jeska's Will", Fit.FAVOURED),
        ("High-Speed Hoverbike", Fit.NEUTRAL),
        ("Chaos Warp", Fit.OFF_PLAN),
    ]
    assert report.style.races


def test_off_plan_staples_are_no_ideas(deck: Deck) -> None:
    rows = (share("Mind Stone", 4, 5, 1, 4), replace(share("Chaos Warp", 5, 5, 4, 4), fit=Fit.OFF_PLAN))
    found = staple_ideas(deck, Staples(rows, 5, Basis.TOP_CUT, 4), SINCE)
    assert [i.name for i in found] == ["Mind Stone"]
