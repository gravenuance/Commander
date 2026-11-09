"""Archidekt refresh tokens, the TopDeck API key and the Cardmarket session cookie in the OS secret store
(Windows Credential Manager); never in a file."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from functools import cache
from typing import Protocol, cast

# keyring is imported inside the functions that use it: it and the Windows backend behind it take 100 ms to
# load, which the app should not wait for before it has a window.


@cache
def _store_errors() -> tuple[type[Exception], ...]:
    import keyring.errors  # noqa: PLC0415

    # keyring passes the Windows backend's own error through rather than wrapping it in KeyringError.
    try:
        windows = importlib.import_module("win32ctypes.pywin32.pywintypes")
    except ImportError:
        return (keyring.errors.KeyringError,)
    error: object = getattr(windows, "error", None)
    if isinstance(error, type) and issubclass(error, Exception):
        return (keyring.errors.KeyringError, error)
    return (keyring.errors.KeyringError,)


SERVICE = "Commander (cmdr) Archidekt"
# Windows Credential Manager holds at most 2560 bytes per secret, stored as UTF-16: 1280 characters.
MAX_TOKEN_LENGTH = 1280


class CredentialError(Exception):
    """The secret store refused a read or write; the message says what to do."""


class TokenStore(Protocol):
    def get(self, user: str) -> str | None: ...

    def put(self, user: str, token: str) -> None: ...

    def delete(self, user: str) -> bool: ...


class KeyringTokenStore:
    """One refresh token per Archidekt username, in the backend `keyring` picks for this OS."""

    def get(self, user: str) -> str | None:
        import keyring  # noqa: PLC0415

        try:
            return keyring.get_password(SERVICE, user.casefold())
        except _store_errors() as error:
            raise CredentialError(f"could not read the saved Archidekt login: {error}") from error

    def put(self, user: str, token: str) -> None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise CredentialError(f"Archidekt sent a login token of unexpected length ({len(token)})")
        import keyring  # noqa: PLC0415

        try:
            keyring.set_password(SERVICE, user.casefold(), token)
        except _store_errors() as error:
            raise CredentialError(f"could not save the Archidekt login: {error}") from error

    def delete(self, user: str) -> bool:
        """Forget the user's token; False when none was saved."""
        import keyring.errors  # noqa: PLC0415

        try:
            keyring.delete_password(SERVICE, user.casefold())
        except keyring.errors.PasswordDeleteError:
            return False
        except _store_errors() as error:
            raise CredentialError(f"could not remove the saved Archidekt login: {error}") from error
        return True


CARDMARKET_SERVICE = "Commander (cmdr) Cardmarket"
CARDMARKET_USER = "session-cookie"
CARDMARKET_SESSION_COOKIE = "PHPSESSID"
CARDMARKET_HOSTS = frozenset({"cardmarket.com", "www.cardmarket.com"})


@dataclass(slots=True, frozen=True)
class SessionCookie:
    """Cardmarket's session cookie, as the browser holds it."""

    name: str
    value: str
    domain: str
    path: str

    def __post_init__(self) -> None:
        if self.name != CARDMARKET_SESSION_COOKIE:
            raise CredentialError(f"only the {CARDMARKET_SESSION_COOKIE} cookie is kept, not {self.name!r}")
        if self.domain.lstrip(".").casefold() not in CARDMARKET_HOSTS:
            raise CredentialError(f"the session cookie is for {self.domain!r}, not Cardmarket")
        if not self.value or not self.path:
            raise CredentialError("the session cookie has no value or no path")

    def to_json(self) -> str:
        return json.dumps({"name": self.name, "value": self.value, "domain": self.domain, "path": self.path})

    @staticmethod
    def from_json(text: str) -> SessionCookie | None:
        """The cookie saved as `to_json` wrote it, or None for any other text."""
        try:
            data: object = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        fields = cast("dict[str, object]", data)
        values = [fields.get(key) for key in ("name", "value", "domain", "path")]
        if not all(isinstance(value, str) for value in values):
            return None
        name, value, domain, path = cast("list[str]", values)
        try:
            return SessionCookie(name, value, domain, path)
        except CredentialError:
            return None


class SessionStore(Protocol):
    """The one Cardmarket session cookie the owner signed in with."""

    def get(self) -> SessionCookie | None: ...

    def put(self, cookie: SessionCookie) -> None: ...

    def delete(self) -> bool: ...


class CardmarketSessionStore:
    """The Cardmarket session cookie, in the backend `keyring` picks for this OS (ADR 0067)."""

    def get(self) -> SessionCookie | None:
        """The saved cookie; None when none is saved or the secret is not one this store wrote."""
        import keyring  # noqa: PLC0415

        try:
            text = keyring.get_password(CARDMARKET_SERVICE, CARDMARKET_USER)
        except _store_errors() as error:
            raise CredentialError(f"could not read the saved Cardmarket session: {error}") from error
        return SessionCookie.from_json(text) if text else None

    def put(self, cookie: SessionCookie) -> None:
        text = cookie.to_json()
        if len(text) > MAX_TOKEN_LENGTH:
            raise CredentialError(f"the Cardmarket session cookie is of unexpected length ({len(text)})")
        import keyring  # noqa: PLC0415

        try:
            keyring.set_password(CARDMARKET_SERVICE, CARDMARKET_USER, text)
        except _store_errors() as error:
            raise CredentialError(f"could not save the Cardmarket session: {error}") from error

    def delete(self) -> bool:
        """Forget the cookie; False when none was saved."""
        import keyring.errors  # noqa: PLC0415

        try:
            keyring.delete_password(CARDMARKET_SERVICE, CARDMARKET_USER)
        except keyring.errors.PasswordDeleteError:
            return False
        except _store_errors() as error:
            raise CredentialError(f"could not remove the saved Cardmarket session: {error}") from error
        return True


TOPDECK_SERVICE = "Commander (cmdr) TopDeck"
TOPDECK_USER = "api-key"


class KeyStore(Protocol):
    """The one API key of a service that issues a key per user."""

    def get(self) -> str | None: ...

    def put(self, key: str) -> None: ...

    def delete(self) -> bool: ...


def check_api_key(key: str) -> str:
    """The key without surrounding whitespace, or CredentialError when it could not be sent as a header."""
    key = key.strip()
    if not key or len(key) > MAX_TOKEN_LENGTH:
        raise CredentialError("an API key is 1 to 1280 characters")
    if not key.isascii() or not key.isprintable() or " " in key:
        raise CredentialError("an API key is one run of plain characters, with no spaces")
    return key


class KeyringKeyStore:
    """The TopDeck API key, in the backend `keyring` picks for this OS."""

    def get(self) -> str | None:
        import keyring  # noqa: PLC0415

        try:
            return keyring.get_password(TOPDECK_SERVICE, TOPDECK_USER)
        except _store_errors() as error:
            raise CredentialError(f"could not read the saved TopDeck key: {error}") from error

    def put(self, key: str) -> None:
        key = check_api_key(key)
        import keyring  # noqa: PLC0415

        try:
            keyring.set_password(TOPDECK_SERVICE, TOPDECK_USER, key)
        except _store_errors() as error:
            raise CredentialError(f"could not save the TopDeck key: {error}") from error

    def delete(self) -> bool:
        """Forget the key; False when none was saved."""
        import keyring.errors  # noqa: PLC0415

        try:
            keyring.delete_password(TOPDECK_SERVICE, TOPDECK_USER)
        except keyring.errors.PasswordDeleteError:
            return False
        except _store_errors() as error:
            raise CredentialError(f"could not remove the saved TopDeck key: {error}") from error
        return True
