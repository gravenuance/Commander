"""Signed-in access to Archidekt: login, card lookup, snapshots and deck edits (ADR 0004).

The endpoints are the ones archidekt.com's own pages call; they are undocumented, so every answer is
shape-checked and every write is sent once, never retried.
"""

import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum

from commander.archidekt import API_BASE, ArchidektError, category_flags, parse_deck
from commander.credentials import TokenStore
from commander.jsonshape import (
    ShapeError,
    as_list,
    as_object,
    opt_str,
    req_bool,
    req_int,
    req_list,
    req_object,
    req_str,
    str_list,
)
from commander.models import DONT_HAVE_LABEL, GETTING_LABEL, Deck
from commander.net import Fetcher, HttpStatusError

log = logging.getLogger(__name__)

# Archidekt's colour label for a card with none, as its own pages send it.
NO_LABEL_COLOUR = "#656565"
DEFAULT_LABEL = f",{NO_LABEL_COLOUR}"
# The colours of the labels cmdr writes when the account has no label of that name: Archidekt's defaults.
LABEL_COLOURS = {DONT_HAVE_LABEL: "#f47373", GETTING_LABEL: "#2ccce4"}
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
MAX_NAME_LENGTH = 200
# A refused login answers 400; elsewhere 400 means the request itself was rejected, with a reason.
_LOGIN_REFUSED = frozenset({400, 401, 403})
_UNAUTHORIZED = frozenset({401, 403})
_BAD_REQUEST = 400
_TOO_MANY = 429


class AuthError(Exception):
    """No usable Archidekt login; the message says how to get one."""


class EditKind(Enum):
    ADD = "add"
    MODIFY = "modify"
    REMOVE = "remove"


@dataclass(slots=True, frozen=True)
class EntryState:
    """The fields of a deck entry an edit must send back unchanged."""

    categories: tuple[str, ...]
    label: str
    modifier: str
    companion: bool
    flipped_default: bool
    custom_cmc: float | None


@dataclass(slots=True, frozen=True)
class LiveDeck:
    deck: Deck
    entries: Mapping[int, EntryState]
    # Each category and whether its cards count in the deck, as this deck has it set.
    categories: Mapping[str, bool] = field(default_factory=dict[str, bool])


@dataclass(slots=True, frozen=True)
class CardEdit:
    kind: EditKind
    card_id: int
    quantity: int
    # Add: the categories to file the card under. Modify and remove: the entry's current state.
    state: EntryState
    entry_id: int | None = None

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError(f"quantity must be at least 1, got {self.quantity}")
        if (self.entry_id is None) != (self.kind is EditKind.ADD):
            raise ValueError("modify and remove need the entry id; add must not have one")


def new_entry_state(category: str, label: str | None = None) -> EntryState:
    # A card saved into a deck is one still to buy (ADR 0010): Don't Have unless the label is given.
    return EntryState(
        (category,),
        label or label_value(DONT_HAVE_LABEL),
        "Normal",
        companion=False,
        flipped_default=False,
        custom_cmc=None,
    )


def label_value(name: str, account: Mapping[str, str] | None = None) -> str:
    """Archidekt's "name,#colour" for a label, in the account's colour for it (ADR 0012), else Archidekt's
    default; "" clears the label."""
    if not name:
        return DEFAULT_LABEL
    colour = (account or {}).get(name) or LABEL_COLOURS.get(name)
    if colour is None:
        raise ValueError(f"no colour known for the label {name!r}")
    return f"{name},{colour}"


def parse_account_labels(raw: object) -> list[tuple[str, str]]:
    """The account's card labels as (name, colour) in Archidekt's order; unnamed or odd ones are skipped."""
    labels: list[tuple[str, str]] = []
    for index, item in enumerate(as_list(raw, "colorTags")):
        entry = as_object(item, f"colorTags[{index}]")
        name = (opt_str(entry, "name", f"colorTags[{index}]") or "").strip()
        colour = (opt_str(entry, "color", f"colorTags[{index}]") or "").strip().lower()
        if name and _COLOUR.fullmatch(colour) and name not in {n for n, _ in labels}:
            labels.append((name, colour))
    return labels


