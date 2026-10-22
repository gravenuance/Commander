# 0053: Search less for alike commanders and deck tags

**Status:** Accepted · **Date:** 2026-10-22 · Amends the search and read limits of alike commanders and deck
tags in [ADR 0015](0015-ideas-from-commanders-that-play-alike.md),
[ADR 0027](0027-reading-other-players-archidekt-decks.md),
[ADR 0028](0028-ideas-from-four-sources.md) and [ADR 0050](0050-search-archidekt-by-view-count.md)

## Context

ADR 0028 gave an alike commander the same search as the deck's own, and a deck tag three pages a bracket and
15 reads. Archidekt answers about ten requests a minute (ADR 0048), so every search page and deck read goes
out 6.5 s apart, and the number of requests is the length of a first sync. On 2026-10-22 a fresh first sync of
the owner's 11 decks took 3466 s. About 450 of the roughly 530 paced requests belonged to one deck,
Chatterfang: its own commander, four alike commanders and nine deck tags. The same sync with the limits below
was estimated at about 165 requests for that deck. A profile could only turn the tag search on or off.

## Options

1. **Keep the limits and make the sync run longer.** Nothing is lost, but a first sync stays an hour long and
   one deck holds the Archidekt pacer for most of it.
2. **Make the limits settings.** The owner could tune them per deck, at the cost of more settings to explain
   and a default that still has to be chosen.
3. **Fixed, smaller limits** for the alike commanders and the deck tags, none for the deck's own commander.

## Decision

Option 3, chosen by the owner. The deck's own commander is unchanged: five search pages a bracket and
`peer_decks` (60) reads (ADR 0050). The rest is smaller and set in code, with no setting that raises it
(`archidekt.py`).

- **Alike commanders:** up to three search pages a bracket (`ALIKE_SEARCH_PAGES_PER_BRACKET`) and a quarter of
  `peer_decks` reads, rounded up (`ALIKE_READ_DIVISOR`), 15 at the default 60. A pair, or a commander with a
  Background, is one entry and gets the same.
- **Which decks an alike entry keeps:** an entry with a single name keeps every deck that commander leads,
  alone or with a partner or Background. An entry that is a pair keeps only the decks led by exactly that
  pair. The deck's own commander keeps only decks led by exactly its own commander or pair, as before. Before,
  a single name dropped the decks that paired it with a partner.
- **Deck tags:** only the deck's three most prevalent deck tags are searched (`MAX_SEARCH_TAGS`), the ones
  that the most of the deck's own main cards match (`ideas.search_tags`), ties in Archidekt's tag order. Each
  tag gets two search pages a bracket (`TAG_SEARCH_PAGES_PER_BRACKET`) and at most 10 reads
  (`TAG_SEARCH_READS`), never more than an alike commander's. No setting searches more tags. The other tags
  stay themes of the deck; they only stop being searched. Tag search is still opt-in per deck (ADR 0028).
- **Answers stored under the old limits stay fresh** and are cut to the new reads, most viewed first, instead
  of being searched again in the same sync (ADR 0050 did the same for its page cap). A deck-tag answer keeps
  the old fingerprint (3 pages, a quarter of `peer_decks`). An alike search that has no answer of its own
  accepts the one stored for the same commander as the deck's own search
  (`deckcache.cached_answer(also_accept=...)`), so it is not searched again for the alike deck. An alike
  answer is stored in an item of its own (`archidekt-alike-*`), because an item keeps one request: in the
  commander's own item, an alike refetch replaced the 60-deck answer that stands in as stale when the
  commander's own refetch fails. The newest of the alike item and the accepted own answers serves the alike
  search; alike answers stored in the own item before this stay accepted.
- **The progress line names the deck:** the reads of a sync run side by side and the line shows one of them
  (ADR 0049), so each step starts with the deck: "Chatterfang: Ygra decks (alike)", "Chatterfang: Landfall
  decks (theme)", and "Chatterfang: *commander* decks" for its own commander. These replace "Archidekt:
  recent *commander* decks" and "Archidekt: recent decks tagged *tag*" of ADR 0047.

## Consequences

- A first sync makes far fewer requests for a deck with several alike commanders and tags. An alike commander
  costs at most 6 search requests and 15 reads, a tag at most 4 and 10, against 10 and 60 for the commander
  itself.
- An alike commander's ideas rest on at most 15 decks, and a deck with more than three tags learns from the
  three that fit its cards best. The pool rule (half of at least three decks, ADR 0028) is unchanged, so
  fewer decks make a pool more likely to fall under three.
- Ideas already on a board are judged by the smaller pools on the next sync; one that no longer clears the
  pool rule is turned down as "no longer makes the cut" (ADR 0028).
- A tag outside the three is no longer among the deck's searched tags, so its tagged ideas on the board are
  judged by the commander's own test, as when a tag leaves the deck (ADR 0028).
- Revisit the numbers if the pools turn out too thin, or if Archidekt raises its limit (ADR 0048).
