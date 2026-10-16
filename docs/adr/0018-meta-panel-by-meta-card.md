# 0018: The Meta panel by meta card

**Status:** Accepted · **Date:** 2026-10-16

## Context

The app's Meta panel ([ADR 0011](0011-app-deck-grid-and-replacement-targets.md),
[ADR 0013](0013-the-decks-own-combos-against-the-meta.md)) had two sections:

- *Your combos:* each combo with the meta cards that stop it.
- *Against your key cards:* each key card with the meta cards that hit it.

The same meta cards (Force of Will, Chain of Vapor) repeated under every combo and every key card. Once
key cards were found from the deck ([ADR 0017](0017-key-cards-found-from-the-deck.md)), Magda had 19 of
them and 11 combos, and the repetition buried what mattered. The owner asked for the view to be
inverted, grouped, or both.

## Options

1. **Group the old rows:** keep one row per combo or key card, and fold the repeats.
2. **Invert by meta card:** one row per meta card, with what of the deck it stops.

## Decision

Option 2, chosen by the owner. The panel has three sections, with shorter headers:

- **Key cards:** chips; hovering one shows why it is key.
- **What stops you:** each meta card that stops one of the deck's combos or hits a key card, once.
  - Rows are grouped as stax, removal or counters. A card goes under the first of these it does, so
    Grafdigger's Cage, which both locks a combo and removes a piece, counts as stax.
  - Within a group, the cards the meta plays most come first; four show, and the rest fold.
  - Each row has chips for the combos it stops and for the key cards it hits that are not already in
    one of those combos. Combo chips shorten names to before the comma ("Magda + Clock of Omens").
  - A row shows four chips, with "+N" for the rest.
  - An *Untouched* line lists the combos and key cards nothing stops.
- **Meta combos:** unchanged.

`cmdr meta` keeps its text report as before.

## Consequences

- On 2026-10-16, Magda has 65 meta cards: 52 removal and 13 counters. Glarb has 77: 12 stax, 47 removal
  and 18 counters. Each card shows once, where before it repeated under every piece it hit.
- The bridge's meta answer replaces `pieces` and `ownCombos` with `keys`, `stoppers` and `untouched`.
