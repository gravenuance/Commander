from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import date, timedelta

import pytest

from commander.answers import AnswerKind, CardAnswer, Piece, piece_facts, read_modes
from commander.archidekt import parse_deck
from commander.carddb import CardDb
from commander.metacombos import (
    MAX_COMBOS,
    MIN_COMBO_EXPOSURE,
    AnswerRole,
    ComboAnswer,
    ComboGroup,
    DeckAnswers,
    LockScope,
    MetaCombo,
    Stop,
    as_cast,
    card_stops,
    combo_answers,
    group_combos,
    meta_combos,
    stop_applies,
)
from commander.metadata import CommanderCombos, MetaCommander
from commander.models import Deck, DeckCard
from commander.spellbook import ComboDetail, ComboPiece, parse_details
from commander.tournaments import CommanderStats, Entry
from conftest import Json, build_card_db, load_fixture
from test_meta import NOW, REMOVAL_TAGS, entry

KINNAN = "Kinnan, Bonder Prodigy"
OTHER = "Tymna the Weaver"
SINCE = date(2026, 8, 16)


@pytest.fixture(scope="module")
def db(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CardDb]:
    with CardDb(build_card_db(tmp_path_factory.mktemp("combodb"), REMOVAL_TAGS)) as opened:
        yield opened


@pytest.fixture
def deck(raw_deck: Json) -> Deck:
    # Magda, Battered Golem, Mountain and Disruptor Flute, whose lock spares mana abilities.
    return parse_deck(raw_deck)


def details() -> dict[str, ComboDetail]:
    return {d.id: d for d in parse_details(load_fixture("spellbook_meta_kinnan.json"))}


def combo(description: str, *, results: tuple[str, ...] = (), zones: tuple[str, ...] = ("B",)) -> ComboDetail:
    piece = ComboPiece("Basalt Monolith", "Artifact", zones)
    return ComboDetail("1-2", (piece,), (), results, description)


def test_activation_locks_reach_the_pieces_whose_abilities_the_steps_activate() -> None:
    kinnan = details()["129-4131"]
    assert stop_applies(Stop.ACTIVATIONS, LockScope("Artifact"), kinnan)
    # Kinnan is the creature, but the steps only activate the Monolith.
    assert not stop_applies(Stop.ACTIVATIONS, LockScope("Creature"), kinnan)


def test_a_lock_that_spares_mana_abilities_misses_steps_that_add_mana() -> None:
    mana_only = combo("Activate Basalt Monolith by tapping it, adding {3}.\nRepeat.")
    assert not stop_applies(Stop.ACTIVATIONS, LockScope(None, spares_mana=True), mana_only)
    assert stop_applies(Stop.ACTIVATIONS, LockScope("Artifact"), mana_only)
    # Untapping the Monolith is no mana ability, so the same lock stops Kinnan's loop.
    assert stop_applies(Stop.ACTIVATIONS, LockScope(None, spares_mana=True), details()["129-4131"])


def test_casting_locks_meet_storm_and_repeated_casts() -> None:
    hullbreaker = details()["513-5034--46"]
    assert stop_applies(Stop.CASTING, LockScope(), hullbreaker)
    assert not stop_applies(Stop.CASTING, LockScope(), details()["129-4131"])
    assert not stop_applies(Stop.ENTER_TRIGGERS, LockScope(), hullbreaker)
    assert not stop_applies(Stop.GRAVEYARD, LockScope(), hullbreaker)


@pytest.mark.parametrize(
    ("stop", "detail"),
    [
        (Stop.GRAVEYARD, combo("Mill it.", zones=("G",))),
        (Stop.OFF_HAND_CASTING, combo("Cast Basalt Monolith from your graveyard.")),
        (Stop.CHEATED_IN, combo("Return Basalt Monolith to the battlefield.")),
        (Stop.ENTER_TRIGGERS, combo("When Basalt Monolith enters, it triggers, untapping a land.")),
        (Stop.ENTER_TRIGGERS, combo("Basalt Monolith enters the battlefield, causing you to win the game.")),
        (Stop.TOKENS, combo("Loop.", results=("Infinite artifact tokens",))),
    ],
)
def test_each_stop_reads_the_steps_it_needs(stop: Stop, detail: ComboDetail) -> None:
    assert stop_applies(stop, LockScope(), detail)
    assert not stop_applies(stop, LockScope(), combo("Activate Basalt Monolith."))


