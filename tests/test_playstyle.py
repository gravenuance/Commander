"""A deck's play-style deck tags weigh the staples the meta says it misses (ADR 0022)."""

import json
from collections.abc import Iterator
from dataclasses import replace

import pytest

from commander.carddb import CardDb, Role
from commander.deckprofile import Profile
from commander.ideas import staple_ideas
from commander.meta import Basis, Staples, analyse
from commander.models import Deck
from commander.playstyle import Fit, PlayStyle, Style, deck_play_style, staple_weights
from commander.scryfall import ROLE_TAGS
from conftest import FILLER_CARDS, build_card_db, filler_card
from test_ideas import share
from test_meta import REMOVAL_TAGS, SINCE, TOP_CUT_LISTS, deck, entry, meta_data, peer

__all__ = ["deck"]  # fixture shared with test_meta

GOLEM = "066dfefd-eef5-4c81-8e8a-310bac01674f"
CLEAR = 1.25
BORDERLINE = 1.1
OFF_PLAN = 0.8
BOUNCE_TAG_ID = "bbbbbbbb-0000-4000-8000-0000000000b1"


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


CARDS = [
    "Arcane Signet",
    "Rhystic Study",
    "Lightning Bolt",
    "Chaos Warp",
    "Battered Golem",
    "Disruptor Flute",
]


def fits(style: PlayStyle, db: CardDb) -> dict[str, Fit]:
    return {key: weight.fit for key, weight in staple_weights(CARDS, style, db).items()}


def test_play_styles_come_from_the_deck_tags(deck: Deck) -> None:
    tagged = replace(deck, tags=("Combo", "TURBO", " stax ", "cEDH", "tempo", "Midrange", "AGGRO", "Control"))
    assert deck_play_style(tagged) == PlayStyle(frozenset(Style))
    assert [style.value for style in Style] == [
        "turbo",
        "stax",
        "control",
        "tempo",
        "midrange",
        "aggro",
        "combo",
    ]
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
    assert fits(PlayStyle(), db) == {}


def test_a_stax_deck_favours_stax_pieces_and_removal_at_any_mana_value(db: CardDb) -> None:
    stax = PlayStyle(frozenset({Style.STAX}))
    assert not stax.races
    assert fits(stax, db) == {
        "arcane signet": Fit.NEUTRAL,
        "rhystic study": Fit.NEUTRAL,
        "lightning bolt": Fit.FAVOURED,
        "chaos warp": Fit.FAVOURED,  # mana value 3
        "battered golem": Fit.FAVOURED,  # a hatebear
        "disruptor flute": Fit.FAVOURED,  # its rules text stops activated abilities
    }


def test_a_turbo_deck_that_also_controls_does_not_race(db: CardDb) -> None:
    assert PlayStyle(frozenset({Style.TURBO, Style.STAX})).races
    both = PlayStyle(frozenset({Style.TURBO, Style.CONTROL}))
    assert not both.races
    found = fits(both, db)
    assert (found["chaos warp"], found["arcane signet"], found["battered golem"]) == (
        Fit.FAVOURED,
        Fit.FAVOURED,
        Fit.NEUTRAL,
    )


# Made up, one card for each thing a play style weighs: (type line, mana value, keywords).
CARD_SPECS: dict[str, tuple[str, float, tuple[str, ...]]] = {
    "Test Ramp": ("Artifact", 2, ()),
    "Test Tutor": ("Sorcery", 2, ()),
    "Test Cantrip": ("Instant", 2, ()),
    "Test Big Draw": ("Sorcery", 5, ()),
    "Test Kill": ("Instant", 4, ()),
    "Test Swords": ("Instant", 1, ()),
    "Test Bounce": ("Instant", 3, ()),
    "Test Quick Bounce": ("Instant", 1, ()),
    "Test Wipe": ("Sorcery", 4, ()),
    "Test Counter": ("Instant", 3, ()),
    "Test Cheap Counter": ("Instant", 2, ()),
    "Test Free Counter": ("Instant", 3, ()),
    "Test Shield": ("Instant", 1, ()),
    "Test Lock": ("Enchantment", 2, ()),
    "Test Bear": ("Creature — Bear", 2, ()),
    "Test Hasty Bear": ("Creature — Bear", 2, ("Haste",)),
    "Test Rock": ("Artifact", 3, ()),
}


