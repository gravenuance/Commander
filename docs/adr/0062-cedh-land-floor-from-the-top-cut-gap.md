# 0062: A bracket 5 deck's land floor is Karsten's count moved by the top-cut gap

**Status:** Accepted · **Date:** 2026-11-03 · Amends [ADR 0036](0036-surplus-cuts-on-one-score.md) (the
floor of the spare-land count at bracket 5)

## Context

A nonland idea may take the place of a land the mana base can spare (ADR 0036). The floor was the greater
of Karsten's count for the deck less `LAND_TOLERANCE` and the compared lists' median land count, at every
bracket. ADR 0019 and ADR 0030 already say Karsten's formula is not made for cEDH. Under that floor, no
bracket 5 deck had a land to spare, so a basic was never offered as a cut.

On 2026-11-03 the owner's four bracket 5 decks were checked against their commanders' TopDeck lists, with
each list run through the same formula. The findings:

- Nearly every cEDH list runs well under Karsten's count. How far under depends on the commander.
- Within one commander, the formula does tell lists apart: a list it asks more lands of runs nearly as
  many more. The gap between a list and its Karsten count varies little within one commander.
- Nothing measured explains what is left of a list's gap: mana value, cheap draw, cheap ramp, zero-mana
  rocks, dorks, rituals, four-drops or land-backed spells.
- Pooled over the commanders, those features explain little of the land counts. No single cEDH formula
  fits; the offset belongs to each commander.

The most-viewed Archidekt decks also run a few lands under Karsten's count. Those decks are popular, not
proven winners, so the owner kept the current rule below bracket 5.

## Options

1. **The top-cut lists' median alone** at bracket 5. Simple, but blind to how this deck's curve and fast
   mana differ from the field's.
2. **Karsten's count for the deck moved by the lists' median gap from theirs.** It keeps what the formula
   gets right within a commander and takes the offset from the field.
3. **A cEDH formula fitted on all lists.** Ruled out by the pooled fit.

## Decision

Option 2, chosen by the owner. The guard's comparison (`manabase.Comparison.karsten_gap`) takes each
list's land count less Karsten's count for that list, with the commanders left out as they are for the
deck, and keeps the median. At bracket 5, with at least `MIN_PEER_LISTS` lists, `ManaGuard.land_floor` is
the deck's Karsten count plus that gap, with no tolerance off it. The gap is already a median of what the
field runs, as the median floor is. With fewer lists, or below bracket 5, the floor of ADR 0036 stands.

Only the spare-land count changes. ADR 0030's check of a single swap (within one of Karsten's count,
closer to it, or inside the lists' range) is unchanged; at bracket 5 the lists' range already decides it.

## Consequences

- On 2026-11-03, Ellivere had four lands to spare and Etali two, where both had none; Magda and Glarb sit
  at their floor. Which land is offered is ADR 0063's: under ADR 0036's scoring a basic still ranked below
  a less-played nonbasic.
- A deck with a lower curve or more fast mana than its field gets a lower floor than the median would
  give, and one with a higher curve a higher floor.
- The compared lists now leave their commanders out. The lists' median land count is unchanged (a
  commander is no land), and their mana cards no longer count a commander that makes mana, as the deck's
  never did.
- With few lists, the gap is a thin estimate, as for a commander whose lists made no top cut.
