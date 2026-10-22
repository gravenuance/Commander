"""Cuts of another role or theme than the idea's: allowed only on a surplus over what the peer decks hold
(ADR 0036), and ranked with the like-for-like ones on one score."""

from collections.abc import Mapping, Sequence

import pytest

from commander.carddb import ResolvedCard, Role
from commander.leanings import Leaning
from commander.peercounts import PeerCounts, RoleBand
from commander.targets import CutRules, Target, role_targets
from commander.weightedshare import NO_SHARES, ShareTable
from test_targets import card, info, names, table

IDEA_SHARE = 0.9
DECK_SHARE = 0.3
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
    leanings: Sequence[str] = (),
    peers: bool = True,
) -> CutRules:
    counts = PeerCounts(roles or {}, themes or {}) if peers else None
    found = tuple(Leaning(tag, (), 0.0, 5) for tag in leanings)
    return CutRules(frozenset(protected), shares, held or {}, found, peers=counts)


def for_draw_idea(deck: list[ResolvedCard], cut_rules: CutRules) -> tuple[Target, ...]:
    return role_targets(info("Mystic Remora", (Role.DRAW,)), [LEADER, *deck], cut_rules)


def for_stax_idea(deck: list[ResolvedCard], cut_rules: CutRules) -> tuple[Target, ...]:
    return role_targets(info("Thalia"), [LEADER, *deck], cut_rules)


def ramp(count: int) -> list[ResolvedCard]:
    return [card(f"Ramp {n}", (Role.RAMP,)) for n in range(1, count + 1)]


def shares_of(deck: list[ResolvedCard], over: Mapping[str, float] | None = None) -> ShareTable:
    """Everything the deck plays at 30%, the idea at 90%, and the named cards as given."""
    return table(
        {**{r.card.name.casefold(): DECK_SHARE for r in deck}, "mystic remora": IDEA_SHARE, **(over or {})}
    )


@pytest.mark.parametrize(
    ("ramp_cards", "middle", "offered"),
    [(9, 7.0, 4), (8, 7.0, 0), (7, 7.0, 0), (6, 7.0, 0), (9, 7.5, 0), (10, 7.5, 4)],
)
def test_a_role_surplus_cut_needs_two_cards_over_the_peers_median(
    ramp_cards: int, middle: float, offered: int
) -> None:
    deck = ramp(ramp_cards)
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, middle)}, shares=shares_of(deck)))
    assert len(found) == offered
    assert all(t.reason.startswith("surplus ramp: ") for t in found)


def test_a_role_the_peers_were_not_counted_for_is_no_surplus() -> None:
    deck = ramp(9)
    assert for_draw_idea(deck, rules(roles={Role.DRAW: band(1.0, 1.0)}, shares=shares_of(deck))) == ()


@pytest.mark.parametrize("role", [Role.RAMP, Role.DRAW, Role.REMOVAL, Role.TUTOR])
def test_ramp_draw_removal_and_tutors_are_the_roles_that_can_run_a_surplus(role: Role) -> None:
    deck = [card(f"Card {n}", (role,)) for n in range(1, 5)]
    found = role_targets(
        info("Mystic Remora"), [LEADER, *deck], rules(roles={role: band(1.0, 2.0)}, shares=shares_of(deck))
    )
    assert len(found) == 4
    assert found[0].reason.startswith(f"surplus {role.value}: 4 against a median of 2 in recent decks;")


def test_a_role_outside_the_four_keeps_the_peers_floor_instead() -> None:
    deck = [card("Shield 1", (Role.PROTECTION,)), card("Shield 2", (Role.PROTECTION,))]
    shares = shares_of(deck)
    # Two protection cards against a floor of two: cutting one leaves one.
    assert for_draw_idea(deck, rules(roles={Role.PROTECTION: band(2.0)}, shares=shares)) == ()
    found = for_draw_idea(deck, rules(roles={Role.PROTECTION: band(1.0)}, shares=shares))
    assert names(found) == ["Shield 1", "Shield 2"]
    assert found[0].reason.startswith("other role (protection);")


