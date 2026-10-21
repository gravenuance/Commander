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
The bulk limit is stricter than the 100: the app's log of 2026-10-21 shows 429 after exactly five bulk requests
every time, with `Retry-After` of 11 to 30 seconds, about five a minute.

TopDeck gives events, not per-commander answers: each standing carries the decklist and a structured deck
(`deckObj`: commanders, mainboard) in placement order, for about 86% of the entries. A 30-day answer for events
of 16+ players is about 58 MB (13 MB at 64+), so the answer has to be fetched in pieces and kept. It also has no spice rating
and no commander statistics, and each standing carries a player's name and id, which this tool has no use for.

## Options

1. **Keep EDHTop16 with a key.** Needs a key from its owner, which the user does not have.
2. **Scrape EDHTop16's site.** Against `online-services.md`: an official API exists and the site's terms forbid
   automated requests.
3. **TopDeck, one request per commander.** Its API cannot filter by commander, so every read would fetch the
   whole window again.
4. **TopDeck, events fetched once and kept; lists and shares worked out locally.** One request per seven days of
   new events, answers for any commander from the same files.

## Decision

Option 4, behind the provider seam that stood in front of EDHTop16 (`tournaments.TournamentSource`;
`topdeck.TopdeckClient` is its one implementation), with these rules:

- **Fetching** (`topdeck.py`). Only events of at least 16 players (`participantMin`, the floor of
  `[meta] min_event_size`) and only the `decklist` column, so no names or ids are asked for. A window is
  fetched by start date in seven-day chunks (about 13.5 MB each on 2026-10-21, under the client's 20 MB cap),
  oldest first, twelve seconds apart through the client's `Pacer` (measured from one request's start to the
  next's, as answers take one to eight seconds), saving each chunk as it arrives, so a failure part-way keeps
  what came before it. A 429 is no failure: `net.send_paced` holds the `Pacer` for the `Retry-After` (or the
  JSON `retryAfterSeconds`, the longer of the two; 30 seconds if neither), logs the wait at INFO and sends the
  request again without using one of the fetcher's attempts. The waits of one request may add up to five
  minutes; past that it fails as a network error. Reads that need the same missing days
  (the commander's lists and the meta field run side by side) wait on one lock and the second finds the events
  stored. A chunk whose answer runs past the cap (`net.ResponseTooLargeError`) is asked for again in two
  halves, down to one day, logged at INFO; a single day still over the cap is skipped with a WARNING and the
  coverage moves past it, so one huge day cannot block the window. Its events are missing until the day
  leaves the window.
- **Up front** (`metadata.start_tournament_refresh`, `syncrun`). A sync with a deck at bracket 4 or 5 starts the
  window's fetch on its own thread before the first deck, through `TournamentSource.load`, which keeps a
  failure for the reads that follow instead of raising it. Each deck's Archidekt and Spellbook reads
  (`metadata.start_gather_for`) start and finish without waiting for it; only the step that reads the deck's
  tournament lists, which then find the events stored, waits for it. A deck is merged as soon as its reads
  are ready: a deck below bracket 4 never uses TopDeck, so its merge goes ahead while the fetch runs, and a
  deck at bracket 4 or 5 is merged once the window is in (at once when it was already stored). Among the
  ready reads the usual order holds, and the sync blocks on TopDeck only when nothing else is left to merge;
  the client's lock keeps the fetch to one per sync. The deck shows as fetching from the start of its reads.
  Before this a first 60-day sync held the first deck for over three minutes.
- **Saying where the sync is** (`phases.ActivePhases`). The reads of a sync run side by side, so the progress
  line lists every one that is under way, in the order they started, joined with " · " (for example
  "TopDeck: recent tournaments · Archidekt: recent Edgar Markov decks"); a read leaves the line when it ends,
  and `metadata.gather` and the others tell their caller both when a step starts and when it ends. A wait for
  a 429 shows in place of the TopDeck step ("TopDeck: waiting 30 s (rate limit)") and goes back when the wait
  is over. Past twelve words (`ui.md`) the line is the first read and "+N more".
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
  Both ask TopDeck whether it takes the key before saving it. Without a key nothing is requested, and `meta`,
  `sync` and the app say why the lists are missing (`topdeck.NO_KEY_MESSAGE`); a refused key says so without
  showing it. A window the store already covers in part still answers when a refresh fails (no key, a refused
  key, TopDeck down): the stored events answer as a stale copy that carries the reason and the day the store
  is complete to (`TopdeckClient.stale_note`, `deckcache.StaleNote`), and is not cached as a fresh answer. A
  window the store never held fails with the error. A client remembers its first key or network failure for
  its lifetime (one sync, one `meta` run, one app read) and gives it again without sending, so a sync does not
  try TopDeck again for every deck.
- **The credit** is a line in every CLI output that shows the data (`meta`, its JSON, `topdeck`), and a link in
  the app's Meta and Considering panels and the TopDeck panel.
- **Removed:** spice (spicy picks, the spice of `meta --card`, the `spice` JSON fields), the unknown-standing
  rule of the weighted share, and the tournament name and player results, which nothing read.
- `[meta] min_event_size` runs from 16 and defaults to it, as with EDHTop16; a smaller value in the config
  is raised to 16 with a logged warning, not refused (the floor was 0 before this ADR). The first build
  fetched 64+ only; on TopDeck's last 30 days to 2026-10-21 that gave Magda 22 lists, Etali 20, Glarb 12 and Ellivere
  2, against 115, 101, 31 and 8 at 16+, too few for the idea test's lower bound (owner's call, 2026-10-21).
  Small events stay small voices through the event-size factor (ADR 0026). The store went to version 2 so
  a store filled at 64+ is fetched again.

## Consequences

- The tool depends on a key each user issues; a user without one gets Archidekt's decks alone, as the report
  already allowed for a source that fails.
- A first run for a 60-day window is about 9 requests and 115 MB (two minutes or so, paced, plus any 429
  waits); later runs are one small request a day. The cache folder holds the compact events, far smaller than the answers.
- Meta shares are over decks with a structured list inside events of 16+, not EDHTop16's figures, so they
  differ from what its site shows. The event-size factor (ADR 0026) and the idea test's 0.30 bound were
  tuned on lists from events of 16+, the same floor, so they stand unchanged. Revisit if the ideas drift.
- TopDeck's answer has no `standing` field; the finish is the place in the list, which the API documents as
  the standings order. If it ever sorts otherwise, finishes and top cuts are wrong without an error.
- Two processes that read at once (`cmdr meta` and an app sync) can fetch the same chunks, and the slower
  one's `set_coverage` can move the coverage back. Event files are written atomically, so the cost is extra
  fetching until a later run's coverage catches up. Accepted: no lock between processes (owner's call,
  2026-10-21).
- The 3-day re-ask means an event that finishes later than that is missed until the window moves past it;
  none of the sampled events ran longer.
