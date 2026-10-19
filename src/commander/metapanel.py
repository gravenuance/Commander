"""The app's Meta section: the deck's key cards; each meta card that stops one of the deck's combos or
hits a key card, once, with what it stops; and each meta combo with the deck cards that stop it, all
tagged with the meta decks that play them."""

from collections.abc import Sequence
from dataclasses import dataclass, field

from commander.answers import AnswerKind
from commander.keycards import KeyCard
from commander.meta import OwnCombo, Threat
from commander.metacombos import AnswerRole, MetaCombo, group_combos
from commander.models import short_commander_name

type Json = dict[str, object]


def key_card_rows(keys: Sequence[KeyCard]) -> list[Json]:
    return [{"name": key.name, "reasons": list(key.reasons)} for key in keys]


def stopper_rows(combos: Sequence[OwnCombo], threats: Sequence[Threat]) -> list[Json]:
    """Each meta card that stops one of the deck's combos or hits a key card, once, with everything it stops.

    A card is filed under the first way it stops anything: stax, then removal, then counters. Within each,
    the cards that show up most come first.
    """
    rows: dict[str, _Stopper] = {}
    for combo in combos:
        label = " + ".join(combo.pieces)
        for answer in combo.answers:
            row = rows.setdefault(answer.card, _Stopper(answer.exposure, answer.played_by))
            row.roles.add(answer.role)
            how = answer.how if answer.role is AnswerRole.STAX else answer.catch
            row.combos.append({"pieces": label, "url": combo.url, "how": how})
            row.in_combos.update(combo.pieces)
    for threat in threats:
        row = rows.setdefault(threat.name, _Stopper(threat.exposure, threat.played_by))
        counters = AnswerKind.COUNTERSPELL in threat.kinds
        row.roles.add(AnswerRole.COUNTER if counters else AnswerRole.REMOVAL)
        row.keys.extend(name for name in threat.hits if name not in row.keys)
        row.kinds.extend(kind.label for kind in threat.kinds if kind.label not in row.kinds)
    order = list(AnswerRole)
    ranked = sorted(rows.items(), key=lambda item: (order.index(item[1].role), -item[1].exposure, item[0]))
    return [
        {
            "card": card,
            "role": row.role.value,
            "exposure": round(row.exposure, 4),
            "decks": _decks(row.played_by),
            "combos": row.combos,
            # A key card in a combo the row already stops would say the same twice.
            "keys": [name for name in row.keys if name not in row.in_combos],
            "kinds": row.kinds,
        }
        for card, row in ranked
    ]


def untouched(keys: Sequence[KeyCard], combos: Sequence[OwnCombo], threats: Sequence[Threat]) -> Json:
    """The key cards nothing in the meta hits and the deck's combos nothing stops."""
    hit = {name for threat in threats for name in threat.hits}
    return {
        "keys": [key.name for key in keys if key.name not in hit],
        "combos": [{"pieces": " + ".join(c.pieces), "url": c.url} for c in combos if not c.answers],
    }


@dataclass(slots=True)
class _Stopper:
    exposure: float
    played_by: tuple[tuple[str, float], ...]
    roles: set[AnswerRole] = field(default_factory=set[AnswerRole])
    combos: list[Json] = field(default_factory=list[Json])
    keys: list[str] = field(default_factory=list[str])
    kinds: list[str] = field(default_factory=list[str])
    in_combos: set[str] = field(default_factory=set[str])

    @property
    def role(self) -> AnswerRole:
        """The first way it stops anything, in the order a stax deck reaches for them."""
        return min(self.roles, key=list(AnswerRole).index)


def combo_rows(combos: Sequence[MetaCombo]) -> list[Json]:
    """The meta's combos as the report ranks them, each with the deck cards that stop it."""
    return [
        {
            "pieces": list(combo.pieces),
            "results": list(combo.results),
            "url": combo.url,
            "exposure": round(combo.exposure, 4),
            "rising": combo.rising,
            "decks": _decks(combo.played_by),
            "answers": [
                {"card": a.card, "how": a.how, "role": a.role.value, "catch": a.catch} for a in combo.answers
            ],
        }
        for combo in combos
    ]


def combo_groups(combos: Sequence[MetaCombo]) -> list[Json]:
    """The meta's combos grouped by shared pieces (ADR 0038), each combo row with its `others`: the pieces
    outside the group's shared ones. A combo on its own has no shared pieces and all of them as `others`."""
    rows = {combo.id: row for combo, row in zip(combos, combo_rows(combos), strict=True)}
    return [
        {
            "shared": list(group.shared),
            "combos": [
                {**rows[combo.id], "others": [p for p in combo.pieces if p not in group.shared]}
                for combo in group.combos
            ],
        }
        for group in group_combos(combos)
    ]


def _decks(played_by: Sequence[tuple[str, float]]) -> list[Json]:
    return [
        {"short": short_commander_name(name), "name": name, "share": round(share, 4)}
        for name, share in played_by
    ]