def made_up_id(name: str) -> str:
    return f"eeeeeeee-0000-4000-8000-{FILLER_CARDS + 100 + list(CARD_SPECS).index(name):012d}"


def made_up_tag(tag_id: str, slug: str, members: list[str], children: list[str] | None = None) -> str:
    return json.dumps(
        {
            "object": "tag",
            "id": tag_id,
            "slug": slug,
            "type": "oracle",
            "child_ids": children or [],
            "taggings": [{"oracle_id": made_up_id(name), "weight": "median"} for name in members],
        }
    )


def role_tag(role: Role, members: list[str], children: list[str] | None = None) -> str:
    return made_up_tag(ROLE_TAGS[role].tag_id, ROLE_TAGS[role].slug, members, children)


def made_up_cards() -> list[str]:
    cards: list[str] = []
    for index, (name, (type_line, mana_value, keywords)) in enumerate(CARD_SPECS.items()):
        raw = json.loads(filler_card(FILLER_CARDS + 100 + index, name, mana_value))
        raw["type_line"] = type_line
        raw["keywords"] = list(keywords)
        cards.append(json.dumps(raw))
    return cards


@pytest.fixture(scope="module")
def made_up_db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    tags = [
        role_tag(Role.RAMP, ["Test Ramp"]),
        role_tag(Role.TUTOR, ["Test Tutor"]),
        role_tag(Role.DRAW, ["Test Cantrip", "Test Big Draw"]),
        # Bounce is a sub-tag of removal, and a sweeper is removal too.
        role_tag(Role.REMOVAL, ["Test Kill", "Test Swords", "Test Wipe"], [BOUNCE_TAG_ID]),
        role_tag(Role.BOARD_WIPE, ["Test Wipe"]),
        role_tag(Role.COUNTERSPELL, ["Test Counter", "Test Cheap Counter", "Test Free Counter"]),
        role_tag(Role.PROTECTION, ["Test Shield"]),
        made_up_tag(BOUNCE_TAG_ID, "removal-bounce", ["Test Bounce", "Test Quick Bounce"]),
        made_up_tag("bbbbbbbb-0000-4000-8000-0000000000b2", "hatebear", ["Test Lock"]),
        made_up_tag("bbbbbbbb-0000-4000-8000-0000000000b3", "alternative-cost", ["Test Free Counter"]),
    ]
    with CardDb(build_card_db(tmp_path_factory.mktemp("madeup"), tags, made_up_cards())) as opened:
        yield opened


def weights(*styles: Style, db: CardDb) -> dict[str, float]:
    found = staple_weights(list(CARD_SPECS), PlayStyle(frozenset(styles)), db)
    return {name: found[name.casefold()].multiplier for name in CARD_SPECS if name.casefold() in found}


def neutral_but(found: dict[str, float], expected: dict[str, float]) -> dict[str, float]:
    return {**dict.fromkeys(found, 1.0), **expected}


def test_a_neutral_deck_weighs_nothing(made_up_db: CardDb) -> None:
    assert weights(db=made_up_db) == {}


def test_turbo_favours_speed_and_finds_every_kind_of_interaction_off_plan(made_up_db: CardDb) -> None:
    found = weights(Style.TURBO, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(
            found,
            {
                "Test Ramp": CLEAR,
                "Test Tutor": CLEAR,
                "Test Cantrip": CLEAR,
                "Test Big Draw": CLEAR,
                "Test Kill": OFF_PLAN,
                "Test Swords": OFF_PLAN,
                "Test Bounce": OFF_PLAN,
                "Test Quick Bounce": OFF_PLAN,
                "Test Wipe": OFF_PLAN,
                "Test Counter": OFF_PLAN,
                "Test Cheap Counter": OFF_PLAN,
                "Test Free Counter": OFF_PLAN,
            },
        )
    )


