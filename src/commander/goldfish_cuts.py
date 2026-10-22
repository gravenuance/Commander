"""The goldfish's tie-break between the cut targets of an idea (ADR 0056).

Targets whose scores are close are ordered by what their swaps do to the headline figures; the rest keep the
order the score gave. Nothing here offers a target the score did not.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from commander.goldfish import FigureChange, GoldfishComparison
from commander.goldfish_json import Json, better_for_deck, figure_label
from commander.goldfish_run import headline
from commander.targets import Target

# Targets this close in score count as tied (the size of `targets.LIKE_FOR_LIKE_BONUS`).
TIE_MARGIN = 0.10
# Scores are floats: this keeps rounding from deciding whether two are exactly TIE_MARGIN apart.
_NOISE = 1e-9


@dataclass(slots=True, frozen=True)
class Verdict:
    """What one target's swap does to the headline figures: the ones that moved against the deck and the ones
    that moved its way."""

    comparison: GoldfishComparison
    worse: tuple[FigureChange, ...]
    better: tuple[FigureChange, ...]


@dataclass(slots=True, frozen=True)
class RankedCut:
    """A target in its final place. `verdict` is None for a target that was not played; `why` names the
    figure that put a target ahead of one the score ranked above it, and is None for any other.
    `score_place` is where the score put it, counting from 1."""

    target: Target
    verdict: Verdict | None
    score_place: int
    moved_up: bool
    why: str | None


def verdict_of(comparison: GoldfishComparison) -> Verdict:
    changes = headline(comparison)
    return Verdict(
        comparison,
        tuple(c for c in changes if better_for_deck(c) is False),
        tuple(c for c in changes if better_for_deck(c) is True),
    )


def tie_groups(targets: Sequence[Target]) -> list[range]:
    """The runs of targets that count as tied, in order, each a range of indices; one that ties with no other
    is a run of one. A run starts at a target and takes those after it that score at most TIE_MARGIN below
    it, so no two in a run are further apart. A target with no score (a picked cut) is a run of its own."""
    groups: list[range] = []
    head: float | None = None
    for index, target in enumerate(targets):
        if head is not None and target.score is not None and head - target.score <= TIE_MARGIN + _NOISE:
            groups[-1] = range(groups[-1].start, index + 1)
            continue
        groups.append(range(index, index + 1))
        head = target.score
    return groups


def rank_cuts(
    targets: Sequence[Target],
    compare: Callable[[Target], GoldfishComparison],
    *,
    everything: bool = False,
) -> list[RankedCut]:
    """The targets in the order the goldfish gives them: within each tie group, the target whose swap makes
    fewer headline figures worse first, then the one that makes more of them better, else the score's order.

    `compare` plays the idea against one target. Only the targets of a group of two or more are played,
    unless `everything` asks for every target's figures, which then change no order.
    """
    verdicts: dict[int, Verdict] = {}
    order: list[int] = []
    for group in tie_groups(targets):
        tied = len(group) > 1
        if tied or everything:
            verdicts.update((i, verdict_of(compare(targets[i]))) for i in group)
        members = list(group)
        if tied:
            members.sort(key=lambda i: (len(verdicts[i].worse), -len(verdicts[i].better)))
        order.extend(members)
    place = {index: spot for spot, index in enumerate(order)}
    return [_ranked(targets, index, place, verdicts) for index in order]


def _ranked(
    targets: Sequence[Target], index: int, place: dict[int, int], verdicts: dict[int, Verdict]
) -> RankedCut:
    verdict = verdicts.get(index)
    if place[index] >= index or verdict is None:
        return RankedCut(targets[index], verdict, index + 1, moved_up=False, why=None)
    passed = next(j for j in range(index) if place[j] > place[index])
    why = _why(targets, index, passed, verdicts)
    return RankedCut(targets[index], verdict, index + 1, moved_up=True, why=why)


def _why(targets: Sequence[Target], ahead: int, behind: int, verdicts: dict[int, Verdict]) -> str:
    """The figure that decided between a target and the one it passed: one the passed target makes worse and
    the other does not, else one the target makes better and the passed one does not."""
    winner, loser = verdicts[ahead], verdicts[behind]
    base = winner.comparison.base
    if len(loser.worse) > len(winner.worse):
        change = next(c for c in loser.worse if not _among(c, winner.worse))
        return f"{figure_label(change, base)} is worse with {targets[behind].name}"
    change = next(c for c in winner.better if not _among(c, loser.better))
    return f"{figure_label(change, base)} is better with {targets[ahead].name}"


def _among(change: FigureChange, changes: Sequence[FigureChange]) -> bool:
    return any((c.kind, c.subject, c.turn) == (change.kind, change.subject, change.turn) for c in changes)


def cuts_json(ranked: Sequence[RankedCut]) -> Json:
    """The targets in their order for the page: each with whether the goldfish moved it up, and why."""
    return {"cuts": [{"name": r.target.name, "up": r.moved_up, "why": r.why} for r in ranked]}
