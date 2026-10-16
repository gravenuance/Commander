import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from commander.archidekt import PeerDeck, parse_deck
from commander.carddb import CardDb
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile
from commander.edhtop16 import CommanderStats, DecklistCard, Entry, Tournament
from commander.meta import (
    AnswerKind,
    Basis,
    analyse,
    card_key,
    check_card,
    key_pieces,
    own_combos,
    performing_lists,
)
from commander.metacombos import AnswerRole
from commander.metadata import CommanderEntries, MetaCommander, MetaData, PeerList, PeerLists
from commander.models import Deck
from commander.spellbook import ComboDetail, ComboPiece
from conftest import CHAOS_WARP, LIGHTNING_BOLT, Json, build_card_db

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SINCE = date(2026, 8, 16)
MAGDA = "Magda, Brazen Outlaw"
# Made up for these tests: the fixture tags hold no creature or permanent removal.
REMOVAL_TAGS = [
    json.dumps(
        {
            "object": "tag",
            "id": "aaaaaaaa-0000-4000-8000-000000000001",
            "slug": "removal-creature",
            "type": "oracle",
            "child_ids": [],
            "taggings": [{"oracle_id": LIGHTNING_BOLT, "weight": "median"}],
        }
    ),
    json.dumps(
        {
            "object": "tag",
            "id": "aaaaaaaa-0000-4000-8000-000000000002",
            "slug": "removal-permanent",
            "type": "oracle",
            "child_ids": [],
            "taggings": [{"oracle_id": CHAOS_WARP, "weight": "median"}],
        }
    ),
]


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("metadb"), REMOVAL_TAGS)) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    # Magda, Battered Golem, 6 Mountain and Disruptor Flute (filed under Stax); two cards on the Maybeboard.
    return parse_deck(raw_deck)


def entry(
    standing: int, size: int, top_cut: int, cards: list[tuple[str, int]], held_on: date = date(2026, 9, 1)
) -> Entry:
    return Entry(
        standing=standing,
        wins=0,
        losses=0,
        draws=0,
        decklist_url=None,
        tournament=Tournament(f"t{standing}-{size}", "Event", held_on, size, top_cut),
        cards=tuple(DecklistCard(name, 1, spice) for name, spice in cards),
    )


def fetched[T](data: T) -> Answer[T]:
    return Answer(data, AnswerSource.FETCHED, NOW, None)


def peer(deck_id: int, cards: list[str], views: int = 10) -> PeerList:
    return PeerList(PeerDeck(deck_id, f"deck {deck_id}", "someone", views, NOW, 5), tuple(cards))


def meta_data(
    entries: list[Entry],
    peers: list[PeerList] | None = None,
    field: list[MetaCommander] | None = None,
) -> MetaData:
    return MetaData(
        MAGDA,
        SINCE,
        fetched(CommanderEntries(MAGDA, known=True, entries=tuple(entries))),
        fetched(tuple(field or [])),
        fetched(PeerLists(tuple(peers or []), 0)),
        fetched(()),
        fetched(()),
    )


# Five top-cut lists (placings 1st..5th of 40, top 8), best first.
TOP_CUT_LISTS = [
    entry(
        1, 40, 8, [("Mind Stone", 0), ("Battered Golem", 1), ("High-Speed Hoverbike", 3), ("Jeska's Will", 4)]
    ),
    entry(2, 40, 8, [("Mind Stone", 0), ("Battered Golem", 1), ("Mountain", 0), ("Jeska's Will", 3)]),
    entry(3, 40, 8, [("Mind Stone", 1), ("Battered Golem", 1), ("High-Speed Hoverbike", 2)]),
    entry(4, 40, 8, [("Arcane Signet", 0), ("Rhystic Study", 2)]),
    entry(5, 40, 8, [("Arcane Signet", 0), ("Magda, Brazen Outlaw", 0), ("Disruptor Flute", 4)]),
]


def test_top_cut_lists_perform_when_there_are_enough() -> None:
    off_cut = entry(30, 40, 8, [("Mind Stone", 0)])
    no_list = entry(1, 40, 8, [])
    lists, basis = performing_lists([off_cut, no_list, *reversed(TOP_CUT_LISTS)])
    assert basis is Basis.TOP_CUT
    assert [e.standing for e in lists] == [1, 2, 3, 4, 5]


def test_too_few_top_cuts_fall_back_to_every_list_best_first() -> None:
    off_cut = entry(30, 40, 8, [("Mind Stone", 0)])
    lists, basis = performing_lists([off_cut, *TOP_CUT_LISTS[:4]])
    assert basis is Basis.ALL
    assert [e.standing for e in lists] == [1, 2, 3, 4, 30]


