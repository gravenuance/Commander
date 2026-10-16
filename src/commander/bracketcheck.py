"""Why a deck's bracket estimate disagrees with the bracket it is set to (ADR 0021).

The causes follow Commander Spellbook's own `estimate_bracket` (backend/spellbook/models/variant.py, MIT),
so each one names the cards or the combo that lifted Spellbook's tag. When Spellbook's tag and these rules
disagree, Spellbook changed its rules, and the check says the causes may be incomplete.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from commander.spellbook import BracketEstimate, BracketTag, CardFlag, Combo, ComboFlag, FlaggedCard, TwoCard

log = logging.getLogger(__name__)

MAX_GAME_CHANGERS = 3
MIN_EXTRA_TURN_CARDS = 2
# Spellbook's combo speed: 4 and up needs at most 4 mana, 3 at most 6, 2 at most 8.
FAST = 4
MEDIUM = 3
SLOW = 2


class Reason(Enum):
    BANNED = "not legal in Commander"
    GAME_CHANGERS = f"more than {MAX_GAME_CHANGERS} game changers"
    GAME_CHANGER = "a game changer"
    EXTRA_TURN_CARDS = f"{MIN_EXTRA_TURN_CARDS} or more extra-turn cards"
    EXTRA_TURN_CARD = "an extra-turn card"
    MASS_LAND_DENIAL = "mass land denial"
    EXTRA_TURN_COMBO = "a combo that takes extra turns"
    LAND_DENIAL_COMBO = "a combo that denies lands"
    CONTROL_ALL_COMBO = "a combo that takes control of every opponent"
    FAST_TWO_CARD = "a fast two-card combo that wins"
    FAST_NEARLY_TWO_CARD = "a fast combo of about two cards"
    LOCK_COMBO = "a combo that locks the game"
    SKIP_TURNS_COMBO = "a combo that makes players skip turns"
    CONTROL_SOME_COMBO = "a combo that takes control of an opponent"
    TWO_CARD = "a two-card combo that wins"
    NEARLY_TWO_CARD = "a combo of about two cards"
    SLOW_TWO_CARD = "a slow two-card combo that wins"


@dataclass(slots=True, frozen=True)
class Cause:
    """Something in the deck that lifts Spellbook's tag to at least tag."""

    tag: BracketTag
    reason: Reason
    cards: tuple[str, ...]
    combo: Combo | None = None


def _arguably_two(combo: Combo) -> bool:
    return combo.two_card in {TwoCard.YES, TwoCard.ARGUABLY}


def _wins(combo: Combo) -> bool:
    return ComboFlag.RELEVANT in combo.flags


def _maybe_wins(combo: Combo) -> bool:
    return ComboFlag.BORDERLINE_RELEVANT in combo.flags


# Highest tag first, so a combo is reported once, under the first rule it meets.
_COMBO_RULES: tuple[tuple[BracketTag, Reason, Callable[[Combo], bool]], ...] = (
    (BracketTag.RUTHLESS, Reason.EXTRA_TURN_COMBO, lambda c: ComboFlag.EXTRA_TURN in c.flags),
    (BracketTag.RUTHLESS, Reason.LAND_DENIAL_COMBO, lambda c: ComboFlag.MASS_LAND_DENIAL in c.flags),
    (BracketTag.RUTHLESS, Reason.CONTROL_ALL_COMBO, lambda c: ComboFlag.CONTROL_ALL_OPPONENTS in c.flags),
    (
        BracketTag.RUTHLESS,
        Reason.FAST_TWO_CARD,
        lambda c: c.speed >= FAST and _wins(c) and c.two_card is TwoCard.YES,
    ),
    (
        BracketTag.SPICY,
        Reason.FAST_NEARLY_TWO_CARD,
        lambda c: (
            c.speed >= FAST
            and ((_wins(c) and _arguably_two(c)) or (_maybe_wins(c) and c.two_card is TwoCard.YES))
        ),
    ),
    (BracketTag.SPICY, Reason.LOCK_COMBO, lambda c: ComboFlag.LOCK in c.flags),
    (BracketTag.SPICY, Reason.SKIP_TURNS_COMBO, lambda c: ComboFlag.SKIP_TURNS in c.flags),
    (BracketTag.SPICY, Reason.CONTROL_SOME_COMBO, lambda c: ComboFlag.CONTROL_SOME_OPPONENTS in c.flags),
    (
        BracketTag.POWERFUL,
        Reason.TWO_CARD,
        lambda c: c.speed >= MEDIUM and _wins(c) and c.two_card is TwoCard.YES,
    ),
    (
        BracketTag.ODDBALL,
        Reason.NEARLY_TWO_CARD,
        lambda c: c.speed >= MEDIUM and _maybe_wins(c) and _arguably_two(c),
    ),
    (
        BracketTag.CORE,
        Reason.SLOW_TWO_CARD,
        lambda c: c.speed >= SLOW and _wins(c) and c.two_card is TwoCard.YES,
    ),
)

