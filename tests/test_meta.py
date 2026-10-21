import json
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from commander.answers import Piece, card_answers
from commander.archidekt import PeerDeck, parse_deck
from commander.carddb import CardDb, Theme
from commander.deckcache import Answer, AnswerSource
from commander.deckprofile import Profile
from commander.meta import (
    AnswerKind,
    Basis,
    analyse,
    card_key,
    check_card,
    meta_pool,
    own_combos,
    performing_lists,
)
from commander.metacombos import AnswerRole, Stop
from commander.metadata import CommanderEntries, MetaCommander, MetaData, PeerList, PeerLists
from commander.models import Deck
from commander.spellbook import ComboDetail, ComboPiece
from commander.tournaments import CommanderStats, DecklistCard, Entry, Tournament
from conftest import CHAOS_WARP, FILLER_CARDS, LIGHTNING_BOLT, Json, build_card_db, filler_card
from test_scryfall import REMOVAL_ARTIFACT, removal_tag

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SINCE = date(2026, 8, 16)
MAGDA = "Magda, Brazen Outlaw"
SWEEPER_TAG = "3fb7e4fd-5304-4120-b7c4-8a89f70ad3f0"
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


BANNED_CARD = "Banned Test Card"
BLUE_CARD = "Blue Test Card"


def extra_card(index: int, name: str, *, identity: list[str], legal: str = "legal") -> str:
    raw = json.loads(filler_card(FILLER_CARDS + index, name))
    raw["color_identity"] = identity
    raw["legalities"] = {"commander": legal}
    return json.dumps(raw)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    # A card banned since the lists were played, and one outside Magda's red identity.
    extra = [
        extra_card(1, BANNED_CARD, identity=[], legal="banned"),
        extra_card(2, BLUE_CARD, identity=["U"]),
    ]
    with CardDb(build_card_db(tmp_path_factory.mktemp("metadb"), REMOVAL_TAGS, extra)) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    # Magda, Battered Golem, 6 Mountain and Disruptor Flute (filed under Stax); two cards on the Maybeboard.
    return parse_deck(raw_deck)


def entry(
    standing: int, size: int, top_cut: int, cards: list[str], held_on: date = date(2026, 9, 1)
) -> Entry:
    return Entry(
        standing=standing,
        tournament=Tournament(f"t{standing}-{size}", held_on, size, top_cut),
        cards=tuple(DecklistCard(name, 1) for name in cards),
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
    entry(1, 40, 8, ["Mind Stone", "Battered Golem", "High-Speed Hoverbike", "Jeska's Will"]),
    entry(2, 40, 8, ["Mind Stone", "Battered Golem", "Mountain", "Jeska's Will"]),
    entry(3, 40, 8, ["Mind Stone", "Battered Golem", "High-Speed Hoverbike"]),
    entry(4, 40, 8, ["Arcane Signet", "Rhystic Study"]),
    entry(5, 40, 8, ["Arcane Signet", "Magda, Brazen Outlaw", "Disruptor Flute"]),
]


def test_top_cut_lists_perform_when_there_are_enough() -> None:
    off_cut = entry(30, 40, 8, ["Mind Stone"])
    no_list = entry(1, 40, 8, [])
    lists, basis = performing_lists([off_cut, no_list, *reversed(TOP_CUT_LISTS)])
    assert basis is Basis.TOP_CUT
    assert [e.standing for e in lists] == [1, 2, 3, 4, 5]


def test_a_single_top_cut_list_is_enough_to_perform() -> None:
    off_cut = entry(30, 40, 8, ["Mind Stone"])
    lists, basis = performing_lists([off_cut, TOP_CUT_LISTS[0]])
    assert basis is Basis.TOP_CUT
    assert [e.standing for e in lists] == [1]


def test_without_any_top_cut_every_list_performs_best_first() -> None:
    first = entry(30, 40, 8, ["Mind Stone"])
    second = entry(12, 40, 8, ["Mind Stone"])
    no_list = entry(1, 40, 8, [])
    lists, basis = performing_lists([first, no_list, second])
    assert basis is Basis.ALL
    assert [e.standing for e in lists] == [12, 30]


def test_sample_counts_only_the_window_and_flags_thin_data(deck: Deck, db: CardDb) -> None:
    old = entry(1, 40, 8, ["Mind Stone"], held_on=date(2026, 8, 15))
    report = analyse(deck, meta_data([old, *TOP_CUT_LISTS]), Profile(), db, now=lambda: NOW)
    sample = report.sample
    assert (sample.entries, sample.with_lists, sample.top_cuts, sample.performing) == (5, 5, 5, 5)
    assert sample.basis is Basis.TOP_CUT
    # Five lists are under the ten it takes to judge a card.
    assert sample.thin


