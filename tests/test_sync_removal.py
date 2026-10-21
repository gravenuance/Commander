"""A deck deleted on Archidekt loses its local folder only when Archidekt confirms it is gone (ADR 0045)."""

import logging
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektClient, parse_deck
from commander.net import Fetcher, NetworkError
from commander.store import SNAPSHOT_FILE, StoredDeck, Workspace
from commander.sync import PullWatch, SyncResult, SyncStatus, sync_decks
from conftest import ACCESS_TOKEN, DECK_ID, FakeArchidekt, Json

GONE_ID = 222
GONE_FOLDER = "222-other"
SYNC_LOG = "commander.sync"


def fake_with_two_decks(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": GONE_ID, "name": "Other"})
    return fake


def pull(
    root: Path,
    fake: FakeArchidekt,
    *,
    signed_in: bool = True,
    dry_run: bool = False,
    watch: PullWatch = PullWatch(),  # noqa: B008 - frozen
    handler: Callable[[httpx.Request], httpx.Response] | None = None,
    workspace: Workspace | None = None,
) -> list[SyncResult]:
    """One sync_decks over the fake, as the signed-in owner unless told otherwise."""
    with httpx.Client(transport=httpx.MockTransport(handler or fake.handler)) as http:
        client = ArchidektClient(
            Fetcher(http, sleep=lambda _: None), token=ACCESS_TOKEN if signed_in else None
        )
        return sync_decks(
            client, workspace or Workspace(root / "decks"), "MAKKIS", dry_run=dry_run, watch=watch
        )


def by_id(results: list[SyncResult]) -> dict[int, SyncStatus]:
    return {r.deck_id: r.status for r in results}


def synced_then_deleted_upstream(root: Path, raw_deck: Json) -> FakeArchidekt:
    """Both decks pulled into root/decks, then GONE_ID deleted on Archidekt (its page answers 404)."""
    fake = fake_with_two_decks(raw_deck)
    pull(root, fake)
    del fake.decks[GONE_ID]
    fake.requests.clear()
    return fake


def folder(root: Path, name: str = GONE_FOLDER) -> Path:
    return root / "decks" / name


def test_a_deck_deleted_on_archidekt_loses_its_folder_and_is_reported_removed(
    tmp_path: Path, raw_deck: Json, caplog: pytest.LogCaptureFixture
) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)

    with caplog.at_level(logging.INFO, logger=SYNC_LOG):
        results = pull(tmp_path, fake)

    assert by_id(results) == {DECK_ID: SyncStatus.UNCHANGED, GONE_ID: SyncStatus.REMOVED}
    assert not folder(tmp_path).exists()
    assert (folder(tmp_path, "10004639-honestly-though") / SNAPSHOT_FILE).is_file()
    assert [r.name for r in results if r.status is SyncStatus.REMOVED] == ["Other"]
    assert f"/api/decks/{GONE_ID}/" in fake.requests
    assert [m for m in caplog.messages if "removed" in m] == [
        f"deck {GONE_ID} (Other) was deleted on Archidekt; removed its folder {GONE_FOLDER}"
    ]


def test_the_removal_runs_under_the_decks_lock_and_tells_the_caller(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    events: list[str] = []

    @contextmanager
    def lock_of(deck_id: int) -> Generator[None]:
        events.append(f"lock {deck_id} (folder there: {folder(tmp_path).is_dir()})")
        yield
        events.append(f"unlock {deck_id} (folder there: {folder(tmp_path).is_dir()})")

    def deck_lock(deck_id: int, /) -> AbstractContextManager[object]:
        return lock_of(deck_id)

    def on_removed(deck_id: int) -> None:
        events.append(f"removed {deck_id}")

    pull(tmp_path, fake, watch=PullWatch(deck_lock=deck_lock, on_removed=on_removed))

    assert events == [
        f"lock {GONE_ID} (folder there: True)",
        f"unlock {GONE_ID} (folder there: False)",
        f"removed {GONE_ID}",
    ]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503])
def test_a_deck_missing_from_the_listing_is_kept_when_its_page_answers_anything_but_404(
    tmp_path: Path, raw_deck: Json, status: int
) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    fake.failing[GONE_ID] = status

    results = pull(tmp_path, fake)

    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()
    [kept] = [r for r in results if r.deck_id == GONE_ID]
    assert kept.error is not None
    assert "not removed" in kept.error


