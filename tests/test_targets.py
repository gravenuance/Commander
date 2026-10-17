"""Which deck cards a board card could replace: hand-picked ones, else the weakest in its role."""

from commander.board import Actor, BoardCard, ReplaceOption, Status
from commander.carddb import CardInfo, ResolvedCard, Role
from commander.models import DeckCard
from commander.targets import CutRules, Target, TargetSource, targets_for


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
    shares = {"compost": 0.6, "archivist of oghma": 0.0, "esper sentinel": 0.1, "sylvan library": 0.05}
    targets = targets_for(
        board_card("Mystic Remora"), info("Mystic Remora", (Role.DRAW,)), DECK, CutRules((), shares)
    )
    assert names(targets) == ["Archivist of Oghma", "Sylvan Library", "Esper Sentinel"]
    assert targets[1].reason == "same role (draw); in 5% of meta lists; unranked on EDHREC"


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


def test_shares_name_what_they_are_of() -> None:
    rules = CutRules((), {"kataki": 0.3}, "recent decks")
    targets = targets_for(board_card("Thalia"), info("Thalia"), DECK, rules)
    assert targets[0].reason == "fills no role either; in 0% of recent decks; EDHREC rank 800"
