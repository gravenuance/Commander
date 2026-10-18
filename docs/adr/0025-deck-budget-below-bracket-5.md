# 0025: A casual deck's budget, read from its own cards

**Status:** Accepted · **Date:** 2026-10-17

## Context

Ideas came most played first, capped only by the profile's price limit, which no deck sets. Below bracket 5
that offered cards far beyond what a deck spends: shock lands for an Ashling list whose dearest card is
€7.16. The owner set the rule on 2026-10-17 for the Mana tab (ADR 0024) and then for every card under
consideration:

- Casual decks (below bracket 5) have budgets; bracket 5 decks buy what wins.
- A card's price is judged against the average price of every card in the deck.
- A margin a bit above the deck's dearest card is fine, for options and flexibility, but most ideas should
  sit close to the working budget.
- Rather than allow a fixed number of dearer cards, ideas are divided and sorted: within budget, then over
  budget.

## Decision

`commander.budget` reads a deck's budget from the cheapest EUR price of each main card, weighted by copies.
Basics are left out (they cost next to nothing and would drag the average down); the commander counts.

- **Working budget:** the average card. An idea up to 25% over it (`WITHIN_MARGIN`) is within budget: the
  owner counts Boros Charm at €4.19 within Arabella's €3.72 average, and Charismatic Conqueror at €21.09
  over it.
- **Ceiling:** 25% above the deck's dearest card (`OVER_BUDGET_MARGIN`). Nothing dearer is offered, and
  nothing unpriced, as the profile's price limit already did.
- **Profile price limit:** when set, it caps both.
- **Bracket 5:** no budget of its own; only the profile's price limit applies.

Where it applies:

- **`cmdr suggest`:** ideas and combo pieces above the ceiling are left out; within budget come first, then
  over budget, each half in its old order, marked "(over budget)" and `withinBudget` in `--json`. The text
  report states the budget. `--any-price` turns the deck's budget off (the profile limit still holds).
- **Ideas compiled on sync (ADR 0014):** the ceiling replaces the profile's price limit, and each source
  places its within-budget ideas first, most played first within each half.
- **Considering in the app:** each board card says whether it is within budget and an "Over budget" badge
  marks the others. Cards the owner or Claude proposed are marked, never refused, and no card is ever taken
  off a board for its price (owner, 2026-10-17). The owner set the order: their own cards, Claude's, the
  meta's, the Archidekt decks', then the rest; within each group, over-budget cards last, the rest by
  weighted share (ADR 0026; at first by inclusion, the share of the top-cut lists for a meta idea and of
  the recent Archidekt decks otherwise).
- **The Mana tab (ADR 0024):** named lands follow the same budget.

## Consequences

- On 2026-10-17 the casual decks' working budgets run from €0.74 (Ashling) to €4.44 (Chatterfang).
- Ideas compiled before this stay on the boards, marked and listed last; new syncs add none over the ceiling.
- A card a little over the average (Muldrotha at €0.83 for Ashling's €0.74) is within budget; the badge
  marks only clearly dearer cards, and is a hint, not a refusal.
- Prices are the cheapest regular printing in Scryfall's daily data; a card without a EUR price is never
  within budget.
