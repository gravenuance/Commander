"""What a deck plays far more of than recent decks with its commander, and how that steers cuts."""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from commander.archidekt import PeerDeck
from commander.carddb import CardDb, ResolvedCard, Role
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile, ThemeSpec
from commander.leanings import Leaning, find_leanings
from commander.metadata import MetaData, PeerList, PeerLists
from commander.models import Deck, DeckCard
from commander.peercounts import RoleBand
from commander.spellbook import ComboDetail, ComboPiece
from commander.targets import CutRules, deck_cut_rules, role_targets, with_ideas
from conftest import ANCIENT_TOMB, build_card_db


def filler(index: int) -> str:
    return f"eeeeeeee-0000-4000-8000-{index:012d}"


def tag(index: int, slug: str, members: list[str], children: tuple[int, ...] = ()) -> str:
    return json.dumps(
        {
            "object": "tag",
            "id": f"cccccccc-0000-4000-8000-{index:012d}",
            "slug": slug,
            "type": "oracle",
            "child_ids": [f"cccccccc-0000-4000-8000-{child:012d}" for child in children],
            "taggings": [{"oracle_id": member, "weight": "median"} for member in members],
        }
    )


COMMANDER = 100
TEN = [filler(i) for i in range(1, 11)]
TAGS = [
    # The commander and a land carry it too, and neither counts on either side.
    tag(1, "extra-combat", [*TEN, filler(COMMANDER), ANCIENT_TOMB]),
    tag(2, "too-narrow", [filler(i) for i in range(1, 6)]),
    tag(3, "card-names", [], children=(4,)),
    tag(4, "alliteration", TEN),
    tag(5, "cycle-test", TEN),
    # reanimate holds Filler Card 20 itself and 21-29 through its child.
    tag(6, "reanimate", [filler(20)], children=(7,)),
    tag(7, "reanimate-creature", [filler(i) for i in range(21, 30)]),
]


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("leaningdb"), TAGS)) as opened:
        yield opened


