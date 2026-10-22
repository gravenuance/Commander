import copy
import functools
import gzip
import json
import shutil
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.backend
import keyring.errors
import pytest

from commander import fsutil, net
from commander.carddb import CARD_DB_FILE, CardDataMeta, write_card_db
from commander.net import HTTP_OK
from commander.scryfall import read_cards, read_tags, role_gates, role_members

FIXTURES = Path(__file__).parent / "fixtures"
DECK_ID = 10000001

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


def put_on_sideboard(raw: Json, entry_id: int, name: str) -> None:
    """Add a card to the deck's Sideboard, un-ticking its category (the fixture counts it in the deck)."""
    for category in raw["categories"]:
        if category["name"] == "Sideboard":
            category["includedInDeck"] = False
    raw["cards"].append(card_entry(entry_id, name, ["Sideboard"]))


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


def filler_card(index: int, name: str | None = None, cmc: float = 1.0) -> str:
    return json.dumps(
        {
            "object": "card",
            "id": f"ffffffff-0000-4000-8000-{index:012d}",
            "oracle_id": f"eeeeeeee-0000-4000-8000-{index:012d}",
            "name": name or f"Filler Card {index}",
            "lang": "en",
            "layout": "normal",
            "set": "tst",
            "collector_number": str(index),
            "digital": False,
            "cmc": cmc,
            "mana_cost": f"{{{cmc:g}}}",
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
    """Real bulk-file lines for the fixture deck and the cards suggested to it, plus filler."""
    return fixture_lines("scryfall_cards.jsonl") + [filler_card(i) for i in range(FILLER_CARDS)]


MAGDA_ORACLE = "3d268e48-3004-4384-bf52-e63243fb5e02"
ARCANE_SIGNET = "0bc7f093-bef0-4f1a-852c-4b75ebf54838"
MIND_STONE = "c97361b5-af16-4a7b-af85-a429dbaf4ad2"
JESKAS_WILL = "0fd114c4-092b-4e28-b0dc-ef529f3bc73e"
ANCIENT_TOMB = "23467047-6dba-4498-b783-1ebc4f74b8c2"
MANA_CRYPT = "2c63e4e1-89d2-4bc6-a232-94e75c4b1c8a"
LIGHTNING_BOLT = "4457ed35-7c10-48c8-9776-456485fdf070"
CHAOS_WARP = "07a0cba9-8768-4fd9-a3d5-b0f83b4bf8e8"
RHYSTIC_STUDY = "53236dd7-845a-444c-96d5-f41ed7325d8f"
UNRANKED_FILLER = "eeeeeeee-0000-4000-8000-000000000000"
# Made up for the tests: the trimmed real tag lines only tag the fixture deck's own cards.
EXTRA_TAGGINGS: dict[str, tuple[str, ...]] = {
    "2f3e4ad7-5e60-41b4-bdbc-653f16869cf6": (  # ramp
        ARCANE_SIGNET,
        MIND_STONE,
        JESKAS_WILL,
        ANCIENT_TOMB,
        MANA_CRYPT,
        UNRANKED_FILLER,
    ),
    "444f824c-f910-4530-9dbe-ede7a84cd7f9": (LIGHTNING_BOLT, CHAOS_WARP),  # removal
    "b6448c45-ce65-4848-aa98-2151e4e07437": (RHYSTIC_STUDY,),  # draw
}


def tag_lines() -> list[str]:
    lines: list[str] = []
    for line in fixture_lines("scryfall_tags.jsonl"):
        tag = json.loads(line)
        extra = EXTRA_TAGGINGS.get(tag["id"], ())
        tag["taggings"] = [*tag["taggings"], *({"oracle_id": o, "weight": "median"} for o in extra)]
        lines.append(json.dumps(tag) if extra else line)
    return lines + [filler_tag(i) for i in range(FILLER_TAGS)]


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


def build_card_db(
    folder: Path, extra_tags: list[str] | None = None, extra_cards: list[str] | None = None
) -> Path:
    cards = card_lines() + (extra_cards or [])
    cards_gz, tags_gz = write_bulk(folder / "bulk", cards=cards, tags=tag_lines() + (extra_tags or []))
    path = folder / CARD_DB_FILE
    meta = CardDataMeta(CARDS_UPDATED_AT, TAGS_UPDATED_AT, "2026-10-15T12:00:00+00:00")
    tags = read_tags(tags_gz)
    write_card_db(path, read_cards(cards_gz), role_members(tags), meta, tags=tags, gates=role_gates(tags))
    return path


@pytest.fixture(scope="session")
def card_db_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A card database built from the fixtures; read-only, so one per test session is enough."""
    return build_card_db(tmp_path_factory.mktemp("carddb"))


@pytest.fixture
def own_cache(tmp_path: Path, card_db_file: Path) -> Path:
    """A cache folder of this test's own, so cached bracket estimates never leak between tests."""
    folder = tmp_path / "cache"
    folder.mkdir()
    shutil.copy(card_db_file, folder / CARD_DB_FILE)
    return folder


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


SPELLBOOK_API = "https://backend.commanderspellbook.com"


class FakeSpellbook:
    """Answers estimate-bracket and find-my-combos with trimmed real answers; records each request body."""

    def __init__(self) -> None:
        self.answer: Json = load_fixture("spellbook_estimate.json")
        self.combos: Json = load_fixture("spellbook_combos.json")
        self.status = HTTP_OK
        self.combos_status = HTTP_OK
        self.bodies: list[Json] = []
        self.combo_bodies: list[Json] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(405)
        url = str(request.url)
        if url == f"{SPELLBOOK_API}/estimate-bracket":
            self.bodies.append(json.loads(request.content))
            return self._reply(self.status, self.answer)
        if url == f"{SPELLBOOK_API}/find-my-combos":
            self.combo_bodies.append(json.loads(request.content))
            return self._reply(self.combos_status, self.combos)
        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    @staticmethod
    def _reply(status: int, answer: Json) -> httpx.Response:
        return httpx.Response(200, json=answer) if status == HTTP_OK else httpx.Response(status)


TOPDECK_API = "https://topdeck.gg/api/v2/tournaments"
# The key the memory keyring holds for every test, and the one the fake TopDeck accepts.
TOPDECK_KEY = "test-topdeck-key"
TOPDECK_SERVICE = "Commander (cmdr) TopDeck"


def topdeck_event(
    tid: str, start: str, size: int, top_cut: int, decks: Sequence[tuple[int, Sequence[str], Sequence[str]]]
) -> Json:
    """A made-up event in the shape of TopDeck's answer, standings in placement order.

    `decks` are (standing, commanders, cards) triples; every other seat is a player with no decklist. The
    players' names and ids are made up too, so the tests can check that none of them is kept.
    """
    by_place = {standing: (commanders, cards) for standing, commanders, cards in decks}
    standings: list[Json] = []
    for place in range(1, size + 1):
        row: Json = {
            "name": f"Player {place} of {tid}",
            "id": f"player-{tid}-{place}",
            "decklist": None,
            "deckObj": None,
            "wins": 0,
            "draws": 0,
            "losses": 0,
        }
        if place in by_place:
            commanders, cards = by_place[place]
            row["decklist"] = "~~Commanders~~\n" + "\n".join(f"1 {name}" for name in commanders)
            row["leader"] = " / ".join(commanders)
            row["deckObj"] = {
                "Commanders": {name: {"id": f"id-{name}", "count": 1} for name in commanders},
                "Mainboard": {name: {"id": f"id-{name}", "count": 1} for name in cards},
                "metadata": {"game": "Magic: The Gathering", "format": "EDH"},
            }
        standings.append(row)
    stamp = int(datetime.fromisoformat(start).timestamp())
    return {
        "TID": tid,
        "tournamentName": f"Event {tid}",
        "swissNum": 5,
        "startDate": stamp,
        "game": "Magic: The Gathering",
        "format": "EDH",
        "topCut": top_cut,
        "standings": standings,
    }


def topdeck_fixture_events() -> list[Json]:
    """The made-up events of `topdeck_events.json`: Ellivere decks in four events, Kinnan decks in two."""
    spec: list[Json] = json.loads((FIXTURES / "topdeck_events.json").read_text(encoding="utf-8"))
    return [
        topdeck_event(
            event["tid"],
            event["start"],
            event["size"],
            event["topCut"],
            [(deck["standing"], deck["commanders"], deck["cards"]) for deck in event["decks"]],
        )
        for event in spec
    ]


class FakeTopdeck:
    """Answers TopDeck's tournament search from `events`, as the real API does: by start date and size.

    Every request body is recorded. A request without the key gets 401, and one without `game` and `format`
    400 (as TopDeck answers on 2026-10-21); `status` makes every answer that status instead.
    """

    def __init__(self) -> None:
        self.events: list[Json] = topdeck_fixture_events()
        self.status = HTTP_OK
        self.key = TOPDECK_KEY
        self.requests: list[Json] = []
        # Added to every answer whatever the window, as a misbehaving API might: out of range or broken.
        self.extra: list[Json] = []
        # Answers given to the next requests, in turn, before the usual ones.
        self.queued: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or str(request.url) != TOPDECK_API:
            return httpx.Response(404)
        body: Json = json.loads(request.content)
        self.requests.append(body)
        if self.queued:
            return self.queued.pop(0)
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        if request.headers.get("Authorization") != self.key:
            return httpx.Response(401, json={"error": "invalid key"})
        if not body.get("game") or not body.get("format"):
            return httpx.Response(400, json={"error": 'Both "game" and "format" fields are required.'})
        found = [
            event
            for event in self.events
            if body.get("start", 0) <= event["startDate"] <= body.get("end", 2**62)
            and len(event["standings"]) >= body.get("participantMin", 0)
        ]
        return httpx.Response(200, json=[*found, *self.extra])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class MemoryKeyring(keyring.backend.KeyringBackend):
    """Stands in for Windows Credential Manager, so no test can read or write the real one."""

    priority = 1  # pyright: ignore[reportAssignmentType] — keyring declares it as a classproperty

    def __init__(self) -> None:
        super().__init__()
        self.secrets: dict[tuple[str, str], str] = {}

    def held(self, service: str) -> dict[tuple[str, str], str]:
        """The secrets saved under one service."""
        return {key: secret for key, secret in self.secrets.items() if key[0] == service}

    def get_password(self, service: str, username: str) -> str | None:
        return self.secrets.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.secrets[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.secrets.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    # A key is saved, as the user's is; a test of the missing key clears it.
    backend.secrets[(TOPDECK_SERVICE, "api-key")] = TOPDECK_KEY
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_retry_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The backoff waits of every fetcher the code builds itself, and of file replaces, recorded instead of
    slept."""
    waits: list[float] = []
    monkeypatch.setattr(net, "retry_sleep", waits.append)
    monkeypatch.setattr(fsutil, "retry_sleep", waits.append)
    return waits


class MemoryKeyStore:
    """A TopDeck key held in memory, for tests that build the client themselves."""

    def __init__(self, key: str | None = TOPDECK_KEY) -> None:
        self.key = key

    def get(self) -> str | None:
        return self.key

    def put(self, key: str) -> None:
        self.key = key

    def delete(self) -> bool:
        had, self.key = self.key is not None, None
        return had


class MemoryTokenStore:
    def __init__(self, tokens: dict[str, str] | None = None) -> None:
        self.tokens = dict(tokens or {})

    def get(self, user: str) -> str | None:
        return self.tokens.get(user.casefold())

    def put(self, user: str, token: str) -> None:
        self.tokens[user.casefold()] = token

    def delete(self, user: str) -> bool:
        return self.tokens.pop(user.casefold(), None) is not None


PASSWORD = "correct horse battery staple"
REFRESH_TOKEN = "refresh-token-1"
ACCESS_TOKEN = "access-token-1"
# Archidekt's ids for the fixture printings: a fixed offset on a number taken from the Scryfall id.
ARCHIDEKT_CARD_ID_BASE = 500_000


def archidekt_card_id(scryfall_id: str) -> int:
    return ARCHIDEKT_CARD_ID_BASE + int(scryfall_id[:6], 16)


class FakeArchidekt:
    """Serves deck pages and decks from dicts, like the real API, and records every request.

    It also signs in, looks up cards from the Scryfall fixture, saves snapshots and applies modifyCards
    edits to its decks, recording each write with its JSON body and Authorization header.
    Requests to Scryfall, Commander Spellbook and TopDeck go to its fakes of those, so one client covers any
    command. A deck search by commander answers with its page in `peer_pages`, else `peer_page`; one by
    deck tag with its page in `tag_pages`.
    """

    def __init__(self, user: str = "TESTER") -> None:
        self.user = user
        self.decks: dict[int, Json] = {}
        self.failing: dict[int, int] = {}
        # Set to make every read of a deck fail once a modifyCards has been applied to it.
        self.fail_reads_after_edit = False
        self.requests: list[str] = []
        self.writes: list[tuple[str, str, Json, str | None]] = []
        self.write_status: dict[str, int] = {}
        self.refresh_tokens = {REFRESH_TOKEN}
        # Private decks are listed and read only with the header (403 without); unlisted ones are left out of
        # an anonymous listing but can be read by id.
        self.private: set[int] = set()
        self.unlisted: set[int] = set()
        # The Authorization header of each deck listing request, None for an anonymous one.
        self.listing_auth: list[str | None] = []
        # What a deck listing that carries the expected header answers with; anything but 200 stands for a
        # refused login.
        self.signed_in_listing_status = HTTP_OK
        # Decks per listing page, 0 for one page with them all. With `signed_in_pages` set, only that many
        # signed-in pages are served before the token counts as expired (401).
        self.listing_page_size = 0
        self.signed_in_pages: int | None = None
        self.next_entry = 9_000_000_000
        self.scryfall = FakeScryfall()
        self.spellbook = FakeSpellbook()
        self.topdeck = FakeTopdeck()
        self.peer_page: Json = deck_page([])
        # Search pages for particular commanders; any other commander gets peer_page.
        self.peer_pages: dict[str, Json] = {}
        # Search pages for particular deck tags, by lower-cased tag (Archidekt ignores case); any other tag
        # finds no decks. `tag_searches` holds the tag of each such search, as sent.
        self.tag_pages: dict[str, Json] = {}
        self.tag_searches: list[str] = []
        # The account's card labels, as /api/decks/colorTags/ lists them; none unless a test sets some.
        self.labels: list[Json] = []
        # The status that read answers with; anything but 200 stands for a failed read.
        self.labels_status = 200
        # How many of the first reads answer 500 before the rest are served as labels_status says.
        self.labels_failures = 0

    def put(self, deck: Json) -> None:
        self.decks[deck["id"]] = copy.deepcopy(deck)

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.endswith(("scryfall.com", "scryfall.io")):
            return self.scryfall.handler(request)
        if host.endswith("commanderspellbook.com"):
            return self.spellbook.handler(request)
        if host.endswith("topdeck.gg"):
            return self.topdeck.handler(request)
        if request.url.path == "/api/decks/colorTags/":
            self.requests.append(request.url.path)
            return self.account_labels(request)
        return self.archidekt(request)

    def account_labels(self, request: httpx.Request) -> httpx.Response:
        if self.labels_failures > 0:
            self.labels_failures -= 1
            return httpx.Response(500)
        if self.labels_status != HTTP_OK:
            return httpx.Response(self.labels_status)
        return httpx.Response(200, json=self.labels) if self._signed_in(request) else httpx.Response(401)

    def archidekt(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        if request.method != "GET":
            return self._write(request)
        path = request.url.path
        if path == "/api/cards/v2/":
            return httpx.Response(200, json=self._card_search(request.url.params))
        if path == "/api/decks/v3/":
            return self._deck_search(request)
        return self._get_deck(request)

    def _deck_search(self, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "deckTagName" in params:
            self.tag_searches.append(params["deckTagName"])
            return httpx.Response(
                200, json=self.tag_pages.get(params["deckTagName"].casefold(), deck_page([]))
            )
        if "commanderName" in params:
            return httpx.Response(200, json=self.peer_pages.get(params["commanderName"], self.peer_page))
        if params.get("ownerUsername") != self.user:
            return httpx.Response(200, json=deck_page([]))
        self.listing_auth.append(request.headers.get("Authorization"))
        signed_in = self._signed_in(request)
        if signed_in and self.signed_in_listing_status != HTTP_OK:
            return httpx.Response(self.signed_in_listing_status)
        page = int(params.get("page", "1"))
        if signed_in and self.signed_in_pages is not None and page > self.signed_in_pages:
            return httpx.Response(401)
        return httpx.Response(200, json=self._listing_page(page, signed_in=signed_in))

    def _listing_page(self, page: int, *, signed_in: bool) -> Json:
        rows = [
            {
                **summary_row(d["id"], d["name"], d["updatedAt"]),
                "tags": d.get("deckTags", []),
                **self._visibility(d["id"]),
            }
            for d in self.decks.values()
            if d.get("owner", {}).get("username", self.user) == self.user
            and (signed_in or not self._hidden(d["id"]))
        ]
        if not self.listing_page_size:
            return deck_page(rows)
        start = (page - 1) * self.listing_page_size
        more = start + self.listing_page_size < len(rows)
        next_url = f"https://archidekt.com/api/decks/v3/?ownerUsername={self.user}&page={page + 1}"
        return deck_page(rows[start : start + self.listing_page_size], next_url if more else None)

    def _hidden(self, deck_id: int) -> bool:
        return deck_id in self.private or deck_id in self.unlisted

    def _visibility(self, deck_id: int) -> Json:
        return {"private": deck_id in self.private, "unlisted": deck_id in self.unlisted}

    def _get_deck(self, request: httpx.Request) -> httpx.Response:
        deck_id = int(request.url.path.removeprefix("/api/decks/").rstrip("/"))
        if deck_id in self.failing:
            return httpx.Response(self.failing[deck_id])
        if deck_id not in self.decks:
            return httpx.Response(404)
        if deck_id in self.private and not self._signed_in(request):
            return httpx.Response(403)
        return httpx.Response(200, json={**self.decks[deck_id], **self._visibility(deck_id)})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def card_writes(self) -> list[Json]:
        """The card entries of every modifyCards request, in order."""
        return [entry for _, path, body, _ in self.writes if "modifyCards" in path for entry in body["cards"]]

    @staticmethod
    def _signed_in(request: httpx.Request) -> bool:
        return request.headers.get("Authorization") == f"JWT {ACCESS_TOKEN}"

    def _card_search(self, params: httpx.QueryParams) -> Json:
        assert "exact" in params, "card lookups must ask for the exact name"
        results: list[Json] = []
        for line in fixture_lines("scryfall_cards.jsonl"):
            card = json.loads(line)
            if card["name"] == params.get("name") and card["set"] == params.get("edition"):
                results.append(
                    {
                        "id": archidekt_card_id(card["id"]),
                        "uid": card["id"],
                        "edition": {"editioncode": card["set"]},
                    }
                )
        return {"count": len(results), "next": None, "results": results}

    def _write(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body: Json = json.loads(request.content)
        self.writes.append((request.method, path, body, request.headers.get("Authorization")))
        for fragment, status in self.write_status.items():
            if fragment in path:
                return httpx.Response(status)
        if path.startswith("/api/rest-auth/"):
            return self._auth(path, body)
        if not self._signed_in(request):
            return httpx.Response(401)
        return self._deck_write(path, body)

    def _auth(self, path: str, body: Json) -> httpx.Response:
        if path == "/api/rest-auth/login/":
            if body.get("password") != PASSWORD or (body.get("username") or body.get("email")) is None:
                return httpx.Response(400, json={"non_field_errors": ["Unable to log in."]})
            user = {"username": self.user, "id": 1}
            return httpx.Response(
                200, json={"token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN, "user": user}
            )
        if body.get("refresh") not in self.refresh_tokens:
            return httpx.Response(401)
        return httpx.Response(200, json={"access": ACCESS_TOKEN})

    def _deck_write(self, path: str, body: Json) -> httpx.Response:
        deck_id = int(path.split("/")[3])
        if deck_id not in self.decks:
            return httpx.Response(404)
        if path.endswith("/snapshots/"):
            return httpx.Response(201, json={"id": 1, "name": body["name"]})
        if path.endswith("/modifyCards/v2/"):
            answer = self._modify(self.decks[deck_id], body["cards"])
            if self.fail_reads_after_edit:
                self.failing[deck_id] = 404
            return httpx.Response(200, json=answer)
        return httpx.Response(404)

    def _modify(self, deck: Json, edits: list[Json]) -> Json:
        added: list[Json] = []
        for edit in edits:
            quantity = edit["modifications"]["quantity"]
            if edit["action"] == "add":
                self.next_entry += 1
                cards = map(json.loads, fixture_lines("scryfall_cards.jsonl"))
                card = next(c for c in cards if archidekt_card_id(c["id"]) == edit["cardid"])
                entry = card_entry(self.next_entry, card["name"], edit["categories"], quantity)
                entry["label"] = edit["modifications"]["label"]
                entry["card"].update(
                    id=edit["cardid"],
                    uid=card["id"],
                    collectorNumber=card["collector_number"],
                    edition={"editioncode": card["set"]},
                    oracleCard={"name": card["name"], "cmc": card["cmc"]},
                )
                deck["cards"].append(entry)
                added.append({"patchId": edit["patchId"], "deckRelationId": self.next_entry})
                continue
            entry = next(e for e in deck["cards"] if e["id"] == edit["deckRelationId"])
            if edit["action"] == "remove":
                deck["cards"].remove(entry)
            else:
                entry["quantity"] = quantity
                entry["categories"] = edit["categories"]
                entry["label"] = edit["modifications"]["label"]
        deck["updatedAt"] = "2026-10-15T12:00:00.000000Z"
        return {"add": added, "createdCategories": []}
