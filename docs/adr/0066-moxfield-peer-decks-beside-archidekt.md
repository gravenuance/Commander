# 0066: Moxfield's peer decks beside Archidekt's

**Status:** Accepted · **Date:** 2026-11-07 · Extends [ADR 0027](0027-reading-other-players-archidekt-decks.md),
[ADR 0028](0028-ideas-from-four-sources.md), [ADR 0050](0050-search-archidekt-by-view-count.md) and
[ADR 0053](0053-search-less-for-alike-commanders-and-deck-tags.md)

## Context

The ideas for Considering and `cmdr meta` learn from other players' recent, most-viewed decks of the
commander, and of the commanders a profile names as alike, all from Archidekt. Many players keep their lists
on Moxfield instead, so a commander with few Archidekt decks over the view floors gets few ideas.

Moxfield has no public API. Its site talks to an undocumented JSON API at `api2.moxfield.com`, whose
`robots.txt` is `Disallow: /`. The owner chose to read it anyway, on the condition that cmdr identifies
itself honestly, reads only public decks without signing in, never works around Moxfield's bot protection
(no cloudscraper, no browser User-Agent, no challenge solving), and stops using Moxfield if Moxfield blocks
honest requests or asks for that.

What the API offers, from a dozen honest, paced requests on 2026-11-07 (User-Agent
`Commander/<version> (personal deck tracker)`, every one answered 200 with JSON):

- `GET /v2/cards/search?q=!"<name>"` answers an `exactMatch` card whose `id` names the card in deck searches.
- `GET /v2/decks/search?pageNumber=&pageSize=&sortType=views&sortDirection=Descending&fmt=commander` lists
  decks most viewed first, each with `publicId`, `viewCount`, `lastUpdatedAtUtc`, `bracket`, `visibility`,
  `format` and `createdByUser`. `commanderCardId=<id>` keeps the commander's decks (every printing), and
  `minBracket`/`maxBracket` the decks at those brackets. `totalPages` tells whether a page follows.
- `GET /v3/decks/all/<publicId>` answers the deck: its boards (`commanders`, `mainboard`, ...) with each
  card's name and quantity, and `authorTags`, the author's tags by card name.
- The site's own search also sends `hubName`, `filter`, `deckName`, `authorUserNames`, `cardId`, `board`,
  `partnerCardId`, signature-spell and `companionCardId` filters. `hubName` (Moxfield's hubs are a fixed list
  of about 150 strategy names) was ignored by both `/v2/decks/search` and `/v2/decks/search-sfw` in three
  tries: the results did not change. There is no filter by update date, price, likes or primer.

## Options

- **Moxfield as a second site under the same rules, counted on its own (chosen).** Each site is searched and
  read under the same view floors, page and read caps, window and pace, and a Moxfield deck counts as much as
  an Archidekt one. Twice the decks at most, and Moxfield's failures stay its own.
- **One shared budget across both sites.** Fewer requests, but the split between sites would be arbitrary and
  a busy Archidekt commander would starve Moxfield; the owner turned it down.
- **Leave Moxfield out.** No unofficial, disallowed API to depend on; the thin commanders stay thin.

## Decision

`moxfield.py` reads Moxfield as `archidekt.py` reads Archidekt, and both now share the search rules in
`peersearch.py`: `PeerDeck` (with its `site`), the view-floor descent and its `Cursor`s (ADR 0050), the
`LimitGuard` for 429s, `most_viewed`, `brackets_to_read`, the floors and the caps.

- **Same rules, separate caps.** For the deck's own commander and each alike commander, Moxfield is searched
  at the deck's bracket and the one above (`minBracket`=`maxBracket` per bracket, none for no bracket), 60
  decks a page, up to 5 pages a bracket (3 for an alike commander), and up to `peer_decks` decks are read
  (a quarter for an alike one), all apart from Archidekt's. The window is applied to `lastUpdatedAtUtc` as
  it is to Archidekt's `updatedAt`, after the search, since neither site filters by date. Requests are 6 s
  apart on Moxfield's own pacer, so both sites are read side by side. Search answers are cached and
  refreshed every third day, staggered, and each deck read is kept until its owner edits it, as on Archidekt
  (ADR 0027), in the same `peer-decks` cache folder.
