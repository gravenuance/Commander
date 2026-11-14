"""The MCP background jobs: sync and meta start at once, run one at a time and are polled by id."""

import copy
import threading
from pathlib import Path
from typing import cast

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from commander.board import Origin, read_board
from commander.mcp import jobs as jobs_module
from commander.mcp.jobs import Job, JobBoard, JobKind, JobState, JobView
from commander.mcp.runner import Runner
from conftest import DECK_ID, FakeArchidekt, Json, deck_page
from mcp_setup import FOLDER, Tools, make_session, prepare_home, sync_decks
from test_cli_apply import NOW
from test_cli_meta import PEER_ID, kinnan_events, magda_events, peer_row

STAMP = NOW.isoformat(timespec="seconds")


class Gate:
    """A client factory that holds a command at its first connection until the test lets it go."""

    def __init__(self, fake: FakeArchidekt) -> None:
        self._fake = fake
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self) -> httpx.Client:
        self.entered.set()
        assert self.release.wait(30), "the test never released the command"
        return self._fake.client()


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    archidekt.peer_page = deck_page([peer_row(DECK_ID, "TESTER", 900), peer_row(PEER_ID, "someone", 600)])
    archidekt.topdeck.events = [*magda_events(), *kinnan_events()]
    return archidekt


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path) -> Path:
    prepare_home(tmp_path, card_db_file)
    return tmp_path


def sync_then_add_a_peer(home: Path, fake: FakeArchidekt, raw_deck: Json) -> None:
    """The user's deck synced, then another player's deck that the meta report reads."""
    sync_decks(home, fake)
    peer = copy.deepcopy(raw_deck)
    peer["id"] = PEER_ID
    peer["owner"] = {"id": 5, "username": "someone"}
    fake.put(peer)


def finished(tools: Tools, view: JobView) -> JobView:
    job = tools.session.job(view["id"])
    assert job.finished.wait(30), "the job did not end"
    return tools.session.jobs.view(job)


def test_a_sync_job_starts_at_once_and_reports_its_result_when_done(home: Path, fake: FakeArchidekt) -> None:
    gate = Gate(fake)
    tools = Tools.over(make_session(home, fake, client_factory=gate))

    started = tools.jobs.start_sync(no_cards=True, no_ideas=True)

    assert started["kind"] == "sync"
    assert started["state"] in {"queued", "running"}
    assert started["startedAt"] == STAMP
    assert gate.entered.wait(30)
    running = tools.session.jobs.view(tools.session.job(started["id"]))
    assert (running["state"], running["result"], running["finishedAt"]) == ("running", None, None)

    gate.release.set()
    done = finished(tools, started)

    assert (done["state"], done["exitCode"], done["error"], done["finishedAt"]) == ("done", 0, None, STAMP)
    result = cast("Json", done["result"])
    assert [(d["id"], d["status"]) for d in result["decks"]] == [(DECK_ID, "new")]
    assert (home / "decks" / FOLDER / "deck.json").is_file()
    # What `cmdr sync` prints on a terminal, which the job's stderr stands in for.
    assert done["progress"] == ["found 1 Commander decks for TESTER", "fetched Sample, deck.."]


def test_a_second_job_and_every_other_command_are_refused_while_one_runs(
    home: Path, fake: FakeArchidekt
) -> None:
    sync_decks(home, fake)
    gate = Gate(fake)
    tools = Tools.over(make_session(home, fake, client_factory=gate))
    first = tools.jobs.start_sync(dry_run=True, no_cards=True, no_ideas=True)
    assert gate.entered.wait(30)
    busy = f"busy: the sync job {first['id']} is running and holds every deck file"

    with pytest.raises(ToolError, match=f"the sync job {first['id']} is still running"):
        tools.jobs.start_meta("magda")
    with pytest.raises(ToolError, match=f"the sync job {first['id']} is still running"):
        tools.local.meta_to_board("magda")
    with pytest.raises(ToolError, match=busy):
        tools.read.list_decks()
    with pytest.raises(ToolError, match=busy):
        tools.local.board_add("magda", "Arcane Signet")
    with pytest.raises(ToolError, match=busy):
        tools.remote.apply_save('deck = "magda"\n')
    assert tools.session.jobs.view(tools.session.job(first["id"]))["state"] == "running"
    assert read_board(home / "decks" / FOLDER).cards == ()

    gate.release.set()
    assert finished(tools, first)["state"] == "done"
    second = tools.jobs.start_sync(dry_run=True, no_cards=True, no_ideas=True)
    assert second["id"] != first["id"]
    assert finished(tools, second)["state"] == "done"
    assert tools.read.list_decks()["warnings"] == []


