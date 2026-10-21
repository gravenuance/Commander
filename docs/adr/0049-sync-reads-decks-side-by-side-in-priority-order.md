# 0049: A sync reads its decks side by side, the open deck's requests first

**Status:** Accepted · **Date:** 2026-10-21 · Completes the "side by side" of
[ADR 0027](0027-reading-other-players-archidekt-decks.md) and refines the order of
[ADR 0034](0034-fetch-the-decks-you-look-at-first.md)

## Context

ADR 0027 says a sync reads its decks side by side, and ADR 0034 puts the deck open in the app first. The ideas
phase did neither well. Its loop picked the next deck, started that deck's reads, ran the deck's alike-commander
and tag reads on the sync thread itself, and merged any deck below bracket 4 as soon as it was started, which
waited for all of that deck's reads. So each deck started only after the one before it had finished: in a real
log, deck 2 started ten minutes after deck 1 and deck 3 after deck 2. Only TopDeck's single fetch and one
deck's own few reads overlapped. Archidekt's pace (one request every 6.5 s, ADR 0048) is the real limit, so
the time went into waiting in line with nothing else asked for.

## Options

1. **Keep one deck at a time, but read each deck's sources in parallel.** What ran before, and what the log
   shows: the decks' own waits for Archidekt add up.
2. **Start every deck's reads at once and let them take Archidekt's pace first come, first served.** Decks
   overlap their other waits (Spellbook, the cache, TopDeck), but the open deck's requests queue behind
   whatever the other decks asked first, which undoes ADR 0034.
3. **Start every deck's reads at once, and have the one Archidekt pacer serve the waiting requests by the
   deck's priority.** The total Archidekt time is the same, the other waits overlap, and the open deck's
   requests go out first.

## Decision

Option 3.

- **One thread per deck:** each deck's whole read (its Archidekt, Spellbook and alike-deck reads, and its meta
  once TopDeck's fetch is over for a deck at bracket 4 or 5) runs on a thread of its own, all started at
  once. The sync thread only merges: it waits for any read to end, polling in slices so Ctrl-C works on
  Windows, and merges the finished reads one at a time. Of several finished it takes the open deck first,
  then the order of ADR 0034 (the most viewed), so a deck opened mid-sync merges next. A read that raises a
  board or profile error ends that deck as incomplete with the same warning as before; any other error is
  raised on the sync thread when that deck is merged. TopDeck's own single fetch, the grace wait for it
  after an error and the one prune after every read stay as in ADRs 0027 and 0047.
- **The card data** is a sqlite connection, which belongs to the thread that opened it, so each read thread
  opens its own read-only one. The merge keeps the sync thread's.
- **The Archidekt pacer serves by rank:** the deck a read is for is a context variable that background threads
  inherit (`background.run_all` runs each job in a copy of its starter's context), so the requests of the
  threads a deck's read starts name that deck. `Pacer.rank_by` gives the pacer a ranking, and each time a
  slot opens it goes to the waiting request whose deck ranks lowest (`FetchPriority.rank`: the open deck, then
  the decks in the sync's order), in the order they asked within a deck. The ranking is read when the slot
  opens, so opening another deck mid-sync moves it up at once; the request already sleeping toward its slot is
  not cut short. Requests that name no deck (the Meta panel, `cmdr meta`) go ahead of every deck's, in the
  order they asked, and a pacer without a ranking stays first come, first served. A 429 still holds
  everyone (`cool_down`). Spellbook's and TopDeck's pacers are unchanged.
- **Progress:** a deck shows as fetching when its thread starts, so several decks show it at once; the
  steps beside Sync name every read under way, as before.

## Consequences

- The ideas phase takes about as long as Archidekt's pace needs for all the decks' searches, and not that
  plus every other wait of every deck; the open deck's reads finish first, and the others follow.
- Every deck's reads hold a thread, and the decks at bracket 4 and 5 hold theirs until TopDeck is done.
  Threads are daemons, as elsewhere, so a stuck request cannot keep the process alive.
- After an unexpected error in the sync the other decks' reads may still be running while the HTTP client
  closes; their failures end up in futures nobody reads, as the single deck's background reads did before.
- Two decks that need the same search still make it once (ADR 0027): the second reads the first's answer.
- The order in which decks merge is no longer fixed by the order they started, only by which reads are done
  and the priority among those.
