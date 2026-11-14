"""The background jobs: sync and meta take minutes, so they start at once and are polled by id.

Each tool's docstring is its description."""

from typing import Any

import anyio
from mcp.server.mcpserver import Context, MCPServer

from commander.mcp.argv import optional_deck
from commander.mcp.hints import READS, READS_THE_SITES, WRITES_LOCAL_FROM_THE_SITES, register_tools
from commander.mcp.jobs import JobKind, JobView
from commander.mcp.session import Session
from commander.mcp.text import ArgumentError, flag, name_arg

MAX_WAIT_SECONDS = 120
POLL_SECONDS = 2.0
MAX_CHECKED_CARDS = 20


class JobTools:
    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, server: MCPServer) -> None:
        register_tools(server, WRITES_LOCAL_FROM_THE_SITES, self.start_sync)
        register_tools(server, READS_THE_SITES, self.start_meta)
        register_tools(server, READS, self.job_status)

    def start_sync(
        self, *, dry_run: bool = False, force: bool = False, no_cards: bool = False, no_ideas: bool = False
    ) -> JobView:
        """Start a sync in the background: pull the user's decks from Archidekt, refresh the card data and
        compile each deck's considering ideas (minutes; the first of the day is the longest). Returns a job
        id at once; poll it with job_status. Only one sync or meta job runs at a time, and while it runs
        every other tool but job_status answers busy. dry_run reports changes without writing; force
        refetches every deck; no_ideas and no_cards skip those steps."""
        switches = (
            (dry_run, "--dry-run"),
            (force, "--force"),
            (no_cards, "--no-cards"),
            (no_ideas, "--no-ideas"),
        )
        argv = ["sync", "--json", *(name for on, name in switches if on)]
        return self._session.start_job(JobKind.SYNC, argv)

    def start_meta(
        self, deck: str | None = None, cards: list[str] | None = None, *, refresh: bool = False
    ) -> JobView:
        """Start the cEDH meta report in the background: what tournament lists and recent peer decks play
        with a deck's commander, the staples it misses and the meta's answers to its key cards (one deck;
        omit deck for every bracket 4 and 5 deck side by side). cards checks named cards (one deck only).
        Needs the user's TopDeck key. Minutes on a cold cache. Returns a job id at once; poll it with
        job_status. Writes only caches (meta_to_board also fills the board)."""
        names = cards or []
        if len(names) > MAX_CHECKED_CARDS:
            raise ArgumentError(f"check at most {MAX_CHECKED_CARDS} cards")
        argv = ["meta", "--json"]
        if refresh:
            argv.append("--refresh")
        argv += [flag("card", name_arg(name, "card")) for name in names]
        return self._session.start_job(JobKind.META, [*argv, *optional_deck(deck)])

    async def job_status(self, job_id: str, ctx: Context[Any, Any], wait_seconds: int = 0) -> JobView:
        """The state of a background job (queued, running, done, failed), its latest progress lines,
        warnings and, when finished, the command's result. wait_seconds (up to 120) holds the call until
        the job ends or the time is up, sending progress notifications meanwhile. Jobs are forgotten when
        the server restarts."""
        job = self._session.job(name_arg(job_id, "job_id"))
        deadline = anyio.current_time() + min(max(wait_seconds, 0), MAX_WAIT_SECONDS)
        ticks = 0
        shown: list[str] = []
        while True:
            finished = job.finished.is_set()
            view = self._session.jobs.view(job)
            if view["progress"] != shown:
                shown = view["progress"]
                ticks += 1
                await ctx.report_progress(ticks, None, shown[-1])
            remaining = deadline - anyio.current_time()
            if finished or remaining <= 0:
                return view
            await anyio.to_thread.run_sync(job.finished.wait, min(remaining, POLL_SECONDS))
