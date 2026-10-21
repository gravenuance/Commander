# 0052: Take a card off the Sideboard or Maybeboard from the app

**Status:** Accepted · **Date:** 2026-10-21 · An exception to the saves of
[ADR 0035](0035-retire-the-maybeboard-and-sideboard.md); amends the groups outside the deck of
[ADR 0044](0044-the-sideboard-and-maybeboard-shown-again.md) and the count changes of
[ADR 0043](0043-count-changes-and-adds-without-a-cut-on-the-board.md)

## Context

ADR 0044 showed the Sideboard, the Maybeboard and the deck's other categories outside the deck as groups of the
grid, with no + or −, because a save would then have to send edits for them, which ADR 0035 avoids. The owner
asked (2026-10-21) to be able to remove a card from the Sideboard and the Maybeboard in the app, so the clutter
ADR 0044 made visible again can be cleared without going to archidekt.com.

ADR 0035's rule is that a save sends no edit the user did not ask for: other users keep cards there on purpose.
A removal the user queues on one card is such a request, so the rule holds for everything else outside the deck.

## Options

1. **A second board list of outside removals.** Its own queue, undo, plan line, save marking and sync
   clearing: a copy of what count changes already do.
2. **A count change with a category.** `CountChange` gains `category`, None for the deck's copies; set, the
   change is to the copies filed under that category outside the deck, and may only take them all off. The
   plan, Review, save marking and sync clearing carry it with a key of card and category.
3. **A save that empties the Maybeboard**, as ADR 0035's first version did. Refused there for good reason.

## Decision

Option 2.

- **Board version 4.** `counts` entries gain `category` (null for the deck). A version 3 board reads with every
  change on the deck and is kept as `board.v3.bak` on its first save; an older `cmdr` refuses a version 4
  board rather than reading a Maybeboard removal as a cut from the deck.
- **One change per card and place.** `Board.count_of(name, category)` and `CountChange.key` tell the deck's
  copies of a card from its Maybeboard copies, so both may be queued at once, and neither blocks a swap of the
  other. A change with a category has count 0; `step_count(..., category=)` takes every copy off with − and
  puts them back with +, and `set_count` accepts 0 or the copies held there. The category must hold the card
  outside the deck; a category the deck counts is refused, since its cards change through the deck's own + and −.
- **The plan** gives `RemoveCard` a `category`, also in plan files (`{name = "Ponder", category =
  "Maybeboard"}`), and `board plan` writes it. `apply` cuts only the entries outside the deck whose first
  category is that one; the deck's copies and every other card outside the deck get no edit. Such a removal
  changes no total and no stat; a line the deck already meets is skipped ("not on Maybeboard").
- **Review** lists it as "*card* from the Maybeboard" (a category of the user's keeps its bare name). A
  mismatch after a save names the card with its category, so a removal Archidekt did not make stays queued
  while the deck's own change to the same card is done.
- **Sync** clears a removal the deck on Archidekt already made, like a met count change.
- **App:** the bridge's `count(deck, name, step, category)`; on a card outside the deck, − ("Remove *card*
  from the Maybeboard") and, once queued, + ("Keep *card* on the Maybeboard"), the card greyed with "Coming
  out" as a deck card is. Every category outside the deck gets them, not only Sideboard and Maybeboard, as
  ADR 0044 groups them by `in_deck`.

## Consequences

- A save still sends edits only for what the board queued; the Maybeboard changes only where the user said.
- Count-change code paths take a category; a new caller that drops it addresses the deck's copies, the safe side.
- Adding a card that sits on the Maybeboard still adds a new entry (ADR 0035); taking the old entry off is now
  one more − on it.
