"""A Pacer must not strand its waiters when the ranking changes between two of their turns."""

import threading
from pathlib import Path

import httpx
import pytest

from commander.config import MetaSettings
from commander.fetchpriority import FetchPriority, fetching_deck
from commander.metadata import meta_sources
from commander.net import Pacer

HUNG_AFTER_SECONDS = 5.0
HUNG_AFTER_SECONDS_IN_MS = 5000


class _OneRacePacer(Pacer):
    """A pacer whose first slot opening is held, after it has woken the waiters, until the other waiter has
    ranked the queue again and gone back to waiting; then `during_race` runs, and the sleeper goes on."""

    def __init__(self, *, recheck: float) -> None:
        super().__init__(5.0, sleep=self._sleep_until_released, monotonic=lambda: 0.0, recheck=recheck)
        self.release_sleeper = threading.Event()
        self.sleeping = threading.Event()
        self.other_ranked = threading.Event()
        self.armed = False
        self.during_race = lambda: None

    def _sleep_until_released(self, _seconds: float) -> None:
        self.sleeping.set()
        assert self.release_sleeper.wait(HUNG_AFTER_SECONDS)

    def _slot_opened(self) -> None:
        # Armed before the wake-up, so the other waiter's first look at the ranking is not missed.
        first = not self.armed
        self.armed = True
        super()._slot_opened()
        if first:
            assert self.other_ranked.wait(HUNG_AFTER_SECONDS)
            self.during_race()


def _race(*, recheck: float, wired: bool) -> tuple[list[int], bool]:
    """Deck 2 is preferred and sleeps for its slot while deck 1 waits behind it. When the slot opens deck 1
    looks first, sees deck 2 ahead of it and waits; then the user opens deck 1, before deck 2 looks.
    Returns the decks served and whether the pacer still had callers waiting."""
    priority = FetchPriority(selected=2)
    order = [1, 2]
    pacer = _OneRacePacer(recheck=recheck)

    def rank(deck: int) -> int:
        if deck == 1 and pacer.armed:
            pacer.other_ranked.set()
        return priority.rank(deck, order)

    pacer.rank_by(rank)
    if wired:
        priority.watch(pacer.rank_changed)
    pacer.during_race = lambda: priority.prefer(1)
    served: list[int] = []

    def ask(deck: int) -> None:
        fetching_deck.set(deck)
        pacer.wait()
        served.append(deck)

    # The first call takes its slot at once; the next two have to wait for the interval.
    pacer.wait()
    sleeper = threading.Thread(target=ask, args=(2,), daemon=True)
    sleeper.start()
    assert pacer.sleeping.wait(HUNG_AFTER_SECONDS)
    other = threading.Thread(target=ask, args=(1,), daemon=True)
    other.start()
    for _ in range(HUNG_AFTER_SECONDS_IN_MS):
        if pacer.waiting == 2:
            break
        threading.Event().wait(0.001)
    pacer.release_sleeper.set()
    other.join(HUNG_AFTER_SECONDS)
    sleeper.join(HUNG_AFTER_SECONDS)
    return sorted(served), pacer.waiting > 0


def test_opening_a_deck_wakes_the_waiters_that_ranked_the_queue_before_it() -> None:
    # The recheck is far off, so only the wake-up on the rank change can end the race.
    assert _race(recheck=3600.0, wired=True) == ([1, 2], False)


def test_waiters_that_missed_a_rank_change_look_again_on_their_own() -> None:
    assert _race(recheck=0.01, wired=False) == ([1, 2], False)


def test_a_pacer_refuses_a_recheck_that_never_comes() -> None:
    with pytest.raises(ValueError, match="recheck must be positive"):
        Pacer(1.0, recheck=0.0)


def test_the_meta_sources_hand_their_archidekt_pacer_to_the_rank_watcher(tmp_path: Path) -> None:
    pacers: list[Pacer] = []
    heard: list[object] = []

    def pacer_factory(interval: float) -> Pacer:
        pacers.append(Pacer(interval))
        return pacers[-1]

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as http:
        meta_sources(
            http,
            pacer_factory,
            "TESTER",
            MetaSettings(),
            tmp_path,
            rank=lambda deck: deck,
            rank_changes=heard.append,
        )
    assert len(heard) == 1
    assert heard[0] == pacers[0].rank_changed
