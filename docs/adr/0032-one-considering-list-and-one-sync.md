# 0032: One Considering list, one Sync, and no writes alongside a sync

**Status:** Accepted · **Date:** 2026-10-18 · Replaces the two board lists of
[ADR 0006](0006-deck-boards-and-desktop-app.md), the Meta panel's refresh in
[ADR 0011](0011-app-deck-grid-and-replacement-targets.md) and the waiting of
[ADR 0023](0023-accounts-and-sync-in-the-app.md)

## Context

A board held two lists, considered and suggested (ADR 0006). The app showed both as one Considered list
(ADR 0011), so the split cost a flag on two commands and a field on every card that the app did not use. The app also
had two ways to refresh data from other players: **Update from Archidekt**, which pulled the decks and
compiled ideas (ADR 0023), and the Meta panel's **Refresh meta**, which fetched the meta again and compiled
the deck's ideas a second time: two ways to fetch the same pages.

Writes were guarded one way only. A save, a label change, the mana swaps and a sign-in or sign-out were
refused while a sync ran (ADR 0023 called that "waiting", though the app answered with an error), but a sync
could start while one of them was under way and rewrite the deck files it was about to read.

## Options

For the lists:

1. **Keep two lists** and show them together. Nothing to migrate, but the field has no meaning.
2. **One list, Considering.** The board file changes, so it needs a version and a way back.

For refreshing:

1. **Keep both buttons.** The panel can refresh alone, but it duplicates the sync.
2. **Sync is the one way,** and the panel reads the cache.

For writes beside a sync:

1. **Queue the write until the sync ends.** Nothing is lost, but the window looks frozen for minutes.
2. **Refuse it with a message,** in both directions.

## Decision

Option 2 each time.

- **Board version 2:** `board.json` has one list and no `section`. A version 1 file is read by dropping the
  field; the first write after reading one first copies the file to `board.v1.bak`. A file newer than the
  program reads is refused. `cmdr board add --suggested` and `research --move` are gone, and `suggest
  --to-board`, `sync` and the Mana tab's swaps all put cards under Considering. The activity log says
  "proposed (considering)".
- **No cut yet:** ideas compiled on sync with no deck card to replace are put on the board too, and the app
  lists them under that heading (ADR 0029).
- **Sync:** the button is **Sync**, `cmdr sync`'s twin, with a spinner and the current step beside it. It is
  disabled with its reason shown: while a sync runs, or when no account is signed in ("Sign in to sync").
  The Meta panel opens from cache only; while a sync runs it says it is updating, and when it ends it reads
  the report again. The panel's Refresh meta button is removed.
- **No overlap:** one lock covers the check and the mark. A sync is refused while a save, a label change,
  the mana swaps, a board edit (proposing, deciding or removing a card), saving the profile settings or a
  sign-in or sign-out runs ("Wait for the save to finish, then sync."), and each of those is refused while a
  sync runs ("Wait for the sync to finish, then save."). Neither can start inside the
  other.
- **Labels in a save:** the read of the account's labels is tried once more after three seconds, then falls
  back to Archidekt's default colours with a message. A refused login still stops the save, as its writes
  would be refused (ADR 0012).

## Consequences

- A board file written by this version is unreadable to the one before it; `board.v1.bak` is the way back.
- The app fetches in one place, so one sync is one set of paced requests (ADR 0027).
- The Meta panel shows what the last sync fetched, so it is as old as that sync.
- A refused click costs a retry once the other action ends, and the message says which.