# Banned first, then Spellbook's order from Ruthless down.
_SEVERITY = {tag: -1 if tag is BracketTag.BANNED else index for index, tag in enumerate(BracketTag)}


def estimate_causes(estimate: BracketEstimate) -> tuple[Cause, ...]:
    """Everything that lifts the deck above Exhibition, the highest tag first."""
    causes = [*_card_causes(estimate.cards)]
    for combo in estimate.combos:
        for tag, reason, applies in _COMBO_RULES:
            if applies(combo):
                causes.append(Cause(tag, reason, combo.cards, combo))
                break
    return tuple(sorted(causes, key=lambda cause: _SEVERITY[cause.tag]))


def _card_causes(cards: Sequence[FlaggedCard]) -> list[Cause]:
    def named(flag: CardFlag) -> tuple[str, ...]:
        return tuple(card.name for card in cards if flag in card.flags)

    def count(flag: CardFlag) -> int:
        return sum(card.quantity for card in cards if flag in card.flags)

    causes: list[Cause] = []
    if banned := named(CardFlag.BANNED):
        causes.append(Cause(BracketTag.BANNED, Reason.BANNED, banned))
    if game_changers := named(CardFlag.GAME_CHANGER):
        if count(CardFlag.GAME_CHANGER) > MAX_GAME_CHANGERS:
            causes.append(Cause(BracketTag.RUTHLESS, Reason.GAME_CHANGERS, game_changers))
        else:
            causes.append(Cause(BracketTag.POWERFUL, Reason.GAME_CHANGER, game_changers))
    if extra_turns := named(CardFlag.EXTRA_TURN):
        if count(CardFlag.EXTRA_TURN) >= MIN_EXTRA_TURN_CARDS:
            causes.append(Cause(BracketTag.RUTHLESS, Reason.EXTRA_TURN_CARDS, extra_turns))
        else:
            causes.append(Cause(BracketTag.CORE, Reason.EXTRA_TURN_CARD, extra_turns))
    if land_denial := named(CardFlag.MASS_LAND_DENIAL):
        causes.append(Cause(BracketTag.RUTHLESS, Reason.MASS_LAND_DENIAL, land_denial))
    return causes


@dataclass(slots=True, frozen=True)
class BracketCheck:
    # One sentence per disagreement between the set bracket, the profile's ceiling and the estimate.
    notes: tuple[str, ...]
    # What lifts the estimate above the lower of the set bracket and the ceiling, highest tag first.
    causes: tuple[Cause, ...]

    @property
    def cause_cards(self) -> frozenset[str]:
        return frozenset(name for cause in self.causes for name in cause.cards)


def check_bracket(
    set_bracket: int | None, ceiling: int | None, estimate: BracketEstimate | None
) -> BracketCheck:
    """Compare the bracket set on Archidekt, the profile's bracket ceiling and Spellbook's estimate."""
    notes: list[str] = []
    if ceiling is not None and set_bracket is not None and ceiling != set_bracket:
        side = "above" if ceiling > set_bracket else "below"
        notes.append(
            f"the profile's bracket ceiling ({ceiling}) is {side} the Archidekt bracket ({set_bracket})"
        )
    if estimate is None:
        return BracketCheck(tuple(notes), ())
    tag = estimate.tag
    causes = estimate_causes(estimate)
    expected = causes[0].tag if causes else BracketTag.EXHIBITION
    limits = [b for b in (set_bracket, ceiling) if b is not None]
    if tag is BracketTag.BANNED:
        notes.append("Commander Spellbook finds cards that are not legal in Commander")
    elif tag.minimum is not None:
        if set_bracket is not None and tag.minimum > set_bracket:
            notes.append(f"the estimate ({tag.brackets}) is above the Archidekt bracket ({set_bracket})")
        if ceiling is not None and tag.minimum > ceiling:
            notes.append(f"the estimate ({tag.brackets}) is above the profile's bracket ceiling ({ceiling})")
        if tag.maximum is not None and set_bracket is not None and set_bracket > tag.maximum:
            notes.append(f"the deck is set to bracket {set_bracket}, but its cards alone fit {tag.brackets}")
    if expected is not tag:
        log.warning("Spellbook's tag %s differs from its rules' %s", tag.label, expected.label)
        notes.append(
            f"Spellbook's tag ({tag.label}) differs from its rules ({expected.label}); "
            "causes may be incomplete"
        )
    target = min(limits) if limits else None
    above = tuple(c for c in causes if target is not None and _lifts_above(c.tag, target))
    return BracketCheck(tuple(notes), above)


def check_json(check: BracketCheck) -> dict[str, object]:
    return {
        "notes": list(check.notes),
        "causes": [
            {
                "tag": cause.tag.label,
                "brackets": cause.tag.brackets,
                "reason": cause.reason.value,
                "cards": list(cause.cards),
                "comboUrl": cause.combo.url if cause.combo else None,
            }
            for cause in check.causes
        ],
    }


def _lifts_above(tag: BracketTag, bracket: int) -> bool:
    return tag.minimum is None or tag.minimum > bracket
