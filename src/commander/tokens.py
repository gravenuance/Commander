"""The tokens, emblems and helper cards a deck's cards can make (ADR 0061).

Scryfall links each card to the tokens it makes. A card that copies a token (populate, "create a token that's
a copy of") makes the Copy marker even when Scryfall does not link it, and a card that says it creates a
token, or makes a helper card or an emblem, but links none of that kind is left for the user to check.
"""

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from commander.carddb import COPY_TOKEN_NAME, CardDb, CardToken, ResolvedCard, TokenKind, name_keys
from commander.models import Deck

log = logging.getLogger(__name__)

# "a token that's a copy", "tokens that are copies", "a token copy of", "a copy of target token", "the copy
# becomes a token", and the keywords populate and myriad (as a keyword, not in a card's name).
COPIES_A_TOKEN = re.compile(
    r"\btokens? (?:that(?:['\u2019]s| is| are)|which (?:is|are)) (?:an? )?cop(?:y|ies)\b"
    r"|\btoken cop(?:y|ies)\b"
    r"|\bcop(?:y|ies) of [^.]*\btokens?\b"
    r"|\bcop(?:y|ies) becomes? (?:an? )?tokens?\b"
    r"|\bpopulate\b"
    r"|(?:^|[\n,] ?)myriad\b(?! (?-i:[A-Z]))",
    re.IGNORECASE,
)
CREATES_A_TOKEN = re.compile(r"\bcreates?\b[^.\n]*\btokens?\b", re.IGNORECASE)
# Token doublers ("an effect would create one or more tokens", "whenever you create or sacrifice a token")
# make none themselves.
_NOT_CREATING = re.compile(r"\bwould create\b|\bcreates? or sacrifices?\b", re.IGNORECASE)
_SENTENCES = re.compile(r"(?<=\.)\s+|\n")


@dataclass(slots=True, frozen=True)
class _HelperWording:
    """Text that says a card makes a helper card or an emblem, and which of the card's linked tokens is it."""

    says: re.Pattern[str]
    is_made: Callable[[CardToken], bool]


# Scryfall misses some of these links (12 of the 60 cards that make you the monarch), so a card with the
# wording and no linked token of the kind is left to check by hand.
_HELPER_WORDINGS = (
    _HelperWording(
        re.compile(r"\bbecomes? the monarch\b", re.IGNORECASE), lambda t: t.name.startswith("The Monarch")
    ),
    _HelperWording(re.compile(r"\bgets? an emblem\b", re.IGNORECASE), lambda t: t.kind is TokenKind.EMBLEM),
    _HelperWording(
        re.compile(r"\btakes? the initiative\b|\bventure into\b", re.IGNORECASE),
        lambda t: t.type_line.startswith("Dungeon"),
    ),
    _HelperWording(
        re.compile(r"\bthe Ring tempts you\b", re.IGNORECASE), lambda t: t.name.startswith("The Ring")
    ),
    _HelperWording(
        re.compile(r"(?:^|[\n,] ?)ascend\b", re.IGNORECASE), lambda t: t.name.startswith("City's Blessing")
    ),
)
# The Copy marker as a token row when the card data has no record of it.
_UNLISTED_COPY = CardToken(
    COPY_TOKEN_NAME.casefold(), COPY_TOKEN_NAME, TokenKind.HELPER, "Token", None, None, "", "", ""
)
_REMINDER_TEXT = re.compile(r"\s*\([^)]*\)")
SHORT_TEXT_CHARS = 40


@dataclass(slots=True, frozen=True)
class DeckToken:
    token: CardToken
    # The deck's cards that make it, by name.
    makers: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DeckTokens:
    # Most makers first, then by name.
    tokens: tuple[DeckToken, ...]
    # Cards whose text says they create a token that Scryfall links to none, and that do not copy one.
    check_by_hand: tuple[str, ...]


