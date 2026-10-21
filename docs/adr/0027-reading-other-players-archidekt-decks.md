# 0027: Reading other players' decks: the bracket ladder, the caps, the pace and the per-deck cache

**Status:** Accepted · **Date:** 2026-10-18 · Replaces the Archidekt ranking, thin-data, pacing and
`peer_decks` rules of [ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md)

## Context

ADR 0007 read the 10 most-viewed Archidekt decks of a commander, from at most five search pages at the
deck's own bracket, two seconds apart, and ADR 0014 gave every deck below bracket 5 its ideas from them. Ten
decks make a thin sample: one deck is a tenth of every share, and the weighted share of ADR 0026 needs a
dozen or more to say anything. A bigger sample means many more requests to a service whose co-founder asked
not to hammer it (ADR 0004), so how much to read, how fast and how often had to be decided together.

## Options

For the sample:

1. **The newest decks.** Cheap to pick, but a deck nobody looked at says little.
2. **The most viewed decks above a fixed view bar.** Quality is steady, but a quiet commander has no deck
   above the bar.
3. **The most viewed, with the bar stepping down until enough pass.**

For repeated runs:

1. **Cache the search and every deck for a day.** Simple, but each new day reads every deck again.
2. **Cache each deck until its owner edits it.** Archidekt lists `updatedAt` with every search hit, so a
   deck read once is read again only after an edit.

## Decision

Option 3 for the sample and option 2 for the cache, in `commander.archidekt`, `commander.deckreads` and
`commander.metadata`.

- **Which brackets:** the deck's own bracket and the one above, since those decks play what this one could
  grow into. Bracket 5 has none above it. The search takes at most 20 pages per commander, shared by the two
  brackets, newest update first, and stops at the first page that reaches back past the window.
- **Which decks:** other players' only. Those with at least 1000 views; when fewer than ten have that many,
  those with at least 750, then 500, and when fewer than ten reach even that, every deck found. The most viewed come first and at most
  `peer_decks` are read (default 60, allowed 0 to 60; 0 reads none).
- **EDHTop16 at bracket 4:** a bracket 4 deck reads its commander's tournament lists too, as bracket 5 does,
  since that bracket plays near cEDH. Its other meta reads (the most-played commanders and their combos) stay
  bracket 5 only. When at least one list made the top cut, the report and the staples use the top-cut lists;
  only when none did, every list. A report is thin when it rests on every list, or on under ten lists. This
  replaces ADR 0007's rule of five top-cut lists.
- **Malformed EDHTop16 entries** are logged and skipped, and events dated after today are left out, so one
  bad entry no longer fails a commander's read.
- **Pace:** searches and deck reads share one pacer, five seconds apart. A 429 holds every request for 180
  seconds, or the `Retry-After` if longer, and the limited request is tried once more after that. A second
  429 fails the source, whose last good answer stands in with a warning, and ends Archidekt for the rest
  of the run: every later request fails at once, so the other commanders fall back to their caches
  without more waiting. Each 429 is logged with the `Retry-After` the server sent (or `none`) and the URL,
  and every request with its time at DEBUG, so the 180 seconds can be judged against real answers; it
  stays until they say otherwise. A deck found in the cache costs no request and no wait. A deck read that answers
  403, 404 or 410 (the deck went private or was deleted) is skipped with a log line and is not a failed
  source. The other services keep their pace (EDHTop16 1.5 s, Spellbook 1 s).
- **The per-deck cache:** `peer-decks` in the meta cache folder holds each deck's commanders, cards, plan
  categories and card counts above one, under its id and the `updatedAt` it was listed with (cache version
  2). A changed `updatedAt` is a miss. The search results stay cached for a day, as the other answers do.
- **Pruning:** a gather that fetches (never a cache-only read) deletes the deck files not used for 30 days,
  those whose deck was last updated before the window (no search lists them again) and those that no longer
  parse. A file's modification time is its last use, set when it is written or hit.
- **Tag searches** (ADR 0028) use the same pacer and cache, with their own caps.

## Consequences

- A busy commander's first sync of the day makes up to 20 search requests and 60 deck reads, five seconds
  apart, so several minutes; later syncs read only the decks edited since. A commander with few decks costs
  less.
- The sample is larger, so the thresholds and the weights of ADR 0026 mean something; a commander with fewer
  than ten decks reads all of them and is thin.
- `peer_decks` is now the read cap, not a sample size: lowering it is the first thing to do if Archidekt
  objects to the volume, as `meta_commanders` is for EDHTop16.
- The cache folder grows by one small file per deck read until the next prune; it lives outside the repo.
- Archidekt's `updatedAt` is trusted: an edit that does not move it is not noticed until the file is pruned.