def test_stax_favours_stax_pieces_and_one_for_one_removal_but_not_wipes_or_counters(
    made_up_db: CardDb,
) -> None:
    found = weights(Style.STAX, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(
            found,
            {
                "Test Lock": CLEAR,
                "Test Kill": CLEAR,
                "Test Swords": CLEAR,
                "Test Bounce": CLEAR,
                "Test Quick Bounce": CLEAR,
            },
        )
    )


def test_control_favours_one_for_one_removal_cheap_draw_and_counterspells_and_leaves_wipes_alone(
    made_up_db: CardDb,
) -> None:
    found = weights(Style.CONTROL, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(
            found,
            {
                "Test Cantrip": CLEAR,
                "Test Kill": CLEAR,
                "Test Swords": CLEAR,
                "Test Bounce": CLEAR,
                "Test Quick Bounce": CLEAR,
                "Test Counter": CLEAR,
                "Test Cheap Counter": CLEAR,
                "Test Free Counter": CLEAR,
            },
        )
    )
    assert (found["Test Big Draw"], found["Test Wipe"]) == (1.0, 1.0)


def test_tempo_favours_removal_and_cheap_interaction_bounce_less_and_finds_wipes_off_plan(
    made_up_db: CardDb,
) -> None:
    found = weights(Style.TEMPO, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(
            found,
            {
                "Test Kill": CLEAR,
                "Test Swords": CLEAR,
                "Test Quick Bounce": CLEAR,  # cheap
                "Test Cheap Counter": CLEAR,
                "Test Free Counter": CLEAR,  # free to cast counts as cheap
                "Test Bounce": BORDERLINE,  # not cheap
                "Test Wipe": OFF_PLAN,
            },
        )
    )
    # A counterspell above mana value 2 is no tempo play.
    assert found["Test Counter"] == 1.0


def test_midrange_favours_counterspells_cheap_interaction_and_tutors_and_is_neutral_on_speed(
    made_up_db: CardDb,
) -> None:
    found = weights(Style.MIDRANGE, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(
            found,
            {
                "Test Tutor": CLEAR,
                "Test Counter": CLEAR,
                # A counterspell and cheap interaction both, but a style counts once.
                "Test Cheap Counter": CLEAR,
                "Test Free Counter": CLEAR,
                "Test Swords": CLEAR,
                "Test Quick Bounce": CLEAR,
            },
        )
    )
    assert (found["Test Ramp"], found["Test Cantrip"], found["Test Kill"]) == (1.0, 1.0, 1.0)


def test_aggro_favours_creatures_and_haste_creatures_a_step_further_and_has_nothing_off_plan(
    made_up_db: CardDb,
) -> None:
    found = weights(Style.AGGRO, db=made_up_db)
    assert found == pytest.approx(
        neutral_but(found, {"Test Bear": BORDERLINE, "Test Hasty Bear": BORDERLINE * BORDERLINE})
    )


def test_combo_favours_tutors_and_protection_a_little(made_up_db: CardDb) -> None:
    found = weights(Style.COMBO, db=made_up_db)
    assert found == pytest.approx(neutral_but(found, {"Test Tutor": BORDERLINE, "Test Shield": BORDERLINE}))


def test_a_borderline_fit_still_reads_as_favoured_and_a_neutral_card_as_neutral(made_up_db: CardDb) -> None:
    found = staple_weights(["Test Shield", "Test Rock"], PlayStyle(frozenset({Style.COMBO})), made_up_db)
    assert (found["test shield"].fit, found["test rock"].fit) == (Fit.FAVOURED, Fit.NEUTRAL)


