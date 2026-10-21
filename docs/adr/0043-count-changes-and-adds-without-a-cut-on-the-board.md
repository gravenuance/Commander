# 0043: Count changes and adds without a cut on the board

**Status:** Accepted · **Date:** 2026-10-20 · Extends the board of
[ADR 0006](0006-deck-boards-and-desktop-app.md) and the board saves of
[ADR 0035](0035-retire-the-maybeboard-and-sideboard.md)

## Context

The owner asked (2026-10-20) for − and + on the decklist's cards, to take a card out or raise its count by
hand, and for adding a card without replacing anything, filed under a category of the deck's they pick. Both
are queued like a swap and saved through Review → Save; nothing is written to Archidekt directly. + is only
for cards a deck may hold more of: basic lands, and cards whose rules text says so ("A deck can have any
number of cards named ...", Seven Dwarves' "up to seven").

The board was swap-shaped. A `BoardCard` is an idea: a card not in the deck, with its research, origin and a
status. Queued (`add`), it names a `choice`, the deck card it replaces, or none; `copies` lets a basic land swap
move several copies. So an add without a cut already existed in the data, with no way to choose its category,
but a removal or a count change of a card already in the deck had no place: `propose` refuses deck cards (but
basics), and `board.find`, the re-judge and the idea feed all treat a board card by name as an idea.

## Options

1. **Make removals and count changes board cards too.** One list, but a deck card would pose as an idea: the
   sync's idea feed, the re-judge, Considering and `find` by name would all need to tell them apart, and the
   statuses (open, rejected, applied) mean nothing for them.
2. **A second list on the board, `counts`, of deck cards queued to end up with a number of copies.** A
   parallel structure, but small, and the ideas code never sees it.
3. **Store a step (+1, −1) instead of a count.** Simple to queue, but a save that reached Archidekt and was
   not marked applied would apply the step again.

## Decision

Option 2, holding a target count, and an add without a cut stays a board card.

- **Board version 3.** `board.json` gains `counts: [{name, was, count, by, at}]`: the deck card, the copies it
  held when first queued, the copies wanted (0 takes it out), who and when. A board card gains `category`
  (only while queued or applied), the category an add is filed under; none files it by card type. Older boards
  read with no counts; the first save keeps the old file as `board.v2.bak`, as version 1 was kept.
- **`BoardService.set_count` and `step_count`** queue a count under the board lock and log it ("queued
  copies 6 → 7", "queued to remove"). Back to `was`, the change is dropped ("count change undone"). More
  than `was` needs `copylimit.copy_limit`: no limit for a basic or "any number", N for "up to N", else one.
  A commander, a card not in the deck and a card in a queued swap (as the card coming in or the cut) are
  refused, and a swap is refused on a card with a count change, so one card never has two queued changes.
  The decide call takes the category and refuses one the deck keeps outside it (Maybeboard, Sideboard, or a
  category whose cards are not counted).
- **The save plan** (`board_plan`) adds a count change as `AddCard(name, count)` (at least) when it is more
  than the live deck holds and `RemoveCard(name, keep=count)` (at most) when it is fewer; an add without a cut
  is `AddCard` with its category. The plan already says how the deck should end up, so `apply` turns these into
  `modifyCards` quantity edits, removals and adds with Don't Have (ADR 0010) and no new code path. A count
  change the live deck already meets is left out and cleared, like a queued card the deck already holds.
  After a save, the count changes Archidekt shows leave the board and the activity says so. `apply` warns about
  several copies only past the card's own limit.
- **CLI:** `board decide <deck> <card> add [--category NAME]` (no `--cut` adds without one), `board count
  <deck> <card> <copies>`, `board show` lists the count changes, and `board plan` writes quantities,
  categories and kept copies, which it used to drop.
- **App:** the bridge's `count(deck, name, ±1)` holds the deck's write lock (ADR 0042) like the other board
  edits; the decklist shows the changes until saved.

## Consequences

- A save is still one `modifyCards` call through `apply.apply_to_live`, with its snapshot, checks and read-back.
- A target count is idempotent, but if the deck changes on Archidekt after the count was queued, the save
  moves it to the target, not by the step; Review shows the change from the live deck before anything is sent.
- An add into a category that holds no card but is set outside the deck on Archidekt is not refused by the
  board (only the deck's cards tell it); Review shows it as outside the deck. The app offers only the deck's
  own categories.
- An older `cmdr` refuses a version 3 board rather than dropping its count changes on its next write.

## Amendment (2026-10-21): the direction of a count change, and sync clears the ones the deck meets

- **A decrease is "at most", an increase "at least".** The save plan compared the queued count with the live
  deck's, so a decrease queued from 5 to 3 turned into an add of 3 once the deck held fewer than 3, or the
  card had left it, on Archidekt. The direction is the change's own (`count` below or above `was`): a
  decrease is met when the deck holds `count` or fewer copies, an increase when it holds `count` or more
  (`CountChange.met_by`), and `counts_met` and `board_plan` both go by it. A decrease of a card the deck no
  longer holds is met, so nothing is sent for it.
- **Sync clears met changes.** A change the deck met, because the owner edited it on Archidekt, kept showing
  "×10 → 12" on a deck holding 12, counted as pending and blocked choosing the card as a cut. The ideas-phase
  merge now drops them under the deck's lock next to `close_held` (ADR 0046), logs "count change done" for
  each, and reports them in `SyncOutcome.counts_cleared` and as `<deck> board: cleared ...` on `cmdr sync`.
  Like `close_held` it runs only with the ideas phase, so `--no-ideas` leaves them for the next save, which
  clears them as before.

## Amendment (2026-10-21): an increase of a card the deck no longer holds is dropped too

The owner decided that a card cut on Archidekt after a count increase was queued (Rats 3 → 5) is not added back
by the next save: the increase is met like a decrease of such a card, and the save and the sync both drop it.
Putting the card back in the deck is a separate, explicit choice (an add on the board). `CountChange.met_by`
is true for a deck holding none of the card, whichever way the change goes.
