import importlib
import json
import logging
from collections.abc import Callable, Iterator

import httpx
import keyring.errors
import pytest

from commander.archidekt import ArchidektError, AuthError, SignInRequiredError
from commander.archidekt_edit import (
    DEFAULT_LABEL,
    ArchidektAuth,
    ArchidektEditor,
    CardEdit,
    EditKind,
    EntryState,
    edit_json,
    label_value,
    new_entry_state,
    parse_account_labels,
    parse_entry_states,
)
from commander.credentials import SERVICE, CredentialError, KeyringTokenStore
from commander.net import MAX_RESPONSE_BYTES, Fetcher, NetworkError
from conftest import (
    ACCESS_TOKEN,
    DECK_ID,
    PASSWORD,
    REFRESH_TOKEN,
    FakeArchidekt,
    Json,
    MemoryKeyring,
    MemoryTokenStore,
    archidekt_card_id,
)

MAGDA_KHM = "079e6263-e54c-4899-a336-5315909b9322"


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    fake = FakeArchidekt()
    fake.put(raw_deck)
    return fake


@pytest.fixture
def fetcher(fake: FakeArchidekt) -> Iterator[Fetcher]:
    with fake.client() as client:
        yield Fetcher(client, sleep=lambda _: None)


def test_login_sends_the_name_and_keeps_only_the_refresh_token(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    store = MemoryTokenStore()

    ArchidektAuth(fetcher, store).login("MAKKIS", "makkis", PASSWORD)

    assert store.tokens == {"makkis": REFRESH_TOKEN}
    method, path, body, _ = fake.writes[0]
    assert (method, path, body) == (
        "POST",
        "/api/rest-auth/login/",
        {"username": "makkis", "password": PASSWORD},
    )


def test_login_with_an_email_sends_it_as_email(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    ArchidektAuth(fetcher, MemoryTokenStore()).login("MAKKIS", "me@example.org", PASSWORD)

    assert fake.writes[0][2] == {"email": "me@example.org", "password": PASSWORD}


def test_a_refused_login_stores_nothing_and_never_echoes_the_password(fetcher: Fetcher) -> None:
    store = MemoryTokenStore()

    with pytest.raises(AuthError, match="refused the login") as raised:
        ArchidektAuth(fetcher, store).login("MAKKIS", "MAKKIS", "wrong password")

    assert store.tokens == {}
    assert "wrong password" not in str(raised.value)


def test_a_login_for_another_account_stores_nothing(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    fake.user = "SomeoneElse"
    store = MemoryTokenStore()

    with pytest.raises(AuthError, match="belongs to SomeoneElse, but cmdr is set up for MAKKIS"):
        ArchidektAuth(fetcher, store).login("MAKKIS", "SomeoneElse", PASSWORD)

    assert store.tokens == {}


def test_login_rejects_an_empty_password_without_a_request(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    with pytest.raises(AuthError, match="empty"):
        ArchidektAuth(fetcher, MemoryTokenStore()).login("MAKKIS", "MAKKIS", "")

    assert fake.writes == []


def test_a_login_answer_without_a_token_is_an_archidekt_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "x", "user": {"username": "MAKKIS"}})

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(ArchidektError, match="refresh_token"),
    ):
        ArchidektAuth(Fetcher(client), MemoryTokenStore()).login("MAKKIS", "MAKKIS", PASSWORD)


def test_access_token_comes_from_the_saved_refresh_token(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    token = ArchidektAuth(fetcher, MemoryTokenStore({"makkis": REFRESH_TOKEN})).access_token("MAKKIS")

    assert token == ACCESS_TOKEN
    assert fake.writes[0][1:3] == ("/api/rest-auth/token/refresh/", {"refresh": REFRESH_TOKEN})


def test_without_a_saved_login_access_token_says_so_for_any_front_end(
    fake: FakeArchidekt, fetcher: Fetcher
) -> None:
    with pytest.raises(SignInRequiredError) as raised:
        ArchidektAuth(fetcher, MemoryTokenStore()).access_token("MAKKIS")

    assert str(raised.value) == "not signed in to Archidekt as MAKKIS"

    assert fake.writes == []


def test_an_expired_refresh_token_says_the_login_expired(fetcher: Fetcher) -> None:
    with pytest.raises(SignInRequiredError) as raised:
        ArchidektAuth(fetcher, MemoryTokenStore({"makkis": "old"})).access_token("MAKKIS")

    assert str(raised.value) == "the saved Archidekt login has expired"


@pytest.mark.parametrize(
    ("answer", "token", "kept"),
    [
        ({"token": "t2"}, "t2", "r1"),
        ({"access": "t3", "refresh": "r2"}, "t3", "r2"),
        ({"access": "t4", "refresh": "r1"}, "t4", "r1"),
    ],
)
def test_access_token_reads_either_field_and_keeps_a_rotated_refresh_token(
    answer: Json, token: str, kept: str
) -> None:
    store = MemoryTokenStore({"makkis": "r1"})

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer))) as client:
        assert ArchidektAuth(Fetcher(client), store).access_token("MAKKIS") == token

    assert store.tokens == {"makkis": kept}