def test_a_deck_missing_from_the_listing_is_kept_when_reading_its_page_fails_on_the_network(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/decks/{GONE_ID}/":
            raise httpx.ConnectError("no route", request=request)
        return fake.handler(request)

    results = pull(tmp_path, fake, handler=handler)

    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()


def test_a_deck_missing_from_the_listing_but_still_readable_is_kept(tmp_path: Path, raw_deck: Json) -> None:
    fake = fake_with_two_decks(raw_deck)
    pull(tmp_path, fake)

    def handler(request: httpx.Request) -> httpx.Response:
        listing = fake.handler(request)
        if request.url.path != "/api/decks/v3/":
            return listing
        page: Json = listing.json()
        page["results"] = [row for row in page["results"] if row["id"] != GONE_ID]
        return httpx.Response(200, json=page)

    results = pull(tmp_path, fake, handler=handler)

    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()


def test_an_anonymous_listing_after_a_refused_login_deletes_nothing(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    fake.signed_in_listing_status = 403
    warnings: list[str] = []

    with httpx.Client(transport=httpx.MockTransport(fake.handler)) as http:
        client = ArchidektClient(Fetcher(http, sleep=lambda _: None), token=ACCESS_TOKEN)
        results = sync_decks(client, Workspace(tmp_path / "decks"), "MAKKIS", warn=warnings.append)

    assert warnings == ["Archidekt refused the login: private and unlisted decks were not updated"]
    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()
    assert f"/api/decks/{GONE_ID}/" not in fake.requests


def test_a_signed_in_listing_that_fails_partway_falls_back_and_deletes_nothing(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    fake.put({**raw_deck, "id": 333, "name": "Third"})
    fake.listing_page_size = 1
    fake.signed_in_pages = 1

    results = pull(tmp_path, fake)

    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()


def test_a_listing_that_cannot_be_read_deletes_nothing(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/decks/v3/":
            return httpx.Response(500)
        return fake.handler(request)

    with pytest.raises(NetworkError):
        pull(tmp_path, fake, handler=handler)

    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()


def test_without_a_login_a_missing_deck_is_only_reported(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)

    results = pull(tmp_path, fake, signed_in=False)

    assert by_id(results) == {DECK_ID: SyncStatus.UNCHANGED, GONE_ID: SyncStatus.MISSING_UPSTREAM}
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()
    assert f"/api/decks/{GONE_ID}/" not in fake.requests


def test_a_dry_run_says_what_it_would_remove_and_removes_nothing(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    removed: list[int] = []

    results = pull(tmp_path, fake, dry_run=True, watch=PullWatch(on_removed=removed.append))

    assert by_id(results)[GONE_ID] is SyncStatus.WOULD_REMOVE
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()
    assert removed == []


class LyingWorkspace(Workspace):
    """A workspace whose folder lookup points outside its root, as a link or a damaged name could."""

    def __init__(self, root: Path, outside: Path) -> None:
        super().__init__(root)
        self._outside = outside

    def folder_for(self, deck_id: int) -> Path | None:
        return self._outside if deck_id == GONE_ID else super().folder_for(deck_id)

    def load_all(self) -> list[StoredDeck]:
        return [
            StoredDeck(self._outside if s.deck.id == GONE_ID else s.folder, s.deck)
            for s in super().load_all()
        ]


def test_a_folder_outside_the_workspace_is_never_removed(tmp_path: Path, raw_deck: Json) -> None:
    fake = synced_then_deleted_upstream(tmp_path, raw_deck)
    outside = tmp_path / "elsewhere" / GONE_FOLDER
    outside.mkdir(parents=True)
    (outside / "precious.txt").write_text("keep me", encoding="utf-8")
    workspace = LyingWorkspace(tmp_path / "decks", outside)

    results = pull(tmp_path, fake, workspace=workspace)

    [result] = [r for r in results if r.deck_id == GONE_ID]
    assert result.status is SyncStatus.FAILED
    assert result.error is not None
    assert "outside" in result.error
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "keep me"
    assert (folder(tmp_path) / SNAPSHOT_FILE).is_file()


def test_a_folder_of_someone_elses_deck_is_not_removed(tmp_path: Path, raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    # A deck folder of another owner that the sync did not write itself, e.g. copied in by hand.
    stranger = replace(parse_deck(raw_deck), id=GONE_ID, name="Other", owner="Somebody")
    stranger_folder = Workspace(tmp_path / "decks").save(stranger)

    results = pull(tmp_path, fake)

    assert by_id(results)[GONE_ID] is SyncStatus.MISSING_UPSTREAM
    assert (stranger_folder / SNAPSHOT_FILE).is_file()
    assert f"/api/decks/{GONE_ID}/" not in fake.requests