WITNESS = ComboPiece("Eternal Witness", "Creature — Human Shaman", ("B",))
TALON_GATES = ComboPiece("Talon Gates of Madara", "Land", ("B",))
CAGE = (
    "Creature cards in graveyards and libraries can't enter the battlefield.\n"
    "Players can't cast spells from graveyards or libraries."
)


def steps(description: str, *pieces: ComboPiece, results: tuple[str, ...] = ()) -> ComboDetail:
    return ComboDetail("1-2", pieces, (), results, description)


def test_enter_trigger_locks_need_a_trigger_on_a_card_of_their_type() -> None:
    creatures = LockScope("Creature")
    # Real Spellbook steps: Eternal Witness's trigger, Talon Gates' (a land) and Hullbreaker's plain entering.
    witness = steps(
        "When Eternal Witness enters, it triggers, returning Snap from your graveyard to your hand.", WITNESS
    )
    gates = steps(
        "When Talon Gates of Madara enters the battlefield, it triggers, "
        "phasing out up to one target creature.",
        TALON_GATES,
    )
    plain = steps("That permanent enters the battlefield.", results=("Infinite creature ETB",))
    assert stop_applies(Stop.ENTER_TRIGGERS, creatures, witness)
    assert not stop_applies(Stop.ENTER_TRIGGERS, creatures, gates)
    assert stop_applies(Stop.ENTER_TRIGGERS, LockScope(), gates)
    assert not stop_applies(Stop.ENTER_TRIGGERS, LockScope(), plain)
    assert not stop_applies(
        Stop.ENTER_TRIGGERS,
        creatures,
        steps("When the Treasure enters the battlefield, Battered Golem triggers."),
    )


def test_a_lock_on_some_zones_misses_cards_put_in_from_others() -> None:
    [(_, scope)] = [found for found in card_stops(CAGE, set()) if found[0] is Stop.CHEATED_IN]
    assert scope == LockScope("Creature", zones=frozenset({"graveyard", "library"}))
    from_hand = steps(
        "Activate Talon Gates of Madara's third ability by paying {4}, "
        "putting it from your hand onto the battlefield.",
        TALON_GATES,
    )
    from_graveyard = steps("Return Eternal Witness from your graveyard to the battlefield.", WITNESS)
    assert not stop_applies(Stop.CHEATED_IN, scope, from_hand)
    assert stop_applies(Stop.CHEATED_IN, scope, from_graveyard)


GRAVEYARD_HATE = {
    "Unlicensed Hearse": "{T}: Exile up to two target cards from a single graveyard.",
    "Deathrite Shaman": "{B}, {T}: Exile target instant or sorcery card from a graveyard. "
    "Each opponent loses 2 life.",
    "Bojuka Bog": "This land enters tapped.\nWhen this land enters, exile target player's graveyard.",
    "Relic of Progenitus": "{T}: Target player exiles a card from their graveyard.\n"
    "{1}, Exile this artifact: Exile all graveyards. Draw a card.",
    "Tormod's Crypt": "{T}, Sacrifice this artifact: Exile target player's graveyard.",
    "Dauthi Voidwalker": "If a card would be put into an opponent's graveyard from anywhere, "
    "instead exile it with a void counter on it.",
    "Rest in Peace": "When this enchantment enters, exile all graveyards.\n"
    "If a card or token would be put into a graveyard from anywhere, exile it instead.",
}
DAWNBRINGER_CLERIC = (
    "When this creature enters, choose one —\n• Cure Wounds — You gain 2 life.\n"
    "• Dispel Magic — Destroy target enchantment.\n• Gentle Repose — Exile target card from a graveyard."
)


@pytest.mark.parametrize("name", GRAVEYARD_HATE)
def test_graveyard_hate_seals_empties_or_keeps_exiling_graveyards(name: str) -> None:
    assert Stop.GRAVEYARD in {stop for stop, _ in card_stops(GRAVEYARD_HATE[name], {Stop.GRAVEYARD})}


def test_exiling_one_card_once_or_nothing_is_no_graveyard_hate() -> None:
    for text in (CAGE, DAWNBRINGER_CLERIC):
        assert Stop.GRAVEYARD not in {stop for stop, _ in card_stops(text, {Stop.GRAVEYARD})}


