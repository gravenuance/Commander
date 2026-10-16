# 0015: Ideas from commanders that play alike

**Status:** Accepted · **Date:** 2026-10-16

## Context

Sync compiles Considered from the commander's own EDHTop16 lists and recent Archidekt decks
([ADR 0014](0014-considered-compiled-on-every-sync.md)). Some decks play differently from most decks of
their commander. Chatterfang is built as a landfall deck, so the owner wants it to borrow ideas from
green and Golgari commanders that play that way: Ygra, Lumra, The Gitrog Monster, and the partner pair
Tevesh Szat and Kodama of the East Tree.

The same check also showed that compiled ideas ignored the deck's profile: Pantlaza got Ephemerate,
though its profile excludes the flicker tag.

## Options

1. **Each named commander's staples on their own:** a card counts when half of any one commander's decks
   play it. More ideas, but each commander brings the cards only it wants (Gitrog's dredge lands, Ygra's
   Food).
2. **One pool with the deck's own decks:** simplest, but the deck's own commander shrinks to a sixth of
   the pool and its staples drop out.
3. **The named commanders' decks as a separate pool:** the deck's own source stays as it was; a card
   counts when half the pool plays it.

## Decision

Option 3, chosen by the owner.

- A profile lists the commanders under `[ideas] commanders`: a name, or a partner pair as its own list.
  At most 8, since each costs an Archidekt search and a few deck reads a day.
- Their decks are read at the deck's own bracket, like its own peers, and cached for a day under the
  same key, so a commander shared by two decks is read once.
- A partner pair is searched by its first name, which Archidekt's search takes, and only decks led by
  both are kept. To find enough of them, up to three search hits are read per wanted deck.
- The pooled decks give "adjacent decks" ideas: cards at least half of at least three pooled decks play,
  up to 10, most played first, each with a deck card to replace.
- Every compiled idea, from any source, passes the profile's exclusions, bracket ceiling and price cap,
  as `suggest`'s ideas do.

## Consequences

- Chatterfang's Considered gains landfall staples that most Chatterfang decks don't play.
- One more search and up to ten deck reads (thirty for a pair) per named commander on the first sync of
  the day.
- A named commander whose search fails is named on stderr; the deck's other ideas still come.
- Short commander names now skip a leading "The" and keep the first word, so the reasons say "Gitrog"
  and "Tevesh" as players do. None of the owner's deck names changed.
