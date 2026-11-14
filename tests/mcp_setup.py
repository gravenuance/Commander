"""What the MCP tests share: a synced home folder over the fake Archidekt, and a session over it."""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from commander.carddb import CARD_DB_FILE
from commander.mcp.runner import Services
from commander.mcp.session import Session, ToolResult
from commander.mcp.tools_jobs import JobTools
from commander.mcp.tools_read import ReadTools
from commander.mcp.tools_write import LocalWriteTools, RemoteWriteTools
from commander.net import Pacer
from conftest import FakeArchidekt, Json, sandbox_env
from test_cli_apply import NOW, run

FOLDER = "10000001-sample-deck"


@dataclass(slots=True, frozen=True)
class Tools:
    session: Session
    read: ReadTools
    local: LocalWriteTools
    remote: RemoteWriteTools
    jobs: JobTools

    @classmethod
    def over(cls, session: Session) -> "Tools":
        return cls(
            session,
            ReadTools(session),
            LocalWriteTools(session),
            RemoteWriteTools(session),
            JobTools(session),
        )


def environment(home: Path) -> dict[str, str]:
    return {
        **sandbox_env(home),
        "CMDR_CACHE_DIR": str(home / "cache"),
        "CMDR_USER": "TESTER",
        "CMDR_WORKSPACE": str(home / "decks"),
    }


def prepare_home(home: Path, card_db_file: Path) -> None:
    """The cache folder with the card database, as `cmdr sync` leaves it."""
    cache = home / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)


def sync_decks(home: Path, fake: FakeArchidekt) -> None:
    result = run(["sync", "--no-cards", "--no-ideas", "--workspace", str(home / "decks")], home, fake)
    assert result.code == 0, result.err
    fake.requests.clear()


def make_session(
    home: Path, fake: FakeArchidekt, *, client_factory: Callable[[], httpx.Client] | None = None
) -> Session:
    return Session(
        Services(
            env=environment(home),
            client_factory=client_factory or fake.client,
            now=lambda: NOW,
            pacer_factory=lambda _: Pacer(0),
        )
    )


def snapshot(folder: Path) -> dict[str, bytes]:
    """Every file under the folder, by relative path."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def rows(answer: ToolResult) -> list[Json]:
    """A tool answer whose data is a JSON list of objects."""
    return cast("list[Json]", answer["result"])


def document(answer: ToolResult) -> Json:
    """A tool answer whose data is one JSON object."""
    return cast("Json", answer["result"])


def text(answer: ToolResult) -> str:
    """A tool answer whose data is the command's text."""
    return cast("str", answer["result"])
