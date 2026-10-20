"""The stats above the app's decklist; the deck's own numbers are checked end to end in test_desk."""

from commander.deckstats import DeckStats, StatCard, deck_stats


def test_unpriced_dont_have_cards_are_counted_apart_and_a_deck_of_lands_has_no_average() -> None:
    cards = [
        StatCard("Mountain", 30, "Don't Have", 12, 0.0, land=True),
        StatCard("Ancient Tomb", 1, "Don't Have", None, 0.0, land=True),
        StatCard("Wastes", 0, "Don't Have", 5, 0.0, land=True),
        StatCard("Command Tower", 1, "Proxy", 25, 0.0, land=True),
    ]
    # 30 Mountains at 0.12 and an unpriced Ancient Tomb; no Wastes left, and a Proxy is not Don't Have.
    assert deck_stats(cards) == DeckStats(
        cards=32, dont_have=31, dont_have_cents=360, dont_have_unpriced=1, average_mana_value=None
    )


def test_the_average_mana_value_weighs_each_copy_and_leaves_lands_out() -> None:
    cards = [
        StatCard("Relentless Rats", 3, "", 20, 3.0, land=False),
        StatCard("Sol Ring", 1, "", 150, 1.0, land=False),
        StatCard("Swamp", 10, "", 10, 0.0, land=True),
    ]
    # (3 x 3 + 1) / 4 copies.
    assert deck_stats(cards).average_mana_value == 2.5