def test_missing_staples_come_from_performing_and_archidekt_lists(deck: Deck, db: CardDb) -> None:
    peers = [peer(1, ["Chaos Warp", "Mind Stone"]), peer(2, ["Chaos Warp"]), peer(3, ["Lightning Bolt"])]
    report = analyse(deck, meta_data(TOP_CUT_LISTS, peers), Profile(), db, now=lambda: NOW)
    assert [(c.name, c.lists, c.peer_lists, c.flagged) for c in report.missing] == [
        ("Mind Stone", 3, 1, False),
        # Two of five is the 40% line; the Hoverbike on the deck's Maybeboard counts as missing.
        ("Arcane Signet", 2, 0, False),
        ("High-Speed Hoverbike", 2, 0, False),
        ("Jeska's Will", 2, 0, False),
        ("Chaos Warp", 0, 2, False),
    ]
    assert report.missing[0].share == pytest.approx(0.6)
    assert report.missing[4].peer_share == pytest.approx(2 / 3)


def test_missing_staples_leave_out_banned_and_off_colour_cards(deck: Deck, db: CardDb) -> None:
    played = ["Mind Stone", BANNED_CARD, BLUE_CARD, "Too New To Know"]
    lists = [entry(i, 40, 8, played) for i in range(1, 6)]
    peers = [peer(i, [BANNED_CARD, BLUE_CARD]) for i in range(1, 4)]
    report = analyse(deck, meta_data(lists, peers), Profile(), db, now=lambda: NOW)
    # A card the card data lacks can't be judged, so it stays.
    assert [c.name for c in report.missing] == ["Mind Stone", "Too New To Know"]


def test_outliers_are_deck_cards_few_performing_lists_play(deck: Deck, db: CardDb) -> None:
    lists = [*TOP_CUT_LISTS[:4], entry(5, 40, 8, ["Arcane Signet"])]
    report = analyse(deck, meta_data(lists), Profile(keep_categories=("Stax",)), db, now=lambda: NOW)
    # Battered Golem is in three of five; Magda is the commander and Mountain a basic.
    assert [(c.name, c.lists, c.flagged) for c in report.outliers] == [("Disruptor Flute", 0, True)]


def test_key_cards_are_the_commander_its_plan_and_what_the_profile_keeps(deck: Deck, db: CardDb) -> None:
    report = analyse(deck, meta_data(TOP_CUT_LISTS), Profile(keep_categories=("stax",)), db, now=lambda: NOW)
    assert [(k.name, k.reasons) for k in report.key_cards] == [
        (MAGDA, ("your commander",)),
        ("Battered Golem", ("you file it under Combo Pieces",)),
        ("Disruptor Flute", ("kept by your profile",)),
    ]


def field() -> list[MetaCommander]:
    big = MetaCommander(
        CommanderStats("Big Commander", 400, 100, 0.25, 0.04),
        (
            entry(1, 40, 8, ["Lightning Bolt", "Chaos Warp", "Mind Stone"]),
            entry(2, 40, 8, ["Lightning Bolt", "Mind Stone"]),
        ),
    )
    small = MetaCommander(
        CommanderStats("Small Commander", 200, 40, 0.2, 0.02),
        tuple(entry(i, 40, 8, ["Chaos Warp"] if i == 1 else ["Mind Stone"]) for i in range(1, 5)),
    )
    return [big, small]


def test_threats_are_common_meta_answers_to_key_pieces(deck: Deck, db: CardDb) -> None:
    report = analyse(
        deck, meta_data(TOP_CUT_LISTS, field=field()), Profile(keep_categories=("Stax",)), db, now=lambda: NOW
    )
    assert [(t.name, t.kinds, t.hits, t.played_by) for t in report.threats] == [
        (
            "Lightning Bolt",
            (AnswerKind.CREATURE_REMOVAL,),
            ("Battered Golem", MAGDA),
            (("Big Commander", 1.0),),
        ),
        # A quarter of the small commander's lists is under the 30% line, so only the big one names it.
        (
            "Chaos Warp",
            (AnswerKind.PERMANENT_REMOVAL,),
            ("Battered Golem", "Disruptor Flute", MAGDA),
            (("Big Commander", 0.5),),
        ),
    ]
    assert report.threats[0].exposure == pytest.approx(0.04)
    assert report.threats[1].exposure == pytest.approx(0.5 * 0.04 + 0.25 * 0.02)
    assert [s.name for s in report.field] == ["Big Commander", "Small Commander"]


def test_without_kept_cards_only_the_found_key_cards_are_threatened(deck: Deck, db: CardDb) -> None:
    report = analyse(deck, meta_data(TOP_CUT_LISTS, field=field()), Profile(), db, now=lambda: NOW)
    assert [t.hits for t in report.threats] == [("Battered Golem", MAGDA), ("Battered Golem", MAGDA)]


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