class ArchidektAuth:
    """Swaps a password for a refresh token once, and refresh tokens for short-lived access tokens."""

    def __init__(self, fetcher: Fetcher, store: TokenStore, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._store = store
        self._base_url = base_url.rstrip("/")

    def has_login(self, user: str) -> bool:
        return self._store.get(user) is not None

    def login(self, user: str, name_or_email: str, password: str) -> None:
        """Sign in and keep only the refresh token; the password is never stored or logged."""
        if not password:
            raise AuthError("the password is empty")
        field = "email" if "@" in name_or_email else "username"
        try:
            raw = self._fetcher.send_json(
                "POST", f"{self._base_url}/rest-auth/login/", {field: name_or_email, "password": password}
            )
        except HttpStatusError as error:
            if error.status in _LOGIN_REFUSED:
                raise AuthError("Archidekt refused the login; check the name and password") from None
            raise
        try:
            answer = as_object(raw, "login")
            refresh = req_str(answer, "refresh_token", "login")
            signed_in = req_str(req_object(answer, "user", "login"), "username", "login.user")
        except ShapeError as error:
            raise ArchidektError(f"unexpected login answer from Archidekt: {error}") from error
        if signed_in.casefold() != user.casefold():
            raise AuthError(
                f"that login belongs to {signed_in}, but cmdr is set up for {user}; nothing was saved"
            )
        self._store.put(user, refresh)

    def logout(self, user: str) -> bool:
        return self._store.delete(user)

    def access_token(self, user: str) -> str:
        refresh = self._store.get(user)
        if refresh is None:
            raise AuthError(f"not signed in to Archidekt as {user}; run `cmdr login`")
        try:
            raw = self._fetcher.send_json(
                "POST", f"{self._base_url}/rest-auth/token/refresh/", {"refresh": refresh}
            )
        except HttpStatusError as error:
            if error.status in _UNAUTHORIZED:
                raise AuthError("the saved Archidekt login has expired; run `cmdr login` again") from None
            if error.status == _BAD_REQUEST:
                raise ArchidektError(f"Archidekt rejected the token refresh: {error.detail}") from None
            raise
        try:
            answer = as_object(raw, "token refresh")
            token = opt_str(answer, "access", "token refresh") or req_str(answer, "token", "token refresh")
            rotated = opt_str(answer, "refresh", "token refresh")
        except ShapeError as error:
            raise ArchidektError(f"unexpected token answer from Archidekt: {error}") from error
        if rotated and rotated != refresh:
            self._store.put(user, rotated)
        return token


class ArchidektEditor:
    """Deck reads and writes for one access token (None reads public decks only)."""

    def __init__(self, fetcher: Fetcher, token: str | None, base_url: str = API_BASE) -> None:
        self._fetcher = fetcher
        self._headers = {"Authorization": f"JWT {token}"} if token else None
        self._base_url = base_url.rstrip("/")

    def get_deck(self, deck_id: int) -> LiveDeck:
        try:
            raw = self._fetcher.get_json(f"{self._base_url}/decks/{deck_id}/", headers=self._headers)
        except HttpStatusError as error:
            # Without a token a 401/403 is the deck being private, which the status error already says.
            if self._headers is not None and error.status in _UNAUTHORIZED:
                raise AuthError("Archidekt refused the login; run `cmdr login` again") from None
            raise
        try:
            return LiveDeck(parse_deck(raw), parse_entry_states(raw), category_flags(raw))
        except (ShapeError, ValueError) as error:
            raise ArchidektError(f"unexpected data for deck {deck_id}: {error}") from error

    def find_card(self, name: str, set_code: str, scryfall_id: str) -> int | None:
        """Archidekt's id for one printing, found by exact name and set, matched on its Scryfall id."""
        if not name or len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"card name must be 1-{MAX_NAME_LENGTH} characters")
        params: dict[str, str | int] = {"name": name, "exact": "", "edition": set_code}
        raw = self._fetcher.get_json(f"{self._base_url}/cards/v2/", params)
        try:
            for index, item in enumerate(req_list(as_object(raw, "cards"), "results", "cards")):
                card = as_object(item, f"cards.results[{index}]")
                if req_str(card, "uid", f"cards.results[{index}]") == scryfall_id:
                    return req_int(card, "id", f"cards.results[{index}]")
        except ShapeError as error:
            raise ArchidektError(f"unexpected card search answer from Archidekt: {error}") from error
        return None

    def account_labels(self) -> list[tuple[str, str]]:
        """The card labels the signed-in account keeps for all its decks, as (name, colour)."""
        if self._headers is None:
            raise AuthError("reading your labels needs an Archidekt login; run `cmdr login`")
        try:
            raw = self._fetcher.get_json(f"{self._base_url}/decks/colorTags/", headers=self._headers)
        except HttpStatusError as error:
            if error.status in _UNAUTHORIZED:
                raise AuthError("Archidekt refused the login; run `cmdr login` again") from None
            raise
        try:
            return parse_account_labels(raw)
        except ShapeError as error:
            raise ArchidektError(f"unexpected labels from Archidekt: {error}") from error

    def create_snapshot(self, deck_id: int, name: str, description: str) -> None:
        self._write("POST", f"/decks/{deck_id}/snapshots/", {"name": name, "description": description})

    def modify_cards(self, deck_id: int, edits: Sequence[CardEdit]) -> None:
        if not edits:
            raise ValueError("no edits to send")
        cards = [edit_json(edit) for edit in edits]
        self._write("PATCH", f"/decks/{deck_id}/modifyCards/v2/", {"cards": cards})

    def _write(self, method: str, path: str, payload: object) -> None:
        if self._headers is None:
            raise AuthError("saving needs an Archidekt login; run `cmdr login`")
        try:
            self._fetcher.send_json(method, self._base_url + path, payload, headers=self._headers)
        except HttpStatusError as error:
            if error.status in _UNAUTHORIZED:
                raise AuthError(
                    f"Archidekt refused the change (HTTP {error.status}); is the deck yours? "
                    "If it is, run `cmdr login` again"
                ) from None
            if error.status == _BAD_REQUEST:
                raise ArchidektError(f"Archidekt rejected the change (HTTP 400): {error.detail}") from None
            if error.status == _TOO_MANY:
                raise ArchidektError(
                    "Archidekt is limiting requests; wait a few minutes and try again"
                ) from None
            raise


