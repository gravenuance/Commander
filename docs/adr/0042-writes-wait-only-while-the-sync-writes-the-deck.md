# 0042: Writes wait only while the sync writes the deck

**Status:** Accepted · **Date:** 2026-10-20 · Amends the no-overlap rule of
[ADR 0032](0032-one-considering-list-and-one-sync.md)

## Context

ADR 0032 refuses every write to a deck (propose, decide, remove, label, settings, the mana swaps and a save to
Archidekt) for as long as a sync runs. Most of a sync is the ideas phase (ADR 0028), which is network-bound:
Archidekt peer and tag searches paced with 180 s cool-downs after a 429, Spellbook and EDHTop16. A tester's
first sync of a new account's decks stayed in that phase for about 50 minutes, and every card change was
refused all that time. The ideas phase writes a deck folder only at its end for each deck, when it merges
the deck's ideas into `board.json`.

## Options

1. **Keep refusing for the whole sync.** Safe and simple, but the app is read-only for most of an hour.
2. **Refuse only while the sync pulls the decks; lock per deck for the ideas merge.** Writes go through once
   the decks are pulled. The merge and each write take the same per-deck lock, and the merge re-reads the
   deck and board under it. Costs a lock and a re-read per deck.
3. **Per-deck locks for the pull too.** Writes would wait only for their own deck's pull, but the pull also
   renames deck folders, writes the account's labels and waits for the card data, which a deck lock
   does not cover. The pull takes minutes, not the better part of an hour.
4. **Queue writes until the sync ends.** Nothing is refused, but the window looks frozen (rejected in ADR 0032).

## Decision

Option 2, approved by the owner.

- **Exclusive part:** from the start of the sync through the deck pull, the account's labels, the wait for
  the card data and the profiles' `[grace]` tables, every write is refused as before, with the phase in the
  message and the log line ("refused the save for deck … now at: …"). `run_sync` then calls `on_pulled`, and
  the desk marks the sync as pulled under the same lock as its check, so a write cannot slip in early.
- **Ideas phase:** each deck's reads (`gather_for`, `gather_idea_pools`) hold no lock. The merge
  (`rejudge_ideas`, `relabel_legacy_ideas`, `compile_ideas`) runs under the deck's lock and reloads the deck
  from its folder first, so it judges the deck as saved now and reads the board as the user left it. A deck
  that left the workspace, or whose commander changed, while its ideas were fetched is not merged; the sync
  warns and marks it incomplete. Ideas fetched for a deck saved meanwhile are kept: the meta and pools are
  about the commander, and the merge filters them against the saved cards.
- **User writes:** every write to one deck holds that deck's lock (`DeckLocks`, re-entrant) for all of its
  work, a save's Archidekt calls and local write-back included, so a write and a merge never interleave on a
  deck. Signing in or out switches the workspace and still waits for the whole sync.
- **Sync during a write, or a second Sync:** unchanged. A sync is refused while any write runs, and a Sync
  press while one runs returns the running sync's status.
- **Review then save:** a merge that lands between the preview and the save still changes `board.json`, so
  the save is refused with "The board changed after the preview". A merge never touches queued cards, but a
  narrower stamp would need its own definition of which board changes matter to a save; one more Review is
  cheap and only happens when the open deck's ideas land in that window.
- **App closed mid-sync:** the sync thread is a daemon. Every file the sync and the writes produce
  (`board.json`, `deck.json`, `list.md`, `profile.toml`, the labels, the caches) goes through
  `write_text_atomic` (temp file and rename), and the card data is built in a temp file and renamed too, so a killed thread leaves the old or the new file, never half of
  one. `activity.jsonl` is appended a line at a time; a torn last line is skipped on read with a warning. A
  `.board.lock` left behind clears as stale (ADR 0006).

## Consequences

- The app stays usable for nearly all of a long sync; the refusals are left for the minutes of the pull.
- A user write to a deck can wait for that deck's merge, which is local and short; the merge can wait for a
  save, which is as long as Archidekt takes.
- `cmdr sync` passes no lock: the CLI runs alone in its process, and the board's lock file still guards each
  board write against the app.
- If the pull itself grows long, option 3 is the next step.
