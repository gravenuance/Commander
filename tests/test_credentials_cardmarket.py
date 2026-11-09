import json

import keyring
import keyring.errors
import pytest

from commander.credentials import CardmarketSessionStore, CredentialError, SessionCookie
from conftest import MemoryKeyring

SERVICE = "Commander (cmdr) Cardmarket"
USER = "session-cookie"
COOKIE = SessionCookie("PHPSESSID", "abc123", "www.cardmarket.com", "/")


class TestSessionCookie:
    def test_it_is_saved_in_the_format_the_prototype_wrote(self, memory_keyring: MemoryKeyring) -> None:
        CardmarketSessionStore().put(COOKIE)

        saved = memory_keyring.held(SERVICE)
        assert list(saved) == [(SERVICE, USER)]
        assert json.loads(saved[(SERVICE, USER)]) == {
            "name": "PHPSESSID",
            "value": "abc123",
            "domain": "www.cardmarket.com",
            "path": "/",
        }

    def test_a_cookie_the_prototype_saved_is_read(self, memory_keyring: MemoryKeyring) -> None:
        memory_keyring.secrets[(SERVICE, USER)] = (
            '{"name": "PHPSESSID", "value": "xyz", "domain": ".cardmarket.com", "path": "/"}'
        )

        assert CardmarketSessionStore().get() == SessionCookie("PHPSESSID", "xyz", ".cardmarket.com", "/")

    def test_the_round_trip_returns_the_same_cookie(self) -> None:
        store = CardmarketSessionStore()

        store.put(COOKIE)

        assert store.get() == COOKIE

    def test_nothing_saved_reads_as_none(self) -> None:
        assert CardmarketSessionStore().get() is None

    def test_delete_forgets_the_cookie_once(self) -> None:
        store = CardmarketSessionStore()
        store.put(COOKIE)

        assert store.delete() is True
        assert store.get() is None
        assert store.delete() is False

    @pytest.mark.parametrize("domain", ["evil.com", "www.cardmarket.com.evil.com", "notcardmarket.com", ""])
    def test_a_cookie_for_another_domain_cannot_be_made(self, domain: str) -> None:
        with pytest.raises(CredentialError, match="not Cardmarket"):
            SessionCookie("PHPSESSID", "abc", domain, "/")

    def test_only_the_session_cookie_is_kept(self) -> None:
        with pytest.raises(CredentialError, match="PHPSESSID"):
            SessionCookie("cm_TD", "abc", "www.cardmarket.com", "/")

    @pytest.mark.parametrize(
        "text",
        [
            "not json",
            "[1, 2]",
            '{"name": "PHPSESSID", "value": "abc", "domain": "evil.com", "path": "/"}',
            '{"name": "cm_TD", "value": "abc", "domain": "www.cardmarket.com", "path": "/"}',
            '{"name": "PHPSESSID", "value": "", "domain": "www.cardmarket.com", "path": "/"}',
            '{"name": "PHPSESSID", "value": 5, "domain": "www.cardmarket.com", "path": "/"}',
            '{"name": "PHPSESSID", "value": "abc", "domain": "www.cardmarket.com"}',
        ],
    )
    def test_a_saved_secret_that_is_not_a_cardmarket_session_reads_as_none(
        self, memory_keyring: MemoryKeyring, text: str
    ) -> None:
        memory_keyring.secrets[(SERVICE, USER)] = text

        assert CardmarketSessionStore().get() is None

    def test_a_cookie_too_long_for_credential_manager_is_refused(self) -> None:
        long = SessionCookie("PHPSESSID", "x" * 1300, "www.cardmarket.com", "/")

        with pytest.raises(CredentialError, match="length"):
            CardmarketSessionStore().put(long)

    def test_a_store_failure_is_a_credential_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def refuse(*_: object) -> None:
            raise keyring.errors.KeyringError("locked")

        monkeypatch.setattr(keyring, "get_password", refuse)
        monkeypatch.setattr(keyring, "set_password", refuse)
        store = CardmarketSessionStore()

        with pytest.raises(CredentialError, match="could not read"):
            store.get()
        with pytest.raises(CredentialError, match="could not save"):
            store.put(COOKIE)
