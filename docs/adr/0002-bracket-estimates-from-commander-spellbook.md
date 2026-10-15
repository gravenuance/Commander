# 0002: Bracket estimates from Commander Spellbook

**Status:** Accepted · **Date:** 2026-10-15

## Context

`bracket` should estimate each deck's Commander bracket and set it beside the bracket the user picked on
Archidekt. The criteria that move a deck up are game changers, mass land denial, extra turns, and two-card
combos, judged by how fast and how decisive they are. Game changers, tutors, mass land denial and extra
turns are already in the local card database (ADR 0001); combos are not.

Commander Spellbook's API, read on 2026-10-15 (`backend.commanderspellbook.com/schema/`, version 7.1.11):

- `POST /estimate-bracket` takes a deck as commanders plus main cards, by name (at most 600 main entries and
  12 commanders), and returns a bracket tag for the whole deck, every card with its game changer, mass land
  denial, extra turn and banned flags, and every combo in the deck with its own tag and flags such as
  `definitelyTwoCard` and `arguablyTwoCard`. One call took 0.5 to 1.4 seconds and returned 80 to 155 KB.
- `POST /find-my-combos` returns the same combos plus those one card away from the deck. Not needed until
  `suggest`.
- The whole combo database is published as `json.commanderspellbook.com/variants.json.gz`, for tools that
  would otherwise page through the API.
- Usage terms in the schema: no login; name the tool in the User-Agent; "a few HTTP calls per user
  interaction"; 80 requests a minute "should be a safe rate"; handle 429; credit and link back to
  commanderspellbook.com. Its `robots.txt` disallows all crawling, which concerns crawlers, not the API the
  schema invites tools to use.
- Tags and the brackets they fit, from the site's syntax guide: Ruthless 4+, Spicy 3-4, Powerful 3+,
  Oddball 2-3, Core 2+, Exhibition 1+, Banned not legal.

## Options

1. **My own bracket rules** over the card database, with combos from `find-my-combos`. Full control, but
   every rule (how fast is "early-game", when does a three-card combo count as two) would be my guess, and
   it would drift from the published guidance as that changes.
2. **Spellbook's `estimate-bracket`**, one call per deck. The judgement comes from the people who maintain
   the combo data, and the answer already carries the combo list. The cost is a network call per deck and a
   dependency on a free service.
3. **The bulk `variants.json` file**, matched against decks locally. Works offline after a download, but it
   holds combos, not deck estimates, so it still needs option 1's rules.

## Decision

Option 2. The estimate is shown as Spellbook's tag plus the range of brackets it fits, and a deck is flagged
when its Archidekt bracket is below the lowest of that range. Game changers, tutors, mass land denial and
extra turns are listed from the local card database, so those lists work offline and agree with `analyze`.
Each answer is cached per deck in `%LOCALAPPDATA%\Commander\spellbook`, keyed by a SHA-256 of the card list
sent and kept for 7 days; requests run at most four at a time. Only card names and quantities leave the
machine, and the decks are public on Archidekt already.

## Consequences

- A run that asks Spellbook about all 11 decks took 5 to 8 seconds on 2026-10-15 (the requests alone took
  3.6 seconds four at a time and 7.5 seconds one at a time). A run within a week of the last makes no
  network calls and takes under a second.
- When Spellbook is unreachable, an older answer for the same card list is shown with a warning. With none,
  the estimate is "unavailable" and `bracket` exits with 1, but the card lists still print.
- The tag is Spellbook's judgement, not the official one, and it rates the whole deck: a three-card combo
  whose third piece is the commander can count as two-card and lift the deck a tag above the combo's own.
- If Spellbook changes its answer's shape, the response check fails with a clear message; a new tag letter
  is refused rather than guessed at.
- ADR 0001 asked to revisit the role list here. On the user's 11 decks on 2026-10-15, Scryfall's
  `game_changer` flags matched Spellbook's in every deck. No deck holds a mass land denial or extra-turn card
  by either source, so those tags are untested against real decks. The tutor tag counts broadly (it
  includes Magda, Brazen Outlaw, Urza's Saga and Opposition Agent); that is acceptable because tutors only
  inform the reader and do not change the estimate. The role list stays as it is.
- `suggest` (step 5) can reuse the Spellbook client for `find-my-combos`, whose near-miss combos are upgrade
  candidates.
