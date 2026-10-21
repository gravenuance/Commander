"""Decks read side by side, so two of them can want the same Archidekt search at once; it is made once."""

import threading
from pathlib import Path

import httpx
import pytest

from commander.metadata import AdjacentDecks, gather_adjacent
from test_metadata import ELLIVERE, MAGDA, FakeArchidektSearch, Rig, ellivere_deck

WAIT_SECONDS = 30


class HeldSearch(FakeArchidektSearch):
    """Holds the first search request until released; says when another search request arrives meanwhile."""

    def __init__(self) -> None:
        super().__init__()
        self.first_arrived = threading.Event()
        self.release = threading.Event()
        self.another_arrived = threading.Event()
        self._guard = threading.Lock()
        self._searches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/decks/v3/":
            with self._guard:
                self._searches += 1
                first = self._searches == 1
            if first:
                self.first_arrived.set()
                self.release.wait(WAIT_SECONDS)
            else:
                self.another_arrived.set()
        return super().handler(request)


@pytest.mark.parametrize(
    ("groups", "tags"),
    [(((ELLIVERE,),), ()), ((), ("tokens",))],
    ids=["a commander group", "a deck tag"],
)
def test_two_decks_asking_for_the_same_search_at_once_make_it_once_and_share_the_answer(
    tmp_path: Path, groups: tuple[tuple[str, ...], ...], tags: tuple[str, ...]
) -> None:
    rig = Rig(tmp_path)
    held = HeldSearch()
    rig.archidekt = held
    sources = rig.sources()
    answers: list[AdjacentDecks] = []

    def ask() -> None:
        answers.append(
            gather_adjacent(
                # Led by another commander, as a tag search leaves out the deck's own commander's decks.
                ellivere_deck(MAGDA),
                groups,
                sources,
                rig.cache,
                now=lambda: rig.now,
                refresh=False,
                tags=tags,
            )
        )

    first, second = threading.Thread(target=ask), threading.Thread(target=ask)
    first.start()
    assert held.first_arrived.wait(WAIT_SECONDS)
    second.start()
    # Not waited out in full when the second caller searches too: it arrives within moments.
    searched_twice = held.another_arrived.wait(1)
    held.release.set()
    first.join(WAIT_SECONDS)
    second.join(WAIT_SECONDS)

    assert not searched_twice
    assert len(held.searches) == 1
    [one, other] = answers
    assert one == other
    assert [*one.decks, *(d for t in one.tagged for d in t.decks)]
