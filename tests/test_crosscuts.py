"""Cuts of another role or theme than the idea's: allowed only against what the peer decks hold (step C2)."""

from collections.abc import Mapping, Sequence

import pytest

from commander.carddb import ResolvedCard, Role
from commander.peercounts import PeerCounts, RoleBand
from commander.targets import CutRules, Target, role_targets
from commander.weightedshare import ShareTable
from test_targets import card, info, names, table

IDEA_SHARE = 0.5
LEADER = card("Leader", (Role.RAMP,), categories=("Commander",))


def band(floor: float, middle: float | None = None) -> RoleBand:
    return RoleBand(floor, floor if middle is None else middle)


def rules(
    *,
    roles: Mapping[Role, RoleBand] | None = None,
    themes: Mapping[str, float] | None = None,
    shares: ShareTable | None = None,
    held: Mapping[str, frozenset[str]] | None = None,
    protected: Sequence[str] = (),
    peers: bool = True,
) -> CutRules:
    counts = PeerCounts(roles or {}, themes or {}) if peers else None
    return CutRules(frozenset(protected), shares, held or {}, peers=counts)


def for_draw_idea(deck: list[ResolvedCard], cut_rules: CutRules) -> tuple[Target, ...]:
    return role_targets(info("Mystic Remora", (Role.DRAW,)), [LEADER, *deck], cut_rules)


def for_stax_idea(deck: list[ResolvedCard], cut_rules: CutRules) -> tuple[Target, ...]:
    return role_targets(info("Thalia"), [LEADER, *deck], cut_rules)


def ramp(count: int) -> list[ResolvedCard]:
    return [card(f"Ramp {n}", (Role.RAMP,)) for n in range(1, count + 1)]


def shares_of(deck: list[ResolvedCard], over: Mapping[str, float] | None = None) -> ShareTable:
    """Everything the deck plays at 90%, the idea at 50%, and the named cards as given."""
    return table({**{r.card.name.casefold(): 0.9 for r in deck}, "mystic remora": IDEA_SHARE, **(over or {})})


@pytest.mark.parametrize(
    ("ramp_cards", "floor", "offered"), [(8, 7.0, 3), (7, 7.0, 0), (6, 7.0, 0), (8, 7.5, 0)]
)
def test_a_cut_of_another_role_must_leave_the_role_at_or_over_the_peers_25th_percentile(
    ramp_cards: int, floor: float, offered: int
) -> None:
    deck = ramp(ramp_cards)
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(floor)}, shares=shares_of(deck)))
    assert len(found) == offered


def test_a_commander_does_not_count_towards_a_role() -> None:
    deck = ramp(7)
    # The leader also fills ramp, which would make eight.
    assert for_draw_idea(deck, rules(roles={Role.RAMP: band(7.0)}, shares=shares_of(deck))) == ()


def test_a_role_already_under_its_floor_loses_no_card_but_other_roles_can() -> None:
    deck = [*ramp(5), card("Spot Removal", (Role.REMOVAL,)), card("Plain")]
    short = rules(roles={Role.RAMP: band(7.0), Role.REMOVAL: band(0.0, 1.0)}, shares=shares_of(deck))
    assert names(for_draw_idea(deck, short)) == ["Plain", "Spot Removal"]


def test_every_role_of_the_cut_card_must_keep_its_floor() -> None:
    both = card("Ramp and Removal", (Role.RAMP, Role.REMOVAL))
    deck = [both, *ramp(7), *[card(f"Removal {n}", (Role.REMOVAL,)) for n in range(1, 6)]]
    shares = shares_of(deck, {"ramp and removal": 0.0})
    # 8 ramp cards and 6 removal ones.
    roomy = rules(roles={Role.RAMP: band(7.0), Role.REMOVAL: band(5.0)}, shares=shares)
    assert names(for_draw_idea(deck, roomy))[0] == "Ramp and Removal"
    tight = rules(roles={Role.RAMP: band(7.0), Role.REMOVAL: band(6.0)}, shares=shares)
    assert "Ramp and Removal" not in names(for_draw_idea(deck, tight))


def test_cutting_from_a_role_the_deck_over_runs_scores_a_bonus_by_how_far_over() -> None:
    deck = [
        *[card(f"Ramp {n}", (Role.RAMP,)) for n in range(1, 10)],
        *[card(f"Removal {n}", (Role.REMOVAL,)) for n in range(1, 8)],
    ]
    shares = shares_of(deck, {"ramp 1": 0.10, "removal 1": 0.08})
    # Ramp is 3 over the peers' median of 6, removal 1 over: 0.5 - 0.10 + 3 * 0.05 beats 0.5 - 0.08 + 0.05.
    over = rules(roles={Role.RAMP: band(1.0, 6.0), Role.REMOVAL: band(1.0, 6.0)}, shares=shares)
    assert names(for_draw_idea(deck, over))[:2] == ["Ramp 1", "Removal 1"]
    # At the median there is no bonus, and the less played removal card leads.
    level = rules(roles={Role.RAMP: band(1.0, 9.0), Role.REMOVAL: band(1.0, 7.0)}, shares=shares)
    assert names(for_draw_idea(deck, level))[0] == "Removal 1"


