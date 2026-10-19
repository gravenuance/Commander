# 0030: A land swap must leave the mana base sound

**Status:** Accepted · **Date:** 2026-10-18 · Amended in part by
[ADR 0036](0036-surplus-cuts-on-one-score.md) (a nonland idea may replace a spare land)

## Context

Cut targets (ADR 0029) rank deck cards by how little other players run them. A land can rank low for a good
reason that the ranking cannot see: it is the third source of a colour that a card needs, or it is one land
too many or too few. The same goes for an idea that is a land. The Mana tab already knows how to judge a
mana base (ADR 0019, ADR 0024), but a swap offered on the board was never run through it.

## Options

1. **Never offer swaps that involve a land.** Safe, but the best upgrade for a mana base, a dual land for a
   basic, is a land swap.
2. **Offer them on rank alone** and leave the Mana tab to show the damage afterwards.
3. **Run each swap through the mana analysis** and offer it only if the mana base stays sound.

## Decision

Option 3, in `commander.landswap` as `ManaGuard`. Any swap in which the idea or the cut is a land, whether a
target was picked or computed, is analysed on the deck as it would stand after the swap. The same
rebuild-from-counts that the mana plan uses (`manaplan.deck_with_counts`) makes that deck. The swap is
refused when:

- **The land count ends out of bounds,** for a swap that adds or removes a land: the deck's lands after the
  swap must be within one of Karsten's count for the deck (`LAND_TOLERANCE`, ADR 0024), or closer to it than
  the deck's count is now (so a deck short of lands may swap towards the target), or inside the fewest and
  most lands of the lists compared with, when there are at least three of them. Those are the commander's top-cut lists at bracket 5 and, below
  it, the recent Archidekt decks that say how many of each basic land they run (ADR 0019). A swap of one
  land for another keeps the count.
- **A colour loses sources:** a card that was short of sources in a colour before the swap must not be
  shorter after it, and no card may become short that was not. Differences under 0.05 of a source are float
  noise.
- **The mana data is missing** for either card. The swap is refused and the reason logged, so a gap in the
  data never lets a swap through.

The guard is built once per deck from the cached meta answers (`mana_guard`), with the mana data of the
ideas added as they come (`with_ideas`). The app's Mana tab reads its comparison lists through the same
`comparison_lists`, so the tab and the guard compare with the same lists.

## Consequences

- A basic is no longer offered as the cut for a card that needs its colour, and a deck at the bottom of its
  land range is not offered a land as the cut for a spell.
- A deck already short in a colour is not frozen: a swap may leave the shortfall as it is but not deepen it,
  so a tap-land can still be traded for an untapped one.
- At bracket 5 Karsten's count does not fit (ADR 0019), so the count rule there rests on the top-cut range.
  With fewer than three lists to compare, only Karsten's count is left to pass, or a move towards it.
- The analysis runs only for the swaps that involve a land and would otherwise be offered, which are the
  few cards that rank first for each idea.
