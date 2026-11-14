"""Command-line pieces the tools share, built only from checked values."""

import math

from commander.mcp.text import ArgumentError, flag, name_arg

MAX_PRICE_EUR = 100_000
MAX_IDEAS = 200


def optional_deck(deck: str | None) -> list[str]:
    """The deck positional, or nothing for every deck."""
    return [] if deck is None else [name_arg(deck, "deck")]


def suggest_options(
    *,
    count: int | None,
    max_price_eur: float | None,
    any_price: bool,
    within_bracket: bool,
    refresh: bool,
) -> list[str]:
    """The flags of `cmdr suggest` that change which ideas it finds."""
    options: list[str] = []
    if count is not None:
        if not 1 <= count <= MAX_IDEAS:
            raise ArgumentError(f"count must be 1 to {MAX_IDEAS}")
        options.append(flag("count", str(count)))
    if max_price_eur is not None:
        if not math.isfinite(max_price_eur) or not 0 <= max_price_eur <= MAX_PRICE_EUR:
            raise ArgumentError(f"max_price_eur must be 0 to {MAX_PRICE_EUR}")
        options.append(flag("max-price", f"{max_price_eur:.2f}"))
    for enabled, name in (
        (any_price, "--any-price"),
        (within_bracket, "--within-bracket"),
        (refresh, "--refresh"),
    ):
        if enabled:
            options.append(name)
    return options