def test_an_unknown_job_id_is_a_tool_error(home: Path, fake: FakeArchidekt) -> None:
    tools = Tools.over(make_session(home, fake))

    with pytest.raises(ToolError, match="no job 'nope'; start_sync and start_meta return the id"):
        tools.session.job("nope")


def test_a_job_whose_command_fails_ends_failed_with_the_cmdr_message(home: Path, fake: FakeArchidekt) -> None:
    sync_decks(home, fake)
    tools = Tools.over(make_session(home, fake))

    done = finished(tools, tools.jobs.start_meta("nonesuch"))

    assert (done["state"], done["exitCode"], done["result"]) == ("failed", 2, None)
    assert done["error"] == "cmdr: no deck matches 'nonesuch'; `cmdr list` shows them all"
    assert done["warnings"] == ["cmdr: no deck matches 'nonesuch'; `cmdr list` shows them all"]


def test_a_meta_job_answers_the_report(home: Path, fake: FakeArchidekt, raw_deck: Json) -> None:
    sync_then_add_a_peer(home, fake, raw_deck)
    tools = Tools.over(make_session(home, fake))

    done = finished(tools, tools.jobs.start_meta("magda", ["Mind Stone"]))

    assert (done["state"], done["exitCode"], done["warnings"]) == ("done", 0, [])
    result = cast("Json", done["result"])
    assert [r["deck"] for r in result["reports"]] == ["Magda"]
    assert [(c["name"], c["inDeck"], c["listsWith"], c["lists"]) for c in result["cards"]] == [
        ("Mind Stone", False, 5, 6)
    ]
    assert result["credit"].startswith("Tournament data from TopDeck.gg")


def test_meta_to_board_is_a_job_that_fills_the_board(home: Path, fake: FakeArchidekt, raw_deck: Json) -> None:
    sync_then_add_a_peer(home, fake, raw_deck)
    tools = Tools.over(make_session(home, fake))

    done = finished(tools, tools.local.meta_to_board("magda"))

    assert done["state"] == "done"
    assert "Magda board: +1 from meta" in done["progress"]
    card = read_board(home / "decks" / FOLDER).find("Mind Stone")
    assert card is not None
    assert card.origin is Origin.META


def test_the_progress_lines_are_kept_apart_from_the_warnings_and_cleaned() -> None:
    board = JobBoard(Runner(), lambda: NOW)
    job = Job("abc", JobKind.SYNC, [], STAMP)
    job.sink.write("fetched 1 of 2\n\ncmdr: card data is old\nWARNING: slow\n\x1b[31mdone\x00 1 of 2\n")

    view = board.view(job)

    assert view["progress"] == ["fetched 1 of 2", "[31mdone 1 of 2"]
    assert view["warnings"] == ["cmdr: card data is old", "WARNING: slow"]
    assert view["state"] == JobState.QUEUED.value


def test_only_the_latest_progress_lines_are_shown() -> None:
    board = JobBoard(Runner(), lambda: NOW)
    job = Job("abc", JobKind.SYNC, [], STAMP)
    job.sink.write("".join(f"step {n}\n" for n in range(1, 31)))

    progress = board.view(job)["progress"]

    assert (len(progress), progress[0], progress[-1]) == (20, "step 11", "step 30")


def test_finished_jobs_are_forgotten_oldest_first(
    home: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs_module, "MAX_JOBS_KEPT", 2)
    sync_decks(home, fake)
    tools = Tools.over(make_session(home, fake))

    ids = [
        finished(tools, tools.jobs.start_sync(dry_run=True, no_cards=True, no_ideas=True))["id"]
        for _ in range(4)
    ]

    with pytest.raises(ToolError, match=f"no job '{ids[0]}'"):
        tools.session.job(ids[0])
    assert [tools.session.job(job_id).id for job_id in ids[-2:]] == ids[-2:]
