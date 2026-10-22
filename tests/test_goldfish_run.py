"""Running the goldfish for a deck and for swaps on it: the headline figures, the swaps a board stands for and
the cache that keeps a deck's games (ADR 0054)."""

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from commander.board import Actor, Board, BoardCard, Status
from commander.carddb import CardDb
from commander.deckprofile import Profile
from commander.goldfish import (
    FigureChange,
    FigureKind,
    GoldfishComparison,
    GoldfishReport,
)
from commander.goldfish_load import GoldfishError, load_goldfish
from commander.goldfish_run import (
    MAX_KEPT_COMPARISONS,
    GoldfishCache,
    GoldfishRun,
    SwapRequest,
    compare_swaps,
    headline,
    headline_turn,
    queued_swaps,
    run,
    swap_of,
)
from commander.models import Deck
from test_meta import NOW, db, deck

__all__ = ["db", "deck"]  # fixtures shared with test_meta

GAMES = 200


@pytest.fixture
def played(deck: Deck, db: CardDb, tmp_path: Path) -> GoldfishRun:
    """Magda's deck of six Mountains, a Golem and a Flute, played 200 times."""
    return run(load_goldfish(deck, db, Profile(), tmp_path, now=lambda: NOW), games=GAMES)


def shown(report: GoldfishReport, keep_bracket: int, turns: tuple[int, ...]) -> GoldfishReport:
    """The report as if it had been played by another bracket, which only its turns and rule tell."""
    return replace(report, keep_bracket=keep_bracket, turns=turns)


def moved(kind: FigureKind, subject: str, turn: int | None, *, did: bool = True) -> FigureChange:
    return FigureChange(kind, subject, turn, 0.25, 0.5, did)


@pytest.mark.parametrize(
    ("keep_bracket", "turns", "expected"),
    [
        (1, (7, 8, 9, 10, 11), 9),
        (2, (6, 7, 8, 9, 10), 8),
        (3, (4, 5, 6, 7, 8), 6),
        (4, (2, 3, 4, 5, 6), 4),
        (5, (1, 2, 3, 4, 5), 3),
    ],
)
def test_the_headline_turn_is_the_brackets_and_the_third_in_bracket_5(
    played: GoldfishRun, keep_bracket: int, turns: tuple[int, ...], expected: int
) -> None:
    assert headline_turn(shown(played.report, keep_bracket, turns)) == expected


def test_a_headline_turn_not_among_the_turns_shown_is_the_nearest_one_and_the_earlier_on_a_tie(
    played: GoldfishRun,
) -> None:
    assert headline_turn(shown(played.report, 4, (1, 2))) == 2
    assert headline_turn(shown(played.report, 3, (7, 8))) == 7
    assert headline_turn(shown(played.report, 3, (4, 8))) == 4


def test_only_the_headline_figures_that_moved_are_shown_at_the_brackets_turn(played: GoldfishRun) -> None:
    base = shown(played.report, 3, (4, 5, 6, 7, 8))
    figures = (
        moved(FigureKind.COMMANDER_CAST_BY, "Magda", 5),
        moved(FigureKind.COMMANDER_CAST_BY, "Magda", 6),
        moved(FigureKind.COMMANDER_CAST_BY, "Partner", 6, did=False),
        moved(FigureKind.COMMANDER_MEDIAN, "Magda", None),
        moved(FigureKind.MANA_MEDIAN, "", 6),
        moved(FigureKind.MANA_WORST_TENTH, "", 7),
        moved(FigureKind.MANA_WORST_TENTH, "", 6),
        moved(FigureKind.COLOUR_SCREW, "", None),
        moved(FigureKind.KEPT_ON_SEVEN, "", None),
        moved(FigureKind.COMBO_LANDED_BY, "1-2", 6),
        moved(FigureKind.COMBO_LANDED_BY, "3-4", 8),
        moved(FigureKind.COMBO_FETCHED, "1-2", None),
        moved(FigureKind.FINISHER_CASTABLE_BY, "Overrun", 6),
    )
    comparison = GoldfishComparison(base, base, figures, ())
    assert [(c.kind, c.subject, c.turn) for c in headline(comparison)] == [
        (FigureKind.COMMANDER_CAST_BY, "Magda", 6),
        (FigureKind.MANA_WORST_TENTH, "", 6),
        (FigureKind.COLOUR_SCREW, "", None),
        (FigureKind.COMBO_LANDED_BY, "1-2", 6),
    ]


