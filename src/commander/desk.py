"""What the desktop app can ask of cmdr: decks, boards, card search, decisions, previews and saves.

Every public method answers with a JSON-ready dict, {"error": "..."} on an expected failure, so nothing
raises into the web page. Each call opens its own card database connection: the window calls in from
several threads, and a SQLite connection belongs to one.
"""

import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path

import httpx

from commander.apply import NotYourDeckError, Resolution, SaveError
from commander.archidekt import SEARCH_INTERVAL_SECONDS, ArchidektError, ArchidektSearch
from commander.archidekt_edit import ArchidektAuth, ArchidektEditor, AuthError
from commander.board import (
    Actor,
    BoardCard,
    BoardError,
    BoardService,
    Status,
    activity_json,
    board_stamp,
    card_json,
    read_activity,
    read_board,
)
from commander.boardsave import preview_board, save_board
from commander.carddb import CardDataError, CardDb, CardInfo
from commander.cli_meta import report_json
from commander.cli_output import change_mark, change_text
from commander.config import ConfigError, MetaSettings
from commander.credentials import CredentialError, TokenStore
from commander.deckprofile import read_profile
from commander.edhtop16 import REQUEST_INTERVAL_SECONDS, SLOW_QUERY_SECONDS, Edhtop16Client, Edhtop16Error
from commander.meta import analyse
from commander.metadata import MetaSources, gather
from commander.net import Fetcher, NetworkError, Pacer
from commander.spellbook import META_REQUEST_INTERVAL_SECONDS, SpellbookClient
from commander.store import StoredDeck, Workspace, WorkspaceError
from commander.suggest import deck_identity

log = logging.getLogger(__name__)

ACTIVITY_SHOWN = 60
IMAGE_URL = "https://cards.scryfall.io/normal/front/{a}/{b}/{id}.jpg"
_EXPECTED = (
    BoardError,
    CardDataError,
    ConfigError,
    NetworkError,
    ArchidektError,
    Edhtop16Error,
    AuthError,
    CredentialError,
    SaveError,
    NotYourDeckError,
    WorkspaceError,
    OSError,
)
_DECISIONS = {"add": Status.ADD, "reject": Status.REJECTED, "reopen": Status.OPEN}

type Json = dict[str, object]


def _guarded[**P](method: Callable[P, Json]) -> Callable[P, Json]:
    """Turn expected failures into {"error": message}; log the rest and say where the details are."""

    @wraps(method)
    def call(*args: P.args, **kwargs: P.kwargs) -> Json:
        log.debug("bridge call %s %s", method.__name__, args[1:])
        try:
            return method(*args, **kwargs)
        except _EXPECTED as error:
            log.warning("%s failed: %s", method.__name__, error)
            return {"error": str(error)}
        except Exception:
            log.exception("%s failed unexpectedly", method.__name__)
            return {"error": "Something went wrong; the details are in the log."}

    return call


@dataclass(slots=True, frozen=True)
class DeskSettings:
    workspace: Path
    card_db: Path
    # None until `cmdr init` sets a username; previews, saves and meta refreshes need it.
    user: str | None
    meta: MetaSettings = field(default_factory=MetaSettings)

    @property
    def cache(self) -> Path:
        # The card database lives in the cache folder, beside the meta answers.
        return self.card_db.parent


