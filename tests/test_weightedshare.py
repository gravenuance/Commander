"""One weighted share per card, pooled from the commander's EDHTop16 lists and recent Archidekt decks."""

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from commander.archidekt import PeerDeck
from commander.metadata import PeerList
from commander.weightedshare import (
    IDEA_LOWER_BOUND,
    MIN_SIZE_FACTOR,
    ShareTable,
    SourceShare,
    WeightedShare,
    effective_sample_size,
    placement_weight,
    recency_weight,
    share_reason,
    share_tip,
    size_factor,
    weighted_shares,
    wilson_lower_bound,
)
from test_meta import NOW, entry, meta_data


def peer_at(deck_id: int, cards: list[str], updated: datetime = NOW) -> PeerList:
    return PeerList(PeerDeck(deck_id, f"deck {deck_id}", "someone", 10, updated, 5), tuple(cards))


def test_a_lists_weight_halves_every_30_days() -> None:
    assert recency_weight(0) == pytest.approx(1.0)
    assert recency_weight(30) == pytest.approx(0.5)
    assert recency_weight(60) == pytest.approx(0.25)
    assert recency_weight(15) == pytest.approx(0.5**0.5)
    # A clock behind the list is no reason to weigh it above a fresh one.
    assert recency_weight(-3) == pytest.approx(1.0)


def test_an_event_counts_by_how_many_doublings_it_is_over_16_players() -> None:
    assert size_factor(16) == pytest.approx(1.0)
    assert size_factor(32) == pytest.approx(2.0)
    assert size_factor(64) == pytest.approx(3.0)
    # Under half the base the factor would reach 0 or below: a floor keeps a tiny event a small voice.
    assert size_factor(8) == pytest.approx(MIN_SIZE_FACTOR)
    assert size_factor(1) == pytest.approx(MIN_SIZE_FACTOR)
    assert 0 < MIN_SIZE_FACTOR < 1


def test_a_placement_weighs_its_percentile_times_the_event_size_factor() -> None:
    # Winner of 32: percentile 1, factor 2. 17th of 32: percentile 1 - 16/32, factor 2.
    assert placement_weight(entry(1, 32, 8, [("A", 0)])) == pytest.approx(2.0)
    assert placement_weight(entry(17, 32, 8, [("A", 0)])) == pytest.approx(1.0)
    # Last of 16: one place short of a zero percentile.
    assert placement_weight(entry(16, 16, 4, [("A", 0)])) == pytest.approx(1 / 16)
    # EDHTop16 gives some entries no standing: they sit in the middle.
    assert placement_weight(entry(0, 16, 4, [("A", 0)])) == pytest.approx(0.5)


def test_a_recent_winner_of_a_big_event_outweighs_an_old_low_finisher() -> None:
    winner = entry(1, 32, 8, [("Mind Stone", 0), ("Jeska's Will", 0)], held_on=date(2026, 10, 15))
    low = entry(9, 16, 4, [("Mind Stone", 0), ("Arcane Signet", 0)], held_on=date(2026, 8, 16))
    table = weighted_shares(meta_data([winner, low]), now=lambda: NOW)
    # Weights 2 and 0.125 (the second is 60 days old, percentile 1/2, factor 1), normalised to mean 1:
    # 2/1.0625 and 0.125/1.0625, out of two lists.
    jeska, signet, stone = (table.of(name) for name in ("Jeska's Will", "Arcane Signet", "Mind Stone"))
    assert jeska is not None
    assert signet is not None
    assert stone is not None
    assert jeska.share == pytest.approx(16 / 17)
    assert signet.share == pytest.approx(1 / 17)
    assert stone.share == pytest.approx(1.0)
    # The raw counts stay one list each.
    assert (jeska.count, jeska.total, signet.count, signet.total) == (1, 2, 1, 2)


def test_each_sources_weights_are_scaled_to_a_mean_of_one_so_a_list_stays_about_one_vote() -> None:
    # Four lists of one event on one day, all far older than the half-life: the weights are tiny, the
    # shares still the plain count.
    lists = [
        entry(1, 16, 4, [("Mind Stone", 0)], held_on=date(2025, 10, 15)),
        entry(1, 16, 4, [("Mind Stone", 0)], held_on=date(2025, 10, 15)),
        entry(1, 16, 4, [("Arcane Signet", 0)], held_on=date(2025, 10, 15)),
        entry(1, 16, 4, [("Arcane Signet", 0)], held_on=date(2025, 10, 15)),
    ]
    data = replace(meta_data(lists), since=date(2025, 1, 1))
    stone = weighted_shares(data, now=lambda: NOW).of("Mind Stone")
    assert stone is not None
    assert stone.share == pytest.approx(0.5)


