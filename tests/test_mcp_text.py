"""What crosses the MCP tool boundary: third-party text cleaned going out, arguments checked going in."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from commander.mcp.text import ArgumentError, clean_json, clean_text, free_text, name_arg

RLO, PDF, LRI, PDI, ZWJ = (chr(0x202E), chr(0x202C), chr(0x2066), chr(0x2069), chr(0x200D))


def test_control_characters_and_bidirectional_overrides_are_dropped() -> None:
    dirty = f"Sol\x1b[31m Ring\x00\x07 {RLO}gnirts{PDF}{LRI}x{PDI} end\x7f\x85  !"
    assert clean_text(dirty) == "Sol[31m Ring gnirtsx end !"


def test_newlines_tabs_and_emoji_survive() -> None:
    text = f"Tap: add {{R}}.\n\tThen\r\nlook \U0001f525 \U0001f468{ZWJ}\U0001f469 — done"
    assert clean_text(text) == f"Tap: add {{R}}.\n\tThen\nlook \U0001f525 \U0001f468{ZWJ}\U0001f469 — done"


def test_every_string_of_a_json_document_is_cleaned_keys_included() -> None:
    document = {"na\x1bme": ["a\x00b", {"x\x07": f"y{RLO}"}, 3, None, True, 1.5]}
    assert clean_json(document) == {"name": ["ab", {"x": "y"}, 3, None, True, 1.5]}


def test_a_name_is_trimmed() -> None:
    assert name_arg("  Sol Ring ", "card") == "Sol Ring"


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "card is empty"),
        ("   ", "card is empty"),
        ("--version", "card must not start with '-'"),
        (" -x", "card must not start with '-'"),
        ("Sol\x00Ring", "card holds control characters"),
        ("Sol\nRing", "card holds control characters"),
        ("a" * 201, "card is longer than 200 characters"),
    ],
)
def test_a_name_that_cmdr_would_misread_is_refused(value: str, message: str) -> None:
    with pytest.raises(ArgumentError, match=message):
        name_arg(value, "card")


def test_free_text_keeps_a_leading_dash_and_refuses_control_characters() -> None:
    assert free_text("- protects stax pieces", "reason") == "- protects stax pieces"
    with pytest.raises(ToolError, match="reason holds control characters"):
        free_text("a\tb", "reason")
