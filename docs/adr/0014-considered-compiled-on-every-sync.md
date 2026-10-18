# 0014: Considered compiled from the meta and recent Archidekt decks on every sync

**Status:** Superseded by [ADR 0028](0028-ideas-from-four-sources.md) · **Date:** 2026-10-16

## Context

Considered in the app ([ADR 0011](0011-app-deck-grid-and-replacement-targets.md)) held what the owner,
Claude and the Archidekt Sideboard or Maybeboard put there, plus the meta's staples after a manual
**Refresh meta** or `cmdr meta --to-board`. Decks below bracket 5 got nothing from other players at all,
and a bracket 5 deck's ideas went stale until someone remembered to refresh. The owner asked for Considered
to be filled from what other players run, and to stay current without a separate step.

Three sources were weighed:

- **EDHTop16 lists** (bracket 5 only): already read and cached by `cmdr meta`
  ([ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md)).
- **Recent Archidekt decks** for the same commander at the deck's bracket, most-viewed first: already read
  by `meta` for bracket 5, under the same informal permission as the rest of Archidekt's API
  ([ADR 0004](0004-deck-changes-through-archidekts-site-api.md)).
- **EDHREC**: its terms forbid automated requests, so it stays out
  ([ADR 0003](0003-suggestions-from-spellbook-and-scryfall.md)); its rank, from Scryfall's bulk data, only
  orders the cards an idea could replace.

## Options

1. **Keep compiling on demand** (Refresh meta, `meta --to-board`), and add a peers-only refresh for other
   brackets. No extra requests, but ideas stay stale unless someone asks.
2. **Compile on every `cmdr sync`**, reading the same caches as `meta`, with a flag to skip it.

## Decision

Option 2.

- After the decks and card data, `sync` compiles each deck's ideas (`ideas.compile_ideas`): bracket 5
  decks read the meta (`gather`), the others recent Archidekt decks at their own bracket alone
  (`gather_peers`). Answers under a day old come from the cache `meta` uses, so a second sync that day
  asks nothing.
- An idea is a staple the deck lacks: in at least 40% of at least two EDHTop16 lists ("meta"), or else in
  at least half of at least three Archidekt decks ("archidekt decks"). Each source puts at most 10 on the
  board (50 since 2026-10-17: no deck came near hundreds, and the owner wants to see more), most played first, and only ideas with a deck card to replace. Ideas already on the board fill
  places, so repeated syncs settle on the current top ideas instead of piling up; a turned-down or banned
  card takes none.
- A board card keeps its source in an optional `origin` field; the app's badge shows it.
- A deck whose sources all fail keeps its board as it was, and the sync still succeeds; the reason goes to
  stderr. `sync --no-ideas` skips the step, and `--dry-run` never runs it.

## Consequences

- Every deck gets ideas from other players, not only the bracket 5 ones, without a separate step.
- The first sync of the day takes minutes: EDHTop16, Archidekt search and Spellbook are paced as in
  ADR 0007, and the seven decks below bracket 5 each add an Archidekt search and a few deck reads.
- Sync's report gains one line per deck that gained ideas; decks with nothing new stay quiet.
- Thin samples (few lists or decks for a commander) give no ideas rather than noisy ones.
