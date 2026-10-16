"""Why a deck's bracket estimate disagrees with its set bracket, by Commander Spellbook's own rules."""

import copy

import pytest

from commander.bracketcheck import Cause, Reason, check_bracket, estimate_causes
from commander.spellbook import (
    BracketEstimate,
    BracketTag,
    CardFlag,
    Combo,
    ComboFlag,
    FlaggedCard,
    TwoCard,
    parse_estimate,
)
from conftest import load_fixture

WINS = frozenset({ComboFlag.RELEVANT, ComboFlag.BORDERLINE_RELEVANT})
MAYBE_WINS = frozenset({ComboFlag.BORDERLINE_RELEVANT})


def combo(
    combo_id: str, speed: int, two_card: TwoCard, flags: frozenset[ComboFlag] = WINS, cards: int = 2
) -> Combo:
    names = tuple(f"{combo_id} piece {i}" for i in range(1, cards + 1))
    return Combo(combo_id, BracketTag.EXHIBITION, names, ("Win the game",), two_card, speed, flags)


def card(name: str, *flags: CardFlag, quantity: int = 1) -> FlaggedCard:
    return FlaggedCard(name, quantity, frozenset(flags))


def estimate(
    tag: BracketTag, combos: tuple[Combo, ...] = (), cards: tuple[FlaggedCard, ...] = ()
) -> BracketEstimate:
    return BracketEstimate(tag, combos, cards)


def summary(causes: tuple[Cause, ...]) -> list[tuple[BracketTag, Reason, tuple[str, ...]]]:
    return [(c.tag, c.reason, c.cards) for c in causes]


def test_the_fixture_deck_is_ruthless_for_a_fast_two_card_combo() -> None:
    parsed = parse_estimate(copy.deepcopy(load_fixture("spellbook_estimate.json")))
    causes = estimate_causes(parsed)
    # 1486-1651-7298: speed 5, relevant, definitely two cards. 4477-6556-7182: only arguably two cards.
    # 1486-3459-7223--67 is no two-card combo and lifts nothing.
    assert [(c.tag, c.reason, c.combo.id if c.combo else None) for c in causes] == [
        (BracketTag.RUTHLESS, Reason.FAST_TWO_CARD, "1486-1651-7298"),
        (BracketTag.SPICY, Reason.FAST_NEARLY_TWO_CARD, "4477-6556-7182"),
        (BracketTag.POWERFUL, Reason.GAME_CHANGER, None),
    ]
    assert causes[2].cards == ("Ancient Tomb",)


def test_more_than_three_game_changers_is_ruthless_and_up_to_three_powerful() -> None:
    three = (card("A", CardFlag.GAME_CHANGER), card("B", CardFlag.GAME_CHANGER, quantity=2))
    four = (*three, card("C", CardFlag.GAME_CHANGER))
    assert summary(estimate_causes(estimate(BracketTag.POWERFUL, cards=three))) == [
        (BracketTag.POWERFUL, Reason.GAME_CHANGER, ("A", "B"))
    ]
    assert summary(estimate_causes(estimate(BracketTag.RUTHLESS, cards=four))) == [
        (BracketTag.RUTHLESS, Reason.GAME_CHANGERS, ("A", "B", "C"))
    ]


def test_extra_turns_land_denial_and_banned_cards() -> None:
    one_turn = (card("Time Warp", CardFlag.EXTRA_TURN),)
    two_turns = (card("Time Warp", CardFlag.EXTRA_TURN, quantity=2),)
    assert summary(estimate_causes(estimate(BracketTag.CORE, cards=one_turn))) == [
        (BracketTag.CORE, Reason.EXTRA_TURN_CARD, ("Time Warp",))
    ]
    assert summary(estimate_causes(estimate(BracketTag.RUTHLESS, cards=two_turns))) == [
        (BracketTag.RUTHLESS, Reason.EXTRA_TURN_CARDS, ("Time Warp",))
    ]
    cards = (card("Armageddon", CardFlag.MASS_LAND_DENIAL), card("Mana Crypt", CardFlag.BANNED))
    assert summary(estimate_causes(estimate(BracketTag.BANNED, cards=cards))) == [
        (BracketTag.BANNED, Reason.BANNED, ("Mana Crypt",)),
        (BracketTag.RUTHLESS, Reason.MASS_LAND_DENIAL, ("Armageddon",)),
    ]


