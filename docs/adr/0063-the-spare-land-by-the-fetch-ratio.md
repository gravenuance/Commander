# 0063: The spare land is chosen by the deck's fetches

**Status:** Accepted · **Date:** 2026-11-03 · Amends [ADR 0036](0036-surplus-cuts-on-one-score.md) (how a
spare land scores, and that the spare lands fill one place) and
[ADR 0030](0030-land-swaps-guarded-by-the-mana-base.md) (the guard also checks the deck's fetches)

## Context

A nonland idea may take the place of a land the mana base can spare (ADR 0036). A nonbasic land there
scored the spare-land bonus plus its share gap to the idea, while a basic scored the bonus alone. For
Gilded Goose in Ellivere, Exotic Orchard, which few lists run, scored far above the Plains. Orchard was
offered and the Plains was not, although the Orchard is a two-colour land in that deck and the Plains is
its least needed land. The share gap misleads for lands: few lists run an Orchard because they run better
duals, not because it is worse than a basic. `_played` already counts a basic as played by no list, but
only the land ideas' cuts used it.

The owner asked which land should go to depend on how the deck fetches. A deck without fetches needs no
particular basic. A deck with fetches needs lands for them to find. On 2026-11-03 the TopDeck lists of the
most-played cEDH commanders, and those of the owner's bracket 5 commanders, were checked for this:

- Multicolour cEDH lists keep one copy of each land their fetches find. Most of them run more fetches
  than targets.
- Two-colour lists fetch a couple of basics, an original dual, a shock land and perhaps one more typed
  land. Three-colour lists fetch the three originals and some shocks, and often no basic at all. Four- and
  five-colour lists fetch originals alone, about one per colour pair, with no shocks and no basics.
- So the targets per fetch stay close to one at every colour count. They differ more from one commander to
  another than with the colours.
- No list leaves a fetch with no land of its types.
- Mono-colour lists rarely fetch. Casual lists run a few fetches into a dozen basics.

## Options

1. **A basic first among the spare lands, whatever the deck fetches.** Simple, but it would cut a fetch
   target from a deck that has too few.
2. **The fetches decide.** A basic goes first, unless losing it costs the deck's fetchers more than its
   field allows. Then a land no fetcher finds takes the place.
3. **Fixed ratios per colour count**, measured once. Rejected by the owner: they would go stale.
   The ratios are worked out from the cached lists, alongside the rest of the mana data.

## Decision

Option 2, chosen by the owner.

- **Fetchers** (`manabase.fetcher`) are fetch lands, which sacrifice themselves to search, and every other
  card that searches its owner's library for a basic land or a land type: Land Tax, Yasharn, Farseek,
  Nature's Lore, and Slimefoot's Survey, which takes any land with a basic land type. Only what the search
  asks for decides it, so Demolition Field's "nonbasic" does not. A search for any land (Crop Rotation)
  never runs dry, so it does not count, nor does a search of another player's library (Path to Exile), nor
  a land that returns to hand rather than going (Thawing Glaciers). Each copy counts once, Land Tax
  included.
- **Targets** (`manabase.fetch_base`) are the lands with a basic land type on the front face that some
  fetcher finds. A fetcher of basics finds only basics.
- **The bar** (`landswap.fetch_bar`) is the median targets per fetcher of the compared lists that run a
  fetcher, given `MIN_PEER_LISTS` of them. Short of that, at bracket 5, it is the median of the cached top
  commanders' lists (TopDeck's meta) with as many colours as the deck's commanders, those commanders left
  out of their lists as the deck's are. Their mana bases depend on the colours more than on who leads them.
  A commander the card data does not know, the deck's or the field's, matches no colour count. Otherwise
  there is no bar.
- **The guard** (`ManaGuard.allows`, so every swap involving a land) refuses a swap that leaves a fetcher
  with nothing to find, and one that takes the targets per fetcher under the bar. A deck already under it
  may still swap a target for a target, but not lose one.
- **The spare lands fill one place** among an idea's four targets. Each scores the spare-land bonus plus
  its share gap, with a basic counted as played by no list. So the basic of the colour with most sources
  to spare comes first, then the other basics, then the mana-only nonbasics. The first the guard allows
  takes the place. ADR 0036 offered no other basic when the guard refused the first one; now the next
  candidate takes the place.

## Consequences

- On 2026-11-03, Ellivere (Land Tax and Yasharn finding 15 basics, far above its bar) is offered a Plains
  for Gilded Goose and Null Rod, ahead of Burgeoning and Dawnbringer Cleric. Exotic Orchard is no longer
  offered.
- Etali and Glarb are under their bars, so no fetch target is offered as a cut. Glarb's nine fetches find
  four lands; its field also runs the original duals Glarb lacks.
- A spare basic now usually comes first for every idea while the deck has lands to spare, because it adds
  the idea's whole share to its score. Only spells that are like for like and much less played rank above
  it.
- A land idea that adds a target is never refused for the ratio, but a mana-only land still cannot replace
  a fetch land (ADR 0036's `_fits_land`). So the guard cannot offer Bayou for one of Glarb's fetches. That
  is a separate decision.
- Adding a fetcher always lowers the targets per fetcher. So a deck at or under its bar is refused every
  fetch-land idea that does not also bring a target, as Etali and Glarb are now. Whether a fetch idea should
  be judged by the ratio at all is an open question for the owner.
- Only swaps involving a land are guarded. A nonland fetcher idea (Farseek, Land Tax) that replaces a spell
  is not checked for what it could find.
- With a deck under its bar, every spare basic is refused in turn before a nonbasic is tried: a few more
  analyses per idea, each remembered for the guard's life.
- The bar is recomputed from the cache with each guard. The top commanders' lists are cached for a day, as
  the meta report reads them.
