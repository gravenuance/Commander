"""Other players' Archidekt decks, read once and kept for as long as they are unchanged.

A deck is cached under its id and the `updatedAt` the search listed it with, so the next run reads again
only the decks their owners have edited since. Entries live in the meta cache folder, which is not the repo.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from commander.archidekt import PeerDeck
from commander.deckcache import DeckCache, request_key
from commander.jsonshape import ShapeError, as_list, as_object, req_list, str_list
from commander.models import Deck, plan_category

CACHE_VERSION = 1
FOLDER = "peer-decks"


@dataclass(slots=True, frozen=True)
class DeckRead:
    """What the meta keeps of another player's deck: its commanders and its main deck's cards."""

    commanders: tuple[str, ...]
    cards: tuple[str, ...]
    # The cards its owner files under a category naming the plan, like "Combo Pieces" (ADR 0017).
    planned: tuple[str, ...]
    # Cards played more than once (basic lands mostly), with their count (ADR 0019).
    multiples: tuple[tuple[str, int], ...]


def read_of(deck: Deck) -> DeckRead:
    counts: dict[str, int] = {}
    for card in deck.main_cards:
        counts[card.name] = counts.get(card.name, 0) + card.quantity
    return DeckRead(
        commanders=deck.commanders,
        cards=tuple(sorted({card.name for card in deck.main_cards})),
        planned=tuple(
            sorted({card.name for card in deck.main_cards if any(map(plan_category, card.categories))})
        ),
        multiples=tuple(sorted((name, count) for name, count in counts.items() if count > 1)),
    )


class DeckReads:
    """The cache of `DeckRead`s in a folder; a missing, unreadable or outdated entry is a miss."""

    def __init__(self, folder: Path) -> None:
        self._cache = DeckCache(
            folder, version=CACHE_VERSION, what="Archidekt deck", encode=_encode, decode=_decode
        )

    def load(self, peer: PeerDeck) -> DeckRead | None:
        cached = self._cache.load(peer.id, _key(peer))
        return None if cached is None else cached.data

    def save(self, peer: PeerDeck, read: DeckRead, at: datetime) -> None:
        self._cache.save(peer.id, _key(peer), read, at)


def _key(peer: PeerDeck) -> str:
    return request_key({"updatedAt": peer.updated_at.isoformat()})


def _encode(read: DeckRead) -> object:
    return {
        "commanders": list(read.commanders),
        "cards": list(read.cards),
        "planned": list(read.planned),
        "multiples": [[name, count] for name, count in read.multiples],
    }


def _decode(raw: object) -> DeckRead:
    obj = as_object(raw, "answer")
    return DeckRead(
        commanders=str_list(obj, "commanders", "answer"),
        cards=str_list(obj, "cards", "answer"),
        planned=str_list(obj, "planned", "answer"),
        multiples=decode_multiples(req_list(obj, "multiples", "answer"), "answer.multiples"),
    )


def decode_multiples(raw: object, where: str) -> tuple[tuple[str, int], ...]:
    """[[name, count], ...] as a deck's cards played more than once are stored."""
    found: list[tuple[str, int]] = []
    for index, item in enumerate(as_list(raw, where)):
        pair = as_list(item, f"{where}[{index}]")
        if len(pair) != 2 or not isinstance(pair[0], str) or not isinstance(pair[1], int):  # noqa: PLR2004 — a pair
            raise ShapeError(f"{where}[{index}]: expected [name, count]")
        found.append((pair[0], pair[1]))
    return tuple(found)
