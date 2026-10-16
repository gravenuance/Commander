"""The app's Meta section: the deck's own combos and its key cards with the meta cards that stop them, and
each meta combo with the deck cards that stop it, all tagged with the meta decks that play them."""

from collections.abc import Sequence

from commander.keycards import KeyCard
from commander.meta import OwnCombo, Threat
from commander.metacombos import MetaCombo
from commander.models import short_commander_name

type Json = dict[str, object]


def piece_threats(keys: Sequence[KeyCard], threats: Sequence[Threat]) -> list[Json]:
    """Every key card with why and the threats that hit it, most exposed first; untouched ones come last."""
    rows: list[tuple[float, KeyCard, list[Json]]] = []
    for key in keys:
        hitting = sorted((t for t in threats if key.name in t.hits), key=lambda t: (-t.exposure, t.name))
        rows.append(
            (
                sum(t.exposure for t in hitting),
                key,
                [
                    {
                        "name": t.name,
                        "kinds": [kind.label for kind in t.kinds],
                        "exposure": round(t.exposure, 4),
                        "decks": _decks(t.played_by),
                    }
                    for t in hitting
                ],
            )
        )
    rows.sort(key=lambda row: (-row[0], row[1].name.casefold()))
    return [{"card": key.name, "reasons": list(key.reasons), "threats": found} for _, key, found in rows]


def own_combo_rows(combos: Sequence[OwnCombo]) -> list[Json]:
    """The deck's combos with the meta cards that stop each: stax, then removal, then counters."""
    return [
        {
            "pieces": list(combo.pieces),
            "results": list(combo.results),
            "url": combo.url,
            "answers": [
                {
                    "card": a.card,
                    "how": a.how,
                    "role": a.role.value,
                    "catch": a.catch,
                    "exposure": round(a.exposure, 4),
                    "decks": _decks(a.played_by),
                }
                for a in combo.answers
            ],
        }
        for combo in combos
    ]


def combo_rows(combos: Sequence[MetaCombo]) -> list[Json]:
    """The meta's combos as the report ranks them, each with the deck cards that stop it."""
    return [
        {
            "pieces": list(combo.pieces),
            "results": list(combo.results),
            "url": combo.url,
            "exposure": round(combo.exposure, 4),
            "decks": _decks(combo.played_by),
            "answers": [
                {"card": a.card, "how": a.how, "role": a.role.value, "catch": a.catch} for a in combo.answers
            ],
        }
        for combo in combos
    ]


def _decks(played_by: Sequence[tuple[str, float]]) -> list[Json]:
    return [
        {"short": short_commander_name(name), "name": name, "share": round(share, 4)}
        for name, share in played_by
    ]
