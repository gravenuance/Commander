"""The write tools: local ones change boards and profiles; remote ones change the user's decks on Archidekt.

Board changes are logged as made by `claude`, as the activity log and the app show who acted. Each tool's
docstring is its description."""

from typing import Literal

from mcp.server.mcpserver import MCPServer

from commander.mcp.argv import suggest_options
from commander.mcp.hints import (
    WRITES_ARCHIDEKT,
    WRITES_LOCAL,
    WRITES_LOCAL_AFTER_ASKING_THE_SITES,
    register_tools,
)
from commander.mcp.jobs import JobKind, JobView
from commander.mcp.session import Session, ToolResult
from commander.mcp.text import ArgumentError, flag, free_text, name_arg, plan_text

MAX_COPIES = 99
MAX_REPLACEMENTS = 10
_BY = "--by=claude"


def _replacements(replace: list[str] | None) -> list[str]:
    """`--replace=CARD=REASON` for each deck card the board card could replace."""
    entries = replace or []
    if len(entries) > MAX_REPLACEMENTS:
        raise ArgumentError(f"give at most {MAX_REPLACEMENTS} cards to replace")
    return [flag("replace", free_text(entry, "replace entry")) for entry in entries]


class LocalWriteTools:
    """What changes the local boards and profiles, and nothing on Archidekt."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, server: MCPServer) -> None:
        register_tools(
            server,
            WRITES_LOCAL,
            self.board_add,
            self.board_research,
            self.board_decide,
            self.board_count,
            self.board_remove,
            self.profile_init,
        )
        register_tools(server, WRITES_LOCAL_AFTER_ASKING_THE_SITES, self.suggest_to_board, self.meta_to_board)

    def board_add(
        self, deck: str, card: str, reason: str = "", replace: list[str] | None = None
    ) -> ToolResult:
        """Put a card on a deck's board to research, with why it might fit and the deck cards it could
        replace (replace entries are 'Card name=why'). The user decides; this only proposes."""
        argv = ["board", "add", _BY, flag("reason", free_text(reason, "reason")), *_replacements(replace)]
        return self._session.text_call([*argv, name_arg(deck, "deck"), name_arg(card, "card")])

    def board_research(
        self,
        deck: str,
        card: str,
        reason: str | None = None,
        replace: list[str] | None = None,
        *,
        clear_replace: bool = False,
    ) -> ToolResult:
        """Record research on a card already on the board: a new reason, or new replace entries that
        take the place of the old ones. clear_replace drops every card to replace instead."""
        if clear_replace and replace:
            raise ArgumentError("give replace entries or clear_replace, not both")
        argv = ["board", "research", _BY]
        if reason is not None:
            argv.append(flag("reason", free_text(reason, "reason")))
        if clear_replace:
            argv.append("--clear-replace")
        argv += _replacements(replace)
        return self._session.text_call([*argv, name_arg(deck, "deck"), name_arg(card, "card")])

    def board_decide(
        self,
        deck: str,
        card: str,
        decision: Literal["add", "reject", "reopen"],
        *,
        cut: str | None = None,
        category: str | None = None,
        note: str = "",
    ) -> ToolResult:
        """Record the USER's decision on a board card: add it (cut names the deck card that makes room;
        category files it), reject it, or reopen it. Only after the user has said which."""
        argv = ["board", "decide", _BY, flag("note", free_text(note, "note"))]
        if cut is not None:
            argv.append(flag("cut", name_arg(cut, "cut")))
        if category is not None:
            argv.append(flag("category", name_arg(category, "category")))
        return self._session.text_call([*argv, name_arg(deck, "deck"), name_arg(card, "card"), decision])

    def board_count(self, deck: str, card: str, copies: int, category: str | None = None) -> ToolResult:
        """Queue how many copies of a deck card the deck should hold (0 takes it out). With category, 0 takes
        the card off that category outside the deck, such as Maybeboard."""
        if not 0 <= copies <= MAX_COPIES:
            raise ArgumentError(f"copies must be 0 to {MAX_COPIES}")
        argv = ["board", "count", _BY]
        if category is not None:
            argv.append(flag("category", name_arg(category, "category")))
        return self._session.text_call([*argv, name_arg(deck, "deck"), name_arg(card, "card"), str(copies)])

    def board_remove(self, deck: str, card: str) -> ToolResult:
        """Take a card off a deck's board."""
        return self._session.text_call(
            ["board", "remove", _BY, name_arg(deck, "deck"), name_arg(card, "card")]
        )

    def suggest_to_board(
        self,
        deck: str,
        count: int | None = None,
        max_price_eur: float | None = None,
        *,
        any_price: bool = False,
        within_bracket: bool = False,
        refresh: bool = False,
    ) -> ToolResult:
        """Run suggest for one deck and put its ideas on the deck's board under considering; answers the
        ideas as suggest does."""
        options = suggest_options(
            count=count,
            max_price_eur=max_price_eur,
            any_price=any_price,
            within_bracket=within_bracket,
            refresh=refresh,
        )
        return self._session.json_call(["suggest", "--json", "--to-board", *options, name_arg(deck, "deck")])

    def meta_to_board(self, deck: str, *, refresh: bool = False) -> JobView:
        """Put the staples a deck is missing, from tournament lists and recent peer decks, on its board.
        Takes minutes on a cold cache, so it is a background job: it returns a job id at once, and
        job_status reports the progress and the report. One sync or meta job runs at a time."""
        options = ["--refresh"] if refresh else []
        argv = ["meta", "--json", "--to-board", *options, name_arg(deck, "deck")]
        return self._session.start_job(JobKind.META, argv)

    def profile_init(self, deck: str) -> ToolResult:
        """Write a commented profile.toml for a deck to fill in (the file is the user's)."""
        return self._session.text_call(["profile", "--init", name_arg(deck, "deck")])


class RemoteWriteTools:
    """What changes the user's real decks on Archidekt (each snapshots or labels there at once)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, server: MCPServer) -> None:
        register_tools(server, WRITES_ARCHIDEKT, self.board_commit_save, self.apply_save, self.toget_mark)

    def board_commit_save(self, deck: str) -> ToolResult:
        """SAVES a deck's queued board to the user's real deck on Archidekt, after a snapshot there, then
        updates the local copy. Only on the user's say-so, after board_commit_preview. A failed read-back
        check is an error that still lists what was saved."""
        return self._session.text_call(["board", "commit", "--save", name_arg(deck, "deck")])

    def apply_save(self, plan_toml: str) -> ToolResult:
        """SAVES a plan's changes to the user's real deck on Archidekt, after a snapshot there, then reads
        the deck back. Preview it with apply_preview first, and save only on the user's say-so. plan_toml is
        the plan file's text."""
        return self._session.json_call(["apply", "--json", "--save", "-"], stdin=plan_text(plan_toml))

    def toget_mark(self, deck: str, card: str, step: Literal["ordered", "arrived"]) -> ToolResult:
        """Label a card of the user's real deck on Archidekt as ordered (label Getting) or arrived (no
        label). Only on the user's say-so."""
        return self._session.text_call(["toget", f"--{step}", name_arg(deck, "deck"), name_arg(card, "card")])