def test_a_tag_alone_makes_no_instant_a_lock() -> None:
    # Tagger files Mindbreak Trap as storm hate, but it is a counterspell; Silence's text is a lock.
    mindbreak = "Exile any number of target spells."
    assert card_stops(mindbreak, {Stop.CASTING}, one_shot=True) == ()
    assert card_stops(mindbreak, {Stop.CASTING}) == ((Stop.CASTING, LockScope()),)
    silence = "Your opponents can't cast spells this turn."
    assert [stop for stop, _ in card_stops(silence, set(), one_shot=True)] == [Stop.CASTING]


def test_pieces_cast_for_nothing_are_marked_free() -> None:
    amber = ComboPiece("Mox Amber", "Legendary Artifact", ("B",))
    vault = ComboPiece("Mana Vault", "Artifact", ("B",))
    facts = {"mox amber": Piece("Mox Amber", "Legendary Artifact", "", mana_value=0)}
    loop = steps("Cast Mana Vault by paying {1}.\nCast Mox Amber by paying {0}", amber, vault)
    assert as_cast(loop, amber, facts).cast_free
    assert not as_cast(loop, vault, facts).cast_free
    # An X spell's mana value is 0, but the combo pays for X.
    ballista = ComboPiece("Walking Ballista", "Artifact Creature — Construct", ("H",))
    ballista_fact = Piece("Walking Ballista", ballista.type_line, "", 0, 0, mana_value=0, x_cost=True)
    assert not as_cast(
        steps("Cast Walking Ballista.", ballista), ballista, {"walking ballista": ballista_fact}
    ).cast_free
    # A piece the card data doesn't know is free only when the steps say so.
    assert as_cast(steps("Cast Mana Vault by paying {0}.", vault), vault, {}).cast_free


def test_an_answer_names_every_piece_it_reaches_and_its_catch() -> None:
    breach = ComboPiece("Underworld Breach", "Enchantment", ("B",))
    led = ComboPiece("Lion's Eye Diamond", "Artifact", ("B",))
    freeze = ComboPiece("Brain Freeze", "Instant", ("H",))
    facts = {"brain freeze": Piece("Brain Freeze", "Instant", "U", mana_value=2, storm=True)}
    chaos_warp = "The owner of target permanent shuffles it into their library."
    cards = {
        "Chaos Warp": (chaos_warp, AnswerKind.PERMANENT_REMOVAL),
        "Mindbreak Trap": ("Exile any number of target spells.", AnswerKind.COUNTERSPELL),
        "Spell Pierce": (
            "Counter target noncreature spell unless its controller pays {2}.",
            AnswerKind.COUNTERSPELL,
        ),
    }
    deck = DeckAnswers({n: CardAnswer(n, (kind,), read_modes(text)) for n, (text, kind) in cards.items()}, {})
    loop = steps("Cast Brain Freeze.", breach, led, freeze)
    assert combo_answers(loop, deck, facts) == (
        ComboAnswer("Chaos Warp", "removes Underworld Breach or Lion's Eye Diamond", AnswerRole.REMOVAL),
        ComboAnswer(
            "Mindbreak Trap",
            "counters Underworld Breach, Lion's Eye Diamond or Brain Freeze",
            AnswerRole.COUNTER,
        ),
        ComboAnswer(
            "Spell Pierce",
            "counters Underworld Breach or Lion's Eye Diamond",
            AnswerRole.COUNTER,
            "unless they pay {2}",
        ),
    )


def test_piece_facts_come_from_the_card_data(db: CardDb) -> None:
    facts = piece_facts(db, [("magda, brazen outlaw", "Creature"), ("Nobody Knows", "Creature — Horror")])
    assert facts["magda, brazen outlaw"] == Piece(
        "Magda, Brazen Outlaw", "Legendary Creature — Dwarf Berserker", "R", 2, 1, mana_value=2.0
    )
    assert facts["nobody knows"] == Piece("Nobody Knows", "Creature — Horror")


KINNAN_SHARE = 0.04
# 14 and 44 days before NOW: inside the last 30 days, and inside the 30 before them.
RECENT = date(2026, 10, 1)
EARLIER = date(2026, 9, 1)
MONOLITH = [KINNAN, "Basalt Monolith"]


