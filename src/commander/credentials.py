"""Archidekt refresh tokens and the TopDeck API key in the OS secret store (Windows Credential Manager); never
in a file."""

import importlib
from functools import cache
from typing import Protocol

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
