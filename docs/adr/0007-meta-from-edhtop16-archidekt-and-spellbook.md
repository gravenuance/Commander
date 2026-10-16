# 0007: The cEDH meta from EDHTop16, Archidekt search and Commander Spellbook

**Status:** Accepted · **Date:** 2026-10-16

## Context

The user's bracket 5 decks (Magda, Ellivere, Etali, Glarb) are built for cEDH, where what matters is what
performing tournament lists play and what the rest of the field will point at the deck. `suggest` (ADR 0003)
knows only overall popularity and combos one card short; it can't say what Magda lists that top-cut play,
or which combos the field brings and whether a deck can stop them.

Three sources cover this:

- **EDHTop16** (`edhtop16.com/api/graphql`) is a public GraphQL API over cEDH tournament results: a
  commander's entries with decklists, standings and a per-card "spice" score, and the most-played
  commanders over a period. Its terms (read 2026-10-15) offer the API as-is but forbid excessive automated
  requests, implying endorsement and redistributing its statistics. Queries take about five seconds.
- **Archidekt's deck search** (`/api/decks/v3/`, newest first) lists other players' public decks for a
  commander. Its written terms forbid automated searches; its co-founder has said on the forum that the API
  is open to use (ADR 0004).
- **Commander Spellbook's** `find-my-combos` (ADR 0003) names the combos inside any list of cards.

EDHREC stays out (ADR 0003).

## Options

1. **EDHTop16 alone.** Tournament results are the best evidence of what performs, but some commanders have
   few lists in a two-month window, and Archidekt's recent decks show newer tech before it places.
2. **EDHTop16 plus a paced, cached Archidekt search and Spellbook.** Fills thin samples and finds combos,
   but leans on Archidekt's informal permission and makes more requests per run.
3. **Archidekt search alone.** One service already in use, but views measure popularity, not results.

## Decision

Option 2, with the scope and rules chosen by the user on 2026-10-15:

- **Scope:** bracket 5 decks only; `cmdr meta` with no deck shows them side by side, and a named deck of
  another bracket runs with a warning.
- **Window:** two months (`window_days` 60, 7 to 90; the API's shortest period over two months is three, so
  the window is cut out of it), events of at least 16 players (`min_event_size`).
- **Ranking:** EDHTop16 lists by placing, top cuts first; Archidekt decks by views among those updated in
  the window (`peer_decks` 10). The user's own decks are left out of the search.
- **Thin data:** with fewer than 5 top-cut lists, every list in the window is read instead; with fewer than
  10 lists in all, the report says the data is thin and its shares are hints. The window stays at two
  months either way rather than widening silently.
- **Staples and spice:** a card the deck lacks is a staple when 40% of the performing lists play it (at
  least two lists), or half the Archidekt decks when there are at least three. Spicy picks come from
  EDHTop16's spice score with their best finish.
- **Threats:** the top `meta_commanders` (10) commanders' best `meta_lists` (30) lists are searched for
  cards whose Tagger tags answer the deck's key pieces: its commander plus the `[keep]` cards in its
  profile, weighted by each commander's meta share.
- **Meta combos:** each top commander's core (cards in a fifth of its lists) goes to Spellbook once; a combo
  counts where a fifth of the lists hold every piece, at most 15 combos. For each, the deck's answers come in
  the order a stax deck reaches for them: **stax pieces** first, then removal, then counters. A stax piece is
  found by Tagger tag or, because Tagger misses some (Null Rod, Stony Silence), by a rules-text phrase, and
  must stop something the combo's steps do: activated abilities, enter triggers, permanents put onto the
  battlefield, casting (or casting from outside the hand), graveyard use, tokens. Locks that spare mana
  abilities don't count against mana steps. Removal counts where its tags reach the piece's type.
- **Pacing and caching:** EDHTop16 requests 1.5 s apart, Archidekt searches 2 s apart, Spellbook meta
  requests 1 s apart. Answers are cached in `%LOCALAPPDATA%\Commander\meta` for a day, Spellbook combos for
  7 days, and a failed fetch falls back to the last good answer with a warning. Nothing from EDHTop16 is
  committed to the repo.
- **Output:** a CLI report (`--card` for one card, `--json`), staples onto the deck's board with
  `--to-board`, and the app's Meta panel, which opens from cache and fetches only on Refresh meta. Every
  report credits the three sources and says none of them endorses this tool.

## Consequences

- The bracket 5 decks get evidence from results rather than popularity, and each combo the field plays comes
  with the deck's answers or a plain "no answer".
- A full run makes dozens of paced requests, so the first run of the day takes minutes; later runs read the
  cache. If EDHTop16 or Archidekt object to the volume, lower `meta_commanders` and `peer_decks` first.
- Tag-based answers are heuristics. Tagger tags can be noisy (Kataki "removes" Lotus Petal), and an
  activation lock is matched to a combo by the piece's name in Spellbook's steps, so wording changes there can
  hide or invent an answer. Read answers as leads to check, like the role counts in ADR 0003.
- EDHTop16's statistics may not be redistributed, so reports stay on this machine and out of git.
- Revisit if Archidekt publishes API terms, or EDHTop16 adds an endpoint for combos or card answers.
