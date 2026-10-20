"""The numbers above the app's decklist: cards in the deck, Don't Have cards and what they cost, and the
average mana value of the spells; for the deck as saved and as the board's queued changes would leave it."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from commander.answers import card_key
from commander.board import Board, board_plan
from commander.carddb import CardDb, ResolvedCard
from commander.models import DONT_HAVE_LABEL, Deck
from commander.pricing import card_price


@dataclass(slots=True, frozen=True)
class StatCard:
    name: str
    quantity: int
    label: str
    # Per copy; None when no price is known.
    unit_cents: int | None
    mana_value: float
    land: bool


@dataclass(slots=True, frozen=True)
class DeckStats:
    cards: int
    dont_have: int
    dont_have_cents: int
    # Don't Have copies without a price, left out of dont_have_cents.
    dont_have_unpriced: int
    # Per copy, over the cards in the deck that are not lands; None when there are none.
    average_mana_value: float | None

    def json(self) -> dict[str, object]:
        average = self.average_mana_value
        return {
            "cards": self.cards,
            "dontHave": self.dont_have,
            "dontHaveCents": self.dont_have_cents,
            "dontHaveUnpriced": self.dont_have_unpriced,
            "averageManaValue": round(average, 2) if average is not None else None,
        }


def deck_stats(cards: Iterable[StatCard]) -> DeckStats:
    counted = [card for card in cards if card.quantity > 0]
    missing = [card for card in counted if card.label == DONT_HAVE_LABEL]
    spells = [card for card in counted if not card.land]
    copies = sum(card.quantity for card in spells)
    return DeckStats(
        cards=sum(card.quantity for card in counted),
        dont_have=sum(card.quantity for card in missing),
        dont_have_cents=sum((card.unit_cents or 0) * card.quantity for card in missing),
        dont_have_unpriced=sum(card.quantity for card in missing if card.unit_cents is None),
        average_mana_value=sum(card.mana_value * card.quantity for card in spells) / copies
        if copies
        else None,
    )


def stat_cards(main: Sequence[ResolvedCard]) -> list[StatCard]:
    """The deck's own cards, priced by their printings as the deck's value is."""
    return [
        StatCard(
            r.card.name,
            r.card.quantity,
            r.card.label or "",
            card_price(r).unit_cents,
            r.info.cmc if r.info is not None else 0.0,
            r.info.is_land if r.info is not None else False,
        )
        for r in main
        if r.card.in_deck
    ]


def stats_after(deck: Deck, main: Sequence[ResolvedCard], board: Board, db: CardDb) -> DeckStats:
    """The stats once the board's queued changes are saved, by the plan a save would send from this deck:
    an added card arrives as Don't Have (ADR 0010) at its cheapest price, more copies keep their entry's
    label, and a cut or a count change lowers the copies."""
    cards = {card_key(card.name): card for card in stat_cards(main)}
    plan = board_plan(deck, board)
    for cut in plan.remove if plan else ():
        key = card_key(cut.name)
        if key in cards:
            cards[key] = replace(cards[key], quantity=min(cards[key].quantity, cut.keep))
    for add in plan.add if plan else ():
        key = card_key(add.name)
        if key in cards:
            cards[key] = replace(cards[key], quantity=max(cards[key].quantity, add.quantity))
            continue
        info = db.card_named(add.name)
        cards[key] = StatCard(
            add.name,
            add.quantity,
            DONT_HAVE_LABEL,
            info.cheapest_cents if info else None,
            info.cmc if info else 0.0,
            info.is_land if info else False,
        )
    return deck_stats(cards.values())