def test_same_role_cuts_stay_first_and_the_others_follow_by_score() -> None:
    deck = [card("Old Draw", (Role.DRAW,)), card("Low Ramp", (Role.RAMP,)), card("High Ramp", (Role.RAMP,))]
    shares = shares_of(deck, {"old draw": 0.8, "low ramp": 0.05, "high ramp": 0.3})
    found = for_draw_idea(deck, rules(shares=shares))
    assert names(found) == ["Old Draw", "Low Ramp", "High Ramp"]
    assert found[0].reason.startswith("same role (draw);")
    assert found[1].reason.startswith("other role (ramp);")


def test_a_cut_of_another_role_says_how_far_the_deck_over_runs_it() -> None:
    deck = ramp(9)
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 6.0)}, shares=shares_of(deck)))
    assert found[0].reason == (
        "other role (ramp, over-run: 9 against a median of 6 in recent decks);"
        " in 90% of meta lists (18/20); EDHREC rank 1,000"
    )


def test_a_card_of_another_theme_is_cut_only_for_an_idea_20_points_more_played() -> None:
    deck = [card("Human A"), card("Human B"), card("Human C")]
    held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}

    def offered(out_share: float) -> list[str]:
        shares = table({"thalia": 0.5, "human a": out_share, "human b": 0.9, "human c": 0.9})
        return names(for_stax_idea(deck, rules(themes={"Humans": 1.0}, shares=shares, held=held)))

    assert offered(0.31) == []
    assert offered(0.30) == ["Human A"]
    assert offered(0.0) == ["Human A"]


def test_the_theme_a_cut_leaves_must_keep_the_peers_count() -> None:
    def offered(humans: int, peer_count: float) -> bool:
        deck = [card(f"Human {n}") for n in "ABCD"[:humans]]
        held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}
        return bool(
            for_stax_idea(
                deck, rules(themes={"Humans": peer_count}, shares=table({"thalia": 0.9}), held=held)
            )
        )

    assert offered(4, 3.0)
    assert not offered(3, 3.0)
    assert not offered(2, 3.0)


def test_every_theme_of_the_cut_card_must_keep_its_count() -> None:
    held = {
        "both": frozenset({"Humans", "Soldiers"}),
        "human": frozenset({"Humans"}),
        "soldier": frozenset({"Soldiers"}),
        "thalia": frozenset({"Stax"}),
    }
    deck = [card("Both"), card("Human"), card("Soldier")]
    shares = table({"thalia": 0.9, "both": 0.0, "human": 0.9, "soldier": 0.9})
    enough = rules(themes={"Humans": 1.0, "Soldiers": 1.0}, shares=shares, held=held)
    assert names(for_stax_idea(deck, enough)) == ["Both"]
    # The deck has two soldiers; losing one would leave fewer than the peers' two.
    short = rules(themes={"Humans": 1.0, "Soldiers": 2.0}, shares=shares, held=held)
    assert for_stax_idea(deck, short) == ()


def test_a_theme_without_a_peer_count_may_lose_one_card() -> None:
    held = {"dinosaur": frozenset({"Dinosaur"}), "thalia": frozenset({"Stax"})}
    shares = table({"thalia": 0.9, "dinosaur": 0.0})
    assert names(for_stax_idea([card("Dinosaur")], rules(shares=shares, held=held))) == ["Dinosaur"]


def test_cards_unranked_on_edhrec_are_cut_last_among_equals() -> None:
    deck = [card("Unranked", (Role.RAMP,), rank=None), card("Ramp 1", (Role.RAMP,), rank=100)]
    deck.append(card("Ramp 2", (Role.RAMP,), rank=200))
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(0.0)}, shares=shares_of(deck)))
    assert names(found) == ["Ramp 2", "Ramp 1", "Unranked"]


def test_without_peer_counts_a_cut_stays_in_the_ideas_role_and_themes() -> None:
    deck = ramp(9)
    assert for_draw_idea(deck, rules(peers=False, shares=shares_of(deck))) == ()
    held = {"human a": frozenset({"Humans"}), "thalia": frozenset({"Stax"})}
    shares = table({"thalia": 0.9, "human a": 0.0})
    assert for_stax_idea([card("Human A")], rules(peers=False, shares=shares, held=held)) == ()


def test_without_shares_nothing_of_another_role_or_theme_is_offered() -> None:
    assert for_draw_idea(ramp(9), rules(roles={Role.RAMP: band(0.0)})) == ()
    held = {"human a": frozenset({"Humans"}), "thalia": frozenset({"Stax"})}
    assert for_stax_idea([card("Human A")], rules(held=held)) == ()


def test_the_decks_kept_cards_are_never_cut_for_another_role_or_theme() -> None:
    deck = ramp(9)
    shares = shares_of(deck, {"ramp 1": 0.0})
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(0.0)}, shares=shares, protected=["ramp 1"]))
    assert "Ramp 1" not in names(found)
    assert len(found) == 3