def test_a_token_refresh_is_never_retried() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(NetworkError, match="HTTP 503"),
    ):
        ArchidektAuth(Fetcher(client), MemoryTokenStore({"makkis": "r1"})).access_token("MAKKIS")

    assert len(calls) == 1


def test_find_card_matches_the_printing_by_scryfall_id(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    editor = ArchidektEditor(fetcher, None)

    assert editor.find_card("Magda, Brazen Outlaw", "khm", MAGDA_KHM) == archidekt_card_id(MAGDA_KHM)
    assert editor.find_card("Magda, Brazen Outlaw", "khm", "not-this-printing") is None
    assert editor.find_card("Magda, Brazen Outlaw", "sld", MAGDA_KHM) is None
    assert fake.writes == []


def test_find_card_rejects_an_unusable_name(fetcher: Fetcher) -> None:
    with pytest.raises(ValueError, match="1-200"):
        ArchidektEditor(fetcher, None).find_card("x" * 201, "khm", MAGDA_KHM)


def test_get_deck_signs_in_for_a_private_deck(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    fake.private.add(DECK_ID)

    with pytest.raises(NetworkError, match="HTTP 403"):
        ArchidektEditor(fetcher, None).get_deck(DECK_ID)
    live = ArchidektEditor(fetcher, ACCESS_TOKEN).get_deck(DECK_ID)

    assert live.deck.id == DECK_ID
    assert live.entries[2531358028] == EntryState(
        ("Stax",), ",#656565", "Normal", companion=False, flipped_default=False, custom_cmc=None
    )


def test_writes_carry_the_token_and_go_out_once(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    editor = ArchidektEditor(fetcher, ACCESS_TOKEN)
    edit = CardEdit(EditKind.REMOVE, 1, 1, new_entry_state("Stax"), 2531358028)

    editor.create_snapshot(DECK_ID, "before", "why")
    editor.modify_cards(DECK_ID, [edit])

    assert [(m, p, auth) for m, p, _, auth in fake.writes] == [
        ("POST", f"/api/decks/{DECK_ID}/snapshots/", f"JWT {ACCESS_TOKEN}"),
        ("PATCH", f"/api/decks/{DECK_ID}/modifyCards/v2/", f"JWT {ACCESS_TOKEN}"),
    ]
    assert fake.writes[0][2] == {"name": "before", "description": "why"}


def test_writing_without_a_token_says_saving_needs_a_login(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    with pytest.raises(SignInRequiredError) as raised:
        ArchidektEditor(fetcher, None).create_snapshot(DECK_ID, "before", "why")

    assert str(raised.value) == "saving needs an Archidekt login"

    assert fake.writes == []


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (
            403,
            SignInRequiredError,
            r"\(HTTP 403\); the deck may not be yours, or the login has expired$",
        ),
        (429, ArchidektError, "limiting requests"),
        (500, NetworkError, "HTTP 500"),
    ],
)
def test_a_refused_write_says_why_and_is_not_retried(
    fake: FakeArchidekt, fetcher: Fetcher, status: int, error: type[Exception], message: str
) -> None:
    fake.write_status["snapshots"] = status

    with pytest.raises(error, match=message):
        ArchidektEditor(fetcher, ACCESS_TOKEN).create_snapshot(DECK_ID, "before", "why")

    assert len(fake.writes) == 1


def test_modify_cards_needs_at_least_one_edit(fetcher: Fetcher) -> None:
    with pytest.raises(ValueError, match="no edits"):
        ArchidektEditor(fetcher, ACCESS_TOKEN).modify_cards(DECK_ID, [])


def test_edit_json_sends_back_the_entry_as_it_was() -> None:
    state = EntryState(
        ("Ramp", "Combo"), "Proxy,#7d664d", "Foil", companion=True, flipped_default=True, custom_cmc=2.5
    )

    entry = edit_json(CardEdit(EditKind.MODIFY, 77, 3, state, 1234))

    assert len(str(entry.pop("patchId"))) == 32
    assert entry == {
        "action": "modify",
        "cardid": 77,
        "customCardId": None,
        "categories": ["Ramp", "Combo"],
        "modifications": {
            "quantity": 3,
            "modifier": "Foil",
            "customCmc": 2.5,
            "companion": True,
            "flippedDefault": True,
            "label": "Proxy,#7d664d",
        },
        "deckRelationId": 1234,
    }


def test_edit_json_for_a_new_card_has_no_entry_id_and_the_dont_have_label() -> None:
    entry = edit_json(CardEdit(EditKind.ADD, 77, 1, new_entry_state("Ramp")))

    assert "deckRelationId" not in entry
    assert entry["categories"] == ["Ramp"]
    assert entry["modifications"] == {
        "quantity": 1,
        "modifier": "Normal",
        "customCmc": None,
        "companion": False,
        "flippedDefault": False,
        "label": "Don't Have,#f47373",
    }


@pytest.mark.parametrize(
    ("kind", "entry_id", "quantity", "message"),
    [
        (EditKind.ADD, 5, 1, "add must not have one"),
        (EditKind.MODIFY, None, 1, "need the entry id"),
        (EditKind.REMOVE, 5, 0, "at least 1"),
    ],
)
def test_card_edit_checks_its_fields(
    kind: EditKind, entry_id: int | None, quantity: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        CardEdit(kind, 1, quantity, new_entry_state("Ramp"), entry_id)


def test_parse_entry_states_skips_deleted_entries_and_fills_gaps(raw_deck: Json) -> None:
    raw_deck["cards"][0]["deletedAt"] = "2026-01-01T00:00:00Z"
    second = raw_deck["cards"][1]
    for key in ("label", "companion", "flippedDefault", "customCmc"):
        second.pop(key, None)

    states = parse_entry_states(raw_deck)

    assert raw_deck["cards"][0]["id"] not in states
    assert states[second["id"]].label == DEFAULT_LABEL
    assert states[second["id"]].companion is False


@pytest.mark.parametrize(("key", "value"), [("customCmc", "2"), ("customCmc", True), ("companion", "no")])
def test_parse_entry_states_rejects_odd_fields(raw_deck: Json, key: str, value: object) -> None:
    raw_deck["cards"][1][key] = value

    with pytest.raises(ValueError, match=key):
        parse_entry_states(json.loads(json.dumps(raw_deck)))


@pytest.mark.parametrize(("raw", "expected"), [(3, 3.0), (2.5, 2.5), (None, None)])
def test_parse_entry_states_reads_a_custom_mana_value(
    raw_deck: Json, raw: object, expected: float | None
) -> None:
    raw_deck["cards"][1]["customCmc"] = raw

    states = parse_entry_states(raw_deck)

    assert states[raw_deck["cards"][1]["id"]].custom_cmc == expected


def test_keyring_token_store_round_trip(memory_keyring: MemoryKeyring) -> None:
    store = KeyringTokenStore()

    store.put("MAKKIS", "token")

    assert store.get("makkis") == "token"
    assert memory_keyring.secrets == {(SERVICE, "makkis"): "token"}
    assert store.delete("MAKKIS") is True
    assert store.delete("MAKKIS") is False
    assert store.get("MAKKIS") is None


@pytest.mark.parametrize("token", ["", "x" * 2001])
def test_keyring_token_store_refuses_odd_tokens(token: str) -> None:
    with pytest.raises(CredentialError, match="unexpected length"):
        KeyringTokenStore().put("MAKKIS", token)


def test_a_rejected_change_shows_archidekts_reason_instead_of_login_advice() -> None:
    answer = {"cards": ["Invalid action 'modify' for card 77."]}
    edit = CardEdit(EditKind.ADD, 77, 1, new_entry_state("Ramp"))

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json=answer))) as client,
        pytest.raises(ArchidektError) as raised,
    ):
        ArchidektEditor(Fetcher(client), ACCESS_TOKEN).modify_cards(DECK_ID, [edit])

    message = str(raised.value)
    assert "rejected the change (HTTP 400)" in message
    assert "Invalid action 'modify' for card 77." in message
    assert "cmdr login" not in message


