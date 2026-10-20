# 0044: The Sideboard and Maybeboard shown again, outside the deck, with stats above the decklist

**Status:** Accepted · **Date:** 2026-10-20 · Amends the app part of
[ADR 0035](0035-retire-the-maybeboard-and-sideboard.md)

## Context

ADR 0035 stopped `cmdr` reading the Sideboard and Maybeboard as a source of ideas; their cards stayed in
`deck.json` as the deck's mirror, but the app no longer showed them. The owner asked (2026-10-20) to see them
under the decklist as categories of their own, with the same share pills, while they stay out of the deck:
out of every total, and still open to the ideas. They also asked for stats above the decklist: Don't Have
cards, cards in the deck, the Don't Have cost, and the average mana value of the spells.

Archidekt marks each category "included in deck" or not; `archidekt.category_flags` reads it, and a card is in
the deck when its first category counts (`DeckCard.in_deck`). Maybeboard and Sideboard are only the default
names: the owner's fixture deck also keeps a category "TO ADD" outside the deck.

## Options

For which cards to show apart:

1. **By name, Maybeboard and Sideboard.** Misses the owner's own outside categories and misfiles a Sideboard
   ticked as counted.
2. **By `in_deck`,** grouped by the card's first category.

For the stats while changes are queued:

1. **The saved deck only.** Never wrong about Archidekt, but blind to what Review would do.
2. **The deck after Review only.** Matches the queued view, but hides what is saved.
3. **Both: the saved figure, and "saved → after Review" where the queued changes alter it.**

## Decision

Option 2 for the groups and option 3 for the stats.

- The grid ends with one group per category outside the deck (Sideboard, then Maybeboard, then the others by
  name), keyed apart from the deck's groups and marked *Not in deck*. Their cards carry the share pill but no
  label flag, no + or −, and are never cuts or targets.
- Nothing about ideas changes: they were already built from `Deck.main_cards`, so a Maybeboard card is not in
  the deck and can be an idea; a test now holds that. The Mana tab's land options left out every card of the
  deck folder, Maybeboard lands too; they now leave out only the deck's.
- Stats (`deckstats`): cards in the deck, Don't Have copies and their cost by each printing's price (copies
  without a price are counted apart), and the average mana value per copy of the cards that are not lands,
  commanders included. The "after" figures run the board's plan (`board_plan`) over the saved deck: an add
  arrives as Don't Have at its cheapest price, more copies keep their entry's label, a cut or count change
  lowers the copies.
- A save still leaves the cards outside the deck alone (ADR 0035).

## Consequences

- The app shows everything `sync` keeps; the Maybeboard's clutter is visible again, as the owner asked.
- The "after" stats are computed from the local copy; Review's preview, made from the live deck, is the word
  before a save.
- Revisit if the owner wants + and − or labels on cards outside the deck: a save would then have to send edits
  for them, which ADR 0035 avoids.
