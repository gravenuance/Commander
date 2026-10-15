"""Archidekt refresh tokens in the OS secret store (Windows Credential Manager); never in a file."""

from typing import Protocol

import keyring
import keyring.errors

SERVICE = "Commander (cmdr) Archidekt"
# Windows Credential Manager holds at most 2560 bytes per secret; a refresh token is a few hundred.
MAX_TOKEN_LENGTH = 2000


class CredentialError(Exception):
    """The secret store refused a read or write; the message says what to do."""


class TokenStore(Protocol):
    def get(self, user: str) -> str | None: ...

    def put(self, user: str, token: str) -> None: ...

    def delete(self, user: str) -> bool: ...


class KeyringTokenStore:
    """One refresh token per Archidekt username, in the backend `keyring` picks for this OS."""

    def get(self, user: str) -> str | None:
        try:
            return keyring.get_password(SERVICE, user.casefold())
        except keyring.errors.KeyringError as error:
            raise CredentialError(f"could not read the saved Archidekt login: {error}") from error

    def put(self, user: str, token: str) -> None:
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise CredentialError(f"Archidekt sent a login token of unexpected length ({len(token)})")
        try:
            keyring.set_password(SERVICE, user.casefold(), token)
        except keyring.errors.KeyringError as error:
            raise CredentialError(f"could not save the Archidekt login: {error}") from error

    def delete(self, user: str) -> bool:
        """Forget the user's token; False when none was saved."""
        try:
            keyring.delete_password(SERVICE, user.casefold())
        except keyring.errors.PasswordDeleteError:
            return False
        except keyring.errors.KeyringError as error:
            raise CredentialError(f"could not remove the saved Archidekt login: {error}") from error
        return True