def commander_field(
    name: str, lists: Sequence[Entry], meta_share: float = KINNAN_SHARE
) -> list[MetaCommander]:
    return [MetaCommander(CommanderStats(name, 1000, 300, 0.3, meta_share), tuple(lists))]


def kinnan_field(lists: str, meta_share: float = KINNAN_SHARE) -> list[MetaCommander]:
    """Kinnan's lists, one per letter: C holds the Basalt Monolith combo, - doesn't."""
    entries = [entry(i + 1, 40, 8, MONOLITH if mark == "C" else ["Sol Ring"]) for i, mark in enumerate(lists)]
    return commander_field(KINNAN, entries, meta_share)


def found() -> list[CommanderCombos]:
    return [CommanderCombos(KINNAN, tuple(details().values()))]


def combos_of(
    deck: Deck, field: Sequence[MetaCommander], db: CardDb, *, total: float = KINNAN_SHARE
) -> tuple[MetaCombo, ...]:
    """The meta combos of `found()` when the top commanders' meta shares add up to `total`."""
    return meta_combos(deck, field, found(), SINCE, db, now=lambda: NOW, meta_total=total)


def test_meta_combos_are_those_enough_lists_hold_with_the_decks_answers(deck: Deck, db: CardDb) -> None:
    bolt = DeckCard("Lightning Bolt", 1, ("Removal",), True, "x", 1, 1, "tst", "1", "Normal")  # noqa: FBT003 — positional like the model
    with_bolt = replace(deck, cards=(*deck.cards, bolt))
    field = kinnan_field("C-")
    [kinnan] = combos_of(with_bolt, field, db)
    assert kinnan.id == "129-4131"
    assert kinnan.played_by == ((KINNAN, 0.5),)
    assert kinnan.exposure == pytest.approx(0.5 * 0.04)
    assert kinnan.answers == (
        ComboAnswer("Disruptor Flute", "stops activated abilities", AnswerRole.STAX),
        ComboAnswer("Lightning Bolt", f"removes {KINNAN}", AnswerRole.REMOVAL),
    )


def test_a_combo_in_too_few_lists_is_left_out(deck: Deck, db: CardDb) -> None:
    field = kinnan_field("C-----")
    assert combos_of(deck, field, db) == ()


def test_lists_before_the_window_do_not_count(deck: Deck, db: CardDb) -> None:
    old = entry(1, 40, 8, MONOLITH, held_on=date(2026, 8, 1))
    assert combos_of(deck, commander_field(KINNAN, [old]), db) == ()


def test_a_list_holds_its_commander_even_when_the_decklist_leaves_it_out(deck: Deck, db: CardDb) -> None:
    lists = [entry(1, 40, 8, ["Basalt Monolith"])]
    assert [c.id for c in combos_of(deck, commander_field(KINNAN, lists), db)] == ["129-4131"]


def test_a_combo_is_listed_from_a_twentieth_of_the_meta(deck: Deck, db: CardDb) -> None:
    # Half of Kinnan's lists hold it: 0.5 x 0.1 = 5% of a meta whose shares add up to 1, and 4.9% at 0.098.
    assert [c.exposure for c in combos_of(deck, kinnan_field("C-", 0.1), db, total=1.0)] == [0.05]
    assert combos_of(deck, kinnan_field("C-", 0.098), db, total=1.0) == ()
    assert MIN_COMBO_EXPOSURE == 0.05


def test_the_floor_is_a_share_of_the_whole_meta_not_of_the_commanders_that_have_lists(
    deck: Deck, db: CardDb
) -> None:
    # The same exposure is under the floor once the other commanders' shares are counted.
    field = kinnan_field("C-", 0.1)
    assert len(combos_of(deck, field, db, total=1.0)) == 1
    assert combos_of(deck, field, db, total=1.2) == ()
    assert combos_of(deck, field, db, total=0.0) == ()


def many_combos() -> list[CommanderCombos]:
    """105 made-up combos of one card each."""
    one = tuple(
        ComboDetail(f"c{n:03d}", (ComboPiece(f"Card {n:03d}", "Artifact", ("B",)),), (), (), "Tap it.")
        for n in range(105)
    )
    return [CommanderCombos(KINNAN, one)]


