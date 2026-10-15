import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DECK_ID = 10004639

type Json = dict[str, Any]
type Handler = Callable[[httpx.Request], httpx.Response]


def load_fixture(name: str) -> Json:
    data: Json = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


@pytest.fixture
def raw_deck() -> Json:
    """A trimmed real Archidekt deck: Magda commander, 9 cards in deck, 2 on the maybeboard."""
    return copy.deepcopy(load_fixture(f"deck_{DECK_ID}.json"))


def card_entry(
    entry_id: int, name: str, categories: list[str], quantity: int = 1, *, deleted: bool = False
) -> Json:
    return {
        "id": entry_id,
        "categories": categories,
        "quantity": quantity,
        "modifier": "Normal",
        "deletedAt": "2026-01-01T00:00:00Z" if deleted else None,
        "card": {
            "id": entry_id + 1_000_000,
            "uid": f"00000000-0000-0000-0000-{entry_id:012d}",
            "collectorNumber": "1",
            "edition": {"editioncode": "tst"},
            "oracleCard": {"name": name, "cmc": 1},
        },
    }


def deck_page(decks: list[Json], next_url: str | None = None) -> Json:
    return {"count": len(decks), "next": next_url, "results": decks}


def summary_row(deck_id: int, name: str, updated_at: str, deck_format: int = 3) -> Json:
    return {"id": deck_id, "name": name, "updatedAt": updated_at, "deckFormat": deck_format}


class FakeArchidekt:
    """Serves deck pages and decks from dicts, like the real API, and records every request."""

    def __init__(self, user: str = "MAKKIS") -> None:
        self.user = user
        self.decks: dict[int, Json] = {}
        self.failing: dict[int, int] = {}
        self.requests: list[str] = []

    def put(self, deck: Json) -> None:
        self.decks[deck["id"]] = copy.deepcopy(deck)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        path = request.url.path
        if path == "/api/decks/v3/":
            if request.url.params.get("ownerUsername") != self.user:
                return httpx.Response(200, json=deck_page([]))
            rows = [summary_row(d["id"], d["name"], d["updatedAt"]) for d in self.decks.values()]
            return httpx.Response(200, json=deck_page(rows))
        deck_id = int(path.removeprefix("/api/decks/").rstrip("/"))
        if deck_id in self.failing:
            return httpx.Response(self.failing[deck_id])
        if deck_id not in self.decks:
            return httpx.Response(404)
        return httpx.Response(200, json=self.decks[deck_id])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))
