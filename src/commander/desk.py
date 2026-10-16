"""What the desktop app can ask of cmdr: decks, boards, card search, decisions, previews and saves.

Every public method answers with a JSON-ready dict, {"error": "..."} on an expected failure, so nothing
raises into the web page. Each call opens its own card database connection: the window calls in from
several threads, and a SQLite connection belongs to one.
"""

import logging
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from functools import wraps
from pathlib import Path

import httpx

from commander.answers import card_key
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
    files_stamp,
    read_activity,
    read_board,
)
from commander.boardsave import preview_board, save_board
from commander.bracket import CACHE_FOLDER as SPELLBOOK_CACHE_FOLDER
from commander.bracket import estimate_cache, estimate_deck
from commander.bracketcheck import check_bracket, check_json
from commander.carddb import CardDataError, CardDb, CardInfo, ResolvedCard
from commander.cli_meta import report_json
from commander.cli_output import change_mark, change_text
from commander.config import ConfigError, MetaSettings
from commander.credentials import CredentialError, TokenStore
from commander.deckexport import topdeck_text
from commander.deckgrid import GridGroup, deck_grid, image_url
from commander.deckprofile import MAX_ADJACENT, Profile, read_profile
from commander.deckthemes import merge_themes
from commander.edhtop16 import REQUEST_INTERVAL_SECONDS, SLOW_QUERY_SECONDS, Edhtop16Client, Edhtop16Error
from commander.ideas import compile_ideas
from commander.labels import LabelError, Step, label_options, mark, set_label, to_get_json
from commander.leanings import leaning_json
from commander.manabase import ALTERNATIVE_COST_TAGS, analyse_mana, mana_json
from commander.meta import analyse, card_shares, performing_entries
from commander.metadata import CEDH_BRACKET, MetaData, MetaSources, cache_stamp, gather, gather_for
from commander.metapanel import combo_rows, key_card_rows, stopper_rows, untouched
from commander.models import Deck
from commander.net import Fetcher, NetworkError, Pacer
from commander.profileedit import save_settings
from commander.spellbook import META_REQUEST_INTERVAL_SECONDS, SpellbookClient
from commander.store import StoredDeck, Workspace, WorkspaceError
from commander.suggest import deck_identity
from commander.targets import CutRules, Target, deck_cut_rules, targets_for, with_ideas

log = logging.getLogger(__name__)

