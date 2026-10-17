from collections.abc import Iterator
from pathlib import Path

import httpx

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
