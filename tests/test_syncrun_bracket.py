"""A deck with no bracket on Archidekt is read as its estimate's lowest bracket, and one with a bracket as
it is (ADR 0051): which brackets the sync searches, and whether it reads tournament lists."""

import copy
import shutil
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from commander import syncrun
from commander.archidekt import parse_deck
from commander.board import BoardCard, Origin
from commander.bracket import estimate_cache, estimate_deck
from commander.carddb import CARD_DB_FILE
from commander.config import MetaSettings
from commander.ideas import gather_idea_pools
from commander.metadata import MetaSources, start_gather_for, start_tournament_refresh
from commander.net import Fetcher
from commander.spellbook import SpellbookClient
from commander.store import StoredDeck, Workspace
from commander.syncrun import SyncOptions, SyncServices, run_sync
from conftest import SPELLBOOK_API, FakeArchidekt, FakeSpellbook, Json, MemoryTokenStore
from test_metadata import MAGDA, Rig
from test_syncrun import NOW, judge_nothing


@dataclass(slots=True)
class Run:
    rig: Rig
    spellbook: FakeSpellbook
    # The bracket each deck had when its ideas were compiled.
    compiled: list[int | None]
    cache: Path


def sync_one(
    tmp_path: Path,
    raw_deck: Json,
    card_db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bracket: int | None,
    answer: str | None = "R",
    seed: bool = False,
) -> Run:
    """A sync of one Magda deck whose bracket is `bracket`, with Spellbook estimating `answer` (down for
    None); with `seed` the estimate is in the cache beforehand and Spellbook is down."""
    raw = copy.deepcopy(raw_deck)
    raw.pop("edhBracket", None)
    if bracket is not None:
        raw["edhBracket"] = bracket
    fake = FakeArchidekt()
    fake.put(raw)
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    spellbook = FakeSpellbook()
    if answer is None:
        spellbook.status = 403
    else:
        spellbook.answer["bracketTag"] = answer
    if seed:
        with spellbook.client() as http:
            estimate_deck(
                parse_deck(raw),
                SpellbookClient(Fetcher(http, max_attempts=1)),
                estimate_cache(cache / "spellbook"),
                now=lambda: NOW - timedelta(days=1),
                refresh=False,
            )
        spellbook.bodies.clear()
        spellbook.status = 403
    rig = Rig(tmp_path)
    rig.archidekt.commander = MAGDA
    compiled: list[int | None] = []

    def routed(request: httpx.Request) -> httpx.Response:
        handler = spellbook.handler if str(request.url).startswith(SPELLBOOK_API) else fake.handler
        return handler(request)

    def real_sources(*_: object, **__: object) -> MetaSources:
        return rig.sources()

    def compile_ideas(stored: StoredDeck, *_: object, **__: object) -> dict[Origin, list[BoardCard]]:
        compiled.append(stored.deck.bracket)
        return {}

    monkeypatch.setattr(syncrun, "meta_sources", real_sources)
    monkeypatch.setattr(syncrun, "start_gather_for", start_gather_for)
    monkeypatch.setattr(syncrun, "start_tournament_refresh", start_tournament_refresh)
    monkeypatch.setattr(syncrun, "gather_idea_pools", gather_idea_pools)
    monkeypatch.setattr(syncrun, "compile_ideas", compile_ideas)
    monkeypatch.setattr(syncrun, "rejudge_ideas", judge_nothing)
    monkeypatch.setattr(syncrun, "relabel_legacy_ideas", judge_nothing)

    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(routed))

    run_sync(
        SyncServices(factory, MemoryTokenStore({}), cache, lambda: NOW),
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False),
        on_progress=lambda _: None,
    )
    return Run(rig, spellbook, compiled, cache)


def searched_brackets(run: Run) -> set[str | None]:
    return {search.get("edhBracket") for search in run.rig.archidekt.searches}


def test_an_unset_deck_estimated_ruthless_reads_tournament_lists_and_searches_brackets_4_and_5(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=None, answer="R")
    assert searched_brackets(run) == {"4", "5"}
    assert run.rig.topdeck.requests
    assert run.compiled == [4]
    assert len(run.spellbook.bodies) == 1


def test_an_unset_deck_estimated_powerful_searches_brackets_3_and_4_and_reads_no_tournament_lists(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=None, answer="P")
    assert searched_brackets(run) == {"3", "4"}
    assert run.rig.topdeck.requests == []
    assert run.compiled == [3]


def test_an_unset_deck_without_an_estimate_is_read_as_before(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=None, answer=None)
    assert searched_brackets(run) == {None}
    assert run.rig.topdeck.requests == []
    assert run.compiled == [None]


def test_an_unset_deck_estimated_banned_is_read_as_before(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=None, answer="B")
    assert searched_brackets(run) == {None}
    assert run.compiled == [None]


def test_a_cached_estimate_is_used_without_asking_spellbook(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=None, answer="R", seed=True)
    assert run.spellbook.bodies == []
    assert searched_brackets(run) == {"4", "5"}


def test_a_set_bracket_wins_over_the_estimate_and_asks_nothing(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = sync_one(tmp_path, raw_deck, card_db_file, monkeypatch, bracket=2, answer="R")
    assert searched_brackets(run) == {"2", "3"}
    assert run.rig.topdeck.requests == []
    assert run.spellbook.bodies == []
    assert run.compiled == [2]
