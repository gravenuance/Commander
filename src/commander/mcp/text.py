"""Text that crosses the tool boundary: third-party data is cleaned on the way out, arguments checked on the
way in."""

import unicodedata
from collections.abc import Mapping, Sequence

from mcp.server.mcpserver.exceptions import ToolError

# Newline and tab keep a card's rules text and a note readable; every other control character is dropped.
_KEPT_CONTROLS = frozenset("\n\t")
# Bidirectional overrides and isolates and the line and paragraph separators can reorder or hide text.
_DROPPED = frozenset(chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x2028, 0x2029))

MAX_NAME_CHARS = 200
MAX_TEXT_CHARS = 2000


class ArgumentError(ToolError):
    """A tool argument the command line would misread or that holds control characters."""


def clean_text(text: str) -> str:
    """The text without control characters (newline and tab stay) and without bidirectional overrides."""
    return "".join(
        char
        for char in text
        if char not in _DROPPED and (char in _KEPT_CONTROLS or unicodedata.category(char) != "Cc")
    )


def clean_json(value: object) -> object:
    """A parsed JSON document with every string, key and value, cleaned."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, Mapping):
        items: Mapping[object, object] = value  # pyright: ignore[reportUnknownVariableType]
        return {clean_text(str(key)): clean_json(item) for key, item in items.items()}
    if isinstance(value, Sequence):
        elements: Sequence[object] = value  # pyright: ignore[reportUnknownVariableType]
        return [clean_json(item) for item in elements]
    return value


def name_arg(value: str, what: str, *, limit: int = MAX_NAME_CHARS) -> str:
    """A deck query, card name or other positional value: trimmed, and never one `cmdr` would read as flag."""
    text = free_text(value, what, limit=limit).strip()
    if not text:
        raise ArgumentError(f"{what} is empty")
    if text.startswith("-"):
        raise ArgumentError(f"{what} must not start with '-'")
    return text


def free_text(value: str, what: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """A reason, note or other text: bounded, and free of control characters."""
    if len(value) > limit:
        raise ArgumentError(f"{what} is longer than {limit} characters")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ArgumentError(f"{what} holds control characters")
    return value


def flag(name: str, value: str) -> str:
    """`--name=value`, the one form that keeps a value starting with '-' from being read as another flag."""
    return f"--{name}={value}"
