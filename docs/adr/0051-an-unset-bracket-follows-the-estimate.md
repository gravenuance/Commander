# 0051: A deck with no bracket set follows its estimate's lowest bracket

**Status:** Accepted · **Date:** 2026-10-21 · Refines the bracket rules of
[ADR 0027](0027-reading-other-players-archidekt-decks.md), and replaces the Meta panel's bracket-5-only rule

## Context

Everything that reads other players' decks goes by the deck's bracket on Archidekt: which brackets the search
covers, whether the commander's tournament lists are read (brackets 4 and 5, ADR 0027), and whether the app
shows the Meta panel (bracket 5 only). A deck with no bracket set has `bracket = None`, so it was searched at
any bracket, read no tournament lists and had no Meta panel, however strong its cards. Commander Spellbook
already says which brackets such a deck fits (`BracketTag`, ADR 0002): Ruthless 4+, Spicy 3-4, Powerful 3+,
Oddball 2-3, Core 2+, Exhibition 1+, and Banned for a deck with an illegal card.

## Options

1. **Leave an unset deck unset.** Simple, and it never guesses, but the owner's unset decks get the weakest
   reads however powerful they are.
2. **The estimate's lowest bracket.** Every tag has one except Banned, it is the bracket the deck is at least
   in, and a bracket that is set never changes.
3. **The estimate's highest bracket.** Most tags have none ("4+"), so it would need a guess.

## Decision

Option 2, as one rule in `commander.bracket`: `effective_bracket(deck, estimate)` is the set bracket, else
`estimate.tag.minimum`, else None (no estimate, or Banned). `with_effective_bracket` gives the deck with that
bracket, for the code that reads `deck.bracket`; such a deck is never saved or written to a snapshot.

- **Where it applies:** the sync's ideas phase (the Archidekt searches for the deck's bracket and the one above,
  the tournament lists and the wait for TopDeck, the tag searches and the ideas compiled at the end, whose
  budget and bracket raises go by it); the app's deck view (the budget, the pool the shares come from and the
  Meta panel); `cmdr meta` and `cmdr profile`. An unset deck estimated Powerful 3+ searches brackets 3 and 4,
  one estimated Ruthless 4+ searches 4 and 5 and reads tournament lists.
- **Where the estimate comes from:** the sync asks Spellbook for each unset deck's estimate before its reads
  start (`estimated_decks`: the 7-day cache of ADR 0002, four requests at a time), as `cmdr meta` does for the
  decks it considers (with `--refresh` asking again). A failure leaves the deck unset and is logged. The app
  never waits for Spellbook in a view: `cached_effective_deck` reads the cache alone, whatever its age, so an
  estimate older than a week still stands until the next sync replaces it. The app's bracket panel does ask, as
  before, and tells the page the effective bracket it found, so a deck view built before the estimate was
  cached is read again.
- **What stays on the set bracket:** the header's "Bracket N" pill, which is empty for an unset deck, the
  bracket check (set bracket against ceiling and estimate), `cmdr bracket`, `cmdr analyze`, `cmdr suggest`
  (including `--within-bracket`) and `cmdr apply`'s bracket marks. They compare against the bracket the owner
  declared, and an estimate's lowest bracket is only a floor.
- **The Meta panel** shows for brackets 4 and 5, set or estimated (`META_BRACKETS` in the page). Bracket 4 reads
  the commander's tournament lists and its recent Archidekt decks only, not the most-played commanders or
  Spellbook's combos (ADR 0027), so the panel shows the key cards and the sample line, and leaves out "What
  stops you" and "Meta combos", which would say nothing is found. The answer carries `cedh`, true at bracket 5,
  for that. A source the bracket does not read ("not read below bracket 5") is no failure: it is not shown as
  unavailable and `cmdr meta` does not exit with 1 for it.
- **The mana tab** keeps its cEDH behaviour for bracket 5 alone (`CEDH_BRACKET`); bracket 4 gets Karsten's
  suggestions.
- **`cmdr meta`:** with no deck named it takes every deck at bracket 4 or 5, effective; a named deck below 4 is
  still read as before (the whole meta) with a note.

## Consequences

- A sync asks Spellbook once a week per unset deck, and an unset deck estimated Ruthless reads the tournament
  lists, so a first sync after this change can take longer.
- An unset deck with an estimate only from an older week is read at that estimate's bracket until the sync
  refreshes it; with no estimate ever cached, the app shows it as unset until a sync or the bracket panel asks.
- A deck's estimate can change with its cards, so its bracket for the reads can change between syncs; the cached
  answers are per bracket, so the next sync reads the new one.
- The estimate's lowest bracket can be below what the deck plays at (Core 2+ for a deck at 4); the owner sets a
  bracket on Archidekt to say so.