def test_a_card_that_fills_no_role_may_go_for_a_role_idea_whatever_the_decks_counts() -> None:
    deck = [card("Plain"), *ramp(3)]
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 3.0)}, shares=shares_of(deck)))
    assert names(found) == ["Plain"]
    assert found[0].reason.startswith("fills no role;")


def test_a_commander_does_not_count_towards_a_role() -> None:
    deck = ramp(7)
    # The leader also fills ramp, which would make eight.
    assert for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 7.0)}, shares=shares_of(deck))) == ()


def test_a_role_less_than_two_over_its_median_loses_no_card_but_other_roles_can() -> None:
    deck = [*ramp(5), card("Removal A", (Role.REMOVAL,)), card("Removal B", (Role.REMOVAL,)), card("Plain")]
    short = rules(roles={Role.RAMP: band(1.0, 7.0), Role.REMOVAL: band(0.0, 1.0)}, shares=shares_of(deck))
    assert names(for_draw_idea(deck, short)) == ["Plain"]
    # Two removal cards over a median of none: the surplus bonus puts them ahead of the plain card.
    roomy = rules(roles={Role.RAMP: band(1.0, 7.0), Role.REMOVAL: band(0.0, 0.0)}, shares=shares_of(deck))
    assert names(for_draw_idea(deck, roomy)) == ["Removal A", "Removal B", "Plain"]


def test_every_role_of_the_cut_card_must_run_two_over_its_median() -> None:
    both = card("Ramp and Removal", (Role.RAMP, Role.REMOVAL))
    deck = [both, *ramp(7), *[card(f"Removal {n}", (Role.REMOVAL,)) for n in range(1, 6)]]
    shares = shares_of(deck, {"ramp and removal": 0.0})
    # 8 ramp cards and 6 removal ones.
    roomy = rules(roles={Role.RAMP: band(1.0, 6.0), Role.REMOVAL: band(1.0, 4.0)}, shares=shares)
    assert names(for_draw_idea(deck, roomy))[0] == "Ramp and Removal"
    tight = rules(roles={Role.RAMP: band(1.0, 6.0), Role.REMOVAL: band(1.0, 5.0)}, shares=shares)
    assert "Ramp and Removal" not in names(for_draw_idea(deck, tight))


def test_a_surplus_cut_scores_a_bonus_by_how_far_over_the_median_the_deck_is() -> None:
    deck = [
        *[card(f"Ramp {n}", (Role.RAMP,)) for n in range(1, 10)],
        *[card(f"Removal {n}", (Role.REMOVAL,)) for n in range(1, 9)],
    ]
    shares = shares_of(deck, {"ramp 1": 0.10, "removal 1": 0.08})
    # Ramp is 3 over the median of 6, removal 2 over: 0.9 - 0.10 + 3 * 0.03 beats 0.9 - 0.08 + 2 * 0.03.
    over = rules(roles={Role.RAMP: band(1.0, 6.0), Role.REMOVAL: band(1.0, 6.0)}, shares=shares)
    assert names(for_draw_idea(deck, over))[:2] == ["Ramp 1", "Removal 1"]
    # Two over for each: the bonus is level and the less played removal card leads.
    level = rules(roles={Role.RAMP: band(1.0, 7.0), Role.REMOVAL: band(1.0, 6.0)}, shares=shares)
    assert names(for_draw_idea(deck, level))[:2] == ["Removal 1", "Ramp 1"]


def test_a_card_of_two_surplus_roles_scores_by_the_role_it_over_runs_most() -> None:
    both = card("Ramp and Removal", (Role.RAMP, Role.REMOVAL))
    deck = [
        both,
        *[card(f"Ramp {n}", (Role.RAMP,)) for n in "ABC"],
        card("Removal A", (Role.REMOVAL,)),
        card("Removal B", (Role.REMOVAL,)),
    ]
    # Ramp is 4 against a median of 1 (3 over), removal 3 against 1 (2 over).
    shares = shares_of(deck, {"ramp and removal": 0.26, "removal a": 0.245})
    found = for_draw_idea(
        deck, rules(roles={Role.RAMP: band(1.0, 1.0), Role.REMOVAL: band(1.0, 1.0)}, shares=shares)
    )
    # 0.64 + 3 * 0.03 = 0.73 for the two-role card against 0.655 + 2 * 0.03 = 0.715; its least over-run role
    # would have scored 0.70, behind it. The ramp cards follow on 0.60 + 0.09 = 0.69.
    assert names(found) == ["Ramp and Removal", "Removal A", "Ramp A", "Ramp B"]
    assert found[0].reason.startswith("surplus ramp: 4 against a median of 1 in recent decks;")


