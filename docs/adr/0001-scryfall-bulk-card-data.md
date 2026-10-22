# 0001: Card data from Scryfall bulk files in a local SQLite database

**Status:** Accepted · **Date:** 2026-10-15 · Amended in part by the 2026-10-22 amendment below (when the
database is rebuilt)
Corrected in place on 2026-10-18 for accuracy (see git history); an exception to never editing accepted records.

## Context

`analyze` and `price` need facts about every card in eleven 100-card decks: mana value, type line, mana
cost, colour identity, Commander legality, the game changer flag, and the EUR price of each printing.
`suggest` looks up hundreds more cards by name. The user chose EUR (Cardmarket) prices.

Scryfall's rules, read on 2026-10-15 (`scryfall.com/docs/api/rate-limits` and `/bulk-data`):

- `/cards/collection` and `/cards/search` allow 2 requests a second; other API calls 10 a second. A 429
  locks the client out for 30 seconds, and repeated overload can get it banned.
- "If you need to rapidly look up card names, prices, or resolve a large number of card images, you must
  use the bulk data files." Prices change once a day.
- Bulk files are gzipped JSON Lines on `data.scryfall.io`, which has no rate limit. Default Cards (every
  printing, about 80 MB compressed, 118,740 lines) carries the per-printing `eur` and `eur_foil` prices.
- Scryfall also publishes Tagger oracle tags (about 6 MB): community-maintained role tags such as `ramp`,
  `removal` and `tutor`, arranged in a hierarchy where each file entry lists only its direct taggings.

## Options

1. **Per-card API calls** (`/cards/collection`, 75 cards per call). Small downloads, but it runs against
   Scryfall's stated rule for price lookups, and step 5's name lookups would multiply the calls.
2. **Oracle Cards bulk file** (one printing per card, 25 MB). Smaller, but its price belongs to whichever
   printing Scryfall picked, not the one in the deck, and it cannot give the cheapest printing.
3. **Default Cards bulk file in a local SQLite database.** The deck's own printing is priced, the cheapest
   printing is known, and lookups are offline and instant. Costs an 80 MB download per day of use and about
   8 seconds to build a 22 MB database.
4. **Roles from my own oracle-text rules** instead of Tagger tags. No extra file, but every rule I wrote
   would be a guess to maintain; the tags already encode the community's judgement, and their mistakes
   show up as differences against the user's own categories.

## Decision

Option 3 with Tagger tags for roles. `cmdr sync` calls `/bulk-data` once, downloads Default Cards and
Oracle Tags only when Scryfall lists a newer `updated_at` than the database holds, and rebuilds
`%LOCALAPPDATA%\Commander\cards.sqlite` in a temporary file that replaces the old one only once it is
complete. Prices are stored as integer cents. Roles are fixed tag UUIDs plus all their sub-tags (tag slugs
can change; the UUIDs are stable), resolved to card lists when the database is built. Land tutors are
excluded from the tutor role.

## Consequences

- `analyze` and `price` never touch the network and never block on Scryfall.
- A failed or malformed download leaves the previous database in place; the reader refuses files with
  fewer than 10,000 cards or more than 1% unreadable lines.
- The database schema is versioned with `PRAGMA user_version`. It is a cache, so a version change rebuilds
  it rather than migrating.
- Role counts depend on community tags, which change daily and count broadly (ramp includes mana lands).
  If a role tag disappears, that role shows as unknown rather than zero. Revisit the role list if `bracket`
  misjudges a deck because of a tag; it relies on them for tutors, extra turns and mass land denial.
- Digital-only printings and tokens are left out of the database. A deck card whose printing is missing is
  matched by name and priced at its cheapest printing.

## Amendment (2026-10-22): rebuilt weekly or with a new set, not on every price update

Scryfall rewrites both bulk files every day for the prices, so comparing `updated_at` rebuilt the database,
with an 80 MB download, on the first sync of every day. The owner decided that the card data is refreshed
weekly or when a new paper set appears (`scryfall.refresh_card_db`):

- **When:** the database is missing or its build time is unreadable, it was built seven days ago or more
  (`MAX_DATA_AGE`), a set that brings new cards came out after the day it was built, or `sync --force`. The
  files' `updated_at` is no longer compared. Otherwise nothing is downloaded, and `/bulk-data` is not called.
- **New sets:** read from Scryfall's `/sets` listing when a rebuild is not already due. Core, expansion,
  masters, draft innovation, Commander and "eternal" (Universes Beyond) sets count, never a digital one.
  Promos, tokens, memorabilia, boxed products, masterpieces, Un-sets, Alchemy and the old reprint products are
  left out on purpose, so that weekly drops do not defeat the weekly cadence. If the listing cannot be read it
  is logged and only the age counts.
- **The sync report says why and when next:** an update reads "7 days old, Scryfall data of ..." or "new set:
  *name*, ...", and a current database "..., next refresh 2026-10-29 or with the next new set". `sync --json`
  carries them as `cardData.reason` and `cardData.nextRefresh`.
- **Prices are as old as the build**, up to a week, not a day, and cards or tags Scryfall adds after the build
  wait for the next refresh.
- **Schema 8.** The `oracle` table keeps Scryfall's `layout` (ADR 0019), and printings and role members are
  indexed by card. The database is built with no rollback journal and its indexes made after the rows are in,
  which is quicker. The next `sync` rebuilds a database of schema 7.
