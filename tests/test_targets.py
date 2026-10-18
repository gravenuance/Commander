"""Which deck cards a board card could replace: hand-picked ones, else the weakest in its role."""

import pytest

from commander.board import Actor, BoardCard, ReplaceOption, Status
from commander.carddb import CardInfo, ResolvedCard, Role
from commander.models import DeckCard
from commander.targets import CutRules, Target, TargetSource, targets_for
from commander.weightedshare import NO_SHARES, ShareTable, SourceShare, WeightedShare


def info(
    name: str, roles: tuple[Role, ...] = (), rank: int | None = 1000, type_line: str = "Creature"
) -> CardInfo:
    return CardInfo(name.lower(), name, "{1}", 1.0, type_line, "G", False, True, 100, rank, frozenset(roles))  # noqa: FBT003 — positional like the model


def card(
    name: str,
    roles: tuple[Role, ...] = (),
    rank: int | None = 1000,
    *,
    categories: tuple[str, ...] = ("Creature",),
    type_line: str = "Creature",
) -> ResolvedCard:
    deck_card = DeckCard(name, 1, categories, True, "sid", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model
    return ResolvedCard(deck_card, info(name, roles, rank, type_line), None)


def board_card(name: str, replace: tuple[ReplaceOption, ...] = (), source: Actor = Actor.CLAUDE) -> BoardCard:
    return BoardCard(
        "id1",
        name,
        name.lower(),
        source,
        "",
        replace,
        Status.OPEN,
        None,
        "",
        "t",
        "t",
    )


def table(shares: dict[str, float], *, meta: bool = True, both: bool = False) -> ShareTable:
    """Shares of 20 lists, from the meta's EDHTop16 lists, else from recent decks, or from 10 of each."""

    def made(share: float, total: int) -> SourceShare:
        return SourceShare(share, round(share * total), total)

    def card(share: float) -> WeightedShare:
        if both:
            return WeightedShare(share, round(share * 20), 20, made(share, 10), made(share, 10))
        source = made(share, 20)
        return WeightedShare(share, source.count, 20, source if meta else None, None if meta else source)

    return ShareTable({key: card(share) for key, share in shares.items()}, card(0.0))


def names(targets: tuple[Target, ...]) -> list[str]:
    return [t.name for t in targets]


DECK = [
    card("Ellivere", categories=("Commander",)),
    card("Esper Sentinel", (Role.DRAW,), rank=300),
    card("Archivist of Oghma", (Role.DRAW,), rank=2500),
    card("Compost", (Role.DRAW,), rank=9000),
    card("Sylvan Library", (Role.DRAW,), rank=None),
    card("Birds of Paradise", (Role.RAMP,), rank=50),
    card("Kataki", rank=4000),
    card("Hushbringer", rank=800),
    card("Forest", rank=None, type_line="Basic Land — Forest", categories=("Land",)),
]


def test_hand_picked_targets_win_while_they_are_still_in_the_deck() -> None:
    picked = board_card(
        "Rhystic Study", (ReplaceOption("Compost", "not stax"), ReplaceOption("Gone Card", ""))
    )
    targets = targets_for(picked, info("Rhystic Study", (Role.DRAW,)), DECK, CutRules((), None))
    assert targets == (Target("Compost", "not stax", TargetSource.PICKED),)


def test_a_tools_generic_cut_list_gives_way_to_the_cards_role() -> None:
    generic = board_card("Mystic Remora", (ReplaceOption("Kataki", "no role, in no combo"),), Actor.CMDR)
    targets = targets_for(generic, info("Mystic Remora", (Role.DRAW,)), DECK, CutRules((), None))
    assert names(targets) == ["Compost", "Archivist of Oghma", "Esper Sentinel"]


def test_without_picks_the_weakest_cards_in_the_same_role_least_played_first_unranked_last() -> None:
    targets = targets_for(
        board_card("Mystic Remora"), info("Mystic Remora", (Role.DRAW,)), DECK, CutRules((), None)
    )
    assert names(targets) == ["Compost", "Archivist of Oghma", "Esper Sentinel"]
    assert targets[0].reason == "same role (draw); EDHREC rank 9,000"
    assert {t.source for t in targets} == {TargetSource.ROLE}


def test_meta_share_ranks_before_popularity_and_shows_in_the_reason() -> None:
    shares = table(
        {"mystic remora": 0.7, "compost": 0.6, "archivist of oghma": 0.0}
        | {"esper sentinel": 0.1, "sylvan library": 0.05}
    )
    targets = targets_for(
        board_card("Mystic Remora"), info("Mystic Remora", (Role.DRAW,)), DECK, CutRules((), shares)
    )
    assert names(targets) == ["Archivist of Oghma", "Sylvan Library", "Esper Sentinel"]
    assert targets[1].reason == "same role (draw); in 5% of meta lists (1/20); unranked on EDHREC"


def test_a_role_counts_by_the_decks_own_category_too() -> None:
    deck = [*DECK, card("Phyrexian Arena", rank=99999, categories=("Card Draw",))]
    targets = targets_for(
        board_card("Mystic Remora"), info("Mystic Remora", (Role.DRAW,)), deck, CutRules((), None)
    )
    assert names(targets)[0] == "Phyrexian Arena"


def test_kept_cards_commanders_and_the_card_itself_are_never_targets() -> None:
    deck = [*DECK, card("Mystic Remora", (Role.DRAW,), rank=99999)]
    targets = targets_for(
        board_card("Mystic Remora"),
        info("Mystic Remora", (Role.DRAW,)),
        deck,
        CutRules({"compost"}, None),
    )
    assert names(targets) == ["Archivist of Oghma", "Esper Sentinel", "Sylvan Library"]


def test_a_card_without_a_role_replaces_cards_without_one_and_lands_only_lands() -> None:
    hatebear = targets_for(board_card("Thalia"), info("Thalia"), DECK, CutRules((), None))
    assert names(hatebear) == ["Kataki", "Hushbringer"]
    assert hatebear[0].reason == "fills no role either; EDHREC rank 4,000"
    land = targets_for(board_card("Savannah"), info("Savannah", type_line="Land"), DECK, CutRules((), None))
    assert names(land) == ["Forest"]


def test_an_unknown_card_has_only_its_hand_picked_targets() -> None:
    assert targets_for(board_card("Nothing"), None, DECK, CutRules((), None)) == ()


def test_a_card_serving_a_theme_is_cut_only_for_an_idea_serving_the_same_and_those_come_first() -> None:
    themes = {
        "kataki": frozenset({"Stax"}),
        "thalia": frozenset({"Stax"}),
        "hushbringer": frozenset({"Stax"}),
    }
    rules = CutRules((), None, themes={**themes, "hushbringer": frozenset({"Stax", "Humans"})})
    deck = [*DECK, card("Grizzly Bears", rank=100)]
    plain = targets_for(board_card("Llanowar Elves"), info("Llanowar Elves"), deck, rules)
    assert names(plain) == ["Grizzly Bears"]
    stax = targets_for(board_card("Thalia"), info("Thalia"), deck, rules)
    assert names(stax) == ["Kataki", "Hushbringer", "Grizzly Bears"]
    assert stax[0].reason == "same theme (Stax); fills no role either; EDHREC rank 4,000"


def test_shares_name_the_sources_they_are_of() -> None:
    thalia = board_card("Thalia")
    decks = targets_for(
        thalia, info("Thalia"), DECK, CutRules((), table({"thalia": 0.5, "kataki": 0.3}, meta=False))
    )
    assert decks[0].reason == "fills no role either; in 0% of recent decks (0/20); EDHREC rank 800"
    pooled = targets_for(
        thalia, info("Thalia"), DECK, CutRules((), table({"thalia": 0.5, "kataki": 0.3}, both=True))
    )
    assert (
        pooled[1].reason
        == "fills no role either; in 30% of meta lists and recent decks (6/20); EDHREC rank 4,000"
    )


def test_a_card_in_both_sources_ranks_by_the_one_pooled_share() -> None:
    shares = table(
        {"mystic remora": 0.95, "compost": 0.3, "archivist of oghma": 0.25, "esper sentinel": 0.9}
        | {"sylvan library": 0.6},
        both=True,
    )
    targets = targets_for(
        board_card("Mystic Remora"), info("Mystic Remora", (Role.DRAW,)), DECK, CutRules((), shares)
    )
    assert names(targets) == ["Archivist of Oghma", "Compost", "Sylvan Library"]


REMORA = info("Mystic Remora", (Role.DRAW,))


def test_a_same_role_card_more_played_than_the_idea_is_not_offered_and_a_less_played_one_is() -> None:
    shares = table(
        {
            "mystic remora": 0.4,
            "esper sentinel": 0.9,
            "compost": 0.5,
            "archivist of oghma": 0.2,
            "sylvan library": 0.1,
        }
    )
    targets = targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), shares))
    assert names(targets) == ["Sylvan Library", "Archivist of Oghma"]