def test_at_most_a_hundred_combos_are_listed_the_most_exposed_first(deck: Deck, db: CardDb) -> None:
    # The first 100 are in every one of Kinnan's 10 lists, the last 5 in 3 of them.
    everything = [KINNAN, *(f"Card {n:03d}" for n in range(105))]
    most = [KINNAN, *(f"Card {n:03d}" for n in range(100))]
    lists = [entry(i + 1, 40, 8, everything if i < 3 else most) for i in range(10)]
    field = commander_field(KINNAN, lists, 0.5)
    listed = meta_combos(deck, field, many_combos(), SINCE, db, now=lambda: NOW, meta_total=0.5)
    assert MAX_COMBOS == 100
    assert [c.id for c in listed] == [f"c{n:03d}" for n in range(100)]
    assert listed[0].exposure == pytest.approx(0.5)


def test_an_old_list_counts_less_so_a_recent_combo_passes_the_fifth(deck: Deck, db: CardDb) -> None:
    # One fresh list holds it; five from 60 days ago (a quarter the weight each) do not: 1 / 2.25, not 1 / 6.
    fresh = entry(1, 40, 8, MONOLITH, held_on=date(2026, 10, 15))
    stale = [entry(n, 40, 8, ["Sol Ring"], held_on=date(2026, 8, 16)) for n in range(2, 7)]
    [kinnan] = combos_of(deck, commander_field(KINNAN, [fresh, *stale]), db)
    assert kinnan.played_by[0][1] == pytest.approx(1 / 2.25)


def test_an_old_list_holding_the_combo_can_pull_it_under_the_fifth(deck: Deck, db: CardDb) -> None:
    # One list from 60 days ago holds it, three fresh ones do not: 0.25 / 3.25 is 8%, though 1 of 4 is 25%.
    stale = entry(1, 40, 8, MONOLITH, held_on=date(2026, 8, 16))
    fresh = [entry(n, 40, 8, ["Sol Ring"], held_on=date(2026, 10, 15)) for n in range(2, 5)]
    assert combos_of(deck, commander_field(KINNAN, [stale, *fresh]), db) == ()


def test_a_bigger_event_weighs_more_by_its_size_factor(deck: Deck, db: CardDb) -> None:
    # Same day: 64 players weigh 1 + log2(4) = 3 and 16 players weigh 1, so the 64-player list is 3 of 4.
    big = entry(1, 64, 8, MONOLITH)
    small = entry(2, 16, 4, ["Sol Ring"])
    [kinnan] = combos_of(deck, commander_field(KINNAN, [big, small]), db)
    assert kinnan.played_by[0][1] == pytest.approx(0.75)


def halves(recent: str, earlier: str, name: str = KINNAN) -> list[Entry]:
    """A commander's lists, one per letter, the recent ones first: C holds the Monolith combo, - doesn't."""
    cards = [name, "Basalt Monolith", KINNAN]
    days = [(mark, RECENT) for mark in recent] + [(mark, EARLIER) for mark in earlier]
    return [
        entry(i, 40, 8, cards if mark == "C" else ["Sol Ring"], held_on=day)
        for i, (mark, day) in enumerate(days, start=1)
    ]


@pytest.mark.parametrize(
    ("recent", "earlier", "rising"),
    [
        # 4/5 is twice 2/5 exactly; 4/5 is 1.87 times 3/7, and 7/10 is 1.4 times 5/10.
        ("CCCC-", "CC---", True),
        ("CCCC-", "CCC----", False),
        ("CCCCCCC---", "CCCCC-----", False),
        # None of the earlier lists hold it: two of the recent ones is enough, one is not.
        ("CC-", "---", True),
        ("C--", "---", False),
        # Neither half holds it more.
        ("CC-", "CC-", False),
        ("C--", "CC-", False),
        # A half under three lists cannot be judged.
        ("CC", "---", False),
        ("CC-", "--", False),
    ],
)
def test_a_combo_is_rising_when_the_recent_lists_hold_it_twice_as_often(
    deck: Deck, db: CardDb, recent: str, earlier: str, *, rising: bool
) -> None:
    [kinnan] = combos_of(deck, commander_field(KINNAN, halves(recent, earlier)), db)
    assert kinnan.rising is rising