def deck_card(name: str, *categories: str) -> DeckCard:
    return DeckCard(name, 1, categories or ("Uncategorized",), True, "sid", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model


def deck_cards(*fillers: int, land: bool = True) -> list[DeckCard]:
    cards = [deck_card(f"Filler Card {COMMANDER}", "Commander")]
    cards += [deck_card(f"Filler Card {i}") for i in fillers]
    return [*cards, deck_card("Ancient Tomb", "Land")] if land else cards


def resolved(db: CardDb, cards: list[DeckCard]) -> list[ResolvedCard]:
    infos = db.cards_named(card.name for card in cards)
    return [ResolvedCard(card, infos.get(card.name.casefold()), None) for card in cards]


NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def peers(count: int, *fillers: int) -> list[PeerList]:
    names = (f"Filler Card {COMMANDER}", "Ancient Tomb", *(f"Filler Card {i}" for i in fillers))
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return [PeerList(PeerDeck(i, f"peer {i}", "someone", 10, when, 3), names) for i in range(count)]


def test_a_tag_the_deck_holds_far_more_of_than_its_peers_is_a_leaning(db: CardDb) -> None:
    main = resolved(db, deck_cards(1, 2, 3, 4))
    found = find_leanings(main, peers(5, 1), db)
    assert found == (
        Leaning("extra-combat", ("Filler Card 1", "Filler Card 2", "Filler Card 3", "Filler Card 4"), 1.0, 5),
    )


def test_too_few_peer_decks_or_cards_or_peers_playing_as_many_make_no_leaning(db: CardDb) -> None:
    main = resolved(db, deck_cards(1, 2, 3, 4))
    assert find_leanings(main, peers(2, 1), db) == ()
    assert find_leanings(resolved(db, deck_cards(1, 2)), peers(5), db) == ()
    # One of the five peer decks plays as many: a fifth, which is no longer fewer than MAX_PEERS_AS_MANY.
    playing = [*peers(4), *peers(1, 1, 2, 3, 4)]
    assert find_leanings(main, playing, db) == ()


def test_name_trivia_cycles_narrow_tags_and_ignored_ones_are_never_leanings(db: CardDb) -> None:
    main = resolved(db, deck_cards(1, 2, 3, 4, 5))
    tags = [leaning.tag for leaning in find_leanings(main, peers(5), db)]
    # alliteration (under card-names), cycle-test and too-narrow hold the same cards.
    assert tags == ["extra-combat"]
    assert find_leanings(main, peers(5), db, ignore={"extra-combat"}) == ()


def test_a_wider_leaning_folds_into_a_narrower_one_holding_most_of_its_cards(db: CardDb) -> None:
    main = resolved(db, deck_cards(20, 21, 22, 23, 24))
    assert [leaning.tag for leaning in find_leanings(main, peers(5), db)] == ["reanimate-creature"]


def test_ignoring_a_narrower_leaning_keeps_the_wider_one_folded_into_it_hidden(db: CardDb) -> None:
    main = resolved(db, deck_cards(20, 21, 22, 23, 24))
    # reanimate's cards are 20-24 and reanimate-creature's 21-24: ignoring the narrower one hides both.
    assert find_leanings(main, peers(5), db, ignore={"reanimate-creature"}) == ()


def peers_each(*plays: tuple[int, ...]) -> list[PeerList]:
    """One recent peer deck per tuple, playing those fillers."""
    when = datetime(2026, 10, 1, tzinfo=UTC)
    return [
        PeerList(
            PeerDeck(i, f"peer {i}", "someone", 10, when, 3),
            (f"Filler Card {COMMANDER}", "Ancient Tomb", *(f"Filler Card {n}" for n in played)),
        )
        for i, played in enumerate(plays)
    ]


def pool(*fillers: int) -> MetaData:
    return pool_of(peers(5, *fillers))


def pool_of(decks: list[PeerList]) -> MetaData:
    lists = Answer(PeerLists(tuple(decks), 0), AnswerSource.FETCHED, None, None)
    gone: Answer[Any] = Answer(None, AnswerSource.UNAVAILABLE, None, "test")
    return MetaData(
        "Filler Card 100", datetime(2026, 8, 16, tzinfo=UTC).date(), gone, gone, lists, gone, gone
    )


def test_cards_in_a_leaning_are_cut_only_for_ideas_in_it_and_lands_never_join_it(db: CardDb) -> None:
    cards = deck_cards(1, 2, 3, 4, 50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    # One peer deck in ten plays the two ideas: 10 points more than the deck's cards, short of the 20 that
    # cuts a card of another theme.
    peer_decks = peers_each((60, 5), *[()] * 9)
    rules = deck_cut_rules(deck, main, Profile(), db, pool_of(peer_decks), now=lambda: NOW)
    assert [leaning.tag for leaning in rules.leanings] == ["extra-combat"]
    themed, plain = db.cards_named(["Filler Card 5", "Filler Card 60"]).values()
    rules = with_ideas(rules, [themed, plain], db)
    # The leaning's cards are in no peer deck, and an idea of no theme does not take them.
    assert [t.name for t in role_targets(plain, main, rules)] == ["Filler Card 50", "Filler Card 51"]
    swap = role_targets(themed, main, rules)
    assert [t.name for t in swap][:3] == ["Filler Card 1", "Filler Card 2", "Filler Card 3"]
    assert swap[0].reason.startswith(
        "same theme (extra-combat); fills no role either; in 0% of recent decks (0/10);"
    )
    tomb = db.cards_named(["Ancient Tomb"])["ancient tomb"]
    assert "extra-combat" not in with_ideas(rules, [tomb], db).themes.get(tomb.oracle_id, frozenset())


def test_the_decks_grace_cards_weight_the_shares_that_order_its_cut_targets(db: CardDb) -> None:
    cards = deck_cards(50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    # Four equally fresh peer decks: the idea is in all, Filler Card 50 in one and 51 in two.
    pool = pool_of(peers_each((50, 60), (51, 60), (51, 60), (60,)))
    [idea] = db.cards_named(["Filler Card 60"]).values()

    def rules_for(profile: Profile) -> CutRules:
        return with_ideas(deck_cut_rules(deck, main, profile, db, pool, now=lambda: NOW), [idea], db)

    def cuts(profile: Profile) -> list[str]:
        return [t.name for t in role_targets(idea, main, rules_for(profile))]

    # Least played first: 50 at 1/4 goes before 51 at 1/2.
    assert cuts(Profile()) == ["Filler Card 50", "Filler Card 51"]
    # Grace x6 on Filler Card 50 weighs the deck playing it 6, 1, 1, 1 (scaled 8/3, 4/9, 4/9, 4/9 of
    # four): 50 is 2/3, 51 is 2/9 and the idea all of them, so now 51 is the less played.
    graced = Profile(grace={"Filler Card 50": 6.0})
    assert cuts(graced) == ["Filler Card 51", "Filler Card 50"]
    shares = rules_for(graced).shares
    assert shares is not None
    fractions = shares.fractions()
    assert [fractions[f"filler card {n}"] for n in (50, 51, 60)] == pytest.approx([2 / 3, 2 / 9, 1.0])


def test_the_rules_hold_the_peer_decks_role_and_theme_counts_only_with_enough_decks(db: CardDb) -> None:
    cards = deck_cards(1, 2, 3, 4, 50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    counts = deck_cut_rules(deck, main, Profile(), db, pool(50, 51), now=lambda: NOW).peers
    assert counts is not None
    # Every peer deck plays Ancient Tomb, the only ramp card, and none of the leaning's nonland cards.
    assert counts.roles[Role.RAMP] == RoleBand(1.0, 1.0)
    assert counts.themes == {"extra-combat": 0.0}
    few = deck_cut_rules(deck, main, Profile(), db, pool_of(peers(2, 50)), now=lambda: NOW)
    assert few.peers is None
    assert deck_cut_rules(deck, main, Profile(), db, None, now=lambda: NOW).peers is None


def test_the_profiles_themes_are_named_by_their_words(db: CardDb) -> None:
    cards = deck_cards(1, 50)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    profile = Profile(themes=ThemeSpec(tags=("reanimate",)))
    main = resolved(db, cards)
    rules = deck_cut_rules(deck, main, profile, db, None, now=lambda: NOW)
    idea = db.cards_named(["Filler Card 21"])["filler card 21"]
    rules = with_ideas(rules, [idea], db)
    assert rules.themes[idea.oracle_id] == frozenset({"reanimate"})
    assert rules.shares is None


def test_a_profile_word_a_deck_tag_names_too_is_one_theme_under_the_deck_tag(db: CardDb) -> None:
    cards = deck_cards(1, 50)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards), tags=("Reanimate",))
    # extra-combat is the profile's own, so its words still become themes, but reanimate is the deck tag's.
    profile = Profile(themes=ThemeSpec(tags=("reanimate", "extra-combat")))
    rules = deck_cut_rules(deck, resolved(db, cards), profile, db, None, now=lambda: NOW)
    idea = db.cards_named(["Filler Card 21"])["filler card 21"]
    assert with_ideas(rules, [idea], db).themes[idea.oracle_id] == frozenset({"Reanimate"})


def test_the_pieces_of_a_combo_inside_the_deck_are_never_cut(db: CardDb) -> None:
    cards = deck_cards(50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    # Spellbook names its pieces in its own case; the combo's other piece is not in the deck.
    pieces = (ComboPiece("filler card 50", "Creature", ("B",)), ComboPiece("Elsewhere", "Artifact", ("B",)))
    combo = ComboDetail("1-2", pieces, (), ("Infinite mana",), "")
    data = replace(pool(), own_combos=Answer((combo,), AnswerSource.FETCHED, None, None))
    protected = deck_cut_rules(deck, main, Profile(), db, data, now=lambda: NOW).protected
    by_name = {r.card.name: r.info.oracle_id for r in main if r.info}
    assert set(protected) == {by_name["Filler Card 50"]}
    assert not deck_cut_rules(deck, main, Profile(), db, pool(), now=lambda: NOW).protected


def test_a_deck_without_spellbooks_combo_answer_logs_that_its_combo_pieces_are_not_protected(
    db: CardDb, caplog: pytest.LogCaptureFixture
) -> None:
    cards = deck_cards(50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    with caplog.at_level(logging.INFO, logger="commander.targets"):
        deck_cut_rules(deck, main, Profile(), db, pool(), now=lambda: NOW)
    [record] = [r for r in caplog.records if "combo" in r.getMessage()]
    assert record.levelno == logging.INFO
    assert "test" in record.getMessage()
    caplog.clear()
    combo = ComboDetail("1-2", (ComboPiece("Elsewhere", "Artifact", ("B",)),), (), ("Infinite mana",), "")
    data = replace(pool(), own_combos=Answer((combo,), AnswerSource.FETCHED, None, None))
    with caplog.at_level(logging.INFO, logger="commander.targets"):
        deck_cut_rules(deck, main, Profile(), db, data, now=lambda: NOW)
    assert not [r for r in caplog.records if "combo" in r.getMessage()]


def test_below_bracket_5_the_cuts_rank_by_the_share_of_recent_decks_weighted_by_age(db: CardDb) -> None:
    cards = deck_cards(50, 51)
    deck = Deck(1, "test", "me", 3, "2026-10-16", tuple(cards))
    main = resolved(db, cards)
    held = (f"Filler Card {COMMANDER}", "Ancient Tomb")
    fresh, stale = NOW, datetime(2026, 3, 30, 12, 0, tzinfo=UTC)
    decks = [
        PeerList(PeerDeck(i, f"peer {i}", "someone", 10, when, 3), (*held, *played, "Filler Card 60"))
        for i, (when, played) in enumerate(
            [
                (fresh, ("Filler Card 50",)),
                (fresh, ("Filler Card 50",)),
                (fresh, ()),
                (fresh, ()),
                (stale, ("Filler Card 51",)),
                (stale, ("Filler Card 51",)),
            ]
        )
    ]
    rules = deck_cut_rules(deck, main, Profile(), db, pool_of(decks), now=lambda: NOW)
    idea = db.cards_named(["Filler Card 60"])["filler card 60"]
    cuts = role_targets(idea, main, with_ideas(rules, [idea], db))
    # All six decks play the idea. Two of six play each card, but the 200-day-old ones count for almost
    # nothing (2 / (4 + 2 * 0.0098) is 50%, 2 * 0.0098 / 4.02 is 0%); unweighted it is a tie.
    assert [t.name for t in cuts] == ["Filler Card 51", "Filler Card 50"]
    assert cuts[0].reason.startswith("fills no role either; in 0% of recent decks (2/6);")
    assert cuts[1].reason.startswith("fills no role either; in 50% of recent decks (2/6);")