def test_a_deck_card_no_list_plays_counts_as_zero_and_is_always_cuttable() -> None:
    shares = table({"mystic remora": 0.05, "esper sentinel": 0.9, "compost": 0.9, "archivist of oghma": 0.9})
    targets = targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), shares))
    assert names(targets) == ["Sylvan Library"]
    assert "in 0% of meta lists" in targets[0].reason


def test_a_card_as_played_as_the_idea_is_not_offered() -> None:
    shares = table({"mystic remora": 0.3, "compost": 0.3, "archivist of oghma": 0.1, "esper sentinel": 0.5})
    targets = targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), shares))
    assert names(targets) == ["Sylvan Library", "Archivist of Oghma"]
    flat = table({"mystic remora": 0.3, "compost": 0.3, "archivist of oghma": 0.3, "esper sentinel": 0.3})
    assert names(targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), flat))) == [
        "Sylvan Library"
    ]


def test_an_idea_with_only_stronger_cards_to_cut_gets_no_target() -> None:
    shares = table(
        {
            "mystic remora": 0.1,
            "esper sentinel": 0.8,
            "compost": 0.7,
            "archivist of oghma": 0.6,
            "sylvan library": 0.5,
        }
    )
    assert targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), shares)) == ()


def test_a_same_theme_card_more_played_than_the_idea_is_not_offered() -> None:
    themes = {key: frozenset({"Stax"}) for key in ("kataki", "thalia", "hushbringer")}
    shares = table({"thalia": 0.3, "kataki": 0.5, "hushbringer": 0.1})
    targets = targets_for(board_card("Thalia"), info("Thalia"), DECK, CutRules((), shares, themes=themes))
    assert names(targets) == ["Hushbringer"]