def test_a_surplus_cut_scoring_higher_than_a_like_for_like_one_comes_first_and_vice_versa() -> None:
    deck = [
        card("Old Draw", (Role.DRAW,)),
        card("Low Ramp", (Role.RAMP,)),
        card("High Ramp", (Role.RAMP,)),
        card("Third Ramp", (Role.RAMP,)),
    ]
    peers = {Role.RAMP: band(1.0, 1.0)}
    # Old Draw 0.10 + (0.9 - 0.45), Low Ramp (0.9 - 0.05) + 2 * 0.03, the others (0.9 - 0.3) + 2 * 0.03.
    low = shares_of(deck, {"old draw": 0.45, "low ramp": 0.05})
    surplus = for_draw_idea(deck, rules(roles=peers, shares=low))
    assert names(surplus) == ["Low Ramp", "High Ramp", "Third Ramp", "Old Draw"]
    assert surplus[0].reason.startswith("surplus ramp: 3 against a median of 1 in recent decks;")
    assert surplus[3].reason.startswith("same role (draw);")
    # Old Draw at 0.05 scores 0.10 + 0.85, ahead of Low Ramp's 0.91.
    like = for_draw_idea(
        deck, rules(roles=peers, shares=shares_of(deck, {"old draw": 0.05, "low ramp": 0.05}))
    )
    assert names(like) == ["Old Draw", "Low Ramp", "High Ramp", "Third Ramp"]


def test_equal_scores_go_to_the_bigger_surplus_then_the_less_popular_card_then_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("commander.targets.SURPLUS_BONUS", 0.0)
    ramp_cards = [card(f"Alpha Ramp {n}", (Role.RAMP,), rank=100) for n in (1, 2, 3)]
    removal = [card(f"Zed Removal {n}", (Role.REMOVAL,), rank=100) for n in (1, 2, 3)]
    deck = [*ramp_cards, *removal]
    peers = {Role.RAMP: band(1.0, 1.0), Role.REMOVAL: band(0.0, 0.0)}
    # Ramp is 2 over its median, removal 3: every card scores 0.6, and removal leads despite the names.
    found = for_draw_idea(deck, rules(roles=peers, shares=shares_of(deck)))
    assert names(found) == ["Zed Removal 1", "Zed Removal 2", "Zed Removal 3", "Alpha Ramp 1"]
    # At equal surplus the higher rank number leads, the unranked card last, then the name.
    same = [
        card("B", (Role.RAMP,), rank=300),
        card("A", (Role.RAMP,), rank=300),
        card("U", (Role.RAMP,), rank=None),
    ]
    same += [card("C", (Role.RAMP,), rank=900)]
    found = for_draw_idea(same, rules(roles={Role.RAMP: band(0.0, 0.0)}, shares=shares_of(same)))
    assert names(found) == ["C", "A", "B", "U"]


def test_a_cut_of_another_role_says_what_makes_it_a_surplus() -> None:
    deck = ramp(9)
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 6.0)}, shares=shares_of(deck)))
    assert found[0].reason == (
        "surplus ramp: 9 against a median of 6 in recent decks;"
        " in 30% of meta lists (6/20); EDHREC rank 1,000"
    )


def test_a_card_filed_as_two_entries_counts_once_towards_a_role_as_it_does_for_the_peers() -> None:
    deck = [*ramp(9), *ramp(1)]
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 6.0)}, shares=shares_of(deck)))
    assert found[0].reason.startswith("surplus ramp: 9 against a median of 6 in recent decks;")


def listed_as_ramp(count: int) -> list[ResolvedCard]:
    """Cards only the deck's own category calls ramp: the peers' counts never see those."""
    return [card(f"Listed {n}", categories=("Ramp",)) for n in range(1, count + 1)]