def test_a_comparison_in_which_nothing_moved_has_no_headline(played: GoldfishRun) -> None:
    figures = (moved(FigureKind.COLOUR_SCREW, "", None, did=False),)
    assert headline(GoldfishComparison(played.report, played.report, figures, ())) == []


def board_card(name: str, status: Status, choice: str | None = None, copies: int = 1) -> BoardCard:
    return BoardCard(
        id=name.casefold(),
        name=name,
        oracle_id="",
        source=Actor.YOU,
        reason="",
        replace=(),
        status=status,
        choice=choice,
        note="",
        added_at="2026-10-15",
        updated_at="2026-10-15",
        copies=copies,
    )


def test_a_card_queued_with_a_cut_is_a_swap_and_nothing_else_is() -> None:
    queued = board_card("Arcane Signet", Status.ADD, "Battered Golem", copies=1)
    assert swap_of(queued) == SwapRequest("Arcane Signet", "Battered Golem", 1)
    assert swap_of(board_card("Mind Stone", Status.ADD)) is None
    assert swap_of(board_card("Mind Stone", Status.OPEN)) is None
    assert swap_of(board_card("Mind Stone", Status.REJECTED)) is None
    assert swap_of(board_card("Mind Stone", Status.APPLIED)) is None
    board = Board(
        (
            queued,
            board_card("Mind Stone", Status.ADD),
            board_card("Mana Crypt", Status.ADD, "Disruptor Flute"),
        )
    )
    assert queued_swaps(board) == [
        SwapRequest("Arcane Signet", "Battered Golem", 1),
        SwapRequest("Mana Crypt", "Disruptor Flute", 1),
    ]


def test_a_swap_is_played_against_the_games_already_played(played: GoldfishRun, db: CardDb) -> None:
    # The library holds 8 cards, so the 7 of the hand and the first draw show every one of them: Mana Crypt
    # (free, 2 colourless) is out on turn 1 in every game, 2 more mana on each turn, whatever the shuffle.
    comparison = compare_swaps(played, db, [SwapRequest("Mana Crypt", "Battered Golem")])
    assert comparison.base is played.report
    assert comparison.swapped.games == GAMES
    assert comparison.base.mana_median == (1, 2, 3, 4, 5)
    assert comparison.swapped.mana_median == (3, 4, 5, 6, 7)
    assert comparison.swapped.mana_worst_tenth == (3, 4, 5, 6, 7)
    assert len(comparison.replayed) == GAMES
    mana = [(c.kind, c.turn) for c in comparison.moved if c.kind is FigureKind.MANA_MEDIAN]
    assert mana == [(FigureKind.MANA_MEDIAN, turn) for turn in (1, 2, 3, 4, 5)]
    # In bracket 5 the headline turn is 3, where the worst tenth went from 3 to 5.
    worst = [c for c in headline(comparison) if c.kind is FigureKind.MANA_WORST_TENTH]
    assert [(c.turn, c.base, c.swapped) for c in worst] == [(3, 3, 5)]


def test_a_swap_of_two_plain_cards_replays_but_moves_nothing(played: GoldfishRun, db: CardDb) -> None:
    comparison = compare_swaps(played, db, [SwapRequest("Chaos Warp", "Disruptor Flute")])
    # Their costs differ, so the games are replayed; neither card makes mana or wins.
    assert comparison.replayed != ()
    assert not comparison.any_moved
    assert headline(comparison) == []


@pytest.mark.parametrize(
    ("swap", "message"),
    [
        (
            SwapRequest("Arcane Signet", "Chaos Warp"),
            "Chaos Warp is not in the deck, so it cannot be swapped out",
        ),
        (
            SwapRequest("Arcane Signet", "Magda, Brazen Outlaw"),
            "Magda, Brazen Outlaw is a commander, so it cannot be swapped out",
        ),
        (SwapRequest("Battered Golem", "Disruptor Flute"), "Battered Golem is in the deck already"),
        (
            SwapRequest("Magda, Brazen Outlaw", "Disruptor Flute"),
            "Magda, Brazen Outlaw is in the deck already",
        ),
        (
            SwapRequest("Not A Real Card", "Disruptor Flute"),
            "'Not A Real Card' is no card the card data knows",
        ),
        (
            SwapRequest("Arcane Signet", "Mountain"),
            "the deck holds 6 of Mountain and 1 are swapped; only a swap of all of them is played",
        ),
    ],
)
def test_a_swap_that_cannot_be_played_says_why_and_a_lenient_comparison_leaves_it_out(
    played: GoldfishRun, db: CardDb, swap: SwapRequest, message: str
) -> None:
    with pytest.raises(GoldfishError) as raised:
        compare_swaps(played, db, [swap])
    assert str(raised.value) == message
    left_out = compare_swaps(played, db, [swap], lenient=True)
    assert not left_out.any_moved
    assert left_out.replayed == ()


