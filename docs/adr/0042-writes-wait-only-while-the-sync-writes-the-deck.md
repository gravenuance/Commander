# 0042: Writes wait only while the sync writes the deck

**Status:** Accepted · **Date:** 2026-10-20 · Amends the no-overlap rule of
[ADR 0032](0032-one-considering-list-and-one-sync.md)

## Context

ADR 0032 refuses every write to a deck (propose, decide, remove, count, label, settings, the mana swaps and a
save to Archidekt) for as long as a sync runs. Most of a sync is the ideas phase (ADR 0028), which is
network-bound: Archidekt peer and tag searches paced with 180 s cool-downs after a 429, Spellbook and
EDHTop16. A tester's first sync of a new account's decks stayed in that phase for about 50 minutes, and every
card change was refused all that time. The ideas phase writes a deck folder only at its end for each deck,
when it merges the deck's ideas into `board.json`.

Before the ideas, the sync pulls the decks (fetched four at a time, each written to its folder, renamed when
its name changed), saves the account's card labels, waits for the card data download (about 40 s on a first
run, longer on a slow link) and gives each profile its `[grace]` table. A first draft of this ADR kept every
write refused for all of that; on a slow link that is still minutes of a read-only app for a deck the pull
does not even touch.

## Options

1. **Keep refusing for the whole sync.** Safe and simple, but the app is read-only for most of an hour.
2. **Refuse only while the sync pulls the decks; lock per deck for the ideas merge.** Writes go through once
   the pull, the labels, the card data and the `[grace]` tables are done. Still minutes of refusals, for
   every deck.
3. **Per-deck state for the pull too.** A deck's write waits only while the pull may still write that deck;
   the pull's writes, the `[grace]` tables and the ideas merge take the deck's lock. Needs the pull to say
   which decks it will write, and a separate rule for the card data.
4. **Queue writes until the sync ends.** Nothing is refused, but the window looks frozen (rejected in ADR 0032).

## Decision

Option 3, approved by the owner.

- **The pull:** until the deck listing is in, the sync may write any deck, so every deck's write is refused.
  Once listed, the decks to fetch (edited on Archidekt, or forced) are the only ones refused; a deck the
  listing shows unchanged, a deck on disk that the listing lacks (the pull only reports it) and a deck that
  failed to load are open at once. Each fetched deck is written under its `DeckLocks` lock as soon as it
  arrives, then opened; one that fails to fetch is opened too. The refusal names the deck: "Wait for
  Ellivere to finish syncing, then propose the card.", and the log line gives the deck, "(still syncing)",
  how long the sync has run and its phase.
- **Open deck first:** the pull picks each next fetch through the same `FetchPriority` as the ideas phase,
  so the deck open in the app goes first and a deck opened mid-pull goes next among those not yet started,
  within the same limit of four fetches at once.
- **Card data:** a write that looks cards up is refused while the download runs ("Wait for the card data to
  finish downloading, then …"): propose, the count + and −, the mana swaps, the settings save (it checks the
  adjacent commanders and new grace cards) and the save to Archidekt. While the download runs the database
  may be missing (first run) or from an older schema, and the finished download is renamed over it, which
  fails on Windows while a long write holds it open. Decide (the pure add included), remove and a label
  change go through: they read at most the card data already there, and briefly. A new deck write is
  treated as looking cards up unless marked otherwise, so it is safe by default.
- **Labels:** the account's labels are one file for the whole account, not per deck. It is written
  atomically, and both the sync and a label change write the list just read from Archidekt, so whichever
  lands last is current; no lock and no refusal.
- **`[grace]` tables:** written per deck under the deck's lock; a folder renamed meanwhile is left for the
  next sync.
- **Ideas phase:** unchanged from the first draft. Each deck's reads hold no lock; the merge
  (`rejudge_ideas`, `relabel_legacy_ideas`, `compile_ideas`) runs under the deck's lock and reloads the deck
  from its folder first. A deck that left the workspace, or whose commander changed, while its ideas were
  fetched is not merged; the sync warns and marks it incomplete.
- **User writes:** every write to one deck holds that deck's lock (`DeckLocks`, re-entrant) for all of its
  work, a save's Archidekt calls and local write-back included. Signing in or out switches the workspace and
  still waits for the whole sync.
- **Sync during a write, or a second Sync:** unchanged. A sync is refused while any write runs, and a Sync
  press while one runs returns the running sync's status.
- **Review then save:** a merge that lands between the preview and the save still changes `board.json`, so
  the save is refused with "The board changed after the preview"; one more Review is cheap.
- **App closed mid-sync:** the sync thread is a daemon. Every file the sync and the writes produce
  (`board.json`, `deck.json`, `list.md`, `profile.toml`, the labels, the caches) goes through
  `write_text_atomic` (temp file and rename), and the card data is built in a temp file and renamed too, so
  a killed thread leaves the old or the new file, never half of one. `activity.jsonl` is appended a line at a
  time; a torn last line is skipped on read with a warning. A `.board.lock` left behind clears as stale
  (ADR 0006).

## Consequences

- A deck the sync does not rewrite is never refused for the pull; one it does is open as soon as it is
  written, and the open one is written first. Card lookups wait for the download alone.
- The pull writes decks as they arrive, so their "fetched" progress lines come in arrival order; the results
  stay in listing order.
- A user write to a deck can wait for that deck's pull write or merge, which are local and short; those can
  wait for a save, which is as long as Archidekt takes.
- `cmdr sync` passes no lock and no priority: the CLI runs alone in its process, and the board's lock file
  still guards each board write against the app.

## Amendment (2026-10-21): the writes that read no card data do not hold it

Decide, remove and a label change were let through during the download because they "read at most the card
data already there, and briefly". The code did not keep that promise:

- A label change opened the card database before its Archidekt round trip and held it open until the activity
  line was written, which can make the download's rename over the file fail on Windows. It now logs through
  `BoardNotes`, which appends to the activity log under the board lock and needs no card data, so it opens
  none.
- With no card database yet, or one of an older schema, the database cannot be opened, and decide and remove
  failed with "run `cmdr sync`" while that sync was the thing downloading it. While a sync runs and its card
  data is not in place, they now answer with the same refusal as a write that looks cards up: "Wait for the
  card data to finish downloading, then decide." Once the download is done, or when no sync runs, the original
  error stands.

## Amendment (2026-10-21): the refusal text keeps to the 12-word budget

A refusal while the whole sync runs ended in the sync's phase, "Wait for the sync to finish, then sign in (now:
Starting the sync).", which is 13 words and longer with a longer phase; the UI budget for status text is 12
(`ui.md`). The text is now "Wait for the sync to finish, then sign in." The phase stays in the log line of the
refusal, with the deck and how long the sync has run.