def test_a_refresh_answered_with_400_shows_the_reason_not_expiry() -> None:
    answer = {"refresh_token": ["This field is required."]}

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json=answer))) as client,
        pytest.raises(ArchidektError, match="This field is required"),
    ):
        ArchidektAuth(Fetcher(client), MemoryTokenStore({"makkis": "r1"})).access_token("MAKKIS")


def test_keyring_backend_errors_become_credential_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    windows_error = importlib.import_module("win32ctypes.pywin32.pywintypes").error

    def refuse(*_: object) -> None:
        raise windows_error(1783, "CredWrite", "The stub received bad data.")

    monkeypatch.setattr("keyring.set_password", refuse)
    monkeypatch.setattr("keyring.get_password", refuse)

    with pytest.raises(CredentialError, match="could not save"):
        KeyringTokenStore().put("MAKKIS", "token")
    with pytest.raises(CredentialError, match="could not read"):
        KeyringTokenStore().get("MAKKIS")


def test_keyring_token_store_refuses_tokens_windows_cannot_hold() -> None:
    KeyringTokenStore().put("MAKKIS", "x" * 1280)

    with pytest.raises(CredentialError, match="unexpected length"):
        KeyringTokenStore().put("MAKKIS", "x" * 1281)


def test_account_labels_keep_archidekt_order_and_skip_unusable_entries() -> None:
    raw = [
        {"id": 1, "color": "#0DE9FA", "name": "Considering change"},
        {"id": 2, "color": "#fa0d11", "name": "Don't Have"},
        {"id": 3, "color": "#fa0d11", "name": "Don't Have"},
        {"id": 4, "color": "red", "name": "Odd"},
        {"id": 5, "color": "#123456", "name": " "},
    ]
    assert parse_account_labels(raw) == [("Considering change", "#0de9fa"), ("Don't Have", "#fa0d11")]


