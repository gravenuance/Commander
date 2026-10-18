# 0035: Retire the Maybeboard and Sideboard as a second home for ideas

**Status:** Accepted · **Date:** 2026-10-18 · Replaces the Sideboard and Maybeboard parts of
[ADR 0006](0006-deck-boards-and-desktop-app.md); the sync step that picked those cards up (ADR 0023) goes with them

## Context

ADR 0006 made a deck's Sideboard and Maybeboard on Archidekt a second home for cards under consideration.
`sync` and every board save put their cards on the board, a cut card moved to the Maybeboard with its
categories behind it, and a turned-down card was dropped from it with the next save. The Considering list
now fills itself from other players' decks (ADR 0028) and the user proposes cards in the app, so the
Maybeboard was a second source that only the user's habits on archidekt.com kept alive. It cost a move
edit with category bookkeeping in every save, a fourth source of board cards that no list backed, and a
Maybeboard that grew with every swap until someone turned cards down.

## Options

For reading the Maybeboard and Sideboard:

1. **Keep picking them up** and leave the clutter to the user.
2. **Stop reading them.** The board has one entrance fewer; `cmdr board add` and the app's Propose a card
   box stay.

For cuts:

1. **Keep moving them to the Maybeboard.**
2. **Remove the entry.** A cut card is a card not in the deck; if it passes the idea test on a later sync
   it is an idea like any other.

For what is already there:

1. **Leave the old ideas on the board and the old cards on Archidekt.**
2. **Re-judge the old ideas on a sync, and empty the Maybeboard and Sideboard with the user's next save.**

## Decision

Option 2 each time.

- **Sync reads neither list and writes nothing.** Their cards stay in `deck.json` and `list.md` as the deck's
  mirror, nothing more.
- **A cut removes the entry outright.** Plan files lose `maybeboard = true` and `drop = [...]`; a plan that
  still has them is refused as an unknown key. An add of a card that sits on the Maybeboard adds a new entry
  and the save clears the old one.
- **A save clears both lists.** The user's Review → Save, `board commit --save` and `apply --save` also
  remove every entry of the deck's Sideboard and Maybeboard that lies outside the deck (a Sideboard that the
  deck counts in the 100 is left alone), in the same `modifyCards` call and under the same snapshot. The
  preview lists them first, one line per board and the card names under it: "Clears the Maybeboard: 7
  cards". The clears sit beside the plan's changes in `Resolution.clears`, so they are not counted among the
  changes, and a plan that changes nothing, or a board whose queued cards are all in the deck already,
  clears nothing. Sync never calls this path.
- **The old ideas are re-judged once the lists are read.** On a sync, each open card with source
  `archidekt` is tested with the same `ShareTable.is_idea` that picks the other ideas (the Wilson lower
  bound of the commander's weighted share, ADR 0026). One that fails is turned down, as `cmdr`, with the
  reason "no longer makes the cut" on its activity line and its current share kept, so it returns only if
  that share rises (ADR 0028); the rest stay open. With no list read, nothing is judged. Cards of other
  sources, queued, applied or already turned-down cards are never touched, and the turn-down checks the card
  is still open under the board lock, so a card queued meanwhile stays queued. This is the one place `cmdr`
  turns a board card down by itself.
- **A cut card is no special case.** It comes back through the ideas like any card not in the deck. An
  applied board card that the deck no longer holds counts as one: the next sync that finds it an idea
  reopens it ("back: no longer in the deck").
- **The meta report no longer marks missing staples that sit on the Sideboard or Maybeboard.**
- **Board format:** unchanged at version 2. `source: "archidekt"` stays a valid value so old boards and
  activity lines read; nothing writes it any more.

## Consequences

- A save sends one more `remove` edit per Maybeboard or Sideboard card, and the Maybeboard is empty after
  the first save of each deck. Cards the user parks there on archidekt.com live until the next save.
- A swap can no longer be undone from the Maybeboard; to try it the other way round, propose the cut card
  again (or wait for it to turn up as an idea).
- Nothing outside a save writes to Archidekt, as before; the clear is previewed and goes into the snapshot
  taken before it.
- Plan files written for ADR 0006's `maybeboard` and `drop` keys need those lines taken out.