@pytest.mark.parametrize(
    ("extra", "age", "recent", "earlier", "rising"),
    [
        # 29 days old is still in the last 30: with it two of four recent lists hold the combo, none earlier.
        ("C", 29, "C--", "---", True),
        # 30 days old is in the 30 before: one of four earlier lists holds it against one of three recent.
        ("C", 30, "C--", "---", False),
        # 59 days old is the last day of the earlier half: 4 of 4 recent against 2 of 4 earlier is twice.
        ("-", 59, "CCCC", "CC-", True),
        # 60 days old is in neither half: 4 of 4 against 2 of 3 is not.
        ("-", 60, "CCCC", "CC-", False),
    ],
)
def test_the_halves_split_at_30_and_60_days(
    deck: Deck, db: CardDb, *, extra: str, age: int, recent: str, earlier: str, rising: bool
) -> None:
    cards = [KINNAN, "Basalt Monolith"]
    old = entry(99, 40, 8, cards if extra == "C" else ["Sol Ring"], held_on=NOW.date() - timedelta(days=age))
    lists = [*halves(recent, earlier), old]
    [kinnan] = combos_of(deck, commander_field(KINNAN, lists), db)
    assert kinnan.rising is rising


def test_a_combo_with_no_recent_lists_is_not_rising(deck: Deck, db: CardDb) -> None:
    # Every list is 44 days old.
    [kinnan] = combos_of(deck, kinnan_field("CC-"), db)
    assert not kinnan.rising


def test_the_halves_pool_across_commanders_that_have_enough_lists(deck: Deck, db: CardDb) -> None:
    flat = commander_field(KINNAN, halves("CC--", "CC--"))
    both = [
        CommanderCombos(KINNAN, tuple(details().values())),
        CommanderCombos(OTHER, tuple(details().values())),
    ]
    # A second commander with 3 recent lists all holding it but only 2 earlier ones is left out of the pool.
    thin = commander_field(OTHER, halves("CCC", "--", OTHER), 0.04)
    [one] = meta_combos(deck, flat + thin, both, SINCE, db, now=lambda: NOW, meta_total=0.08)
    assert not one.rising
    assert sorted(name for name, _ in one.played_by) == [KINNAN, OTHER]
    # With a third earlier list it is judged, and the pool is 5/7 of the recent lists against 2/7.
    judged = commander_field(OTHER, halves("CCC", "---", OTHER), 0.04)
    [two] = meta_combos(deck, flat + judged, both, SINCE, db, now=lambda: NOW, meta_total=0.08)
    assert two.rising


def test_an_off_hand_casting_lock_reaches_only_the_zones_it_names() -> None:
    # Squee + Food Chain casts Squee from exile: Grafdigger's Cage seals graveyards and libraries only.
    from_exile = combo(
        "Exile Squee, the Immortal with Food Chain.\nCast Squee, the Immortal from exile.\nRepeat."
    )
    from_graveyard = combo("Cast Basalt Monolith from your graveyard.")
    cage = card_stops(CAGE, set())
    assert (Stop.OFF_HAND_CASTING, LockScope(None, zones=frozenset({"graveyard", "library"}))) in cage
    assert not stop_applies(
        Stop.OFF_HAND_CASTING, LockScope(None, zones=frozenset({"graveyard", "library"})), from_exile
    )
    assert stop_applies(
        Stop.OFF_HAND_CASTING, LockScope(None, zones=frozenset({"graveyard", "library"})), from_graveyard
    )
    # Drannith Magistrate names no zone: anywhere but the hand.
    assert stop_applies(Stop.OFF_HAND_CASTING, LockScope(), from_exile)


def test_a_lock_only_during_its_owners_turn_stops_no_combo() -> None:
    # Grand Abolisher keeps opponents quiet on its owner's turn; their combos happen on theirs.
    abolisher = (
        "During your turn, your opponents can't cast spells or activate abilities of artifacts, creatures, "
        "enchantments, or planeswalkers."
    )
    assert card_stops(abolisher, {Stop.ACTIVATIONS}) == ()


def test_a_lock_on_noncreature_spells_misses_a_loop_that_casts_creatures() -> None:
    squee = ComboPiece("Squee, the Immortal", "Legendary Creature — Goblin", ("E",))
    food_chain = ComboPiece("Food Chain", "Enchantment", ("B",))
    loop = steps(
        "Exile Squee with Food Chain, adding mana.\nCast Squee from exile.\nRepeat.\nCast Squee again.",
        squee,
        food_chain,
    )
    [(stop, scope)] = card_stops("Each player can't cast more than one noncreature spell each turn.", set())
    assert stop is Stop.CASTING
    assert not stop_applies(stop, scope, loop)
    # Silence stops every spell.
    [(stop, scope)] = card_stops("Your opponents can't cast spells this turn.", set())
    assert stop_applies(stop, scope, loop)