def test_the_two_sources_pool_by_their_list_counts_whatever_their_ages() -> None:
    # Two meta lists a year old, one running the card; three fresh decks, one running it.
    old = date(2025, 10, 15)
    lists = [entry(1, 16, 4, [("Mind Stone", 0)], held_on=old), entry(1, 16, 4, [("Fury", 0)], held_on=old)]
    peers = [peer_at(1, ["Mind Stone"]), peer_at(2, ["Fury"]), peer_at(3, ["Fury"])]
    data = replace(meta_data(lists, peers), since=date(2025, 1, 1))
    stone = weighted_shares(data, now=lambda: NOW).of("Mind Stone")
    # (2 x 1/2 + 3 x 1/3) / 5: the old lists' small weights change nothing between the sources.
    assert stone is not None
    assert (stone.count, stone.total, stone.share) == (2, 5, pytest.approx(0.4))
    assert stone.meta is not None
    assert (stone.meta.count, stone.meta.total, stone.meta.share) == (1, 2, pytest.approx(0.5))
    assert stone.archidekt is not None
    assert (stone.archidekt.count, stone.archidekt.total, stone.archidekt.share) == (
        1,
        3,
        pytest.approx(1 / 3),
    )


def test_an_archidekt_deck_weighs_by_its_age_alone() -> None:
    peers = [
        peer_at(1, ["Mind Stone"], NOW),
        peer_at(2, ["Fury"], datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        peer_at(3, ["Fury"], datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
    ]
    stone = weighted_shares(meta_data([], peers), now=lambda: NOW).of("Mind Stone")
    # Weights 1, 0.5, 0.5 over a mean of 2/3: the fresh deck is 1.5 of 3.
    assert stone is not None
    assert stone.share == pytest.approx(0.5)
    assert (stone.count, stone.total) == (1, 3)
    assert stone.meta is None


def test_a_card_counts_once_per_list_and_a_list_without_cards_or_outside_the_window_not_at_all() -> None:
    twice = entry(1, 16, 4, [("Mind Stone", 0), ("Mind Stone", 1)])
    bare = entry(2, 16, 4, [])
    stale = entry(3, 16, 4, [("Mind Stone", 0)], held_on=date(2026, 8, 15))
    table = weighted_shares(meta_data([twice, bare, stale]), now=lambda: NOW)
    stone = table.of("mind stone")
    assert stone is not None
    assert (stone.count, stone.total, stone.share) == (1, 1, pytest.approx(1.0))


def test_fewer_than_three_archidekt_decks_make_no_source() -> None:
    two = meta_data([], [peer_at(1, ["Mind Stone"]), peer_at(2, ["Mind Stone"])])
    table = weighted_shares(two, now=lambda: NOW)
    assert not table
    assert table.of("Mind Stone") is None


def test_no_data_gives_no_share() -> None:
    assert not weighted_shares(None, now=lambda: NOW)
    nothing = weighted_shares(meta_data([]), now=lambda: NOW)
    assert not nothing
    assert nothing.of("Mind Stone") is None


def test_a_card_no_list_plays_is_zero_of_the_lists_read() -> None:
    peers = [peer_at(i, ["Fury"]) for i in range(1, 4)]
    lists = [entry(1, 16, 4, [("Fury", 0)]), entry(2, 16, 4, [("Fury", 0)])]
    gone = weighted_shares(meta_data(lists, peers), now=lambda: NOW).of("Mind Stone")
    assert gone == WeightedShare(0.0, 0, 5, SourceShare(0.0, 0, 2), SourceShare(0.0, 0, 3))


def test_the_wording_names_the_sources_there_are() -> None:
    both = WeightedShare(0.34, 12, 35, SourceShare(0.41, 8, 20), SourceShare(0.27, 4, 15))
    assert share_reason(both) == "in 34% of meta lists and recent decks (12/35)"
    assert share_tip(both) == "12/35 · meta 41% (8/20 lists) · Archidekt 27% (4/15 decks)"
    meta = WeightedShare(0.41, 8, 20, SourceShare(0.41, 8, 20), None)
    assert share_reason(meta) == "in 41% of meta lists (8/20)"
    assert share_tip(meta) == "meta 41% (8/20 lists)"
    decks = WeightedShare(0.27, 4, 15, None, SourceShare(0.27, 4, 15))
    assert share_reason(decks) == "in 27% of recent decks (4/15)"
    assert share_tip(decks) == "Archidekt 27% (4/15 decks)"


def test_the_wilson_lower_bound_matches_hand_computed_values() -> None:
    # 50 of 100: the textbook 95% interval is 0.4038 to 0.5962.
    assert wilson_lower_bound(0.5, 100) == pytest.approx(0.4038, abs=1e-4)
    # 1 of 1: 1 / (1 + z^2) from the closed form.
    assert wilson_lower_bound(1.0, 1) == pytest.approx(0.2065, abs=1e-4)
    assert wilson_lower_bound(1.0, 3) == pytest.approx(0.4385, abs=1e-4)
    assert wilson_lower_bound(0.6, 10) == pytest.approx(0.3127, abs=1e-4)
    assert wilson_lower_bound(0.7, 10) == pytest.approx(0.3968, abs=1e-4)
    # A looser z gives a higher bound: 80% confidence, z = 1.2816.
    assert wilson_lower_bound(0.5, 100, z=1.2816) == pytest.approx(0.4364, abs=1e-4)


def test_the_wilson_lower_bound_of_nothing_seen_or_a_card_no_list_plays_is_zero() -> None:
    assert wilson_lower_bound(0.5, 0) == 0.0
    assert wilson_lower_bound(0.5, -1) == 0.0
    assert wilson_lower_bound(0.0, 20) == pytest.approx(0.0, abs=1e-12)


def test_the_kish_effective_sample_size_shrinks_as_the_weights_spread() -> None:
    assert effective_sample_size([1, 1, 1, 1]) == pytest.approx(4.0)
    # (2 + 1 + 1)^2 / (4 + 1 + 1) = 16 / 6.
    assert effective_sample_size([2, 1, 1]) == pytest.approx(8 / 3)
    # Scaling every weight changes nothing.
    assert effective_sample_size([0.5, 0.25, 0.25]) == pytest.approx(8 / 3)
    assert effective_sample_size([]) == 0.0
    assert effective_sample_size([0.0, 0.0]) == 0.0


def test_the_table_takes_its_effective_sample_size_from_every_scaled_weight() -> None:
    peers = [
        peer_at(1, ["Mind Stone"], NOW),
        peer_at(2, ["Fury"], datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
        peer_at(3, ["Fury"], datetime(2026, 9, 15, 12, 0, tzinfo=UTC)),
    ]
    # Weights 1, 0.5, 0.5 scale to 1.5, 0.75, 0.75: 3^2 / (2.25 + 0.5625 + 0.5625).
    table = weighted_shares(meta_data([], peers), now=lambda: NOW)
    assert table.effective_n == pytest.approx(8 / 3)
    assert weighted_shares(None, now=lambda: NOW).effective_n == 0.0


def test_a_card_is_an_idea_from_a_lower_bound_of_32_percent() -> None:
    assert IDEA_LOWER_BOUND == 0.32
    seen = WeightedShare(0.0, 0, 10, None, None)

    def table(share: float, n: float) -> ShareTable:
        card = WeightedShare(share, round(share * 10), 10, None, SourceShare(share, round(share * 10), 10))
        return ShareTable({"mind stone": card}, seen, n)

    # Lower bounds 0.3968 and 0.3127 at ten lists.
    assert table(0.7, 10).is_idea("Mind Stone")
    assert not table(0.6, 10).is_idea("Mind Stone")
    # The same share on a fifth of the evidence is not enough, and on ten times of it is.
    assert not table(0.7, 2).is_idea("Mind Stone")
    assert table(0.42, 100).is_idea("Mind Stone")
    assert not table(0.4, 100).is_idea("Mind Stone")
    # A card no list plays, and a table with no lists at all.
    assert not table(0.7, 10).is_idea("Fury")
    assert not ShareTable({}, WeightedShare(0.0, 0, 0, None, None)).is_idea("Mind Stone")
