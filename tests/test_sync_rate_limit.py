"""The owner's deck pull when Archidekt answers 429 (ADR 0048), on a fake clock that only sleeping moves."""

import threading
from pathlib import Path

import httpx

from commander.archidekt import RATE_LIMIT_PAUSE_SECONDS, ArchidektClient
from commander.net import Fetcher, Pacer
from commander.store import Workspace
from commander.sync import SyncStatus, sync_decks
from conftest import FakeArchidekt, Json

FIRST_ID = 500
DECKS = 11
# Archidekt lifted its limit 5 to 16 seconds after the first 429 (ADR 0048).
LIFTS_AFTER_SECONDS = 16.0


class Clock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._now = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(seconds)
            self._now += seconds


class Limiting:
    """Archidekt with `DECKS` decks whose deck pages answer 429, from the first request of a deck page, until
    `lifts_after` seconds have passed (None: for good)."""

    def __init__(self, raw_deck: Json, clock: Clock, *, lifts_after: float | None) -> None:
        self.fake = FakeArchidekt()
        for offset in range(DECKS):
            self.fake.put({**raw_deck, "id": FIRST_ID + offset, "name": f"Deck {offset}"})
        self.clock = clock
        self.lifts_after = lifts_after
        self._lock = threading.Lock()
        self._first: float | None = None
        self.deck_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.startswith("/api/decks/") or request.url.path == "/api/decks/v3/":
            return self.fake.handler(request)
        with self._lock:
            self.deck_requests += 1
            now = self.clock.now()
            self._first = now if self._first is None else self._first
            if self.lifts_after is None or now < self._first + self.lifts_after:
                return httpx.Response(429)
        return self.fake.handler(request)

    def pull(self, tmp_path: Path) -> dict[int, SyncStatus]:
        fetcher_clock = self.clock
        with httpx.Client(transport=httpx.MockTransport(self.handler)) as http:
            client = ArchidektClient(
                Fetcher(http, sleep=fetcher_clock.sleep, jitter=lambda: 1.0),
                pacer=Pacer(0.0, sleep=fetcher_clock.sleep, monotonic=fetcher_clock.now),
            )
            results = sync_decks(client, Workspace(tmp_path / "decks"), "TESTER")
        return {result.deck_id: result.status for result in results}


def test_a_pull_that_meets_429s_waits_a_minute_and_then_reads_every_deck(
    tmp_path: Path, raw_deck: Json
) -> None:
    clock = Clock()
    limiting = Limiting(raw_deck, clock, lifts_after=LIFTS_AFTER_SECONDS)
    statuses = limiting.pull(tmp_path)
    assert statuses == {FIRST_ID + n: SyncStatus.NEW for n in range(DECKS)}
    assert RATE_LIMIT_PAUSE_SECONDS in clock.sleeps


def test_a_pull_that_stays_limited_fails_its_decks_after_one_pause_without_a_request_storm(
    tmp_path: Path, raw_deck: Json
) -> None:
    clock = Clock()
    limiting = Limiting(raw_deck, clock, lifts_after=None)
    statuses = limiting.pull(tmp_path)
    assert set(statuses.values()) == {SyncStatus.FAILED}
    assert len(statuses) == DECKS
    # Four reads in flight at the start, each tried once more after the pause; the rest fail unsent.
    assert limiting.deck_requests <= 8
    assert set(clock.sleeps) == {RATE_LIMIT_PAUSE_SECONDS}
    assert len(clock.sleeps) <= 2


def test_a_pull_that_meets_no_429_never_waits(tmp_path: Path, raw_deck: Json) -> None:
    clock = Clock()
    limiting = Limiting(raw_deck, clock, lifts_after=0.0)
    statuses = limiting.pull(tmp_path)
    assert statuses == {FIRST_ID + n: SyncStatus.NEW for n in range(DECKS)}
    assert clock.sleeps == []
