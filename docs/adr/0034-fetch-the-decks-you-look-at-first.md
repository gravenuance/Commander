# 0034: Fetch the decks you look at first

**Status:** Accepted · **Date:** 2026-10-18 · Extends [ADR 0028](0028-ideas-from-four-sources.md)
and [ADR 0032](0032-one-considering-list-and-one-sync.md)

## Context

A sync pulls every deck, then compiles each deck's ideas from the meta and from Archidekt searches. Those
requests are paced (ADR 0027), so the ideas phase takes minutes per commander, and the decks came in folder
order. The deck the user is looking at could be the last one fetched, and the Meta panel and Considering
list of a deck opened mid-sync stayed old until the sync reached it.

## Options

For the order:

1. **Keep the folder order.** Nothing to store, but it ignores what the user cares about.
2. **Fetch the open deck first and the rest by how often the user opens them.** Needs a small record of opens.

For a deck opened during a sync:

1. **Leave the order fixed.** Simple, but the deck the user switched to waits behind the rest.
2. **Let the loop read a preference before it picks each deck.** The fetch in progress is never cut short.

## Decision

Option 2 each time.

- **View score:** each time the user opens a deck in the app, its score gains 1; a score halves every 30
  days (`score = Σ 0.5^(age in days / 30)`). `views.json` in `%LOCALAPPDATA%\Commander` (the folder of the
  card data and the log; `CMDR_CACHE_DIR` moves it) keeps per deck id the score and the UTC time it was
  last updated, as `{"version": 1, "decks": {"<id>": {"score": 1.5, "at": "2026-10-15T12:00:00Z"}}}`. An
  open decays the stored score to now and adds 1; a score under 0.01 is dropped on the next write. The
  file is written atomically, never leaves the machine and is not part of `decks/`.
  A missing, unreadable or damaged file (or one of another version) makes every score zero, with a
  warning in the log; the next open writes a fresh file.
- **Order:** the ideas phase takes the deck open in the app when Sync started first, then the rest by score,
  highest first, ties in the current order. `cmdr sync` has no open deck and goes by score alone. Pulling
  the decks themselves keeps its order.
- **Switching mid-sync:** the desk sets a thread-safe preference when a deck is opened during a sync. The
  loop reads it before it picks each deck: the deck being fetched finishes, then the newly opened deck goes
  next among those not yet done. Each deck is fetched once; decks that share a commander share the cached
  answers.
- **What counts as an open:** the page's `open_deck` call, made when the user picks a deck, not when the page
  reloads the deck after a save or a changed board. The deck the page opens by itself when it shows the
  list is selected but adds no view.
- The step text beside Sync keeps naming what is being fetched.

## Consequences

- The open deck's ideas arrive first, and a deck opened mid-sync waits for one commander's fetch, not the
  rest of the list.
- The order rests on a local usage count; deleting `views.json` resets it to the folder order.
- A deck opened and finished already is not fetched again when it is opened a second time.
