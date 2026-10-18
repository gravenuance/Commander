"""Which deck the sync fetches next (ADR 0034)."""

from commander.fetchpriority import FetchPriority


def test_the_preferred_deck_goes_first_among_those_waiting() -> None:
    priority = FetchPriority(selected=3)
    assert priority.pick([1, 2, 3, 4], lambda deck: deck) == 3


def test_without_a_preference_or_with_one_already_done_the_first_waiting_goes() -> None:
    assert FetchPriority().pick([1, 2], lambda deck: deck) == 1
    assert FetchPriority(selected=9).pick([1, 2], lambda deck: deck) == 1


def test_a_newer_preference_replaces_the_older() -> None:
    priority = FetchPriority(selected=3)
    priority.prefer(2)
    assert priority.pick([1, 2, 3], lambda deck: deck) == 2
