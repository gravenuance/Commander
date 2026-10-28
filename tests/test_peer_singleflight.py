"""Decks read side by side, so two of them can want the same Archidekt search at once; it is made once."""

import json
import threading
from pathlib import Path

import httpx
import pytest

from commander.metadata import AdjacentDecks, MetaData, gather, gather_adjacent
from conftest import FakeSpellbook, Json
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


def _is_core_request(body: Json) -> bool:
    """A meta core, shared between decks; the deck's own list holds Containment Priest."""
    return {"card": "Containment Priest", "quantity": 1} not in body["main"]


class HeldCombos(FakeSpellbook):
    """Holds the first meta-core combos request until released; says when another one arrives meanwhile."""

    def __init__(self) -> None:
        super().__init__()
        self.first_arrived = threading.Event()
        self.release = threading.Event()
        self.another_arrived = threading.Event()
        self._guard = threading.Lock()
        self._cores = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("find-my-combos") and _is_core_request(json.loads(request.content)):
            with self._guard:
                self._cores += 1
                first = self._cores == 1
            if first:
                self.first_arrived.set()
                self.release.wait(WAIT_SECONDS)
            else:
                self.another_arrived.set()
        return super().handler(request)


def test_two_decks_wanting_the_same_meta_combos_at_once_ask_spellbook_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    held = HeldCombos()
    held.combos = rig.spellbook.combos
    rig.spellbook = held
    sources = rig.sources()
    answers: list[MetaData] = []

    def ask() -> None:
        answers.append(gather(ellivere_deck(), sources, rig.cache, now=lambda: rig.now, refresh=False))

    first, second = threading.Thread(target=ask), threading.Thread(target=ask)
    first.start()
    assert held.first_arrived.wait(WAIT_SECONDS)
    second.start()
    # Not waited out in full when the second caller asks too: it arrives within moments.
    asked_twice = held.another_arrived.wait(1)
    held.release.set()
    first.join(WAIT_SECONDS)
    second.join(WAIT_SECONDS)

    assert not asked_twice
    # One core for each of the field's two commanders, however many decks asked.
    assert len([b for b in held.combo_bodies if _is_core_request(b)]) == 2
    [one, other] = answers
    assert one.combos.data == other.combos.data