def deck_tokens(db: CardDb, cards: Sequence[ResolvedCard]) -> DeckTokens:
    """What the cards (a deck's main cards, resolved) make; a card the card data does not know makes none."""
    known = [(r.card.name, r.info.oracle_id) for r in cards if r.info is not None]
    oracle_ids = {oracle_id for _, oracle_id in known}
    linked = db.tokens_of(oracle_ids)
    texts = db.card_texts(oracle_ids)
    makers: dict[str, set[str]] = {}
    records: dict[str, CardToken] = {}
    copy_makers: set[str] = set()
    copy_marker: CardToken | None = None
    unchecked: set[str] = set()
    for name, oracle_id in known:
        text = texts[oracle_id].text if oracle_id in texts else ""
        own = linked.get(oracle_id, ())
        for token in own:
            if token.name == COPY_TOKEN_NAME:
                copy_marker = token
                continue
            makers.setdefault(token.oracle_id, set()).add(name)
            records[token.oracle_id] = token
        # Embalm and eternalize copy the card itself into a token of its own name, which is no Copy marker's.
        makes_itself = any(token.name.casefold() in name_keys(name) for token in own)
        copies = COPIES_A_TOKEN.search(text) or any(token.name == COPY_TOKEN_NAME for token in own)
        if copies and not makes_itself:
            copy_makers.add(name)
        elif (not own and creates_a_token(text)) or missing_helper(text, own):
            unchecked.add(name)
    if copy_makers:
        copy = copy_marker or _copy_token(db)
        makers[copy.oracle_id] = copy_makers
        records[copy.oracle_id] = copy
    rows = [DeckToken(records[o], tuple(sorted(names, key=_name_order))) for o, names in makers.items()]
    rows.sort(key=lambda r: (-len(r.makers), r.token.name.casefold(), r.token.oracle_id))
    return DeckTokens(tuple(rows), tuple(sorted(unchecked, key=_name_order)))


def creates_a_token(text: str) -> bool:
    """Whether the rules text creates a token; a sentence about what an effect "would create" or about
    creating or sacrificing one is a token doubler's, not a maker's."""
    return any(
        CREATES_A_TOKEN.search(sentence) and not _NOT_CREATING.search(sentence)
        for sentence in _SENTENCES.split(text)
    )


def missing_helper(text: str, linked: Iterable[CardToken]) -> bool:
    """Whether the text says the card makes a helper card or an emblem of which it links none."""
    return any(
        wording.says.search(text) and not any(wording.is_made(token) for token in linked)
        for wording in _HELPER_WORDINGS
    )


def tokens_of_deck(db: CardDb, deck: Deck) -> DeckTokens:
    return deck_tokens(db, db.resolve(deck.main_cards))


def colour_letters(token: CardToken) -> str:
    """The token's colours as WUBRG letters, C for colourless."""
    return token.colors or "C"


def stats(token: CardToken) -> str:
    """Power and toughness as "1/1"; empty for a token without them."""
    if token.power is None or token.toughness is None:
        return ""
    return f"{token.power}/{token.toughness}"


def short_text(token: CardToken, limit: int = SHORT_TEXT_CHARS) -> str:
    """The token's rules text on one line, without reminder text, cut to limit characters."""
    lines = (
        _REMINDER_TEXT.sub("", line).strip().removesuffix(".") for line in token.oracle_text.splitlines()
    )
    text = "; ".join(line for line in lines if line)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    # A cut inside a mana symbol drops the symbol whole: "{G}{" would read as a typo.
    if cut.rfind("{") > cut.rfind("}"):
        cut = cut[: cut.rfind("{")]
    return cut.rstrip() + "…"


def hint(token: CardToken) -> str:
    """What a creature token does, in a few words ("Lifelink"); empty for any other token."""
    is_creature = token.kind is TokenKind.TOKEN and "Creature" in token.type_line.split()
    return short_text(token) if is_creature else ""


def describe(token: CardToken) -> str:
    """A token in a line: "W 1/1 Vampire (Lifelink)"; an emblem or helper card is just its name."""
    if token.kind is not TokenKind.TOKEN:
        return token.name
    description = " ".join(part for part in (colour_letters(token), stats(token), token.name) if part)
    text = hint(token)
    return f"{description} ({text})" if text else description


def token_json(token: CardToken) -> dict[str, object]:
    return {
        "oracleId": token.oracle_id,
        "name": token.name,
        "kind": token.kind.value,
        "typeLine": token.type_line,
        "power": token.power,
        "toughness": token.toughness,
        "colours": token.colors,
        "text": token.oracle_text,
        "scryfallId": token.scryfall_id,
    }


def _copy_token(db: CardDb) -> CardToken:
    found = db.token_named(COPY_TOKEN_NAME)
    if found is None:
        log.warning("the card data has no %s marker; listing it without an image", COPY_TOKEN_NAME)
    return found or _UNLISTED_COPY


def _name_order(name: str) -> tuple[str, str]:
    return name.casefold(), name