def test_a_sweeper_with_unreadable_text_reaches_creatures_only_as_a_board_wipe(tmp_path: Path) -> None:
    mystery = "Reshape the battlefield in a way no rule here follows."
    cards: list[str] = []
    for index, name in ((3, "Vandal Twin"), (4, "Wipe Twin")):
        raw = json.loads(extra_card(index, name, identity=[]))
        raw["oracle_text"] = mystery
        cards.append(json.dumps(raw))
    vandal, wipe = (f"eeeeeeee-0000-4000-8000-{FILLER_CARDS + i:012d}" for i in (3, 4))
    # Both are sweepers; Tagger files only the Vandal Twin under removal of artifacts (ADR 0020).
    sweeper = removal_tag(SWEEPER_TAG, "sweeper", [vandal, wipe])
    artifacts = removal_tag(REMOVAL_ARTIFACT, "removal-artifact", [vandal])
    with CardDb(build_card_db(tmp_path, [sweeper, artifacts], cards)) as db:
        answers = card_answers(db, {vandal: "Vandal Twin", wipe: "Wipe Twin"})
    creature = Piece("Test Elf", "Creature — Elf", "G", power=1, toughness=1, mana_value=1)
    rock = Piece("Test Rock", "Artifact", "", mana_value=2)
    assert [answers[vandal].removal(p) for p in (creature, rock)] == [None, ""]
    assert [answers[wipe].removal(p) for p in (creature, rock)] == ["", None]


def test_a_card_check_compares_lists_with_and_without_the_card(deck: Deck) -> None:
    off_cut = entry(20, 40, 8, ["Arcane Signet"])
    peers = [peer(1, ["Mind Stone"]), peer(2, ["Chaos Warp"])]
    data = meta_data([*TOP_CUT_LISTS, off_cut], peers, field())
    check = check_card("mind stone", deck, data)
    assert not check.in_deck
    assert (check.performing, check.performing_with, check.lists, check.lists_with) == (5, 3, 6, 3)
    assert (check.top_cuts_with, check.top_cuts_without) == (3, 2)
    # Placings 0, 1/40, 2/40 with it; 3/40, 4/40 and 19/40 without.
    assert check.mean_placing_with == pytest.approx(1 / 40)
    assert check.mean_placing_without == pytest.approx((3 + 4 + 19) / 3 / 40)
    assert (check.peers, check.peers_with) == (2, 1)
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
    report = analyse(deck, data, Profile(), db, now=lambda: NOW)
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
    assert analyse(deck, meta_data([]), Profile(), db, now=lambda: NOW).deck == "Magda"
    assert tmp_path.exists()


GOLEM_COMBO = ComboDetail(
    "1-2",
    (
        ComboPiece(MAGDA, "Legendary Creature — Dwarf Berserker", ("C",)),
        ComboPiece("Battered Golem", "Artifact Creature — Golem", ("B",)),
    ),
    (),
    ("Infinite artifact ETB",),
    "Activate Battered Golem.\nRepeat.",
)


def test_the_decks_own_combos_list_what_the_meta_commonly_plays_against_them(db: CardDb) -> None:
    [found] = own_combos([GOLEM_COMBO], meta_pool(field(), db), db)
    assert found.pieces == (MAGDA, "Battered Golem")
    # Most played first; Mind Stone stops nothing; Chaos Warp's small-commander quarter is under the line.
    assert [(a.card, a.role, a.played_by) for a in found.answers] == [
        ("Lightning Bolt", AnswerRole.REMOVAL, (("Big Commander", 1.0),)),
        ("Chaos Warp", AnswerRole.REMOVAL, (("Big Commander", 0.5),)),
    ]
    assert found.answers[1].exposure == pytest.approx(0.5 * 0.04 + 0.25 * 0.02)
    assert own_combos([], meta_pool(field(), db), db) == ()


def test_the_meta_pool_is_worked_out_once_for_the_threats_and_the_decks_combos(
    deck: Deck, db: CardDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[tuple[Theme, tuple[str, ...]]] = []
    real = db.matching

    def counting(theme: Theme, oracle_ids: Iterable[str]) -> frozenset[str]:
        ids = tuple(oracle_ids)
        asked.append((theme, ids))
        return real(theme, ids)

    monkeypatch.setattr(db, "matching", counting)
    data = replace(meta_data(TOP_CUT_LISTS, field=field()), own_combos=fetched((GOLEM_COMBO,)))
    report = analyse(deck, data, Profile(keep_categories=("Stax",)), db, now=lambda: NOW)
    assert report.threats
    assert report.own_combos[0].answers
    # Bolt, Chaos Warp and Mind Stone are the cards 30% of a top commander's lists play. Each answer kind and
    # each way to stop a combo is looked up for them once, not once per report that reads them.
    pool = {
        info.oracle_id for info in db.cards_named(["Lightning Bolt", "Chaos Warp", "Mind Stone"]).values()
    }
    assert sum(1 for _, ids in asked if set(ids) == pool) == len(AnswerKind) + len(Stop)