def made_up(combo_id: str, exposure: float, *pieces: str) -> MetaCombo:
    return MetaCombo(combo_id, f"https://example.test/{combo_id}", pieces, (), (), exposure, (), ())


def grouped(*combos: MetaCombo) -> list[tuple[tuple[str, ...], list[str]]]:
    return [(g.shared, [c.id for c in g.combos]) for g in group_combos(combos)]


def test_combos_sharing_two_pieces_group_under_the_largest_core_they_hold() -> None:
    # A and B share {a, b, c}; C shares only {a, b} with each, a core that A and B have left for a larger one.
    a = made_up("A", 0.5, "a", "b", "c", "x")
    b = made_up("B", 0.4, "a", "b", "c", "y")
    c = made_up("C", 0.3, "a", "b", "z", "w")
    # C's only core is left with C alone, so C stands on its own.
    assert grouped(a, b, c) == [(("a", "b", "c"), ["A", "B"]), ((), ["C"])]


def test_a_core_shared_by_three_combos_holds_all_three_and_commanders_count_as_pieces() -> None:
    kinnan = "Kinnan, Bonder Prodigy"
    a = made_up("A", 0.3, kinnan, "Thassa's Oracle", "x")
    b = made_up("B", 0.2, kinnan, "Thassa's Oracle", "y")
    c = made_up("C", 0.1, kinnan, "Thassa's Oracle", "z")
    assert grouped(a, b, c) == [(("Kinnan, Bonder Prodigy", "Thassa's Oracle"), ["A", "B", "C"])]


def test_a_combo_fitting_two_cores_of_one_size_joins_the_core_with_more_exposure() -> None:
    x = made_up("X", 0.2, "a", "b", "c", "d")
    y = made_up("Y", 0.1, "a", "b", "p", "q")
    z = made_up("Z", 0.3, "c", "d", "r", "s")
    # {c, d} has X and Z for 0.5 against 0.3 for {a, b}; Y is then alone with a core of one combo.
    assert grouped(x, y, z) == [(("c", "d"), ["X", "Z"]), ((), ["Y"])]


def test_equal_core_sizes_and_exposure_go_to_the_core_first_by_name() -> None:
    x = made_up("X", 0.2, "c", "d", "a", "b")
    y = made_up("Y", 0.3, "a", "b", "p", "q")
    z = made_up("Z", 0.3, "c", "d", "r", "s")
    assert grouped(x, y, z) == [(("a", "b"), ["X", "Y"]), ((), ["Z"])]


def test_combos_sharing_fewer_than_two_pieces_with_every_other_stay_single() -> None:
    a = made_up("A", 0.4, "a", "b", "c")
    b = made_up("B", 0.3, "a", "x", "y")
    c = made_up("C", 0.2, "b", "p", "q")
    assert grouped(a, b, c) == [((), ["A"]), ((), ["B"]), ((), ["C"])]


def test_groups_and_singles_come_by_best_exposure_and_a_group_keeps_its_given_order() -> None:
    low = made_up("Low", 0.1, "a", "b", "c")
    alone = made_up("Alone", 0.25, "u", "v", "w")
    high = made_up("High", 0.3, "a", "b", "d")
    other = made_up("Other", 0.2, "m", "n", "o")
    # The group's best exposure, 0.3, puts it ahead of the single at 0.25; Low stays ahead of High inside it.
    assert grouped(low, alone, high, other) == [
        (("a", "b"), ["Low", "High"]),
        ((), ["Alone"]),
        ((), ["Other"]),
    ]


def test_combos_with_the_same_pieces_group_and_no_combos_give_no_groups() -> None:
    same = [made_up("A", 0.2, "a", "b"), made_up("B", 0.1, "a", "b")]
    assert grouped(*same) == [(("a", "b"), ["A", "B"])]
    assert group_combos([]) == ()


def test_a_group_carries_its_combos_unchanged() -> None:
    a, b = made_up("A", 0.2, "a", "b", "x"), made_up("B", 0.1, "a", "b", "y")
    assert group_combos([a, b]) == (ComboGroup(("a", "b"), (a, b)),)
