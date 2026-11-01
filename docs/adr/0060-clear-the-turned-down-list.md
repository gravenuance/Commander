# 0060: Clear the turned-down list

**Status:** Accepted · **Date:** 2026-10-31 · Extends [ADR 0026](0026-one-weighted-share-per-card.md)

## Context

A turned-down card stays on the board and comes back as an idea only once its weighted share of the lists
has risen 20 points since it was turned down (ADR 0026). Over many syncs the *Turned down* fold fills with
cards turned down by the owner and by the sync's re-judge, and nothing lets the owner start afresh: each card
can only be reconsidered one at a time.

## Options

1. **Remove every turned-down card from the board.** The ideas phase skips only cards on the board, so any
   that still pass the idea test come back at the next compile.
2. **Remove only those cmdr turned down.** Keeps the owner's own refusals, but leaves the list half full.
3. **Reopen them all.** Brings them back at once, without asking the lists whether they still belong.

For when Considering is worked out again: start the app's full sync at once, start a sync limited to the
cleared deck, or wait for the next sync.

## Decision

Option 1, with a sync of the cleared deck started at once, decided with the owner on 2026-10-31 and 2026-11-01
(the first version started the full sync; only one deck had changed, so only that deck is recompiled).

- **Clear** in the *Turned down* fold asks first ("Clear N cards?" beside Cancel; Escape, Cancel, a change to
  the list or folding it gives up), then `Desk.clear_rejected` takes every turned-down card off the board,
  whoever put it there (`BoardService.clear_rejected`), in one write, with one activity line per card
  ("removed", "turned-down list cleared").
- `Desk.clear_rejected` then starts a sync limited to that deck (`SyncOptions.only`, a set of deck ids),
  unless nothing was cleared or no account can sync; it answers `sync` as `started`, `queued` or `none`.
  The page reloads the deck and reads the account, so the running sync shows at once.
- **A sync already running** may not cover the deck: it may be limited to another deck, or have merged this
  deck's board before the clear. So the deck goes into a set kept by the desk (under the sync lock) and the
  answer is `queued`. When the running sync ends, successfully or not, it starts one sync limited to every
  queued deck ("Syncing Magda", or "Syncing N decks"), in the same step under the lock that ends it, so a
  clear arriving at that moment is either in the set or finds the new sync running. No sync starts beside a
  write (ADR 0042), so a clear made while another deck's write runs (with no sync at all, or as a sync ends)
  is queued too, and the queued sync starts as the last write ends. Only a queue that waited for a sync
  takes on that sync's summary (below). The page says the ideas are compiled once the running sync or save
  ends.
- A limited sync lists the account's decks once, but fetches, writes and settles only that deck, reads the
  meta and Archidekt decks for it alone, refreshes TopDeck's tournaments for it alone and merges only its
  board, so the deck list shows only that deck fetching. The card data and previews checks run as in any
  sync, and the folders set aside by earlier removals wait for the next full sync. The ideas phase refills
  Considering; its reads are cached for the day.

## Consequences

- A cleared card loses its share baseline: if it is still among its source's top ideas, the next sync puts
  it back as an open idea, with no rise in share needed.
- The activity log keeps who turned each card down and that the list was cleared.
- Clearing one deck recompiles that deck alone; the others' boards and fetch states are untouched. If a sync
  is already running, the deck waits for it and is recompiled by the sync that follows, even when the running
  one is a full sync (it may have merged the board before the clear). Decks cleared meanwhile share that one
  sync.
- A failed sync is followed by the queued one all the same. A finished sync that failed or warned keeps its
  summary in front of the queued one's, once however many follow ("Sync failed: ... Then: Synced 1 deck, 0
  changed."), with the warnings of both, each once, and the status shows a failure if either failed; one
  that went well gives way to the queued one's summary.
- The page reloads the decks when no sync runs any more, so with a queued sync it does so once that ends.
- Switching account or signing out forgets the queued decks, which belong to the account left, and so does
  closing the app: the queue is not kept, and the next sync of the deck compiles its ideas.
- A full sync asked for while a sync runs is still left to the running one (the Sync button is off then).
