# 0033: Check and reveal lands are untapped by the deck's own lands

**Status:** Accepted · **Date:** 2026-10-17 · Replaces the flat quarter that
[ADR 0024](0024-mana-base-suggestion.md) gave check and reveal lands

## Context

The Mana tab (ADR 0019) and the mana plan (ADR 0024) need to know how often a land enters tapped. A land
that enters tapped only when a condition fails counted as a quarter of a tap-land, whatever the condition:
a check land ("enters tapped unless you control a Plains or an Island"), a reveal land ("you may reveal a
Plains or Island card from your hand"), a fast land and Fabled Passage alike. A check land in a deck of
nine Plains and Islands and one in a deck with two are very different cards, and the plan took them for the
same one. It also judged "untapped enough" two ways: some steps accepted only a land that was never tapped,
others any land under a quarter.

## Options

1. **Keep the quarter for every conditional land.** Simple, but it ignores the one thing that decides a
   check land, the deck's own lands.
2. **A fixed chance per kind of land,** such as a half for a check land. Closer, but still blind to the deck.
3. **Work the chance out from the deck:** the chance that the condition fails by turn three.

## Decision

Option 3, in `commander.manabase`. A land whose condition names basic land types (text of the form "unless
you control a ..." or "you may reveal a ...") is tapped with the chance that none of the cards seen by turn
three has one of those types: the opening seven and two draws, nine cards (`CARDS_SEEN`). That is the
hypergeometric chance C(N - K, 9) / C(N, 9), where N is the 99 less the land itself and K counts the deck's
cards, copies included, whose front face has at least one of the types. Basic lands and typed duals such as
shock lands both count. Lands with another condition (a fast land, Fabled Passage) keep the quarter
(`CONDITIONAL_TAPPED`). Shock lands count as untapped and lands that put a found land onto the battlefield
tapped as tapped, as before.

The mana plan judges untapped lands one way: a land counts as untapped when it enters tapped no more than a
quarter of the time (`mostly_untapped`). That covers the named lands that close a gap, the untapped twins
for tap-lands, and which tap-lands qualify for a twin.

## Consequences

- A check land is nearly always untapped in a deck of many basics and duals and often tapped in a deck of
  few, and the Mana tab's turn-one section and the plan follow.
- The chance is worked out for the deck as it stands. A swap that changes the lands changes the chance of
  the others, and the plan reads it again for the deck after the swap.
- The nine cards are a convention (the opening hand and two draws), not a measurement of how long the land
  is needed. The constant is named, so a different turn is one change.
- Fast lands and Fabled Passage stay rough; their conditions depend on the turn, not on a count of cards.