def test_a_label_takes_the_account_colour_else_archidekt_default() -> None:
    assert label_value("Getting", {"Getting": "#0d9ffa"}) == "Getting,#0d9ffa"
    assert label_value("Getting") == "Getting,#2ccce4"
    assert label_value("") == ",#656565"
    with pytest.raises(ValueError, match="no colour known"):
        label_value("Wishlist")


def test_reading_the_account_labels_needs_a_login() -> None:
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client,
        pytest.raises(SignInRequiredError) as raised,
    ):
        ArchidektEditor(Fetcher(client), None).account_labels()

    assert str(raised.value) == "reading your labels needs an Archidekt login"


def test_an_expired_login_on_the_labels_read_says_the_login_was_refused(fetcher: Fetcher) -> None:
    with pytest.raises(SignInRequiredError) as raised:
        ArchidektEditor(fetcher, "expired access token").account_labels()

    assert str(raised.value) == "Archidekt refused the login (HTTP 401)"


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_login_on_a_deck_read_says_the_login_was_refused(
    fake: FakeArchidekt, fetcher: Fetcher, status: int
) -> None:
    fake.failing[DECK_ID] = status

    with pytest.raises(SignInRequiredError) as raised:
        ArchidektEditor(fetcher, ACCESS_TOKEN).get_deck(DECK_ID)

    assert str(raised.value) == f"Archidekt refused the login (HTTP {status})"