def edit_json(edit: CardEdit) -> dict[str, object]:
    """One entry of a modifyCards/v2 request, shaped as archidekt.com's deck editor sends it."""
    state = edit.state
    entry: dict[str, object] = {
        "action": edit.kind.value,
        "cardid": edit.card_id,
        "customCardId": None,
        "categories": list(state.categories),
        # The editor matches its answer to requests by this id; any unique string works.
        "patchId": uuid.uuid4().hex,
        "modifications": {
            "quantity": edit.quantity,
            "modifier": state.modifier,
            "customCmc": state.custom_cmc,
            "companion": state.companion,
            "flippedDefault": state.flipped_default,
            "label": state.label,
        },
    }
    if edit.entry_id is not None:
        entry["deckRelationId"] = edit.entry_id
    return entry


def parse_entry_states(raw: object) -> dict[int, EntryState]:
    states: dict[int, EntryState] = {}
    for index, item in enumerate(as_list(as_object(raw, "deck").get("cards"), "deck.cards")):
        where = f"deck.cards[{index}]"
        entry = as_object(item, where)
        if entry.get("deletedAt") is not None:
            continue
        companion = entry.get("companion")
        flipped = entry.get("flippedDefault")
        states[req_int(entry, "id", where)] = EntryState(
            categories=str_list(entry, "categories", where),
            label=opt_str(entry, "label", where) or DEFAULT_LABEL,
            modifier=opt_str(entry, "modifier", where) or "Normal",
            companion=req_bool(entry, "companion", where) if companion is not None else False,
            flipped_default=req_bool(entry, "flippedDefault", where) if flipped is not None else False,
            custom_cmc=_opt_number(entry, "customCmc", where),
        )
    return states


def _opt_number(obj: Mapping[str, object], key: str, where: str) -> float | None:
    value = obj.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ShapeError(f"{where}.{key}: expected a number")
    return value
