# 0055: Show upcoming cards that fit a deck

**Status:** Accepted · **Date:** 2026-10-22

## Context

Wizards previews a set's cards for weeks before it comes out, and Scryfall lists each card as it is revealed.
The owner wants to see, per deck, which of those cards would suit it, so a deck can be planned around a set
before release. Nothing in the app shows them: every idea path reads the card database, keeps only
Commander-legal cards, and the database is rebuilt weekly or when a set comes out (ADR 0053's weekly card
data), so a card revealed today can be days away from it and stays illegal there until release anyway.

Scryfall's API asks for 50–100 ms between requests (about 10 a second, 429 beyond that) and an identifying
`User-Agent` and `Accept` header, which the app's fetcher already sends. Its card search answers up to 175
cards a page with a `next_page` link. Card images come from `cards.scryfall.io`, which has no rate limit.

## Options

1. **Rebuild the card database more often during a preview season.** Previews would arrive with everything
   else, but each rebuild downloads about 500 MB, and the idea paths would all need to learn to tell an
   unreleased card from an illegal one.
2. **Ask Scryfall per deck when the panel opens.** Always current, but the app would wait on the network, and
   each deck would repeat the same requests.
3. **Probe the upcoming sets in the sync and keep their cards in a small cache of their own.** One search per
   set a day, nothing at all between seasons, and the card database stays as it is.

## Decision

Option 3, decided with the owner on 2026-10-22.

- **When to probe:** the sync checks Scryfall's `/sets` list. A preview season is on when the list has a set
  that brings new cards (`SetInfo.brings_new_cards`), is not out yet (`released_at` after the local day)
  and already has cards (`card_count` above 0). For each such set the sync searches its cards
  (`/cards/search?q=e:<code>&unique=cards`, following `next_page`), at most once a UTC day. The `/sets` answer
  is read once per sync and shared with the card data's new-set check. With no upcoming set, no card search
  is made.
- **Pacing and limits:** the probe waits at least 200 ms between its own requests (5 a second, half
  Scryfall's limit) and reads at most 10 pages per set. A `next_page` link that does not point at Scryfall's
  API is refused. Scryfall's 404 for a set with no cards yet means an empty set.
- **What counts as a preview:** a printed card from the search, not a token or other non-deck layout, whose
  Commander legality is `not_legal` (a reprint that is already legal is no preview), a valid Scryfall id
  (its image address is made from it), and whose own release date is after the local day. Release dates are
  calendar days, so the local day decides, not UTC's. The cache is read with that day, so a card leaves the
  panel on its release day even before the next sync.
- **The cache:** `previews.json` beside the card database (`%LOCALAPPDATA%\Commander`), with a format
  version; a file of another version is ignored and rebuilt by the next probe. Each successful probe
  replaces it whole (atomic write), and a probe that finds no upcoming set empties it. A failed probe is
  logged and named as a sync warning, the last good cache is kept, and the sync carries on. The card
  database is never rebuilt for previews.
- **Which cards fit a deck:** previews within the deck's colour identity, ranked by how many of the deck's
  dominant tags they match, ties broken by whether they fill a role the deck runs short of (the
  suggest targets), then by name, and the first 6 kept. A card matching none of the dominant tags is
  never shown. The dominant tags are the deck's up to three most prevalent Archidekt theme tags
  (`ideas.dominant_tags`, split out of `search_tags`, which keeps the profile's tag-search switch for the
  Archidekt search alone). A tag matches when the card has one of its types or keywords, read from the probe,
  or one of its Tagger tags, read from the card database's `tag_member` data by oracle id. A card revealed
  after the weekly tag refresh matches by its types and keywords only until its tags arrive.
- **Legendary creatures** are judged like any other card. They are never offered as alike commanders.
- **The panel:** "Previews (N)" in the right column under "No cut yet", shown only for a deck with fitting
  previews, collapsed by default, and loaded through the page's `panel()` helper when opened. Each tile
  shows the art (a click zooms it as on the grid), the name, the type line, the set, "Out <date>", the
  matched tags ("Fits: #Tokens #Lifegain") and a "Rules text" fold. It has no actions and no price: a
  preview cannot be added to a deck, and its prices are guesses. Once a card is out, it reaches the board
  through the usual idea paths.

## Consequences

- During a preview season a sync makes one `/sets` request and one search per upcoming set and page, usually
  two to six requests. Between seasons it adds at most the one `/sets` request a day, which the card data's
  new-set check usually makes in the same sync anyway.
- Tag matches lag Scryfall's Tagger by up to a week for newly revealed cards; type and keyword matches do
  not.
- A deck with no Archidekt theme tags, or whose tags no preview matches, shows no panel.
- Revisit the limit of 6 and the ranking once the owner has used the panel through a season, and if
  Scryfall changes its search paging or rate guidance.

## Amendment (2026-10-22): the profile's exclusions

The owner decided after the review that previews honour the deck profile's exclusions like every other
suggestion: a card named in `exclude_cards`, or with one of the `[exclude]` types, keywords or tags (sub-tags
included), is never shown.