@pytest.mark.parametrize("shares", [None, NO_SHARES])
def test_without_shares_every_card_in_the_role_stays_a_target(shares: ShareTable | None) -> None:
    targets = targets_for(board_card("Mystic Remora"), REMORA, DECK, CutRules((), shares))
    assert names(targets) == ["Compost", "Archivist of Oghma", "Esper Sentinel"]


def test_claudes_picked_cut_more_played_than_the_idea_gives_way_to_the_roles_weaker_cards() -> None:
    shares = table({"mystic remora": 0.4, "compost": 0.9, "archivist of oghma": 0.1, "esper sentinel": 0.9})
    picked = board_card("Mystic Remora", (ReplaceOption("Compost", "not stax"),))
    targets = targets_for(picked, REMORA, DECK, CutRules((), shares))
    assert names(targets) == ["Sylvan Library", "Archivist of Oghma"]
    assert {t.source for t in targets} == {TargetSource.ROLE}


def test_a_cut_the_user_picked_stays_however_played_it_is() -> None:
    shares = table({"mystic remora": 0.1, "compost": 0.9})
    mine = board_card("Mystic Remora", (ReplaceOption("Compost", "my pick"),), Actor.YOU)
    assert targets_for(mine, REMORA, DECK, CutRules((), shares)) == (
        Target("Compost", "my pick", TargetSource.PICKED),
    )


def test_an_idea_the_user_proposed_gets_only_computed_cuts_less_played_than_it() -> None:
    shares = table({"mystic remora": 0.4, "compost": 0.9, "archivist of oghma": 0.1, "esper sentinel": 0.9})
    mine = board_card("Mystic Remora", source=Actor.YOU)
    assert names(targets_for(mine, REMORA, DECK, CutRules((), shares))) == [
        "Sylvan Library",
        "Archivist of Oghma",
    ]