def test_the_decks_ramp_category_does_not_lift_a_card_over_the_peers_median() -> None:
    deck = [*ramp(5), *listed_as_ramp(3)]
    played = {f"listed {n}": 0.4 for n in range(1, 4)}
    found = names(
        for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 5.0)}, shares=shares_of(deck, played)))
    )
    # Five tagged ramp cards are exactly the median: none of them may go, the category-only ones can.
    assert found == ["Listed 1", "Listed 2", "Listed 3"]


def test_the_decks_ramp_category_does_not_make_it_over_run() -> None:
    deck = [*ramp(6), *listed_as_ramp(3)]
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 6.0)}, shares=shares_of(deck)))
    # Six tagged ramp cards are the peers' median, not over it; the category-only ones are no surplus.
    assert names(found) == ["Listed 1", "Listed 2", "Listed 3"]
    assert all(t.reason.startswith("other role (ramp);") for t in found)


def test_a_card_of_another_theme_is_cut_only_for_an_idea_20_points_more_played() -> None:
    deck = [card("Human A"), card("Human B"), card("Human C")]
    held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}

    def offered(out_share: float) -> list[str]:
        shares = table({"thalia": 0.35, "human a": out_share, "human b": 0.9, "human c": 0.9})
        return names(for_stax_idea(deck, rules(themes={"Humans": 1.0}, shares=shares, held=held)))

    # An idea at 35% needs only 17.5 points over a cut for MIN_GAP, so the theme's 20 points decide.
    assert offered(0.16) == []
    assert offered(0.15) == ["Human A"]
    assert offered(0.0) == ["Human A"]


def test_a_theme_surplus_cut_needs_two_cards_over_the_peers_median() -> None:
    def offered(humans: int, peer_count: float) -> bool:
        deck = [card(f"Human {n}") for n in "ABCDEF"[:humans]]
        held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}
        return bool(
            for_stax_idea(
                deck, rules(themes={"Humans": peer_count}, shares=table({"thalia": 0.9}), held=held)
            )
        )

    assert offered(5, 3.0)
    assert not offered(4, 3.0)
    assert not offered(3, 3.0)
    assert not offered(2, 3.0)
    # A median that is not a whole card: five Humans are not two over 3.5, six are.
    assert not offered(5, 3.5)
    assert offered(6, 3.5)


def test_every_theme_of_the_cut_card_must_run_two_over_its_median() -> None:
    held = {
        "both": frozenset({"Humans", "Soldiers"}),
        "human a": frozenset({"Humans"}),
        "human b": frozenset({"Humans"}),
        "soldier a": frozenset({"Soldiers"}),
        "soldier b": frozenset({"Soldiers"}),
        "thalia": frozenset({"Stax"}),
    }
    deck = [card("Both"), card("Human A"), card("Human B"), card("Soldier A"), card("Soldier B")]
    shares = table(
        {"thalia": 0.9, "both": 0.0, "human a": 0.9, "human b": 0.9, "soldier a": 0.9, "soldier b": 0.9}
    )
    enough = rules(themes={"Humans": 1.0, "Soldiers": 1.0}, shares=shares, held=held)
    assert names(for_stax_idea(deck, enough)) == ["Both"]
    # The deck has three soldiers, which is not two over the peers' two.
    short = rules(themes={"Humans": 1.0, "Soldiers": 2.0}, shares=shares, held=held)
    assert for_stax_idea(deck, short) == ()


def test_a_theme_without_a_peer_count_is_no_surplus_and_loses_no_card() -> None:
    held = {"dinosaur": frozenset({"Dinosaur"}), "thalia": frozenset({"Stax"})}
    shares = table({"thalia": 0.9, "dinosaur": 0.0})
    assert for_stax_idea([card("Dinosaur")], rules(shares=shares, held=held)) == ()