class Desk:
    def __init__(
        self,
        settings: DeskSettings,
        *,
        client_factory: Callable[[], httpx.Client],
        token_store: TokenStore,
        now: Callable[[], datetime],
        pacer_factory: Callable[[float], Pacer] = Pacer,
    ) -> None:
        self._settings = settings
        self._pacer_factory = pacer_factory
        self._workspace = Workspace(settings.workspace)
        self._client_factory = client_factory
        self._token_store = token_store
        self._now = now

    @_guarded
    def decks(self) -> Json:
        rows: list[Json] = []
        for stored in self._workspace.load_all():
            board = read_board(stored.folder)
            rows.append(
                {
                    "id": stored.deck.id,
                    "short": stored.deck.short_name,
                    "name": stored.deck.name,
                    "queued": board.pending(stored.deck),
                    "open": sum(1 for c in board.cards if c.status is Status.OPEN),
                }
            )
        rows.sort(key=lambda row: str(row["short"]).casefold())
        return {"decks": rows}

    @_guarded
    def stamps(self) -> Json:
        # Polled every few seconds, so it only looks at file times, never reads a deck.
        return {"stamps": {str(i): board_stamp(folder) for i, folder in self._workspace.deck_folders()}}

    @_guarded
    def deck(self, deck_id: int) -> Json:
        stored = self._stored(deck_id)
        board = read_board(stored.folder)
        profile = read_profile(stored.folder)
        drop = set(board.to_drop(stored.deck))
        with self._db() as db:
            identity = deck_identity(db.resolve(stored.deck.main_cards))
            cards = [{**self._card_view(db, card), "dropping": card.name in drop} for card in board.cards]
        activity = read_activity(stored.folder)[-ACTIVITY_SHOWN:]
        return {
            "id": stored.deck.id,
            "short": stored.deck.short_name,
            "name": stored.deck.name,
            "url": stored.deck.url,
            "bracket": stored.deck.bracket,
            "commanders": list(stored.deck.commanders),
            "archetype": profile.archetype,
            "identity": identity,
            "stamp": board_stamp(stored.folder),
            "pending": board.pending(stored.deck),
            "cards": cards,
            "deckCards": sorted({c.name for c in stored.deck.main_cards if "Commander" not in c.categories}),
            "activity": [activity_json(entry) for entry in reversed(activity)],
        }

    @_guarded
    def search(self, deck_id: int, text: str) -> Json:
        stored = self._stored(deck_id)
        held = {c.name.casefold() for c in stored.deck.main_cards}
        with self._db() as db:
            identity = deck_identity(db.resolve(stored.deck.main_cards))
            found = db.search_names(text, identity if identity is not None else "WUBRG")
        return {
            "cards": [
                {"name": info.name, "manaCost": info.mana_cost, "typeLine": info.type_line}
                for info in found
                if info.name.casefold() not in held
            ]
        }

    @_guarded
    def propose(self, deck_id: int, name: str) -> Json:
        with self._service(deck_id) as service:
            card = service.propose(name, Actor.YOU)
        return {"card": card_json(card)}

    @_guarded
    def decide(
        self, deck_id: int, card_id: str, decision: str, choice: str | None = None, note: str = ""
    ) -> Json:
        status = _DECISIONS.get(decision)
        if status is None:
            return {"error": f"unknown decision {decision!r}"}
        with self._service(deck_id) as service:
            card = service.decide(card_id, status, Actor.YOU, choice=choice or None, note=note)
        return {"card": card_json(card)}

    @_guarded
    def remove(self, deck_id: int, card_id: str) -> Json:
        with self._service(deck_id) as service:
            service.remove(card_id, Actor.YOU)
        return {"ok": True}

    @_guarded
    def preview(self, deck_id: int) -> Json:
        stored = self._stored(deck_id)
        user = self._user()
        with self._db() as db, self._client_factory() as http:
            fetcher = Fetcher(http)
            auth = ArchidektAuth(fetcher, self._token_store)
            # A preview reads the deck; a public one needs no login, a private one does.
            token = auth.access_token(user) if auth.has_login(user) else None
            outcome = preview_board(ArchidektEditor(fetcher, token), stored, db, user=user, now=self._now)
        return _resolution_json(outcome.resolution)

    @_guarded
    def commit(self, deck_id: int) -> Json:
        stored = self._stored(deck_id)
        user = self._user()
        with self._service_and_db(stored) as (service, db), self._client_factory() as http:
            fetcher = Fetcher(http)
            editor = ArchidektEditor(fetcher, ArchidektAuth(fetcher, self._token_store).access_token(user))
            saved = save_board(
                editor, self._workspace, stored, db, service, user=user, actor=Actor.YOU, now=self._now
            )
        view = _resolution_json(saved.resolution)
        result = saved.result
        view["saved"] = result is not None
        view["snapshot"] = result.snapshot if result else None
        view["mismatches"] = list(result.mismatches) if result else []
        view["applied"] = list(saved.applied)
        return view

    @_guarded
    def meta(self, deck_id: int) -> Json:
        """The deck's meta report from cached answers only, so the panel opens at once."""
        return self._meta(deck_id, offline=True)

    @_guarded
    def refresh_meta(self, deck_id: int) -> Json:
        """Fetch the meta answers again (about a minute when nothing is cached), then report."""
        self._user()
        return self._meta(deck_id, offline=False)

    def _meta(self, deck_id: int, *, offline: bool) -> Json:
        stored = self._stored(deck_id)
        with self._db() as db, self._client_factory() as http:
            sources = MetaSources(
                Edhtop16Client(
                    Fetcher(http, slow_after=SLOW_QUERY_SECONDS),
                    self._pacer_factory(REQUEST_INTERVAL_SECONDS),
                ),
                ArchidektSearch(Fetcher(http), self._pacer_factory(SEARCH_INTERVAL_SECONDS)),
                SpellbookClient(Fetcher(http)),
                self._pacer_factory(META_REQUEST_INTERVAL_SECONDS),
                self._settings.user or "",
                self._settings.meta,
            )
            data = gather(
                stored.deck,
                sources,
                self._settings.cache,
                now=self._now,
                refresh=not offline,
                offline=offline,
            )
            report = analyse(stored.deck, data, read_profile(stored.folder), db)
        if data.lists.data is None and data.field.data is None and data.peers.data is None:
            return {"report": None, "errors": [] if offline else [s.error for s in report.sources if s.error]}
        return {"report": report_json(report)}

    def _stored(self, deck_id: int) -> StoredDeck:
        folder = self._workspace.folder_for(int(deck_id))
        deck = self._workspace.load(int(deck_id)) if folder else None
        if folder is None or deck is None:
            raise BoardError(f"deck {deck_id} is not in {self._settings.workspace}; run `cmdr sync`")
        return StoredDeck(folder, deck)

    def _user(self) -> str:
        if not self._settings.user:
            raise ConfigError("no Archidekt username; run `cmdr init` first")
        return self._settings.user

    @contextmanager
    def _db(self) -> Generator[CardDb]:
        with CardDb(self._settings.card_db) as db:
            yield db

    @contextmanager
    def _service(self, deck_id: int) -> Generator[BoardService]:
        stored = self._stored(deck_id)
        with self._db() as db:
            yield BoardService(stored.folder, stored.deck, db, now=self._now)

    @contextmanager
    def _service_and_db(self, stored: StoredDeck) -> Generator[tuple[BoardService, CardDb]]:
        with self._db() as db:
            yield BoardService(stored.folder, stored.deck, db, now=self._now), db

    def _card_view(self, db: CardDb, card: BoardCard) -> Json:
        view = card_json(card)
        info = db.cards([card.oracle_id]).get(card.oracle_id)
        text = db.card_text(card.oracle_id)
        view.update(
            manaCost=info.mana_cost if info else "",
            typeLine=info.type_line if info else "",
            priceCents=info.cheapest_cents if info else None,
            gameChanger=info.game_changer if info else False,
            text=text.text if text else "",
            image=_image_url(db, info) if info else None,
        )
        return view


def _image_url(db: CardDb, info: CardInfo) -> str | None:
    printings = db.printings_of(info.oracle_id)
    regular = [p for p in printings if p.regular] or printings
    if not regular:
        return None
    scryfall_id = regular[-1].scryfall_id
    return IMAGE_URL.format(a=scryfall_id[0], b=scryfall_id[1], id=scryfall_id)


def _resolution_json(resolution: Resolution) -> Json:
    return {
        "deck": resolution.deck.id,
        "changes": [
            {"kind": c.kind.value, "mark": change_mark(c), "name": c.name, "text": change_text(c)}
            for c in resolution.changes
        ],
        "problems": list(resolution.problems),
        "warnings": list(resolution.warnings),
        "skipped": list(resolution.skipped),
        "before": resolution.main_before,
        "after": resolution.main_after,
    }