def test_sample_counts_only_the_window_and_flags_thin_data(deck: Deck, db: CardDb) -> None:
    old = entry(1, 40, 8, [("Mind Stone", 0)], held_on=date(2026, 8, 15))
    report = analyse(deck, meta_data([old, *TOP_CUT_LISTS]), Profile(), db)
    sample = report.sample
    assert (sample.entries, sample.with_lists, sample.top_cuts, sample.performing) == (5, 5, 5, 5)
    assert sample.basis is Basis.TOP_CUT
    # Five lists are under the ten it takes to judge a card.
    assert sample.thin


def test_missing_staples_come_from_performing_and_archidekt_lists(deck: Deck, db: CardDb) -> None:
    peers = [peer(1, ["Chaos Warp", "Mind Stone"]), peer(2, ["Chaos Warp"]), peer(3, ["Lightning Bolt"])]
    report = analyse(deck, meta_data(TOP_CUT_LISTS, peers), Profile(), db)
    assert [(c.name, c.lists, c.peer_lists, c.spice, c.flagged) for c in report.missing] == [
        ("Mind Stone", 3, 1, 1, False),
        # Two of five is the 40% line; the deck has the Hoverbike on its Maybeboard.
        ("Arcane Signet", 2, 0, 0, False),
        ("High-Speed Hoverbike", 2, 0, 3, True),
        ("Jeska's Will", 2, 0, 4, False),
        ("Chaos Warp", 0, 2, None, False),
    ]
    assert report.missing[0].share == pytest.approx(0.6)
    assert report.missing[4].peer_share == pytest.approx(2 / 3)


def test_spicy_picks_skip_deck_cards_and_mild_ones(deck: Deck, db: CardDb) -> None:
    report = analyse(deck, meta_data(TOP_CUT_LISTS), Profile(), db)
    assert [(p.name, p.spice, p.lists, p.best.standing) for p in report.spicy] == [
        ("Jeska's Will", 4, 2, 1),
        ("High-Speed Hoverbike", 3, 1, 1),
    ]


def test_outliers_are_deck_cards_few_performing_lists_play(deck: Deck, db: CardDb) -> None:
    lists = [*TOP_CUT_LISTS[:4], entry(5, 40, 8, [("Arcane Signet", 0)])]
    report = analyse(deck, meta_data(lists), Profile(keep_categories=("Stax",)), db)
    # Battered Golem is in three of five; Magda is the commander and Mountain a basic.
    assert [(c.name, c.lists, c.flagged) for c in report.outliers] == [("Disruptor Flute", 0, True)]


def test_key_pieces_are_the_commander_and_what_the_profile_keeps(deck: Deck) -> None:
    assert key_pieces(deck, Profile()) == (frozenset({MAGDA}), False)
    assert key_pieces(deck, Profile(keep_categories=("stax",))) == (
        frozenset({MAGDA, "Disruptor Flute"}),
        True,
    )
    assert key_pieces(deck, Profile(keep_cards=("battered golem",))) == (
        frozenset({MAGDA, "Battered Golem"}),
        True,
    )


def field() -> list[MetaCommander]:
    big = MetaCommander(
        CommanderStats("Big Commander", 400, 100, 0.25, 0.04),
        (
            entry(1, 40, 8, [("Lightning Bolt", 0), ("Chaos Warp", 0), ("Mind Stone", 0)]),
            entry(2, 40, 8, [("Lightning Bolt", 0), ("Mind Stone", 0)]),
        ),
    )
    small = MetaCommander(
        CommanderStats("Small Commander", 200, 40, 0.2, 0.02),
        tuple(entry(i, 40, 8, [("Chaos Warp", 0)] if i == 1 else [("Mind Stone", 0)]) for i in range(1, 5)),
    )
    return [big, small]


def test_threats_are_common_meta_answers_to_key_pieces(deck: Deck, db: CardDb) -> None:
    report = analyse(deck, meta_data(TOP_CUT_LISTS, field=field()), Profile(keep_categories=("Stax",)), db)
    assert [(t.name, t.kinds, t.hits, t.played_by) for t in report.threats] == [
        ("Lightning Bolt", (AnswerKind.CREATURE_REMOVAL,), (MAGDA,), (("Big Commander", 1.0),)),
        # A quarter of the small commander's lists is under the 30% line, so only the big one names it.
        (
            "Chaos Warp",
            (AnswerKind.PERMANENT_REMOVAL,),
            ("Disruptor Flute", MAGDA),
            (("Big Commander", 0.5),),
        ),
    ]
    assert report.threats[0].exposure == pytest.approx(0.04)
    assert report.threats[1].exposure == pytest.approx(0.5 * 0.04 + 0.25 * 0.02)
    assert [s.name for s in report.field] == ["Big Commander", "Small Commander"]


def test_without_kept_pieces_only_the_commander_is_threatened(deck: Deck, db: CardDb) -> None:
    report = analyse(deck, meta_data(TOP_CUT_LISTS, field=field()), Profile(), db)
    assert not report.keys_from_profile
    assert [t.hits for t in report.threats] == [(MAGDA,), (MAGDA,)]