- **The commander's card id** is looked up by exact name (a double-faced card by both faces, as Moxfield's search
  matches no front face alone) each time
  the search is refreshed; a commander Moxfield does not know has no Moxfield decks, which is no error.
- **No tag search on Moxfield.** Its `hubName` filter did not filter, and hubs are not Archidekt's deck tags;
  matching one to the other would be a guess, so tagged ideas stay Archidekt's alone.
- **Plan cards** (ADR 0017) are the cards the author tags with a plan word ("Combo Pieces", "Wincon"), as an
  Archidekt category names them.
- **A deck cross-posted to both sites counts once:** a Moxfield deck with the same commanders, cards and card
  counts as an Archidekt deck read is dropped. A copy edited by even one card on one site counts twice; that
  is a known limitation, as finding near-copies would mean comparing every pair.
- **On the board** the ideas keep their origin: a Moxfield deck counts the same as an Archidekt one, so the
  commander's decks of both sites are one pool, "archidekt decks" (`Origin.DECKS`), and alike commanders' one
  pool, "adjacent decks". No board file or app grouping changes. The reason names the sites read ("in 7 of 9
  most-viewed recent Archidekt and Moxfield decks").
- **Getting the data is not analysing it.** The fetch step gathers peer decks from each site; the analysis
  (`compile_ideas`, `rejudge_ideas`) sees one pool of them and never asks which site a deck came from or
  which site is missing. When one site cannot be read, the ideas are compiled and re-judged against the
  decks that were gathered, exactly as if they were the whole pool, so the board re-evaluates and never
  just grows or freezes. Only when no peer decks at all could be gathered (neither site read, or one
  unreadable and the other without decks) is there no answer: nothing is re-judged, as before Moxfield.
  A deck is not marked incomplete for a site that was not read.
- **Failing soft.** A 429 is handled as on Archidekt: one retry after the pause, then Moxfield is given up
  for the run. A 403, or a 200 that is not JSON (a bot challenge page), on the card lookup or a search ends
  Moxfield for the run: nothing more is sent. So does a challenge page on a deck read. A 403 on a single
  deck read (a private deck answers 403) skips that deck only, until three deck reads in a row are refused
  (a read that succeeds starts the count again), which ends Moxfield for the run too. The failure is reported, not analysed: `sync` warns once per run
  per site ("Moxfield decks could not be read (Moxfield refused cmdr's requests (HTTP 403)); ideas are
  judged by the decks that were read"), not once per deck or alike commander, and `cmdr meta` notes that the
  decks shown are partly read. A cache-only read (the app's panels) treats a site with nothing stored as
  no site: no error, nothing missing.

**Removal condition.** If Moxfield blocks honest requests (a Cloudflare challenge or 403 that persists across
syncs) or asks us to stop, Moxfield is removed as a source: no workaround is tried.

## Consequences

- Commanders that are popular on Moxfield get ideas from up to twice as many decks, and Moxfield's views
  count to the same floors. A first sync after this reads every commander's Moxfield decks once (each deck
  answer is about 600 KB, so a commander's first read is tens of megabytes; later syncs read only edited
  decks).
- The tool depends on a second undocumented API that its owner's `robots.txt` disallows. Every answer is
  shape-checked, and a change or block costs the Moxfield half of the decks, never the sync or the
  re-judging of the ideas.
- `cmdr meta` lists the peer decks of both sites, each with its link, and its source line reads "Archidekt
  and Moxfield decks".
- The app's origin labels still say "Archidekt" for these pools; whether to rename them is open.
- Revisit if Moxfield publishes an API or terms for it, if its `hubName` filter starts working (a tag search
  could then match hubs by exact name), or if the removal condition is met.
