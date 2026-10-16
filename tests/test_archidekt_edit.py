import importlib
import json
from collections.abc import Iterator

import httpx
import pytest

from commander.archidekt import ArchidektError
from commander.archidekt_edit import (
    DEFAULT_LABEL,
    ArchidektAuth,
    ArchidektEditor,
    AuthError,
    CardEdit,
    EditKind,
    EntryState,
    edit_json,
    new_entry_state,
    parse_entry_states,
)
from commander.credentials import SERVICE, CredentialError, KeyringTokenStore
from commander.net import Fetcher, NetworkError
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


def test_without_a_saved_login_access_token_asks_for_cmdr_login(
    fake: FakeArchidekt, fetcher: Fetcher
) -> None:
    with pytest.raises(AuthError, match="run `cmdr login`"):
        ArchidektAuth(fetcher, MemoryTokenStore()).access_token("MAKKIS")

    assert fake.writes == []


def test_an_expired_refresh_token_asks_to_log_in_again(fetcher: Fetcher) -> None:
    with pytest.raises(AuthError, match="expired; run `cmdr login` again"):
        ArchidektAuth(fetcher, MemoryTokenStore({"makkis": "old"})).access_token("MAKKIS")


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

    with pytest.raises(NetworkError, match="HTTP 404"):
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


def test_writing_without_a_token_asks_for_cmdr_login(fake: FakeArchidekt, fetcher: Fetcher) -> None:
    with pytest.raises(AuthError, match="run `cmdr login`"):
        ArchidektEditor(fetcher, None).create_snapshot(DECK_ID, "before", "why")

    assert fake.writes == []


@pytest.mark.parametrize(
    ("status", "error", "message"),
    [
        (403, AuthError, r"HTTP 403\); is the deck yours"),
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
