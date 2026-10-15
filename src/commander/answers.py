"""What a card does to other cards, read from Tagger tags: shared by the meta threat and combo reports."""

from collections.abc import Sequence
from enum import Enum

from commander.carddb import CardDb, Theme


def card_key(name: str) -> str:
    """The name lists are compared by: the front face, casefolded, as sites spell two-faced cards apart."""
    return name.casefold().split(" // ")[0]


class AnswerKind(Enum):
    """What a meta card does to a key piece, by the Tagger tags that say so and the piece types it reaches."""

    # Widest reach first, so a card's first kind is the broadest thing it does.
    COUNTERSPELL = ("counterspell", ("counterspell",), "any")
    # Most sweepers clear creatures; ones that reach other permanents carry a removal tag as well.
    SWEEPER = ("sweeper", ("sweeper",), "Creature")
    PERMANENT_REMOVAL = (
        "permanent removal",
        ("removal-nonland", "removal-permanent", "removal-bounce"),
        "permanent",
    )
    CREATURE_REMOVAL = ("creature removal", ("removal-creature",), "Creature")
    ARTIFACT_REMOVAL = ("artifact removal", ("removal-artifact",), "Artifact")
    ENCHANTMENT_REMOVAL = ("enchantment removal", ("removal-enchantment",), "Enchantment")

    def __init__(self, label: str, slugs: tuple[str, ...], reaches: str) -> None:
        self.label = label
        self.slugs = slugs
        # A card type word, "permanent" for any nonland permanent, or "any" for every piece.
        self.reaches = reaches

    def hits(self, type_line: str) -> bool:
        """Whether this kind of answer reaches a card of this type line (front face of a two-faced card)."""
        words = type_line.split(" // ", maxsplit=1)[0].split()
        if self.reaches == "any":
            return True
        if self.reaches == "permanent":
            return not ({"Instant", "Sorcery"} & set(words)) and "Land" not in words
        return self.reaches in words


def answer_kinds(db: CardDb, oracle_ids: Sequence[str]) -> dict[AnswerKind, frozenset[str]]:
    slugs = {slug for kind in AnswerKind for slug in kind.slugs}
    ids = db.tag_ids(slugs)
    return {
        kind: db.matching(Theme(tag_ids=tuple(ids[s] for s in kind.slugs if s in ids)), oracle_ids)
        for kind in AnswerKind
    }
