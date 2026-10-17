import copy
import functools
import gzip
import json
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import keyring
import keyring.backend
import keyring.errors
import pytest

from commander.carddb import CARD_DB_FILE, CardDataMeta, write_card_db
from commander.net import HTTP_OK
from commander.scryfall import read_cards, read_tags, role_gates, role_members

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


EDHTOP16_API = "https://edhtop16.com/api/graphql"


class FakeEdhtop16:
    """Answers EDHTop16 GraphQL queries with made-up answers, one queued page per entries request.

    `pages` maps a commander name to the answers its entries requests get, in order; an unknown commander gets
    the real "no rows" answer. Every request's variables are recorded.
    """

    def __init__(self) -> None:
        self.pages: dict[str, list[Json]] = {
            "Ellivere of the Wild Court": [load_fixture("edhtop16_entries_ellivere.json")],
            "Kinnan, Bonder Prodigy": [load_fixture("edhtop16_entries_kinnan_top.json")],
        }
        self.commanders: Json = load_fixture("edhtop16_commanders.json")
        self.status = HTTP_OK
        self.requests: list[Json] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or str(request.url) != EDHTOP16_API:
            return httpx.Response(404)
        body: Json = json.loads(request.content)
        variables: Json = body["variables"]
        self.requests.append(variables)
        if self.status != HTTP_OK:
            return httpx.Response(self.status)
        if "query Commanders" in body["query"]:
            return httpx.Response(200, json=self.commanders)
        queued = self.pages.get(variables["name"])
        if not queued:
            return httpx.Response(200, json=load_fixture("edhtop16_missing.json"))
        return httpx.Response(200, json=queued.pop(0) if len(queued) > 1 else queued[0])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


class MemoryKeyring(keyring.backend.KeyringBackend):
    """Stands in for Windows Credential Manager, so no test can read or write the real one."""

    priority = 1  # pyright: ignore[reportAssignmentType] — keyring declares it as a classproperty

    def __init__(self) -> None:
        super().__init__()
        self.secrets: dict[tuple[str, str], str] = {}

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
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


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
    Requests to Scryfall, Commander Spellbook and EDHTop16 go to its fakes of those, so one client covers any
    command. A deck search by commander answers with its page in `peer_pages`, else `peer_page`.
    """

    def __init__(self, user: str = "MAKKIS") -> None:
        self.user = user
        self.decks: dict[int, Json] = {}
        self.failing: dict[int, int] = {}
        # Set to make every read of a deck fail once a modifyCards has been applied to it.
        self.fail_reads_after_edit = False
        self.requests: list[str] = []
        self.writes: list[tuple[str, str, Json, str | None]] = []
        self.write_status: dict[str, int] = {}
        self.refresh_tokens = {REFRESH_TOKEN}
        self.private: set[int] = set()
        self.next_entry = 9_000_000_000
        self.scryfall = FakeScryfall()
        self.spellbook = FakeSpellbook()
        self.edhtop16 = FakeEdhtop16()
        self.peer_page: Json = deck_page([])
        # Search pages for particular commanders; any other commander gets peer_page.
        self.peer_pages: dict[str, Json] = {}
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
        if host.endswith("edhtop16.com"):
            return self.edhtop16.handler(request)
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
        if path == "/api/decks/v3/" and "commanderName" in request.url.params:
            commander = request.url.params["commanderName"]
            return httpx.Response(200, json=self.peer_pages.get(commander, self.peer_page))
        if path == "/api/decks/v3/":
            if request.url.params.get("ownerUsername") != self.user:
                return httpx.Response(200, json=deck_page([]))
            rows = [
                {**summary_row(d["id"], d["name"], d["updatedAt"]), "tags": d.get("deckTags", [])}
                for d in self.decks.values()
                if d.get("owner", {}).get("username", self.user) == self.user
            ]
            return httpx.Response(200, json=deck_page(rows))
        return self._get_deck(request)

    def _get_deck(self, request: httpx.Request) -> httpx.Response:
        deck_id = int(request.url.path.removeprefix("/api/decks/").rstrip("/"))
        if deck_id in self.failing:
            return httpx.Response(self.failing[deck_id])
        if deck_id not in self.decks or (deck_id in self.private and not self._signed_in(request)):
            return httpx.Response(404)
        return httpx.Response(200, json=self.decks[deck_id])

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
