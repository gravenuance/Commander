import copy
import functools
import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from commander.carddb import CARD_DB_FILE, CardDataMeta, write_card_db
from commander.net import HTTP_OK
from commander.scryfall import read_cards, read_tags, role_members

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


CARDS_UPDATED_AT = "2026-10-15T09:05:41.217+00:00"
TAGS_UPDATED_AT = "2026-10-15T09:00:38.101+00:00"
CARDS_URL = "https://data.scryfall.io/default-cards/default-cards-20261015090541.jsonl.gz"
TAGS_URL = "https://data.scryfall.io/oracle-tags/oracle-tags-20261015090038.jsonl.gz"
# The real files hold ~100k cards and ~4.5k tags; the reader refuses files far below that.
FILLER_CARDS = 10_000
FILLER_TAGS = 100


def fixture_lines(name: str) -> list[str]:
    return [line for line in (FIXTURES / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def filler_card(index: int) -> str:
    return json.dumps(
        {
            "object": "card",
            "id": f"ffffffff-0000-4000-8000-{index:012d}",
            "oracle_id": f"eeeeeeee-0000-4000-8000-{index:012d}",
            "name": f"Filler Card {index}",
            "lang": "en",
            "layout": "normal",
            "set": "tst",
            "collector_number": str(index),
            "digital": False,
            "cmc": 1.0,
            "mana_cost": "{1}",
            "type_line": "Artifact",
            "color_identity": [],
            "game_changer": False,
            "legalities": {"commander": "legal"},
            "prices": {"eur": None, "eur_foil": None},
        }
    )


def filler_tag(index: int) -> str:
    return json.dumps(
        {
            "object": "tag",
            "id": f"dddddddd-0000-4000-8000-{index:012d}",
            "slug": f"filler-{index}",
            "type": "oracle",
            "child_ids": [],
            "taggings": [],
        }
    )


def gzip_lines(lines: list[str]) -> bytes:
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), compresslevel=1)


def card_lines() -> list[str]:
    """The fixture deck's cards from the real bulk file, plus filler up to a believable size."""
    return fixture_lines("scryfall_cards.jsonl") + [filler_card(i) for i in range(FILLER_CARDS)]


def tag_lines() -> list[str]:
    return fixture_lines("scryfall_tags.jsonl") + [filler_tag(i) for i in range(FILLER_TAGS)]


@functools.cache
def default_cards_gz() -> bytes:
    return gzip_lines(card_lines())


@functools.cache
def oracle_tags_gz() -> bytes:
    return gzip_lines(tag_lines())


def write_bulk(
    folder: Path, cards: list[str] | None = None, tags: list[str] | None = None
) -> tuple[Path, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    cards_gz, tags_gz = folder / "cards.jsonl.gz", folder / "tags.jsonl.gz"
    cards_gz.write_bytes(gzip_lines(card_lines() if cards is None else cards))
    tags_gz.write_bytes(gzip_lines(tag_lines() if tags is None else tags))
    return cards_gz, tags_gz


def build_card_db(folder: Path) -> Path:
    cards_gz, tags_gz = write_bulk(folder / "bulk")
    path = folder / CARD_DB_FILE
    meta = CardDataMeta(CARDS_UPDATED_AT, TAGS_UPDATED_AT, "2026-10-15T12:00:00+00:00")
    write_card_db(path, read_cards(cards_gz), role_members(read_tags(tags_gz)), meta)
    return path


@pytest.fixture(scope="session")
def card_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A card database built from the fixtures; read-only, so one per test session is enough."""
    return build_card_db(tmp_path_factory.mktemp("carddb"))


class FakeScryfall:
    """Serves the bulk-data list and the two bulk files, and records every request."""

    def __init__(self) -> None:
        self.cards_updated_at = CARDS_UPDATED_AT
        self.tags_updated_at = TAGS_UPDATED_AT
        self.failing: dict[str, int] = {}
        self.requests: list[str] = []
        self.cards_gz = default_cards_gz()
        self.tags_gz = oracle_tags_gz()

    def listing(self) -> Json:
        return {
            "object": "list",
            "has_more": False,
            "data": [
                {
                    "object": "bulk_data",
                    "type": "oracle_cards",
                    "updated_at": self.cards_updated_at,
                    "jsonl_download_uri": "https://data.scryfall.io/oracle-cards/oracle-cards-1.jsonl.gz",
                    "compressed_size": 1,
                },
                {
                    "object": "bulk_data",
                    "type": "default_cards",
                    "updated_at": self.cards_updated_at,
                    "jsonl_download_uri": CARDS_URL,
                    "compressed_size": len(self.cards_gz),
                },
                {
                    "object": "bulk_data",
                    "type": "oracle_tags",
                    "updated_at": self.tags_updated_at,
                    "jsonl_download_uri": TAGS_URL,
                    "compressed_size": len(self.tags_gz),
                },
                {"object": "bulk_data", "type": "rulings", "updated_at": self.cards_updated_at},
            ],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.failing:
            return httpx.Response(self.failing[url])
        if url == "https://api.scryfall.com/bulk-data":
            return httpx.Response(200, json=self.listing())
        if url == CARDS_URL:
            return httpx.Response(200, content=self.cards_gz)
        if url == TAGS_URL:
            return httpx.Response(200, content=self.tags_gz)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


SPELLBOOK_URL = "https://backend.commanderspellbook.com/estimate-bracket"


class FakeSpellbook:
    """Answers estimate-bracket with a trimmed real answer, and records each request body."""

    def __init__(self) -> None:
        self.answer: Json = load_fixture("spellbook_estimate.json")
        self.status = 200
        self.bodies: list[Json] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or str(request.url) != SPELLBOOK_URL:
            return httpx.Response(404)
        self.bodies.append(json.loads(request.content))
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        return httpx.Response(200, json=self.answer)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class FakeArchidekt:
    """Serves deck pages and decks from dicts, like the real API, and records every request.

    Requests to Scryfall and Commander Spellbook go to its fakes of those, so one client covers any command.
    """

    def __init__(self, user: str = "MAKKIS") -> None:
        self.user = user
        self.decks: dict[int, Json] = {}
        self.failing: dict[int, int] = {}
        self.requests: list[str] = []
        self.scryfall = FakeScryfall()
        self.spellbook = FakeSpellbook()

    def put(self, deck: Json) -> None:
        self.decks[deck["id"]] = copy.deepcopy(deck)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.endswith(("scryfall.com", "scryfall.io")):
            return self.scryfall.handler(request)
        if host.endswith("commanderspellbook.com"):
            return self.spellbook.handler(request)
        return self.archidekt(request)

    def archidekt(self, request: httpx.Request) -> httpx.Response:
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
