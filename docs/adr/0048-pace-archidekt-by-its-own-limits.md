# 0048: Pace Archidekt by its own limits

**Status:** Accepted · **Date:** 2026-10-21 · Replaces the pace, the 429 pause and the page size of
[ADR 0027](0027-reading-other-players-archidekt-decks.md)

## Context

A tester's 0.8.0 sync (3 decks, 1076 s) logged two Archidekt 429s, each followed by ADR 0027's 180 s pause,
and searches "paged in full" with 1198 and 1195 decks, more than the 20 pages of 50 that the code asked for
could hold. Live requests on 2026-10-21 showed why:

- **Page size:** the deck search and the owner's deck listing ignore `pageSize` (20, 50, 60 and 100 all gave
  60 decks a page; the listing gave 11 for `pageSize=10`). `count` stops at 1000, but paging does not: page 30
  of a busy commander still answered.
- **Rate limit:** after a quiet minute, a burst at one request a second got 10 and 11 answers before 429s; at
  5.4 s apart the eleventh request within a minute got a 429. No answer carried a `Retry-After`. The limit
  lifted 5 s, 11.5 s and over 16 s after the first 429, which fits about ten requests a minute, likely counted
  in whole minutes.

So the 5 s pace (twelve a minute) ran into the limit about once a minute of searching, and each 429 cost three
minutes where Archidekt needed under one.

## Options

1. **6.5 s pace, 60 s pause:** about nine requests a minute, under the limit, so 429s become rare; a 429 waits
   out at most one counting minute. Each request is 30% slower in name, but a sync loses no 180 s pauses.
2. **6 s pace, 60 s pause:** exactly ten a minute: fastest, but on the edge, so the odd 429 is likely.
3. **Keep 5 s, 60 s pause:** a 429 about every minute of searching, each costing up to a minute.

For the page cap: keep 20 pages (now 1200 decks) and log a search cut short; raise it to 30; or page to the
window's end, which for the busiest commanders is 40 or more pages once a week.

## Decision

Option 1 (the owner's choice): searches and deck reads 6.5 s apart (`SEARCH_INTERVAL_SECONDS`), and after a
429 nothing goes out for 60 s, or the `Retry-After` if longer (`RATE_LIMIT_PAUSE_SECONDS`); the retry once and
the give-up after a second 429 stay. No `pageSize` is sent, since Archidekt decides it. The page caps stay (20
per commander, 3 per bracket for a tag), and a search that uses them up with more of the window still listed
logs how far back it reached ("stopped at its 20-page cap; it reaches back to 2026-09-27"). The full search's
log line says "full search" rather than "paged in full", which read as "reached the end of the window".

## Consequences

- A busy commander's weekly full search still makes up to 20 requests, now over about 2 minutes instead of
  1 minute 40, and its sample is the newest 1200 decks, about the last month for the most played commanders.
- If 429s still show in the log, the pace is the first thing to lengthen; if none show for weeks, 6 s is the
  next step to try.
- The probes behind the numbers were one session's; Archidekt may change its limit without notice, and the
  429 lines in the log (with the `Retry-After`, if one is ever sent) are what to watch.
