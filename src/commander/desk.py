"""What the desktop app can ask of cmdr: decks, boards, card search, decisions, previews and saves.

Every public method answers with a JSON-ready dict, {"error": "..."} on an expected failure, so nothing
raises into the web page. Each call opens its own card database connection: the window calls in from
several threads, and a SQLite connection belongs to one. Every write to a deck's board, profile or Archidekt
refuses while a sync runs, and a sync refuses while one of them runs.
"""

import logging
import threading
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import cast

import httpx

from commander.apply import NotYourDeckError, Resolution, SaveError
from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    ArchidektAuth,
    ArchidektEditor,
    AuthError,
    ReadBackError,
    SignInRequiredError,
)
from commander.background import in_background
from commander.board import (
    Actor,
    BoardCard,
    BoardError,
    BoardService,
    Relabel,
    ReplaceOption,
    Status,
    activity_json,
    board_digest,
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
from commander.bracketpreview import preview_json, spellbook_preview
from commander.budget import resolved_budget
from commander.carddb import CardDataError, CardDb, CardInfo, ResolvedCard
from commander.changetext import change_mark, change_text
from commander.config import ConfigError, MetaSettings, forget_account, switch_account, validate_user
from commander.credentials import CredentialError, TokenStore
from commander.deckexport import cardmarket_wants_text, topdeck_text
from commander.deckgrid import GridGroup, deck_grid, image_url
from commander.deckprofile import MAX_ADJACENT, Profile, read_profile
from commander.deckthemes import merge_themes
from commander.edhtop16 import Edhtop16Error
from commander.fetchpriority import FetchPriority
from commander.ideas import idea_labels
from commander.labels import LabelError, label_options, set_label
from commander.landswap import comparison_lists
from commander.leanings import leaning_json
from commander.manabase import ALTERNATIVE_COST_TAGS, analyse_mana, mana_json
from commander.manaplan import BASIC_NAMES, LandOption, ManaPlan, plan_json, suggest_mana
from commander.meta import analyse
from commander.metadata import (
    CEDH_BRACKET,
    MetaData,
    MetaSources,
    cache_stamp,
    gather,
    gather_for,
    meta_sources,
)
from commander.metajson import report_json
from commander.metapanel import combo_rows, key_card_rows, stopper_rows, untouched
from commander.models import COMMANDER_CATEGORY, Deck
from commander.net import Fetcher, NetworkError, Pacer
from commander.profileedit import save_settings
from commander.spellbook import SpellbookClient
from commander.store import StoredDeck, Workspace, WorkspaceError
from commander.suggest import deck_identity
from commander.sync import SyncStatus
from commander.syncrun import CardState, FetchState, SyncOutcome, SyncServices, run_sync
from commander.targets import CutRules, Target, deck_cut_rules, targets_for, with_ideas
from commander.viewscore import VIEWS_FILE, ViewScores
from commander.weightedshare import (
    ShareTable,
    SourceShare,
    WeightedShare,
    share_label,
    share_tip,
    weighted_shares,
)

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
    return _guard(method, show_args=True)


def _guarded_secret[**P](method: Callable[P, Json]) -> Callable[P, Json]:
    """As _guarded, for a call that carries a password: its arguments never reach the log."""
    return _guard(method, show_args=False)


def _guard[**P](method: Callable[P, Json], *, show_args: bool) -> Callable[P, Json]:
    @wraps(method)
    def call(*args: P.args, **kwargs: P.kwargs) -> Json:
        log.debug("bridge call %s %s", method.__name__, args[1:] if show_args else "(arguments withheld)")
        try:
            return method(*args, **kwargs)
        except _EXPECTED as error:
            log.warning("%s failed: %s", method.__name__, error)
            return {"error": _app_text(error)}
        except Exception:
            log.exception("%s failed unexpectedly", method.__name__)
            return {"error": "Something went wrong; the details are in the log."}

    return call


def _app_text(error: Exception) -> str:
    """The error's message plus what to press in the app; shared modules leave the remedy to the front end."""
    if isinstance(error, SignInRequiredError):
        return f"{error}. Sign in to Archidekt."
    if isinstance(error, ReadBackError):
        return f"{error}. Press Sync."
    return str(error)


def _sync_thread(work: Callable[[], None]) -> object:
    return in_background(work, name="app-sync")


@dataclass(slots=True)
class _SyncState:
    """The app's one sync at a time; guarded by Desk._sync_lock."""

    running: bool = False
    progress: str = ""
    summary: str | None = None
    failed: bool = False
    warnings: list[str] = field(default_factory=list[str])
    # Each deck's step in the ideas phase while the sync runs; nothing is kept once it ends.
    decks: dict[int, FetchState] = field(default_factory=dict[int, FetchState])


def sync_summary(outcome: SyncOutcome) -> str:
    """One line for the status bar: what changed, or what failed."""
    counts = {status: sum(1 for r in outcome.results if r.status is status) for status in SyncStatus}
    problems: list[str] = []
    if failed := counts[SyncStatus.FAILED]:
        problems.append(f"{_decks(failed)} failed")
    if outcome.cards.state is CardState.FAILED:
        problems.append(f"card data failed ({outcome.cards.detail})")
    if problems:
        return f"Sync finished with problems: {', '.join(problems)}; see the log."
    changed = counts[SyncStatus.NEW] + counts[SyncStatus.UPDATED]
    return f"Synced {_decks(len(outcome.results))}, {changed} changed."


def _decks(count: int) -> str:
    return f"{count} deck{'' if count == 1 else 's'}"


@dataclass(slots=True, frozen=True)
class DeskSettings:
    workspace: Path
    card_db: Path
    # None until `cmdr init` sets a username; previews and saves need it.
    user: str | None
    meta: MetaSettings = field(default_factory=MetaSettings)
    # The config file that sign-in writes the account to, and where a first account's decks go.
    config: Path | None = None
    documents: Path | None = None

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
        background: Callable[[Callable[[], None]], object] | None = None,
    ) -> None:
        self._settings = settings
        self._pacer_factory = pacer_factory
        # Runs the sync off the window's thread; tests run it inline.
        self._background: Callable[[Callable[[], None]], object] = background or _sync_thread
        self._workspace = Workspace(settings.workspace)
        self._client_factory = client_factory
        self._token_store = token_store
        self._now = now
        # Per deck: the meta cache stamp the answers were read at, and what was made of them.
        self._pools: dict[int, tuple[str, MetaData]] = {}
        self._tables: dict[int, tuple[str, ShareTable]] = {}
        self._rules: dict[int, tuple[str, CutRules]] = {}
        self._sync_lock = threading.Lock()
        self._sync = _SyncState()
        # How often each deck is opened, and the deck open now; the sync fetches by both (ADR 0034).
        self._views = ViewScores(settings.cache / VIEWS_FILE, now)
        self._open_deck: int | None = None
        # Set while a sync runs, so a deck opened meanwhile goes next; guarded by _sync_lock like the sync.
        self._priority: FetchPriority | None = None
        # What each running write to the decks or Archidekt is called; guarded by _sync_lock like the sync.
        self._writes: list[str] = []

    @_guarded
    def account(self) -> Json:
        """Who is signed in, and how the app's sync is going."""
        user = self._settings.user
        return {
            "user": user,
            "signedIn": bool(user) and self._token_store.get(user or "") is not None,
            "sync": self._sync_json(),
        }

    @_guarded_secret
    def sign_in(self, user: str, password: str) -> Json:
        """Sign in to Archidekt, make the account the config's and switch to its decks (ADR 0023).

        The password goes to Archidekt's login only; the token is kept as `cmdr login` keeps it. A first
        sign-in, with no decks in the account's folder yet, starts a sync.
        """
        config = self._config()
        user = user.strip()
        validate_user(user)
        with self._writing("sign-in", "sign in") as busy:
            if busy:
                return busy
            with self._client_factory() as http:
                ArchidektAuth(Fetcher(http), self._token_store).login(user, user, password)
            folder = switch_account(config, user, documents=self._documents())
            self._use_account(user, folder)
            log.info("signed in as %s; decks in %s", user, folder)
        if not self._workspace.load_all():
            self._start_sync(user)
        return self.account()

    @_guarded
    def sign_out(self) -> Json:
        """Remove the saved login and forget the account; its deck folder stays for the next sign-in."""
        config = self._config()
        with self._writing("sign-out", "sign out") as busy:
            if busy:
                return busy
            user = self._settings.user
            if user:
                self._token_store.delete(user)
            forget_account(config)
            self._settings = replace(self._settings, user=None)
            log.info("signed out %s; decks stay in %s", user, self._settings.workspace)
        return self.account()

    @_guarded
    def sync(self) -> Json:
        """Start syncing the account's decks in the background; sync_status follows it."""
        if busy := self._start_sync(self._user()):
            return busy
        return self._sync_json()

    @_guarded
    def sync_status(self) -> Json:
        return self._sync_json()

    def _config(self) -> Path:
        if self._settings.config is None:
            raise ConfigError("signing in needs the config file's location")
        return self._settings.config

    def _documents(self) -> Path:
        return self._settings.documents or Path.home() / "Documents"

    def _use_account(self, user: str, folder: Path) -> None:
        self._settings = replace(self._settings, user=user, workspace=folder)
        self._workspace = Workspace(folder)
        self._pools.clear()
        self._rules.clear()
        with self._sync_lock:
            self._open_deck = None

    @contextmanager
    def _writing(self, doing: str, then: str) -> Generator[Json | None]:
        """Marks a write to the decks or Archidekt as running, so no sync starts meanwhile.

        Yields the refusal instead, and marks nothing, when a sync already runs: the check and the mark
        are one step under the lock, so a sync and a write cannot both start.
        """
        with self._sync_lock:
            busy = self._sync.running
            if not busy:
                self._writes.append(doing)
        if busy:
            yield {"error": f"Wait for the sync to finish, then {then}."}
            return
        try:
            yield None
        finally:
            with self._sync_lock:
                self._writes.remove(doing)

    def _start_sync(self, user: str) -> Json | None:
        """Starts the sync unless one runs already; the refusal when a write is under way."""
        with self._sync_lock:
            if self._writes:
                return {"error": f"Wait for the {self._writes[0]} to finish, then sync."}
            if self._sync.running:
                return None
            self._sync = _SyncState(running=True, progress="Starting the sync")
            self._priority = priority = FetchPriority(self._open_deck)
        self._background(lambda: self._run_sync(user, priority))
        return None

    def _run_sync(self, user: str, priority: FetchPriority) -> None:
        services = SyncServices(
            self._client_factory, self._token_store, self._settings.cache, self._now, self._pacer_factory
        )
        try:
            outcome = run_sync(
                services,
                self._workspace,
                user,
                self._settings.meta,
                priority=priority,
                on_deck=self._sync_deck,
                on_progress=self._sync_progress,
            )
        except _EXPECTED as error:
            log.warning("sync failed: %s", error)
            self._finish_sync(f"Sync failed: {error}", failed=True)
            return
        except Exception:
            log.exception("sync failed unexpectedly")
            self._finish_sync("Sync failed; the details are in the log.", failed=True)
            return
        for warning in outcome.warnings:
            log.warning("sync: %s", warning)
        self._finish_sync(sync_summary(outcome), failed=outcome.failed, warnings=outcome.warnings)

    def _sync_progress(self, message: str) -> None:
        with self._sync_lock:
            self._sync.progress = message

    def _sync_deck(self, deck_id: int, state: FetchState) -> None:
        with self._sync_lock:
            self._sync.decks[deck_id] = state

    def _finish_sync(self, summary: str, *, failed: bool, warnings: Sequence[str] = ()) -> None:
        with self._sync_lock:
            self._sync = _SyncState(summary=summary, failed=failed, warnings=list(warnings))
            self._priority = None

    def _sync_json(self) -> Json:
        with self._sync_lock:
            state = self._sync
            return {
                "running": state.running,
                "progress": state.progress,
                "summary": state.summary,
                "failed": state.failed,
                "warnings": list(state.warnings),
                "decks": {str(deck_id): fetch.value for deck_id, fetch in state.decks.items()},
            }

    @_guarded
    def open_deck(self, deck_id: int) -> Json:
        """The user picked a deck: a view, and the deck a running or coming sync fetches first (ADR 0034)."""
        stored = self._stored(deck_id)
        self._views.record_open(stored.deck.id)
        with self._sync_lock:
            self._open_deck = stored.deck.id
            if self._priority:
                self._priority.prefer(stored.deck.id)
        return {}

    @_guarded
    def decks(self) -> Json:
        rows: list[Json] = []
        for stored in self._workspace.load_all():
            row: Json = {
                "id": stored.deck.id,
                "short": stored.deck.short_name,
                "name": stored.deck.name,
                "queued": 0,
                "open": 0,
            }
            try:
                board = read_board(stored.folder)
            except BoardError as error:
                # One deck's damaged board must not hide the others.
                log.warning("deck %s: %s", stored.deck.id, error)
                row["error"] = str(error)
            else:
                row["queued"] = len(board.queued)
                row["open"] = sum(1 for c in board.cards if c.status is Status.OPEN)
            rows.append(row)
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
        with self._db() as db:
            main = db.resolve(stored.deck.main_cards)
            identity = deck_identity(main)
            grid = deck_grid(main)
            infos = db.cards(card.oracle_id for card in board.cards)
            rules = with_ideas(self._deck_rules(stored, main, profile, db), infos.values(), db)
            budget = resolved_budget(stored.deck.bracket, main, profile.max_card_cents)
            shares = self._shares(stored.deck)
            cards = [
                {
                    **self._card_view(db, card),
                    "withinBudget": None
                    if budget.working is None
                    else budget.within(info.cheapest_cents if (info := infos.get(card.oracle_id)) else None),
                    "share": _share_json(shares.of(card.name)),
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
            "budget": budget.json(),
            "pending": len(board.queued),
            "cards": cards,
            "deckCards": sorted({c.name for c in stored.deck.main_cards if "Commander" not in c.categories}),
            "labels": [{"name": n, "colour": c} for n, c in label_options(stored.deck, account)],
            "grid": [_group_json(group, dict(account or ()), shares) for group in grid],
            "activity": [activity_json(entry) for entry in reversed(activity)],
        }

    @_guarded
    def set_label(self, deck_id: int, name: str, label: str) -> Json:
        """Give one deck card one of the deck's labels ("" for none) on Archidekt at once (ADR 0010)."""
        with self._writing("label change", "change the label") as busy:
            if busy:
                return busy
            stored = self._stored(deck_id)
            user = self._user()
            with self._service_and_db(stored) as (service, _), self._client_factory() as http:
                editor = self._editor(http, user)
                marked = set_label(
                    editor, self._workspace, stored, service, name=name, label=label, actor=Actor.YOU
                )
        return {"card": marked.card, "before": marked.before, "after": marked.after}

    @_guarded
    def settings(self, deck_id: int) -> Json:
        """The profile's adjacent commanders, tag search, ignored themes and the deck's themes."""
        stored = self._stored(deck_id)
        profile = read_profile(stored.folder)
        with self._db() as db:
            derived = merge_themes(stored.deck, Profile(), db).derived
            main = db.resolve(stored.deck.main_cards)
            leanings = self._deck_rules(stored, main, profile, db).leanings
        return {
            "adjacent": [list(group) for group in profile.adjacent],
            "ignore": list(profile.ignore_themes),
            "tagSearch": profile.tag_search,
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
    def save_settings(
        self,
        deck_id: int,
        adjacent: list[list[str]],
        ignore: list[str],
        tag_search: bool,  # noqa: FBT001 — the page's bridge passes positionals
    ) -> Json:
        """Write the adjacent commanders, tag search and ignored themes into profile.toml, comments kept."""
        with self._writing("settings save", "save the settings") as busy:
            if busy:
                return busy
            stored = self._stored(deck_id)
            with self._db() as db:
                for group in adjacent:
                    for name in group:
                        found = {info.name.casefold() for info in db.search_commanders(name)}
                        if name.strip().casefold() not in found:
                            return {"error": f"{name!r} is not a commander the card data knows"}
            save_settings(stored.folder, stored.deck, adjacent=adjacent, ignore=ignore, tag_search=tag_search)
        return self.settings(deck_id)

    @_guarded
    def mana(self, deck_id: int) -> Json:
        """The deck's mana base against Karsten's numbers, what other lists of its commander run and, below
        bracket 5, how it should change (ADR 0024)."""
        view, plan = self._mana(deck_id)
        if plan is None:
            view["plan"] = None
            return view
        board = read_board(self._stored(deck_id).folder)
        shown = plan_json(plan)
        for swap in cast("list[Json]", shown["swaps"]):
            held = board.find(str(swap["add"]))
            swap["board"] = held.status.value if held is not None else None
        view["plan"] = shown
        return view

    @_guarded
    def mana_to_board(self, deck_id: int) -> Json:
        """Put the suggested swaps on the board under Considering, each with its cut; the user decides."""
        with self._writing("swaps", "add the swaps") as busy:
            if busy:
                return busy
            _, plan = self._mana(deck_id)
            if plan is None:
                return {"error": "bracket 5 decks follow the tournament lists, not Karsten's numbers"}
            board = read_board(self._stored(deck_id).folder)
            added: list[Json] = []
            with self._service(deck_id) as service:
                for swap in plan.swaps:
                    # Like compiled ideas, a card on the board in any state, turned down too, is left alone.
                    if board.find(swap.add) is not None:
                        continue
                    card = service.propose(
                        swap.add,
                        Actor.CMDR,
                        reason=swap.reason,
                        replace_options=(ReplaceOption(swap.cut, swap.reason),),
                        copies=swap.copies,
                    )
                    added.append(card_json(card))
        return {"added": added, "skipped": len(plan.swaps) - len(added)}

    def _mana(self, deck_id: int) -> tuple[Json, ManaPlan | None]:
        stored = self._stored(deck_id)
        deck = stored.deck
        label, lists = comparison_lists(deck, self._pool(deck))
        cedh = deck.bracket == CEDH_BRACKET
        with self._db() as db:
            names = {card.name for card in deck.main_cards} | {name for listed in lists for name in listed}
            cards = db.card_mana(names | set(BASIC_NAMES.values()))
            ids = [info.oracle_id for card in deck.main_cards if (info := cards.get(card.name.casefold()))]
            lineage = db.tag_lineage(ids)
            options, budget = ([], None) if cedh else _land_options(db, deck, stored.folder)
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
        if cedh:
            return view, None
        plan = suggest_mana(
            deck, cards, report, alternative_cost=alternative, options=options, budget_cents=budget
        )
        return view, plan

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
    def cardmarket_wants(self) -> Json:
        """Every deck's Don't Have cards as Cardmarket wants-list text, for the clipboard."""
        wants = cardmarket_wants_text(stored.deck for stored in self._workspace.load_all())
        return {"text": wants.text, "cards": wants.cards, "skipped": list(wants.skipped)}

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
        with self._writing("proposal", "propose the card") as busy:
            if busy:
                return busy
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
        with self._writing("decision", "decide") as busy:
            if busy:
                return busy
            stored = self._stored(deck_id)
            board_card = read_board(stored.folder).find(card_id)
            found = (
                self._shares(stored.deck).of(board_card.name)
                if status is Status.REJECTED and board_card
                else None
            )
            share = found.share if found is not None else None
            with self._service(deck_id) as service:
                card = service.decide(
                    card_id,
                    status,
                    Actor.YOU,
                    choice=choice or None,
                    note=note,
                    share=share,
                    relabel=self._relabel(stored, board_card) if status is Status.OPEN else None,
                )
        return {"card": card_json(card)}

    @_guarded
    def remove(self, deck_id: int, card_id: str) -> Json:
        with self._writing("removal", "remove the card") as busy:
            if busy:
                return busy
            with self._service(deck_id) as service:
                service.remove(card_id, Actor.YOU)
        return {"ok": True}

    @_guarded
    def preview(self, deck_id: int) -> Json:
        stored = self._stored(deck_id)
        user = self._user()
        # Taken before the board is read, so a change made during the preview shows up at the save.
        stamp = board_digest(stored.folder)
        with self._db() as db, self._client_factory() as http:
            fetcher = Fetcher(http)
            auth = ArchidektAuth(fetcher, self._token_store)
            # A preview reads the deck; a public one needs no login, a private one does.
            token = auth.access_token(user) if auth.has_login(user) else None
            outcome = preview_board(ArchidektEditor(fetcher, token), stored, db, user=user, now=self._now)
            ceiling = read_profile(stored.folder).bracket_ceiling
            bracket = spellbook_preview(
                outcome.resolution, fetcher, self._settings.cache, ceiling=ceiling, now=self._now
            )
        view = _resolution_json(outcome.resolution)
        view["bracket"] = preview_json(bracket)
        view["stamp"] = stamp
        return view

    @_guarded
    def commit(self, deck_id: int, stamp: str) -> Json:
        """Save the queued cards, if the board is still as the preview with this stamp saw it."""
        with self._writing("save", "save") as busy:
            if busy:
                return busy
            stored = self._stored(deck_id)
            user = self._user()
            if board_digest(stored.folder) != stamp:
                return {"error": "The board changed after the preview; open Review again before saving."}
            with self._service_and_db(stored) as (service, db), self._client_factory() as http:
                editor = self._editor(http, user)
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
        """The deck's meta report from cached answers only; a sync refreshes them."""
        stored = self._stored(deck_id)
        data = self._gather(stored.deck)
        with self._db() as db:
            report = analyse(stored.deck, data, read_profile(stored.folder), db)
        if data.lists.data is None and data.field.data is None and data.peers.data is None:
            return {"report": None}
        return {
            "report": report_json(report),
            "keys": key_card_rows(report.key_cards),
            "stoppers": stopper_rows(report.own_combos, report.threats),
            "untouched": untouched(report.key_cards, report.own_combos, report.threats),
            "combos": combo_rows(report.combos),
            "racing": report.style.races,
        }

    def _relabel(self, stored: StoredDeck, card: BoardCard | None) -> Relabel | None:
        """What the cached lists make of a card the user reconsiders; None when it is not one to label."""
        if card is None or card.status is not Status.REJECTED or not card.relabellable:
            return None
        with self._db() as db:
            labels = idea_labels(stored.deck, self._pool(stored.deck), db, self._shares(stored.deck), [card])
        return labels.get(card.id)

    def _shares(self, deck: Deck) -> ShareTable:
        """Each card's weighted share of the lists other players run with the deck's commander, from cached
        answers; empty without any."""
        stamp = f"{cache_stamp(self._settings.cache)}|{self._now().date()}"
        known = self._tables.get(deck.id)
        if known is not None and known[0] == stamp:
            return known[1]
        table = weighted_shares(self._pool(deck), now=self._now)
        self._tables[deck.id] = (stamp, table)
        return table

    def _deck_rules(
        self, stored: StoredDeck, main: Sequence[ResolvedCard], profile: Profile, db: CardDb
    ) -> CutRules:
        """The deck's cut rules, kept until its snapshot, its profile or the cached meta answers change."""
        stamp = (
            f"{files_stamp(stored.folder, ('deck.json', 'profile.toml'))}"
            f"|{cache_stamp(self._settings.cache)}|{self._now().date()}"
        )
        known = self._rules.get(stored.deck.id)
        if known is not None and known[0] == stamp:
            return known[1]
        rules = deck_cut_rules(stored.deck, main, profile, db, self._pool(stored.deck), now=self._now)
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
        data = self._gather(deck, read=gather_for)
        self._pools[deck.id] = (stamp, data)
        return data

    def _gather(self, deck: Deck, *, read: Callable[..., MetaData] = gather) -> MetaData:
        """Cached meta answers only: fetching belongs to the sync."""
        with self._client_factory() as http:
            return read(
                deck,
                self._meta_sources(http),
                self._settings.cache,
                now=self._now,
                refresh=False,
                offline=True,
            )

    def _meta_sources(self, http: httpx.Client) -> MetaSources:
        return meta_sources(http, self._pacer_factory, self._settings.user or "", self._settings.meta)

    def _stored(self, deck_id: int) -> StoredDeck:
        folder = self._workspace.folder_for(int(deck_id))
        deck = self._workspace.load(int(deck_id)) if folder else None
        if folder is None or deck is None:
            raise BoardError(f"deck {deck_id} is not in {self._settings.workspace}; press Sync")
        return StoredDeck(folder, deck)

    def _user(self) -> str:
        if not self._settings.user:
            raise ConfigError("Sign in to Archidekt first.")
        return self._settings.user

    def _editor(self, http: httpx.Client, user: str) -> ArchidektEditor:
        """An Archidekt editor signed in as the user; the sign-in must be on file."""
        fetcher = Fetcher(http)
        return ArchidektEditor(fetcher, ArchidektAuth(fetcher, self._token_store).access_token(user))

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
            pool=_pool_json(card),
        )
        return view


def _pool_json(card: BoardCard) -> Json | None:
    """For an alike or tagged idea, how many of its pool's decks play it, the pool's size and the plain share;
    None for any other card and for one the board holds no pool numbers for (the next sync adds them)."""
    if card.origin_pool is None:
        return None
    pool = card.origin_pool
    return {"count": pool.count, "total": pool.total, "share": round(pool.share, 4)}


def _land_options(db: CardDb, deck: Deck, folder: Path) -> tuple[list[LandOption], int | None]:
    """Lands the suggestion may name, and the deck's working budget (ADR 0024, 0025).

    Options are in the commander's identity, neither in the deck nor turned down on its board, and within the
    deck budget's ceiling.
    """
    main = db.resolve(deck.main_cards)
    identity = deck_identity(main) or ""
    budget = resolved_budget(deck.bracket, main, read_profile(folder).max_card_cents)
    skip = {card.name.casefold() for card in deck.cards} | {
        name.casefold() for name in read_board(folder).rejected_names
    }
    options = [
        LandOption(mana, cents)
        for mana, cents in db.lands(identity, max_cents=budget.ceiling)
        if mana.name.casefold() not in skip
    ]
    return options, budget.line


def _image_url(db: CardDb, info: CardInfo) -> str | None:
    printings = db.printings_of(info.oracle_id)
    regular = [p for p in printings if p.regular] or printings
    if not regular:
        return None
    return image_url(regular[-1].scryfall_id)


def _group_json(group: GridGroup, colours: Mapping[str, str], shares: ShareTable) -> Json:
    """One grid group; a label the account keeps shows in the account's colour (ADR 0012). The commander
    is in every list by definition, so it carries no share."""
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
                "share": None if group.name == COMMANDER_CATEGORY else _share_json(shares.of(c.name)),
            }
            for c in group.cards
        ],
    }


def _share_json(share: WeightedShare | None) -> Json | None:
    """A card's weighted share, its raw count and each source's part, with the page's wording; None when no
    list was read."""
    if share is None:
        return None
    return {
        **_source_json(share),
        "label": share_label(share),
        "tip": share_tip(share),
        "meta": _source_json(share.meta) if share.meta else None,
        "archidekt": _source_json(share.archidekt) if share.archidekt else None,
    }


def _source_json(part: SourceShare | WeightedShare) -> Json:
    return {"share": round(part.share, 4), "count": part.count, "total": part.total}


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
