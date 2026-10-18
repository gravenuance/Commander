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
2. **Re-judge the old ideas on a sync.** The cards on Archidekt stay.
3. **Re-judge them, and empty the Maybeboard and Sideboard with the user's next save.**

## Decision

Option 2 for reading and for cuts, and option 2 for what is already there. A first version built option 3
and was changed before release.

- **Sync reads neither list and writes nothing.** Their cards stay in `deck.json` and `list.md` as the deck's
  mirror, nothing more.
- **A cut removes the entry outright.** Plan files lose `maybeboard = true` and `drop = [...]`; a plan that
  still has them is refused as an unknown key. An `add` whose category is Maybeboard or Sideboard is refused ("name a
  category in the deck"), since nothing reads those lists any more. An add of a card that sits on the
  Maybeboard adds a new entry and leaves the old one.
- **A save never touches cards outside the deck.** The user's Review → Save, `board commit --save` and
  `apply --save` send edits only for the plan's own changes; whatever sits on the Sideboard or Maybeboard is
  not in the `modifyCards` payload and stays as the user left it. The first version had the save empty both
  lists. It was removed: other users may keep cards there on purpose, and a tool others run should not
  delete cards it was not asked to change.
- **The old ideas are re-judged with all the compiled ones.** From the first sync on, an open card with source
  `archidekt` is tested like any idea `cmdr` compiled, with `ShareTable.is_idea` (the Wilson lower bound of the
  commander's weighted share, ADR 0026), and turned down as `cmdr` with "no longer makes the cut" when it
  fails ([ADR 0028](0028-ideas-from-four-sources.md) has the rule).
- **Old cards are labelled like compiled ones.** A Reconsider on a turned-down card whose source is `archidekt` or `cmdr`
  (not an alike or tagged idea) recomputes its label from the cached lists, as compile would: the source with
  the larger weighted share, the meta on a tie, becomes its origin, its source becomes `cmdr` and its reason
  is worded as a compiled idea's. If no list plays it, or none is cached, only the reason changes
  ("reconsidered; no list plays it") and the card stays neutral. The reopened card carries `reconsidered:
  true` on the board (optional, absent in old boards) so the re-judge leaves it until the user decides it.
  Undoing a queued swap is a plain reopen: it neither sets the mark nor relabels. Each
  sync also relabels the open `archidekt` cards that pass the re-judge, so no neutral Maybeboard leftover
  stays in Considering. Cuts are computed on read and need no change.
- **A cut card is no special case.** It comes back through the ideas like any card not in the deck. An
  applied board card that the deck no longer holds counts as one: the next sync that finds it an idea
  reopens it ("back: no longer in the deck").
- **The meta report no longer marks missing staples that sit on the Sideboard or Maybeboard.**
- **Board format:** unchanged at version 2. `source: "archidekt"` stays a valid value so old boards and
  activity lines read; nothing writes it any more.

## Consequences

- A save sends no more edits than the plan has changes, and the Maybeboard and Sideboard on Archidekt are the
  user's to keep or empty.
- A swap can no longer be undone from the Maybeboard; to try it the other way round, propose the cut card
  again (or wait for it to turn up as an idea).
- Nothing outside a save writes to Archidekt, as before.
- Plan files written for ADR 0006's `maybeboard` and `drop` keys need those lines taken out.