def test_several_styles_multiply_per_card_and_each_counts_once(made_up_db: CardDb) -> None:
    stax_control = weights(Style.STAX, Style.CONTROL, db=made_up_db)
    assert stax_control["Test Kill"] == pytest.approx(CLEAR * CLEAR)
    assert stax_control["Test Lock"] == pytest.approx(CLEAR)
    assert stax_control["Test Counter"] == pytest.approx(CLEAR)
    midrange_combo = weights(Style.MIDRANGE, Style.COMBO, db=made_up_db)
    assert midrange_combo["Test Tutor"] == pytest.approx(CLEAR * BORDERLINE)
    tempo_midrange = weights(Style.TEMPO, Style.MIDRANGE, db=made_up_db)
    assert tempo_midrange["Test Cheap Counter"] == pytest.approx(CLEAR * CLEAR)
    assert tempo_midrange["Test Wipe"] == pytest.approx(OFF_PLAN)


def test_off_plan_stands_unless_another_style_favours_the_role(made_up_db: CardDb) -> None:
    # Control favours one-for-one removal and counterspells, not wipes: a Turbo and Control deck does not
    # find those off-plan, but still finds the wipe so.
    turbo_control = weights(Style.TURBO, Style.CONTROL, db=made_up_db)
    assert turbo_control["Test Kill"] == pytest.approx(CLEAR)
    assert turbo_control["Test Counter"] == pytest.approx(CLEAR)
    assert turbo_control["Test Wipe"] == pytest.approx(OFF_PLAN)
    assert turbo_control["Test Ramp"] == pytest.approx(CLEAR)
    # Stax favours removal at any mana value, so Turbo and Stax keep it; counterspells stay off-plan.
    turbo_stax = weights(Style.TURBO, Style.STAX, db=made_up_db)
    assert (turbo_stax["Test Kill"], turbo_stax["Test Counter"]) == pytest.approx((CLEAR, OFF_PLAN))
    # Tempo's wipes and Turbo's counterspells are off-plan whatever else is favoured.
    tempo_turbo = weights(Style.TEMPO, Style.TURBO, db=made_up_db)
    assert (tempo_turbo["Test Wipe"], tempo_turbo["Test Counter"]) == pytest.approx((OFF_PLAN, OFF_PLAN))


def test_off_plan_cancelled_by_another_style_reads_as_favoured(made_up_db: CardDb) -> None:
    both = PlayStyle(frozenset({Style.TURBO, Style.STAX}))
    found = staple_weights(["Test Kill", "Test Counter"], both, made_up_db)
    assert (found["test kill"].fit, found["test counter"].fit) == (Fit.FAVOURED, Fit.OFF_PLAN)


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


def test_missing_staples_rank_by_share_times_weight_so_a_well_played_off_plan_card_can_lead(
    deck: Deck, db: CardDb
) -> None:
    played = {"Chaos Warp": 8, "Arcane Signet": 5, "High-Speed Hoverbike": 6}
    lists = [
        entry(i, 40, 8, [(name, 0) for name, count in played.items() if i <= count]) for i in range(1, 11)
    ]
    turbo = analyse(replace(deck, tags=("Turbo",)), meta_data(lists), Profile(), db)
    # 0.8 x 0.8 = 0.64, 0.5 x 1.25 = 0.625 and 0.6: the off-plan card still carries its mark.
    assert [(c.name, c.fit, c.multiplier) for c in turbo.missing] == [
        ("Chaos Warp", Fit.OFF_PLAN, 0.8),
        ("Arcane Signet", Fit.FAVOURED, 1.25),
        ("High-Speed Hoverbike", Fit.NEUTRAL, 1.0),
    ]
    neutral = analyse(deck, meta_data(lists), Profile(), db)
    assert [c.name for c in neutral.missing] == ["Chaos Warp", "High-Speed Hoverbike", "Arcane Signet"]


def test_off_plan_staples_are_no_ideas(deck: Deck) -> None:
    rows = (share("Mind Stone", 4, 5, 1, 4), replace(share("Chaos Warp", 5, 5, 4, 4), fit=Fit.OFF_PLAN))
    found = staple_ideas(deck, Staples(rows, 5, Basis.TOP_CUT, 4), SINCE)
    assert [i.name for i in found] == ["Mind Stone"]