def test_a_theme_surplus_cut_says_so_and_scores_by_how_far_over_the_median_the_deck_is() -> None:
    deck = [card("Axe Soldier 1"), card("Axe Soldier 2"), card("Human X"), card("Human Y"), card("Human Z")]
    held = {
        r.card.name.casefold(): frozenset({"Soldiers" if "Soldier" in r.card.name else "Humans"})
        for r in deck
    }
    held["thalia"] = frozenset({"Stax"})
    shares = table({"thalia": 0.5, **{r.card.name.casefold(): 0.2 for r in deck}})
    # Three Humans against a median of none, two Soldiers: 0.3 + 3 * 0.03 beats 0.3 + 2 * 0.03, though the
    # soldiers would lead alphabetically.
    found = for_stax_idea(deck, rules(themes={"Humans": 0.0, "Soldiers": 0.0}, shares=shares, held=held))
    assert names(found) == ["Human X", "Human Y", "Human Z", "Axe Soldier 1"]
    assert found[0].reason == (
        "surplus theme (Humans): 3 against a median of 0 in recent decks; idea 30 points more played;"
        " in 20% of meta lists (4/20); EDHREC rank 1,000"
    )


def test_a_leanings_cards_are_not_cut_as_a_theme_surplus() -> None:
    deck = [card(f"Untapper {n}") for n in range(1, 4)]
    held = {r.card.name.casefold(): frozenset({"extra-combat"}) for r in deck} | {
        "thalia": frozenset({"Stax"})
    }
    shares = table({"thalia": 0.9})
    plain = rules(themes={"extra-combat": 0.0}, shares=shares, held=held)
    assert len(for_stax_idea(deck, plain)) == 3
    leaning = rules(themes={"extra-combat": 0.0}, shares=shares, held=held, leanings=["extra-combat"])
    assert for_stax_idea(deck, leaning) == ()


def test_a_leanings_cards_are_not_cut_as_a_role_surplus_either_unless_the_idea_serves_it() -> None:
    deck = ramp(3)
    held = {r.card.name.casefold(): frozenset({"extra-combat"}) for r in deck}

    def offered(*leanings: str) -> int:
        cut_rules = rules(
            roles={Role.RAMP: band(1.0, 1.0)}, shares=shares_of(deck), held=held, leanings=leanings
        )
        return len(for_draw_idea(deck, cut_rules))

    assert offered() == 3
    assert offered("extra-combat") == 0
    # An idea of the leaning replaces its cards as before: it shares the theme, and ramp runs a surplus.
    held["mystic remora"] = frozenset({"extra-combat"})
    assert offered("extra-combat") == 3


def test_a_card_carrying_a_surplus_role_is_never_judged_as_a_theme_cut() -> None:
    deck = [card("Human Ramp", (Role.RAMP,)), card("Human B"), card("Human C")]
    held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}
    # Humans run 3 against a median of 1, which would let the ramp card go, but ramp runs 1 against 5.
    found = for_stax_idea(
        deck,
        rules(
            roles={Role.RAMP: band(1.0, 5.0)},
            themes={"Humans": 1.0},
            shares=table({"thalia": 0.9}),
            held=held,
        ),
    )
    assert names(found) == ["Human B", "Human C"]


def test_a_card_carrying_a_surplus_role_goes_by_the_role_whatever_its_theme_holds() -> None:
    deck = [card(f"Human Ramp {n}", (Role.RAMP,)) for n in range(1, 4)]
    held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {"thalia": frozenset({"Stax"})}
    # The deck runs 3 Humans against a median of 5, which would refuse a theme cut.
    found = for_stax_idea(
        deck,
        rules(
            roles={Role.RAMP: band(1.0, 1.0)},
            themes={"Humans": 5.0},
            shares=table({"thalia": 0.9}),
            held=held,
        ),
    )
    assert len(found) == 3
    assert found[0].reason.startswith("surplus ramp: 3 against a median of 1 in recent decks;")


def test_cards_unranked_on_edhrec_are_cut_last_among_equals() -> None:
    deck = [card("Unranked", (Role.RAMP,), rank=None), card("Ramp 1", (Role.RAMP,), rank=100)]
    deck.append(card("Ramp 2", (Role.RAMP,), rank=200))
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(0.0)}, shares=shares_of(deck)))
    assert names(found) == ["Ramp 2", "Ramp 1", "Unranked"]


