# 0050: Search Archidekt by view count, not by last edit

**Status:** Accepted · **Date:** 2026-10-21 · Replaces the search order, the incremental search and the weekly
full search of [ADR 0027](0027-reading-other-players-archidekt-decks.md) · Amended in part by
[ADR 0053](0053-search-less-for-alike-commanders-and-deck-tags.md) (the tag search's pages and reads)

## Context

ADR 0027 searched a commander's decks newest-updated first (`orderBy=-updatedAt`), at most 20 pages shared by
the two brackets, and kept every hit so a refresh paged only to the newest deck it already held, with a full
search about weekly. Archidekt answers a page with 60 decks whatever `pageSize` says, so 20 pages are the 1200
newest decks. A busy commander has more than that in a 60-day window, and the decks worth reading, the viewed
ones, are scattered through it: the search saw only the newest slice and then chose the most viewed among those.

Archidekt also sorts by `orderBy=-viewCount`, in the same answer shape (checked 2026-10-21). Ellivere's first
page runs from 42691 views to 1260, all 60 over 1000, and its second from 1239 to 857. The read already keeps
only decks with at least 1000, 750 or 500 views (ADR 0027), so a search in view order reaches every deck that
can pass those bars in a few pages, and the cost that made the incremental search worth having is gone.

## Options

1. **Keep the date order and raise the page cap.** A busy commander would take dozens of requests five
   seconds apart, and most of them list decks nobody will read.
2. **Search by views, and fall back to the date order when few decks are popular.** Reads the recent
   low-view decks of a small commander too, which the owner does not want: a deck under 500 views says little.
3. **Search by views only, down the same view bars as the read, and never below the last.**

## Decision

Option 3, in `ArchidektSearch.popular_decks`.

- **Order and stop:** one search per searched bracket (the deck's own and the one above, as before), each
  paging down its listing most viewed first. The bars are 1000, 750 and 500 views (`VIEW_FLOORS`), and ten
  decks (`MIN_DECKS`) have to reach one for it to be enough. The searches page until a page ends under 1000
  views; if fewer than ten decks updated in the window reach 1000, they carry on down the same listings
  (no new search) to 750, then to 500, and stop there whatever they have. A page that ends exactly at the bar
  is followed by the next, since more decks at the bar may sit on it. The decks of both brackets count together.
- **The window:** a deck counts only if it was updated at or after the window's start, on whichever page
  it was seen.
- **No date-ordered search:** a commander with fewer than ten decks at 500 views reads those few, or none,
  and shows as having no Archidekt decks as before when there are none. Every `orderBy=-updatedAt` search,
  `stop_at` and the "every deck found" step of `most_viewed` are gone with it.
- **Pages:** at most 5 per bracket (`MAX_SEARCH_PAGES`, 300 decks), no longer shared between the two. It is a
  safety limit, not a normal one: Kinnan, a popular commander, is under 1000 views by page 3. When a bracket
  reaches it with the bar still not passed, the log says "was cut short". A deck-tag search keeps its 3 pages
  per bracket and its quarter of `peer_decks` reads, with the same bars (ADR 0028).
- **Reads:** unchanged. At most `peer_decks` (60) decks are read, most viewed first, each cached until its
  owner edits it.
- **What is removed:** the cached search hits (`PeerLists.search`, `commander.peersearch`), the weekly full
  search day (`peerschedule.full_search_due`) and the incremental refresh. Each search answer is still
  refreshed every third day on its own day (`commander.peerschedule`); the refresh is now a new search by
  views, which is cheap and also brings the view counts up to date.
- **Cached answers:** an answer stored by the earlier version holds a `search` field that nothing reads now.
  The cache version stays 2, the reader ignores the field, and the request fingerprint of a commander's answer
  keeps its old page-cap value (`_COMMANDER_REQUEST_PAGES`, 20), so every stored answer still counts as the
  answer to its question and is replaced on its own refresh day. Bumping the version, or fingerprinting the new
  cap of 5, would have sent every group to a fresh search in the same sync, which ADR 0027 avoided.
- **Log:** one INFO line per search: "Archidekt search for <item> by views: 3 pages, 51 decks in the window",
  and a second one when a bracket was cut short.

## Consequences

- A busy commander reaches the decks over 1000 views in the 60-day window in about two or three pages per
  bracket, instead of seeing only the newest 1200 decks.
- A commander's search is at most 10 requests (two brackets of 5 pages), against 20 before, and a refresh is
  that same search instead of a one-page incremental one; refreshes are one in three days per group, so a sync
  makes about a third of them.
- A quiet commander no longer reads its recent decks under 500 views: its ideas rest on fewer decks, or on
  none, and the ideas for it stay thin or absent. The window and the view bars were the owner's choice.
- A deck that went viral long ago but is out of the window is listed on the early pages and dropped, so on
  a commander with many such decks the pages hold fewer in-window decks than before; the 5-page limit bounds
  it.
- View counts are current at every refresh, not only at a weekly full search.

## Amendment (2026-10-22): the smaller searches are not the commander's

The five pages and the 60 reads are the deck's own commander's. An alike commander is searched three pages a
bracket and read for 15 decks, and a deck-tag search two pages and 10 reads, not "3 pages per bracket and its
quarter of `peer_decks` reads" as under *Pages* (ADR 0053). The view bars are the same for all of them.
