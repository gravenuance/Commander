"""The app's Meta section: each key deck card with the meta cards that stop it, and each meta combo with the
deck cards that stop it, both tagged with the meta decks that play them."""

from collections.abc import Sequence

from commander.meta import Threat
from commander.metacombos import MetaCombo
from commander.models import short_commander_name

type Json = dict[str, object]


def piece_threats(pieces: Sequence[str], threats: Sequence[Threat]) -> list[Json]:
    """Every key piece with the threats that hit it, most exposed first; pieces nothing hits come last."""
    rows: list[tuple[float, str, list[Json]]] = []
    for piece in pieces:
        hitting = sorted((t for t in threats if piece in t.hits), key=lambda t: (-t.exposure, t.name))
        rows.append(
            (
                sum(t.exposure for t in hitting),
                piece,
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
    rows.sort(key=lambda row: (-row[0], row[1].casefold()))
    return [{"card": piece, "threats": found} for _, piece, found in rows]


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