@pytest.mark.parametrize(
    ("built", "expected"),
    [
        # Two-card combos that win, by speed: 4+ Ruthless, 3 Powerful, 2 Core, 1 nothing.
        (combo("x", 4, TwoCard.YES), (BracketTag.RUTHLESS, Reason.FAST_TWO_CARD)),
        (combo("x", 3, TwoCard.YES), (BracketTag.POWERFUL, Reason.TWO_CARD)),
        (combo("x", 2, TwoCard.YES), (BracketTag.CORE, Reason.SLOW_TWO_CARD)),
        (combo("x", 1, TwoCard.YES), None),
        # Arguably two cards, or definitely two cards that only maybe win.
        (combo("x", 4, TwoCard.ARGUABLY), (BracketTag.SPICY, Reason.FAST_NEARLY_TWO_CARD)),
        (combo("x", 4, TwoCard.YES, MAYBE_WINS), (BracketTag.SPICY, Reason.FAST_NEARLY_TWO_CARD)),
        (combo("x", 3, TwoCard.ARGUABLY, MAYBE_WINS), (BracketTag.ODDBALL, Reason.NEARLY_TWO_CARD)),
        (combo("x", 2, TwoCard.ARGUABLY, MAYBE_WINS), None),
        (combo("x", 5, TwoCard.NO), None),
        # What a combo does, however many cards and however slow.
        (
            combo("x", 1, TwoCard.NO, frozenset({ComboFlag.EXTRA_TURN})),
            (BracketTag.RUTHLESS, Reason.EXTRA_TURN_COMBO),
        ),
        (
            combo("x", 1, TwoCard.NO, frozenset({ComboFlag.MASS_LAND_DENIAL})),
            (BracketTag.RUTHLESS, Reason.LAND_DENIAL_COMBO),
        ),
        (
            combo("x", 1, TwoCard.NO, frozenset({ComboFlag.CONTROL_ALL_OPPONENTS})),
            (BracketTag.RUTHLESS, Reason.CONTROL_ALL_COMBO),
        ),
        (combo("x", 1, TwoCard.NO, frozenset({ComboFlag.LOCK})), (BracketTag.SPICY, Reason.LOCK_COMBO)),
        (
            combo("x", 1, TwoCard.NO, frozenset({ComboFlag.SKIP_TURNS})),
            (BracketTag.SPICY, Reason.SKIP_TURNS_COMBO),
        ),
        (
            combo("x", 1, TwoCard.NO, frozenset({ComboFlag.CONTROL_SOME_OPPONENTS})),
            (BracketTag.SPICY, Reason.CONTROL_SOME_COMBO),
        ),
    ],
)
def test_each_combo_rule(built: Combo, expected: tuple[BracketTag, Reason] | None) -> None:
    causes = estimate_causes(estimate(BracketTag.EXHIBITION, (built,)))
    assert [(c.tag, c.reason) for c in causes] == ([expected] if expected else [])


def test_a_combo_meeting_several_rules_is_named_once_under_the_highest() -> None:
    locking = combo("x", 4, TwoCard.YES, WINS | {ComboFlag.LOCK})
    assert [(c.tag, c.reason) for c in estimate_causes(estimate(BracketTag.RUTHLESS, (locking,)))] == [
        (BracketTag.RUTHLESS, Reason.FAST_TWO_CARD)
    ]


MIXED = estimate(
    BracketTag.RUTHLESS,
    (combo("fast", 4, TwoCard.YES), combo("medium", 3, TwoCard.YES)),
    (card("Ancient Tomb", CardFlag.GAME_CHANGER),),
)


def test_only_causes_above_the_set_bracket_are_named() -> None:
    # Set to 3: only Ruthless (4+) lifts the deck above it; Powerful (3+) fits.
    check = check_bracket(3, None, MIXED)
    assert check.notes == ("the estimate (4+) is above the Archidekt bracket (3)",)
    assert summary(check.causes) == [
        (BracketTag.RUTHLESS, Reason.FAST_TWO_CARD, ("fast piece 1", "fast piece 2"))
    ]
    assert check.cause_cards == {"fast piece 1", "fast piece 2"}
    # Set to 2, Powerful lifts it too.
    assert [c.tag for c in check_bracket(2, None, MIXED).causes] == [
        BracketTag.RUTHLESS,
        BracketTag.POWERFUL,
        BracketTag.POWERFUL,
    ]


def test_the_ceiling_is_checked_too_and_the_lower_limit_picks_the_causes() -> None:
    check = check_bracket(4, 2, MIXED)
    assert check.notes == (
        "the profile's bracket ceiling (2) is below the Archidekt bracket (4)",
        "the estimate (4+) is above the profile's bracket ceiling (2)",
    )
    assert len(check.causes) == 3


def test_a_deck_set_above_what_its_cards_fit_gets_a_note_and_no_causes() -> None:
    spicy = estimate(BracketTag.SPICY, (combo("x", 4, TwoCard.ARGUABLY),))
    check = check_bracket(5, None, spicy)
    assert check.notes == ("the deck is set to bracket 5, but its cards alone fit 3-4",)
    assert check.causes == ()
    # Ruthless fits 4 and up, so a bracket 5 deck fits it.
    assert check_bracket(5, None, MIXED).notes == ()


def test_a_ceiling_above_the_set_bracket_is_a_note_even_without_an_estimate() -> None:
    check = check_bracket(2, 3, None)
    assert check.notes == ("the profile's bracket ceiling (3) is above the Archidekt bracket (2)",)
    assert check.causes == ()


def test_banned_cards_are_always_a_cause() -> None:
    banned = estimate(BracketTag.BANNED, cards=(card("Mana Crypt", CardFlag.BANNED),))
    check = check_bracket(5, None, banned)
    assert check.notes == ("Commander Spellbook finds cards that are not legal in Commander",)
    assert summary(check.causes) == [(BracketTag.BANNED, Reason.BANNED, ("Mana Crypt",))]


def test_a_tag_the_rules_dont_explain_says_the_causes_may_be_incomplete(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Spellbook says Ruthless, but nothing in the answer meets a Ruthless rule.
    unexplained = estimate(BracketTag.RUTHLESS, (combo("medium", 3, TwoCard.YES),))
    check = check_bracket(2, None, unexplained)
    assert check.notes == (
        "the estimate (4+) is above the Archidekt bracket (2)",
        (
            "Commander Spellbook's tag (Ruthless) differs from its published rules (Powerful), "
            "so the causes may be incomplete"
        ),
    )
    assert [c.reason for c in check.causes] == [Reason.TWO_CARD]
    assert "differs from its rules" in caplog.text