ACTIVITY_SHOWN = 60
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
    LabelError,
    OSError,
)
_DECISIONS = {"add": Status.ADD, "reject": Status.REJECTED, "reopen": Status.OPEN}
# The settings pane's autocomplete: names offered at once, and the letters before tags are searched.
MAX_SUGGESTIONS = 12
MIN_TAG_SEARCH = 2

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
        # Per deck: the meta cache stamp the shares were read at, and the shares.
        self._pools: dict[int, tuple[str, MetaData]] = {}
        self._rules: dict[int, tuple[str, CutRules]] = {}

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
        return {
            "stamps": {str(i): board_stamp(folder) for i, folder in self._workspace.deck_folders()},
            "meta": cache_stamp(self._settings.cache),
        }

    @_guarded
    def deck(self, deck_id: int) -> Json:
        stored = self._stored(deck_id)
        board = read_board(stored.folder)
        profile = read_profile(stored.folder)
        drop = set(board.to_drop(stored.deck))
        with self._db() as db:
            main = db.resolve(stored.deck.main_cards)
            identity = deck_identity(main)
            grid = deck_grid(main)
            infos = db.cards(card.oracle_id for card in board.cards)
            rules = with_ideas(self._deck_rules(stored, main, profile, db), infos.values(), db)
            cards = [
                {
                    **self._card_view(db, card),
                    "dropping": card.name in drop,
                    "targets": [
                        _target_json(t) for t in targets_for(card, infos.get(card.oracle_id), main, rules)
                    ],
                }
                for card in board.cards
            ]
        activity = read_activity(stored.folder)[-ACTIVITY_SHOWN:]
        account = self._workspace.account_labels()
        return {
            "id": stored.deck.id,
            "short": stored.deck.short_name,
            "name": stored.deck.name,
            "url": stored.deck.url,
            "bracket": stored.deck.bracket,
            "commanders": list(stored.deck.commanders),
            "archetype": profile.archetype,
            "leanings": [leaning_json(leaning) for leaning in rules.leanings],
            "tags": list(stored.deck.tags or ()),
            "identity": identity,
            "stamp": board_stamp(stored.folder),
            "pending": board.pending(stored.deck),
            "cards": cards,
            "deckCards": sorted({c.name for c in stored.deck.main_cards if "Commander" not in c.categories}),
            "toGet": to_get_json(stored.deck),
            "labels": [{"name": n, "colour": c} for n, c in label_options(stored.deck, account)],
            "grid": [_group_json(group, dict(account or ())) for group in grid],
            "activity": [activity_json(entry) for entry in reversed(activity)],
        }

    @_guarded
    def set_label(self, deck_id: int, name: str, label: str) -> Json:
        """Give one deck card one of the deck's labels ("" for none) on Archidekt at once (ADR 0010)."""
        stored = self._stored(deck_id)
        user = self._user()
        with self._service_and_db(stored) as (service, _), self._client_factory() as http:
            fetcher = Fetcher(http)
            editor = ArchidektEditor(fetcher, ArchidektAuth(fetcher, self._token_store).access_token(user))
            marked = set_label(
                editor, self._workspace, stored, service, name=name, label=label, actor=Actor.YOU
            )
        return {"card": marked.card, "before": marked.before, "after": marked.after}

    @_guarded
    def mark(self, deck_id: int, name: str, step: str) -> Json:
        """Mark one deck card ordered or arrived on Archidekt at once (ADR 0010)."""
        try:
            chosen = Step(step)
        except ValueError:
            return {"error": f"unknown step {step!r}"}
        stored = self._stored(deck_id)
        user = self._user()
        with self._service_and_db(stored) as (service, _), self._client_factory() as http:
            fetcher = Fetcher(http)
            editor = ArchidektEditor(fetcher, ArchidektAuth(fetcher, self._token_store).access_token(user))
            marked = mark(editor, self._workspace, stored, service, name=name, step=chosen, actor=Actor.YOU)
        return {
            "card": marked.card,
            "before": marked.before,
            "after": marked.after,
            "toGet": to_get_json(marked.deck),
        }

    @_guarded
    def settings(self, deck_id: int) -> Json:
        """The profile's adjacent commanders and ignored themes, with the deck's own themes to pick from."""
        stored = self._stored(deck_id)
        profile = read_profile(stored.folder)
        with self._db() as db:
            derived = merge_themes(stored.deck, Profile(), db).derived
            main = db.resolve(stored.deck.main_cards)
            leanings = self._deck_rules(stored, main, profile, db).leanings
        return {
            "adjacent": [list(group) for group in profile.adjacent],
            "ignore": list(profile.ignore_themes),
            "themes": [{"name": t.name, "kind": t.source.value} for t in derived.themes],
            "leanings": [leaning.tag for leaning in leanings],
            "maxAdjacent": MAX_ADJACENT,
        }

    @_guarded
    def find_commanders(self, text: str) -> Json:
        """Commanders whose name holds text, for the adjacent commanders' autocomplete."""
        with self._db() as db:
            found = db.search_commanders(text, limit=MAX_SUGGESTIONS)
        return {"names": [info.name for info in found]}

    @_guarded
    def find_tags(self, text: str) -> Json:
        """Tagger tags whose slug holds text, most cards first, for the ignored themes' autocomplete."""
        if len(text.strip()) < MIN_TAG_SEARCH:
            return {"tags": []}
        with self._db() as db:
            found = db.suggest_tags(text, limit=MAX_SUGGESTIONS)
        return {"tags": [{"name": tag.slug, "cards": tag.cards} for tag in found]}

    @_guarded
    def save_settings(self, deck_id: int, adjacent: list[list[str]], ignore: list[str]) -> Json:
        """Write the adjacent commanders and ignored themes into profile.toml, keeping its comments."""
        stored = self._stored(deck_id)
        with self._db() as db:
            for group in adjacent:
                for name in group:
                    found = {info.name.casefold() for info in db.search_commanders(name)}
                    if name.strip().casefold() not in found:
                        return {"error": f"{name!r} is not a commander the card data knows"}
        save_settings(stored.folder, stored.deck, adjacent=adjacent, ignore=ignore)
        return self.settings(deck_id)

    @_guarded
    def mana(self, deck_id: int) -> Json:
        """The deck's mana base against Karsten's numbers and what other lists of its commander run."""
        deck = self._stored(deck_id).deck
        label, lists = self._mana_lists(deck)
        with self._db() as db:
            names = {card.name for card in deck.main_cards} | {name for listed in lists for name in listed}
            cards = db.card_mana(names)
            ids = [info.oracle_id for card in deck.main_cards if (info := cards.get(card.name.casefold()))]
            lineage = db.tag_lineage(ids)
        alternative = [
            info.name
            for info in cards.values()
            if lineage.get(info.oracle_id, frozenset()) & ALTERNATIVE_COST_TAGS
        ]
        report = analyse_mana(
            deck, cards, alternative_cost=alternative, comparison=(label, lists) if lists else None
        )
        view = mana_json(report)
        view["compareWith"] = label
        return view

    def _mana_lists(self, deck: Deck) -> tuple[str, list[dict[str, int]]]:
        """Bracket 5: the commander's top-cut tournament lists; else its recent Archidekt decks (ADR 0019)."""
        data = self._pool(deck)
        if data is None:
            return "", []
        if deck.bracket == CEDH_BRACKET:
            return "top-cut lists", [
                {c.name: c.count for c in entry.cards} for entry in performing_entries(data)
            ]
        lists: list[dict[str, int]] = []
        for peer in data.peers.data.decks if data.peers.data else ():
            # Peer caches from before ADR 0019 don't say how many of each basic land a deck runs.
            if peer.multiples is None:
                continue
            counts = dict.fromkeys(peer.cards, 1)
            counts.update(peer.multiples)
            lists.append(counts)
        return "recent decks", lists

    @_guarded
    def bracket(self, deck_id: int) -> Json:
        """Spellbook's estimate beside the set bracket and the ceiling, and what lifts it (ADR 0021).

        An answer under a week old comes from the cache; otherwise Spellbook is asked, which takes seconds.
        """
        stored = self._stored(deck_id)
        ceiling = read_profile(stored.folder).bracket_ceiling
        cache = estimate_cache(self._settings.cache / SPELLBOOK_CACHE_FOLDER)
        with self._client_factory() as http:
            result = estimate_deck(
                stored.deck, SpellbookClient(Fetcher(http)), cache, now=self._now, refresh=False
            )
        estimate = result.data
        return {
            "estimate": {"tag": estimate.tag.label, "brackets": estimate.tag.brackets} if estimate else None,
            "fetchedAt": result.fetched_at.isoformat() if result.fetched_at else None,
            # Not "error": the page takes that key for a failed call, and the ceiling is still checked.
            "unavailable": result.error,
            **check_json(check_bracket(stored.deck.bracket, ceiling, estimate)),
        }

    @_guarded
    def export(self, deck_id: int) -> Json:
        """The saved deck as TopDeck.gg decklist text, for the clipboard."""
        deck = self._stored(deck_id).deck
        return {"text": topdeck_text(deck), "cards": sum(card.quantity for card in deck.main_cards)}

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
        stored = self._stored(deck_id)
        board_card = read_board(stored.folder).find(card_id)
        shares = self._meta_shares(stored.deck) if status is Status.REJECTED else None
        share = shares.get(card_key(board_card.name), 0.0) if shares is not None and board_card else None
        with self._service(deck_id) as service:
            card = service.decide(card_id, status, Actor.YOU, choice=choice or None, note=note, share=share)
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
        data = self._gather(stored.deck, offline=offline)
        with self._db() as db:
            report = analyse(stored.deck, data, read_profile(stored.folder), db)
            if not offline and stored.deck.bracket == CEDH_BRACKET:
                # A refresh compiles the deck's ideas, as `cmdr sync` does (ADR 0014).
                compile_ideas(stored, data, db, now=self._now)
        if data.lists.data is None and data.field.data is None and data.peers.data is None:
            return {"report": None, "errors": [] if offline else [s.error for s in report.sources if s.error]}
        return {
            "report": report_json(report),
            "keys": key_card_rows(report.key_cards),
            "stoppers": stopper_rows(report.own_combos, report.threats),
            "untouched": untouched(report.key_cards, report.own_combos, report.threats),
            "combos": combo_rows(report.combos),
        }

    def _meta_shares(self, deck: Deck) -> dict[str, float] | None:
        """Each card's share of the meta's lists, for bracket 5 decks with cached meta answers; else None."""
        data = self._pool(deck) if deck.bracket == CEDH_BRACKET else None
        return (card_shares(data) or None) if data is not None else None

    def _deck_rules(
        self, stored: StoredDeck, main: Sequence[ResolvedCard], profile: Profile, db: CardDb
    ) -> CutRules:
        """The deck's cut rules, kept until its snapshot, its profile or the cached meta answers change."""
        stamp = (
            f"{files_stamp(stored.folder, ('deck.json', 'profile.toml'))}|{cache_stamp(self._settings.cache)}"
        )
        known = self._rules.get(stored.deck.id)
        if known is not None and known[0] == stamp:
            return known[1]
        rules = deck_cut_rules(stored.deck, main, profile, db, self._pool(stored.deck))
        self._rules[stored.deck.id] = (stamp, rules)
        return rules

    def _pool(self, deck: Deck) -> MetaData | None:
        """What other players do with the deck's commander, from cache only; None for a deck without one."""
        if not deck.commanders:
            return None
        stamp = cache_stamp(self._settings.cache)
        known = self._pools.get(deck.id)
        if known is not None and known[0] == stamp:
            return known[1]
        data = self._gather(deck, offline=True, read=gather_for)
        self._pools[deck.id] = (stamp, data)
        return data

    def _gather(self, deck: Deck, *, offline: bool, read: Callable[..., MetaData] = gather) -> MetaData:
        with self._client_factory() as http:
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
            return read(
                deck, sources, self._settings.cache, now=self._now, refresh=not offline, offline=offline
            )

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
    return image_url(regular[-1].scryfall_id)


def _group_json(group: GridGroup, colours: Mapping[str, str]) -> Json:
    """One grid group; a label the account keeps shows in the account's colour (ADR 0012)."""
    return {
        "name": group.name,
        "count": group.count,
        "cents": group.cents,
        "cards": [
            {
                "name": c.name,
                "quantity": c.quantity,
                "image": c.image,
                "category": c.category,
                "label": c.label,
                "labelColour": colours.get(c.label, c.label_colour) if c.label else c.label_colour,
                "cents": c.cents,
            }
            for c in group.cards
        ],
    }


def _target_json(target: Target) -> Json:
    return {"name": target.name, "reason": target.reason, "source": target.source.value}


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
