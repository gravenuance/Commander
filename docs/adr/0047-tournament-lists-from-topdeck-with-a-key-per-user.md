# 0047: Tournament lists from TopDeck.gg, with a key for each user

**Status:** Accepted · **Date:** 2026-10-21 · Replaces the EDHTop16 source of
[ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md), and of
[ADR 0027](0027-reading-other-players-archidekt-decks.md) and [ADR 0028](0028-ideas-from-four-sources.md)
where they name it

## Context

The meta report and the ideas of ADR 0028 learn from the commander's tournament lists, which came from
EDHTop16's GraphQL API. Since 2026-10-19 it answers plain queries with "Only persisted queries are allowed in
production" and needs a key. TopDeck.gg publishes the same tournaments: `POST /api/v2/tournaments` with
`Authorization: <key>`, free keys issued per user at topdeck.gg/developers, 100 requests a minute (fewer for
bulk queries), and terms that ask for a visible credit with a link to TopDeck.gg wherever the data is shown.

TopDeck gives events, not per-commander answers: each standing carries the decklist and a structured deck
(`deckObj`: commanders, mainboard) in placement order, for about 86% of the entries. A 30-day answer for events
of 64+ players is about 13 MB, so the answer has to be fetched in pieces and kept. It also has no spice rating
and no commander statistics, and each standing carries a player's name and id, which this tool has no use for.

## Options

1. **Keep EDHTop16 with a key.** Needs a key from its owner, which the user does not have.
2. **Scrape EDHTop16's site.** Against `online-services.md`: an official API exists and the site's terms forbid
   automated requests.
3. **TopDeck, one request per commander.** Its API cannot filter by commander, so every read would fetch the
   whole window again.
4. **TopDeck, events fetched once and kept; lists and shares worked out locally.** One request per week of new
   events, answers for any commander from the same files.

## Decision

Option 4, behind the provider seam that stood in front of EDHTop16 (`tournaments.TournamentSource`;
`topdeck.TopdeckClient` is its one implementation), with these rules:

- **Fetching** (`topdeck.py`). Only events of at least 64 players (`participantMin`, the floor of
  `[meta] min_event_size`) and only the `decklist` column, so no names or ids are asked for. A window is
  fetched by start date in weeks, oldest first, two seconds apart through the client's `Pacer`, saving each
  week as it arrives, so a failure part-way keeps what came before it. Reads that need the same missing days
  (the commander's lists and the meta field run side by side) wait on one lock and the second finds the events
  stored.
- **Keeping** (`topdeckstore.py`). One compact JSON file per event in `<cache>\topdeck\events\` holding the
  commander (partners joined in alphabetical order, as before), each deck's finish (its place in the standings)
  and cards, and `coverage.json`, the span of start dates that is complete. A run asks only for days outside
  the coverage; the last three days are always asked for again, because an event that started then may not
  have finished or shown its decks. A window that reaches back before the coverage is fetched whole. Events
  older than the window are deleted. The folder is cache only: an unknown version is dropped and fetched again.
- **Personal data.** Names and ids are dropped as an answer is read (`topdeck._deck`) and never stored or
  logged. The key is sent in a header the fetcher never logs.
- **Working out the answers** (`TopdeckClient.entries`, `top_commanders`). A commander's entries are its decks
  in the window, newest event first or best placing first; a commander with none is unknown. The most-played
  commanders are ranked by decks played, with top cuts (finish at or inside the event's top cut), conversion
  (top cuts over decks) and meta share (decks over all decks with a structured list). Entries without a
  structured deck cannot be attributed to a commander and count only in their event's size.
- **The meta answers** keep their 24 hour cache (`CACHE_VERSION` 2, so the old ones are ignored), and the
  cached answers of EDHTop16 are deleted on the next run. `--refresh` works them out again from the stored
  events and asks TopDeck only for unseen days.
- **The key** is one per user in Windows Credential Manager, service `Commander (cmdr) TopDeck`, user
  `api-key` (`credentials.KeyringKeyStore`). `cmdr topdeck` asks for it with no echo, never from the command
  line (`--key-stdin` for scripts); the app's TopDeck panel has the steps, **Test key** and **Save key**.
  Both ask TopDeck whether it takes the key before saving it. Without a key nothing is requested, a stored
  window still answers, and `meta`, `sync` and the app say why the lists are missing
  (`topdeck.NO_KEY_MESSAGE`); a refused key says so without showing it.
- **The credit** is a line in every CLI output that shows the data (`meta`, its JSON, `topdeck`), and a link in
  the app's Meta and Considering panels and the TopDeck panel.
- **Removed:** spice (spicy picks, the spice of `meta --card`, the `spice` JSON fields), the unknown-standing
  rule of the weighted share, and the tournament name and player results, which nothing read.
- `[meta] min_event_size` runs from 64 and defaults to it, because smaller events are not fetched.

## Consequences

- The tool depends on a key each user issues; a user without one gets Archidekt's decks alone, as the report
  already allowed for a source that fails.
- A first run for a 60-day window is nine requests (about 20 seconds paced); later runs are one small one a
  day. The cache folder holds a few tens of megabytes of events for a 90-day window.
- Meta shares are over decks with a structured list inside events of 64+, not EDHTop16's figures, so they
  differ from what its site shows. The weighted share's event-size factor (ADR 0026) is still centred on 16
  players although none now comes in under 64: every list weighs more, and the proportion to Archidekt decks
  is unchanged only because each source's weights are scaled to a mean of one. Revisit if the ideas drift.
- TopDeck's answer has no `standing` field; the finish is the place in the list, which the API documents as
  the standings order. If it ever sorts otherwise, finishes and top cuts are wrong without an error.
- The 3-day re-ask means an event that finishes later than that is missed until the window moves past it;
  none of the sampled events ran longer.