def test_a_refused_deck_read_without_a_login_stays_a_network_error(
    fake: FakeArchidekt, fetcher: Fetcher
) -> None:
    fake.failing[DECK_ID] = 403

    with pytest.raises(NetworkError, match="HTTP 403"):
        ArchidektEditor(fetcher, None).get_deck(DECK_ID)


@pytest.mark.parametrize(
    "answer",
    [b"<html>saved</html>", b"[" + b" " * MAX_RESPONSE_BYTES + b"]"],
    ids=["html", "over the size cap"],
)
def test_a_saved_write_counts_whatever_the_body_of_the_answer_is(answer: bytes) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, content=answer)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        editor = ArchidektEditor(Fetcher(client, sleep=lambda _: None), ACCESS_TOKEN)
        editor.create_snapshot(DECK_ID, "before", "why")
        editor.modify_cards(DECK_ID, [CardEdit(EditKind.ADD, 1, 1, new_entry_state("Ramp"))])

    # Raising here would have the user retry, and the add would be applied twice.
    assert calls == ["POST", "PATCH"]


def refresh_handler(
    seen: list[str], on_refresh: Callable[[str], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        refresh = json.loads(request.content)["refresh"]
        seen.append(refresh)
        return on_refresh(refresh)

    return handler


def test_a_rotated_token_that_cannot_be_saved_is_logged_and_the_access_token_still_returned(
    memory_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = KeyringTokenStore()
    store.put("MAKKIS", "r1")

    def locked(*_: str) -> None:
        raise keyring.errors.PasswordSetError("vault locked")

    monkeypatch.setattr(memory_keyring, "set_password", locked)
    answer = {"access": "a2", "refresh": "r2-secret"}

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=answer))) as client,
        caplog.at_level(logging.WARNING, logger="commander.archidekt_edit"),
    ):
        token = ArchidektAuth(Fetcher(client), store).access_token("MAKKIS")

    assert token == "a2"
    assert store.get("MAKKIS") == "r1"
    [warning] = caplog.messages
    assert warning == (
        "could not save the rotated Archidekt login for MAKKIS: "
        "could not save the Archidekt login: vault locked"
    )


def test_a_refused_refresh_is_retried_once_with_the_token_another_process_saved() -> None:
    store = MemoryTokenStore({"makkis": "r1"})
    seen: list[str] = []

    def on_refresh(refresh: str) -> httpx.Response:
        if refresh == "r1":
            store.put("MAKKIS", "r2")  # the app refreshed and rotated it meanwhile
            return httpx.Response(401)
        return httpx.Response(200, json={"access": "a3"})

    with httpx.Client(transport=httpx.MockTransport(refresh_handler(seen, on_refresh))) as client:
        assert ArchidektAuth(Fetcher(client), store).access_token("MAKKIS") == "a3"

    assert seen == ["r1", "r2"]


def test_a_refused_refresh_is_not_retried_when_the_saved_token_is_unchanged() -> None:
    seen: list[str] = []

    with (
        httpx.Client(
            transport=httpx.MockTransport(refresh_handler(seen, lambda _: httpx.Response(401)))
        ) as client,
        pytest.raises(AuthError, match="has expired"),
    ):
        ArchidektAuth(Fetcher(client), MemoryTokenStore({"makkis": "r1"})).access_token("MAKKIS")

    assert seen == ["r1"]


def test_the_retry_with_a_newer_token_happens_once_only() -> None:
    store = MemoryTokenStore({"makkis": "r1"})
    seen: list[str] = []

    def on_refresh(refresh: str) -> httpx.Response:
        store.put("MAKKIS", "r2" if refresh == "r1" else "r3")
        return httpx.Response(401)

    with (
        httpx.Client(transport=httpx.MockTransport(refresh_handler(seen, on_refresh))) as client,
        pytest.raises(AuthError, match="has expired"),
    ):
        ArchidektAuth(Fetcher(client), store).access_token("MAKKIS")

    assert seen == ["r1", "r2"]