def test_without_peer_counts_a_cut_stays_in_the_ideas_role_and_themes_but_fills_four_slots() -> None:
    deck = ramp(9)
    assert for_draw_idea(deck, rules(peers=False, shares=shares_of(deck))) == ()
    held = {"human a": frozenset({"Humans"}), "thalia": frozenset({"Stax"})}
    shares = table({"thalia": 0.9, "human a": 0.0})
    assert for_stax_idea([card("Human A")], rules(peers=False, shares=shares, held=held)) == ()
    draws = [card(f"Draw {n}", (Role.DRAW,)) for n in range(1, 6)]
    found = for_draw_idea(draws, rules(peers=False, shares=shares_of(draws)))
    assert names(found) == ["Draw 1", "Draw 2", "Draw 3", "Draw 4"]
    assert all(t.reason.startswith("same role (draw);") for t in found)


def test_without_shares_nothing_of_another_role_or_theme_is_offered() -> None:
    assert for_draw_idea(ramp(9), rules(roles={Role.RAMP: band(0.0)})) == ()
    assert for_draw_idea(ramp(9), rules(roles={Role.RAMP: band(0.0)}, shares=NO_SHARES)) == ()
    held = {"human a": frozenset({"Humans"}), "thalia": frozenset({"Stax"})}
    assert for_stax_idea([card("Human A")], rules(held=held)) == ()


def test_the_decks_kept_cards_are_never_cut_for_another_role_or_theme() -> None:
    deck = ramp(9)
    shares = shares_of(deck, {"ramp 1": 0.0})
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(0.0)}, shares=shares, protected=["ramp 1"]))
    assert "Ramp 1" not in names(found)
    assert len(found) == 4
    humans = [card("Human A"), card("Human B")]
    held = {"human a": frozenset({"Humans"}), "human b": frozenset({"Humans"}), "thalia": frozenset({"Stax"})}
    shares = table({"thalia": 0.9})
    kept = rules(themes={"Humans": 0.0}, shares=shares, held=held, protected=["human a"])
    assert names(for_stax_idea(humans, kept)) == ["Human B"]


def test_a_card_of_another_role_is_cut_only_for_an_idea_more_played_than_it() -> None:
    deck = [
        card("Played Ramp", (Role.RAMP,)),
        card("Level Ramp", (Role.RAMP,)),
        card("Rare Ramp", (Role.RAMP,)),
    ]
    shares = table(
        {"mystic remora": IDEA_SHARE, "played ramp": 0.95, "level ramp": IDEA_SHARE, "rare ramp": 0.2}
    )
    assert names(for_draw_idea(deck, rules(roles={Role.RAMP: band(0.0)}, shares=shares))) == ["Rare Ramp"]


def test_a_card_of_a_role_the_idea_shares_but_of_another_theme_goes_by_theme_surplus() -> None:
    deck = [card("Human Draw", (Role.DRAW,)), card("Human B"), card("Human C")]
    held = {r.card.name.casefold(): frozenset({"Humans"}) for r in deck} | {
        "mystic remora": frozenset({"Stax"})
    }
    found = for_draw_idea(deck, rules(themes={"Humans": 1.0}, shares=shares_of(deck), held=held))
    assert names(found) == ["Human B", "Human C", "Human Draw"]
    assert found[2].reason.startswith("surplus theme (Humans): 3 against a median of 1 in recent decks;")


def test_scores_equal_up_to_float_noise_go_to_the_bigger_surplus() -> None:
    # Old Draw: 0.10 + (0.95 - 0.0) and each ramp card: (0.95 - 0.05) + 5 * 0.03 are both 1.05, though the
    # first computes as 1.05 and the second as 1.0499999999999998.
    ramp_cards = ramp(6)
    deck = [card("Old Draw", (Role.DRAW,)), *ramp_cards]
    shares = shares_of(
        deck, {"mystic remora": 0.95, "old draw": 0.0, **{r.card.name.casefold(): 0.05 for r in ramp_cards}}
    )
    found = for_draw_idea(deck, rules(roles={Role.RAMP: band(1.0, 1.0)}, shares=shares))
    assert names(found) == ["Ramp 1", "Ramp 2", "Ramp 3", "Ramp 4"]