def test_answer_kinds_reach_the_piece_types_they_name(db: CardDb) -> None:
    flute = db.card_named("Disruptor Flute")
    magda = db.card_named(MAGDA)
    bolt = db.card_named("Lightning Bolt")
    assert flute is not None
    assert magda is not None
    assert bolt is not None
    assert AnswerKind.ARTIFACT_REMOVAL.hits(flute.type_line)
    assert not AnswerKind.CREATURE_REMOVAL.hits(flute.type_line)
    assert AnswerKind.SWEEPER.hits(magda.type_line)
    assert not AnswerKind.PERMANENT_REMOVAL.hits(bolt.type_line)
    assert AnswerKind.COUNTERSPELL.hits(bolt.type_line)


def test_a_card_check_compares_lists_with_and_without_the_card(deck: Deck) -> None:
    off_cut = entry(20, 40, 8, [("Arcane Signet", 0)])
    peers = [peer(1, ["Mind Stone"]), peer(2, ["Chaos Warp"])]
    data = meta_data([*TOP_CUT_LISTS, off_cut], peers, field())
    check = check_card("mind stone", deck, data)
    assert not check.in_deck
    assert (check.performing, check.performing_with, check.lists, check.lists_with) == (5, 3, 6, 3)
    assert (check.top_cuts_with, check.top_cuts_without) == (3, 2)
    # Placings 0, 1/40, 2/40 with it; 3/40, 4/40 and 19/40 without.
    assert check.mean_placing_with == pytest.approx(1 / 40)
    assert check.mean_placing_without == pytest.approx((3 + 4 + 19) / 3 / 40)
    assert (check.peers, check.peers_with, check.spice) == (2, 1, 1)
    assert (check.meta_lists, check.meta_lists_with) == (6, 5)


def test_an_unknown_commander_reports_as_unknown(deck: Deck, db: CardDb) -> None:
    data = MetaData(
        MAGDA,
        SINCE,
        fetched(CommanderEntries(MAGDA, known=False, entries=())),
        fetched(()),
        Answer(None, AnswerSource.UNAVAILABLE, None, "offline"),
        Answer(None, AnswerSource.UNAVAILABLE, None, "offline"),
        Answer(None, AnswerSource.UNAVAILABLE, None, "offline"),
    )
    report = analyse(deck, data, Profile(), db)
    assert not report.sample.known
    assert report.sample.basis is Basis.ALL
    assert report.missing == ()
    assert report.outliers == ()
    assert report.peers == ()
    assert [(s.name, s.source, s.error) for s in report.sources][2] == (
        "Archidekt decks",
        AnswerSource.UNAVAILABLE,
        "offline",
    )


def test_two_faced_cards_compare_by_their_front_face() -> None:
    assert card_key("Sundering Eruption // Volcanic Fissure") == card_key("sundering eruption")


def test_the_report_names_the_deck_by_its_commander(deck: Deck, db: CardDb, tmp_path: Path) -> None:
    assert analyse(deck, meta_data([]), Profile(), db).deck == "Magda"
    assert tmp_path.exists()


def test_a_spicy_card_in_one_list_off_the_cut_is_left_out(deck: Deck, db: CardDb) -> None:
    lists = [
        entry(20, 40, 8, [("Mana Crypt", 4)]),
        entry(30, 40, 8, [("Jeska's Will", 4)]),
        entry(31, 40, 8, [("Jeska's Will", 3)]),
    ]
    report = analyse(deck, meta_data(lists), Profile(), db)
    assert [(p.name, p.lists) for p in report.spicy] == [("Jeska's Will", 2)]


def test_the_decks_own_combos_list_what_the_meta_commonly_plays_against_them(db: CardDb) -> None:
    combo = ComboDetail(
        "1-2",
        (
            ComboPiece(MAGDA, "Legendary Creature — Dwarf Berserker", ("C",)),
            ComboPiece("Battered Golem", "Artifact Creature — Golem", ("B",)),
        ),
        (),
        ("Infinite artifact ETB",),
        "Activate Battered Golem.\nRepeat.",
    )
    [found] = own_combos([combo], field(), db)
    assert found.pieces == (MAGDA, "Battered Golem")
    # Most played first; Mind Stone stops nothing; Chaos Warp's small-commander quarter is under the line.
    assert [(a.card, a.role, a.played_by) for a in found.answers] == [
        ("Lightning Bolt", AnswerRole.REMOVAL, (("Big Commander", 1.0),)),
        ("Chaos Warp", AnswerRole.REMOVAL, (("Big Commander", 0.5),)),
    ]
    assert found.answers[1].exposure == pytest.approx(0.5 * 0.04 + 0.25 * 0.02)
    assert own_combos([], field(), db) == ()
