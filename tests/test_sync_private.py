"""Sync pulls the account's private and unlisted decks when it is signed in, and leaves them alone when not
(ADR 0040)."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from commander.archidekt import ArchidektClient, ArchidektError, parse_deck
from commander.config import MetaSettings
from commander.credentials import CredentialError
from commander.net import Fetcher, HttpStatusError
from commander.store import SNAPSHOT_FILE, Workspace
from commander.sync import SyncStatus
from commander.syncrun import SyncOptions, SyncOutcome, SyncServices, run_sync
from conftest import ACCESS_TOKEN, DECK_ID, REFRESH_TOKEN, FakeArchidekt, Json, MemoryTokenStore

PRIVATE_ID = 10000003
UNLISTED_ID = 10000004
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
NOT_SIGNED_IN = "Not signed in to Archidekt: private and unlisted decks were not updated"
REFUSED = "Archidekt refused the login: private and unlisted decks were not updated"
EXPIRED = (
    "Archidekt login not usable (the saved Archidekt login has expired): "
    "private and unlisted decks were not updated"
)
SIGNED_IN = f"JWT {ACCESS_TOKEN}"


class BrokenTokenStore(MemoryTokenStore):
    def get(self, user: str) -> str | None:
        raise CredentialError(f"the credential store is locked for {user}")


def archidekt_with_hidden_decks(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": PRIVATE_ID, "name": "Private test"})
    fake.put({**raw_deck, "id": UNLISTED_ID, "name": "Unlisted one"})
    fake.private.add(PRIVATE_ID)
    fake.unlisted.add(UNLISTED_ID)
    return fake


def sync(
    tmp_path: Path, fake: FakeArchidekt, tokens: MemoryTokenStore | None = None, *, dry_run: bool = False
) -> SyncOutcome:
    services = SyncServices(fake.client, tokens or MemoryTokenStore(), tmp_path / "cache", lambda: NOW)
    return run_sync(
        services,
        Workspace(tmp_path / "decks"),
        "TESTER",
        MetaSettings(),
        SyncOptions(cards=False, ideas=False, dry_run=dry_run),
        on_progress=lambda _: None,
    )


def signed_in() -> MemoryTokenStore:
    return MemoryTokenStore({"tester": REFRESH_TOKEN})


def statuses(outcome: SyncOutcome) -> dict[int, SyncStatus]:
    return {r.deck_id: r.status for r in outcome.results}


def snapshot_bytes(tmp_path: Path, folder: str) -> bytes:
    return (tmp_path / "decks" / folder / SNAPSHOT_FILE).read_bytes()


PRIVATE_FOLDER = f"{PRIVATE_ID}-private-test"
UNLISTED_FOLDER = f"{UNLISTED_ID}-unlisted-one"


def test_a_signed_in_sync_pulls_the_private_and_unlisted_decks(tmp_path: Path, raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)

    outcome = sync(tmp_path, fake, signed_in())

    assert statuses(outcome) == {
        DECK_ID: SyncStatus.NEW,
        PRIVATE_ID: SyncStatus.NEW,
        UNLISTED_ID: SyncStatus.NEW,
    }
    assert outcome.warnings == []
    assert (tmp_path / "decks" / PRIVATE_FOLDER / SNAPSHOT_FILE).is_file()
    assert (tmp_path / "decks" / UNLISTED_FOLDER / SNAPSHOT_FILE).is_file()
    assert fake.listing_auth == [SIGNED_IN]


def test_without_a_login_only_the_public_decks_sync_and_nothing_warns(tmp_path: Path, raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)

    outcome = sync(tmp_path, fake)

    assert statuses(outcome) == {DECK_ID: SyncStatus.NEW}
    assert outcome.warnings == []
    assert [p.name for p in (tmp_path / "decks").iterdir()] == ["10000001-sample-deck"]
    assert fake.listing_auth == [None]


def test_without_a_login_a_public_only_account_gets_no_warning_on_a_later_sync(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake)

    assert sync(tmp_path, fake).warnings == []


def test_without_a_login_the_private_decks_on_disk_stay_and_one_warning_says_so(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())
    private_before = snapshot_bytes(tmp_path, PRIVATE_FOLDER)
    unlisted_before = snapshot_bytes(tmp_path, UNLISTED_FOLDER)

    outcome = sync(tmp_path, fake)

    assert outcome.warnings == [NOT_SIGNED_IN]
    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED}
    assert snapshot_bytes(tmp_path, PRIVATE_FOLDER) == private_before
    assert snapshot_bytes(tmp_path, UNLISTED_FOLDER) == unlisted_before


def test_a_login_that_has_expired_falls_back_with_a_warning_and_leaves_the_private_decks(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())
    private_before = snapshot_bytes(tmp_path, PRIVATE_FOLDER)
    fake.refresh_tokens.clear()

    outcome = sync(tmp_path, fake, signed_in())

    assert EXPIRED in outcome.warnings
    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED}
    assert snapshot_bytes(tmp_path, PRIVATE_FOLDER) == private_before
    assert (tmp_path / "decks" / UNLISTED_FOLDER / SNAPSHOT_FILE).is_file()
    assert fake.listing_auth == [SIGNED_IN, None]


def test_a_credential_store_that_fails_falls_back_with_a_warning(tmp_path: Path, raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)

    outcome = sync(tmp_path, fake, BrokenTokenStore())

    assert (
        "Archidekt login not usable (the credential store is locked for TESTER): "
        "private and unlisted decks were not updated"
    ) in outcome.warnings
    assert statuses(outcome) == {DECK_ID: SyncStatus.NEW}
    assert fake.listing_auth == [None]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_signed_in_listing_falls_back_to_the_anonymous_one(
    tmp_path: Path, raw_deck: Json, status: int
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())
    private_before = snapshot_bytes(tmp_path, PRIVATE_FOLDER)
    fake.signed_in_listing_status = status

    outcome = sync(tmp_path, fake, signed_in())

    assert REFUSED in outcome.warnings
    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED}
    assert snapshot_bytes(tmp_path, PRIVATE_FOLDER) == private_before
    assert fake.listing_auth == [SIGNED_IN, SIGNED_IN, None]


def test_a_token_that_expires_partway_through_the_listing_is_listed_again_anonymously_from_the_start(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    fake.listing_page_size = 1
    sync(tmp_path, fake, signed_in())
    private_before = snapshot_bytes(tmp_path, PRIVATE_FOLDER)
    fake.listing_auth.clear()
    fake.signed_in_pages = 1

    outcome = sync(tmp_path, fake, signed_in())

    assert outcome.warnings == [REFUSED]
    # The one signed-in page that was served is dropped, not mixed into the anonymous listing.
    assert [r.deck_id for r in outcome.results] == [DECK_ID]
    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED}
    assert snapshot_bytes(tmp_path, PRIVATE_FOLDER) == private_before
    assert fake.listing_auth == [SIGNED_IN, SIGNED_IN, None]


def test_a_token_revoked_after_the_listing_fails_the_decks_to_fetch_and_deletes_nothing(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())
    before = {folder: snapshot_bytes(tmp_path, folder) for folder in (PRIVATE_FOLDER, UNLISTED_FOLDER)}
    public_before = snapshot_bytes(tmp_path, "10000001-sample-deck")
    # Edited on Archidekt since the last sync, so both are fetched; the login is refused on those reads.
    for deck_id in (DECK_ID, PRIVATE_ID):
        fake.decks[deck_id]["updatedAt"] = "2026-10-16T08:00:00.000000Z"
        fake.failing[deck_id] = 401

    outcome = sync(tmp_path, fake, signed_in())

    assert fake.listing_auth == [SIGNED_IN, SIGNED_IN]
    assert statuses(outcome) == {
        DECK_ID: SyncStatus.FAILED,
        PRIVATE_ID: SyncStatus.FAILED,
        UNLISTED_ID: SyncStatus.UNCHANGED,
    }
    assert {r.error for r in outcome.results if r.status is SyncStatus.FAILED} == {
        "Archidekt refused the login (HTTP 401)"
    }
    assert outcome.warnings == []
    assert snapshot_bytes(tmp_path, "10000001-sample-deck") == public_before
    assert {folder: snapshot_bytes(tmp_path, folder) for folder in before} == before


def test_a_signed_in_listing_that_fails_otherwise_is_not_a_fallback(tmp_path: Path, raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    fake.signed_in_listing_status = 404

    with pytest.raises(HttpStatusError, match="HTTP 404"):
        sync(tmp_path, fake, signed_in())

    assert fake.listing_auth == [SIGNED_IN]


def test_a_deck_gone_from_the_signed_in_listing_and_deleted_on_archidekt_is_removed(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())
    del fake.decks[PRIVATE_ID]
    fake.private.discard(PRIVATE_ID)

    outcome = sync(tmp_path, fake, signed_in())

    assert statuses(outcome)[PRIVATE_ID] is SyncStatus.REMOVED
    assert not (tmp_path / "decks" / PRIVATE_FOLDER).exists()
    assert (tmp_path / "decks" / UNLISTED_FOLDER / SNAPSHOT_FILE).is_file()


def test_a_public_deck_gone_from_an_anonymous_listing_is_still_reported_missing_and_kept(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    fake.put({**raw_deck, "id": 222, "name": "Other"})
    sync(tmp_path, fake)
    del fake.decks[222]

    outcome = sync(tmp_path, fake)

    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED, 222: SyncStatus.MISSING_UPSTREAM}
    assert (tmp_path / "decks" / "222-other" / SNAPSHOT_FILE).is_file()


def test_a_deck_that_turns_private_is_fetched_again_even_when_its_edit_time_is_unchanged(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake, signed_in())
    fake.private.add(DECK_ID)
    fake.requests.clear()

    outcome = sync(tmp_path, fake, signed_in())

    assert f"/api/decks/{DECK_ID}/" in fake.requests
    assert statuses(outcome) == {DECK_ID: SyncStatus.UNCHANGED}
    stored = Workspace(tmp_path / "decks").load(DECK_ID)
    assert stored is not None
    assert stored.private


def test_a_dry_run_reads_the_hidden_decks_with_the_login_and_writes_nothing(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)

    outcome = sync(tmp_path, fake, signed_in(), dry_run=True)

    assert statuses(outcome) == {
        DECK_ID: SyncStatus.NEW,
        PRIVATE_ID: SyncStatus.NEW,
        UNLISTED_ID: SyncStatus.NEW,
    }
    assert not (tmp_path / "decks").exists()


def test_the_saved_flags_come_back_from_the_snapshot(tmp_path: Path, raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    sync(tmp_path, fake, signed_in())

    workspace = Workspace(tmp_path / "decks")
    flags = {
        deck_id: (stored.private, stored.unlisted)
        for deck_id in (DECK_ID, PRIVATE_ID, UNLISTED_ID)
        if (stored := workspace.load(deck_id)) is not None
    }

    assert flags == {DECK_ID: (False, False), PRIVATE_ID: (True, False), UNLISTED_ID: (False, True)}


def test_a_snapshot_saved_before_the_flags_existed_reads_as_a_public_deck(
    tmp_path: Path, raw_deck: Json
) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    sync(tmp_path, fake)
    path = tmp_path / "decks" / "10000001-sample-deck" / SNAPSHOT_FILE
    text = path.read_text(encoding="utf-8")
    assert '"private": false' in text
    flagless = [
        line for line in text.splitlines(keepends=True) if "private" not in line and "unlisted" not in line
    ]
    path.write_text("".join(flagless), encoding="utf-8")

    stored = Workspace(tmp_path / "decks").load(DECK_ID)

    assert stored is not None
    assert (stored.private, stored.unlisted) == (False, False)


def test_no_log_line_holds_a_token(tmp_path: Path, raw_deck: Json, caplog: pytest.LogCaptureFixture) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)

    with caplog.at_level(logging.DEBUG):
        sync(tmp_path, fake, signed_in())
        fake.signed_in_listing_status = 401
        outcome = sync(tmp_path, fake, signed_in())
        fake.refresh_tokens.clear()
        outcome_expired = sync(tmp_path, fake, signed_in())

    assert caplog.records
    text = "\n".join(
        [record.getMessage() for record in caplog.records]
        + [record.exc_text or "" for record in caplog.records]
        + outcome.warnings
        + outcome_expired.warnings
    )
    assert ACCESS_TOKEN not in text
    assert REFRESH_TOKEN not in text


def test_the_single_deck_read_takes_the_flags_from_the_response(raw_deck: Json) -> None:
    # The recorded response of a public deck names both flags; a missing key would also read as false.
    assert (raw_deck["private"], raw_deck["unlisted"]) == (False, False)
    raw_deck["private"], raw_deck["unlisted"] = True, True

    deck = parse_deck(raw_deck)

    assert (deck.private, deck.unlisted) == (True, True)


def test_the_flags_must_be_true_or_false(raw_deck: Json) -> None:
    assert (parse_deck(raw_deck).private, parse_deck(raw_deck).unlisted) == (False, False)
    raw_deck["private"] = "yes"

    with pytest.raises(ValueError, match="private"):
        parse_deck(raw_deck)


class Recorder:
    """Keeps the requests a client sends, answering from a FakeArchidekt."""

    def __init__(self, fake: FakeArchidekt) -> None:
        self.fake = fake
        self.sent: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        return self.fake.handler(request)

    def fetcher(self) -> Fetcher:
        return Fetcher(httpx.Client(transport=httpx.MockTransport(self.handler)), sleep=lambda _: None)


def test_the_anonymous_client_sends_the_same_requests_as_a_plain_fetch(raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    recorded = Recorder(fake)
    fetcher = recorded.fetcher()
    client = ArchidektClient(fetcher)

    client.list_commander_decks("TESTER")
    client.get_deck(DECK_ID)
    fetcher.get_json("https://archidekt.com/api/decks/v3/", {"ownerUsername": "TESTER"})
    fetcher.get_json(f"https://archidekt.com/api/decks/{DECK_ID}/")

    listing, deck, plain_listing, plain_deck = recorded.sent
    assert str(listing.url) == "https://archidekt.com/api/decks/v3/?ownerUsername=TESTER"
    assert str(deck.url) == f"https://archidekt.com/api/decks/{DECK_ID}/"
    for sent, plain in ((listing, plain_listing), (deck, plain_deck)):
        assert (sent.method, sent.url, sent.content) == (plain.method, plain.url, plain.content)
        assert sorted(sent.headers.raw) == sorted(plain.headers.raw)
        assert "authorization" not in sent.headers


def test_the_signed_in_client_sends_the_header_on_every_page_and_deck_read(raw_deck: Json) -> None:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    recorded = Recorder(fake)
    client = ArchidektClient(recorded.fetcher(), token=ACCESS_TOKEN)

    client.list_commander_decks("TESTER")
    client.get_deck(DECK_ID)

    assert [r.headers.get("Authorization") for r in recorded.sent] == [SIGNED_IN, SIGNED_IN]


def test_the_header_goes_to_the_listing_and_never_to_a_next_link_outside_the_api() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("Authorization")))
        if request.url.params.get("page") == "2":
            return httpx.Response(200, json={"count": 0, "next": None, "results": []})
        return httpx.Response(
            200,
            json={"count": 0, "next": "https://evil.example/api/decks/v3/?page=2", "results": []},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = ArchidektClient(Fetcher(http, sleep=lambda _: None), token=ACCESS_TOKEN)

    with pytest.raises(ArchidektError, match="outside the Archidekt API"):
        client.list_commander_decks("TESTER")

    assert seen == [("https://archidekt.com/api/decks/v3/?ownerUsername=TESTER", SIGNED_IN)]


def test_a_private_deck_is_refused_without_the_header_and_read_with_it(raw_deck: Json) -> None:
    fake = archidekt_with_hidden_decks(raw_deck)
    recorded = Recorder(fake)
    fetcher = recorded.fetcher()

    with pytest.raises(HttpStatusError, match="HTTP 403"):
        ArchidektClient(fetcher).get_deck(PRIVATE_ID)

    assert ArchidektClient(fetcher, token=ACCESS_TOKEN).get_deck(PRIVATE_ID).private
    assert ArchidektClient(fetcher).get_deck(UNLISTED_ID).unlisted