def test_a_card_cut_twice_is_an_error_and_the_second_swap_is_left_out_when_lenient(
    played: GoldfishRun, db: CardDb
) -> None:
    swaps = [SwapRequest("Arcane Signet", "Disruptor Flute"), SwapRequest("Mind Stone", "Disruptor Flute")]
    with pytest.raises(GoldfishError, match="Disruptor Flute is cut twice"):
        compare_swaps(played, db, swaps)
    both = compare_swaps(played, db, swaps, lenient=True)
    first = compare_swaps(played, db, swaps[:1])
    assert both.swapped.mana_median == first.swapped.mana_median
    assert both.replayed == first.replayed


def test_swaps_are_found_by_any_case_or_the_front_face_of_a_two_faced_name(
    played: GoldfishRun, db: CardDb
) -> None:
    lower = compare_swaps(played, db, [SwapRequest("arcane signet", "disruptor flute")])
    exact = compare_swaps(played, db, [SwapRequest("Arcane Signet", "Disruptor Flute")])
    assert lower.swapped.mana_median == exact.swapped.mana_median


def test_a_deck_is_played_once_for_its_stamp_and_again_when_the_stamp_changes(played: GoldfishRun) -> None:
    cache = GoldfishCache()
    plays: list[str] = []

    def play() -> GoldfishRun:
        plays.append("played")
        return played

    assert cache.played(1, "a", play) is played
    assert cache.played(1, "a", play) is played
    assert len(plays) == 1
    cache.played(1, "b", play)
    assert len(plays) == 2
    cache.played(2, "b", play)
    assert len(plays) == 3
    cache.clear()
    cache.played(1, "b", play)
    assert len(plays) == 4


def test_a_swap_is_compared_once_per_stamp_and_a_new_stamp_forgets_it(played: GoldfishRun) -> None:
    cache = GoldfishCache()
    compared: list[SwapRequest] = []
    answer = GoldfishComparison(played.report, played.report, (), ())
    swaps = (SwapRequest("Arcane Signet", "Disruptor Flute"),)

    def compare(_: GoldfishRun) -> GoldfishComparison:
        compared.extend(swaps)
        return answer

    for _ in range(2):
        assert cache.compared(1, "a", swaps, lambda: played, compare) is answer
    assert len(compared) == 1
    other = (SwapRequest("Mind Stone", "Battered Golem"),)
    cache.compared(1, "a", other, lambda: played, compare)
    assert len(compared) == 2
    cache.compared(1, "b", swaps, lambda: played, compare)
    assert len(compared) == 3


def test_a_deck_keeps_only_so_many_comparisons(played: GoldfishRun) -> None:
    cache = GoldfishCache()
    made: list[int] = []
    answer = GoldfishComparison(played.report, played.report, (), ())

    def compare(_: GoldfishRun) -> GoldfishComparison:
        made.append(1)
        return answer

    def ask(number: int) -> None:
        cache.compared(1, "a", (SwapRequest(f"Card {number}", "Disruptor Flute"),), lambda: played, compare)

    for number in range(MAX_KEPT_COMPARISONS + 1):
        ask(number)
    count = len(made)
    ask(MAX_KEPT_COMPARISONS)
    assert len(made) == count
    # The first was dropped to make room for the last.
    ask(0)
    assert len(made) == count + 1


def test_two_askers_for_the_same_deck_play_it_once(played: GoldfishRun) -> None:
    cache = GoldfishCache()
    started, release = threading.Event(), threading.Event()
    plays: list[int] = []

    def slow_play() -> GoldfishRun:
        plays.append(1)
        started.set()
        assert release.wait(timeout=10)
        return played

    results: list[GoldfishRun] = []

    def ask() -> None:
        results.append(cache.played(1, "a", slow_play))

    first = threading.Thread(target=ask)
    first.start()
    assert started.wait(timeout=10)
    second = threading.Thread(target=ask)
    second.start()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert plays == [1]
    assert results == [played, played]
