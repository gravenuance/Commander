# 0065: A swap that cuts a fetcher is not held to the bar

**Status:** Accepted · **Date:** 2026-11-04 · Amends [ADR 0064](0064-a-fetcher-is-added-against-a-cap.md)
(its open consequence that a fetcher spell cut for a spell is not checked) and
[ADR 0063](0063-the-spare-land-by-the-fetch-ratio.md) (a swap that cuts a fetch land skips the bar)

## Context

ADR 0064 checks a fetcher spell that replaces a spell, but not a spell that replaces a fetcher spell, such as
Farseek cut for a draw spell. It left that open: cutting one can lose targets, and nothing checks it. A fetch
land cut in a swap with a land, or for a spell, is checked, and held to ADR 0063's bar.

A swap that cuts a fetcher and adds none takes no land a kept fetcher finds. A fetcher spell is no land, and
none of the 51 Commander-legal fetch lands in the card database on 2026-11-04 has a basic land type, so
none is a target itself. What comes in is a land, which can only add targets, or a spell. So:

- every fetcher the deck keeps finds at least the lands it found, and none is newly stranded;
- the fetchers fall, so the cap is never passed;
- the fetchers kept share their lands among one fewer.

The targets do fall when the cut fetcher was the only one to find some lands. The targets per fetcher then
fall only when it alone found more lands than the ratio. That ratio is a sum over all fetchers; no fetcher
the deck keeps is worse off, so the bar would refuse a sound swap.

On 2026-11-04 every deck in the workspace that fetches (Bello, Ellivere, Chatterfang, Etali, Glarb, Ashling,
Pantlaza) was checked by cutting each of its fetchers in turn, fetch lands included. No cut lowered the
targets per fetcher, so the change offers none of them anything new today. Bello's Farseek comes closest: it
alone finds one land, and the ratio still rises from 7.33 to 10.50.

## Options

1. **Judge a fetcher spell cut for a spell by the fetch check, bar included.** Rejected: under its bar, a
   deck whose one fetcher spell alone finds many duals would be refused the cut, though every fetcher left
   finds what it found.
2. **Leave the fetcher spell cut unjudged, and keep the bar for a fetch land cut.** Consistent with today's
   decks, but leaves the same wrong refusal for fetch lands.
3. **Leave the fetcher spell cut unjudged, and let any swap that cuts a fetcher skip the bar.** It must still
   strand no fetcher.

## Decision

Option 3, chosen by the owner. In `ManaGuard._fetches_hold`, a swap whose fetchers after it are fewer than
before skips the bar; the stranded check stands. A spell cut for a spell is still judged only when the idea
fetches (ADR 0064).

## Consequences

- A fetch land may go for a land or a spell, and a fetcher spell for a spell, even when the targets per
  fetcher fall under the bar. `test_landswap` pins both.
- The guard now agrees with leaving the fetcher spell cut unjudged: the fetch check would let it through as
  well, so judging it later changes nothing.
- The rule rests on no fetch land having a basic land type. A fetch land printed with one would be a target
  as well, and cutting it could take a land from a kept fetcher; revisit the rule if one appears.
