import json
import threading
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektClient
from commander.net import Fetcher
from commander.store import Workspace
from commander.sync import SyncStatus, sync_decks
from conftest import DECK_ID, FakeArchidekt, Json

OTHER_ID = 222


class BrokenBody(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b'{"id": '
        raise httpx.DecodingError("bad gzip data")


def test_a_deck_whose_answer_cannot_be_decoded_fails_alone(tmp_path: Path, raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": OTHER_ID, "name": "Other"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/decks/{DECK_ID}/":
            return httpx.Response(200, stream=BrokenBody())
        return fake.handler(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        results = sync_decks(
            ArchidektClient(Fetcher(client, sleep=lambda _: None)), Workspace(tmp_path / "decks"), "MAKKIS"
        )

    assert {r.deck_id: r.status for r in results} == {
        DECK_ID: SyncStatus.FAILED,
        OTHER_ID: SyncStatus.NEW,
    }
    assert (tmp_path / "decks" / "222-other").is_dir()


def owned_by(fake: FakeArchidekt, owners: dict[int, str]) -> httpx.Client:
    """A client whose deck answers name the given owner for those decks, as a mislisted deck would."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        for deck_id, owner in owners.items():
            if request.url.path == f"/api/decks/{deck_id}/":
                return httpx.Response(
                    200, json={**json.loads(response.content), "owner": {"username": owner}}
                )
        return response

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_deck_owned_by_someone_else_is_skipped_and_reported(tmp_path: Path, raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": OTHER_ID, "name": "Other"})

    with owned_by(fake, {DECK_ID: "Somebody", OTHER_ID: "makkis"}) as client:
        results = sync_decks(
            ArchidektClient(Fetcher(client, sleep=lambda _: None)), Workspace(tmp_path / "decks"), "MAKKIS"
        )

    by_id = {r.deck_id: r for r in results}
    assert by_id[DECK_ID].status is SyncStatus.FAILED
    assert by_id[DECK_ID].error == "belongs to Somebody, not MAKKIS; skipped"
    # The owner's name is compared without regard to case, as Archidekt does.
    assert by_id[OTHER_ID].status is SyncStatus.NEW
    assert [p.name for p in (tmp_path / "decks").iterdir()] == ["222-other"]


def test_an_interrupted_sync_leaves_no_thread_that_keeps_the_process_alive(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": OTHER_ID, "name": "Other"})
    release = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/decks/{OTHER_ID}/":
            release.wait(timeout=30)
        return fake.handler(request)

    def on_progress(message: str) -> None:
        # Ctrl-C once the first deck is in, while the second fetch is still blocked.
        if message.startswith("fetched"):
            raise KeyboardInterrupt

    before = set(threading.enumerate())
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(KeyboardInterrupt):
            sync_decks(
                ArchidektClient(Fetcher(client, sleep=lambda _: None)),
                Workspace(tmp_path / "decks"),
                "MAKKIS",
                on_progress=on_progress,
            )
        started = set(threading.enumerate()) - before
        assert started
        assert all(thread.daemon for thread in started)
    finally:
        release.set()


def test_an_interrupted_sync_cancels_the_fetches_still_queued(tmp_path: Path, raw_deck: Json) -> None:
    # Seven decks against four fetch threads: the first deck answers at once, the next five block, and the
    # last two wait in the queue.
    ids = [DECK_ID, *range(301, 307)]
    fake = FakeArchidekt()
    for deck_id in ids:
        fake.put({**raw_deck, "id": deck_id, "name": f"Deck {deck_id}"})
    release = threading.Event()
    fetched: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/api/decks/") and request.url.path != "/api/decks/v3/":
            deck_id = int(request.url.path.removeprefix("/api/decks/").rstrip("/"))
            fetched.append(deck_id)
            if deck_id != DECK_ID:
                release.wait(timeout=30)
        return fake.handler(request)

    def on_progress(message: str) -> None:
        if message.startswith("fetched"):
            raise KeyboardInterrupt

    before = set(threading.enumerate())
    # The client stays open until the fetch threads are done, or a fetch left running would fail on it.
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        try:
            with pytest.raises(KeyboardInterrupt):
                sync_decks(
                    ArchidektClient(Fetcher(client, sleep=lambda _: None)),
                    Workspace(tmp_path / "decks"),
                    "MAKKIS",
                    on_progress=on_progress,
                )
        finally:
            release.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(timeout=30)

    # The worker that finished the first deck may take the fifth before the cancel; nothing later runs.
    assert set(fetched) <= set(ids[:5])
