# 0031: Play styles weigh the staples as score multipliers

**Status:** Accepted · **Date:** 2026-10-18 · Replaces [ADR 0022](0022-play-style-weighs-the-meta.md)

## Context

ADR 0022 gave three deck tags (Turbo, Stax, Control) a say in what the meta's staples mean and sorted the
missing staples in three bands: favoured, neutral, off-plan. Two limits showed. Four more tags describe how a
deck plays (Tempo, Midrange, Aggro, Combo) and weighed nothing. And a band is blunt: the least played
favoured staple sorted above every neutral one, however rarely the lists play it, while the ideas of ADR 0028
are capped at 50 per source and need an order that a slight preference can enter without taking over.

## Options

1. **Keep the bands,** and add the four tags to them. Easy to explain, but a favoured card of one list in ten
   outranks a neutral staple of nine in ten.
2. **A multiplier on the share.** A favoured card needs only a little less play to catch up, and the share
   still leads. Harder to explain than a band, though the mark shows the outcome.

## Decision

Option 2, in `commander.playstyle`. A deck's styles are its tags Turbo, Stax, Control, Tempo, Midrange, Aggro
and Combo, by casefolded name. Every other tag is neutral.

- **What a card is,** from the card data: its roles (ramp, tutor, draw, counterspell, protection), cheap draw
  (a draw card at mana value 2 or less or free to cast), one-for-one removal, a bounce spell (Tagger's
  `removal-bounce`), a board wipe, cheap interaction (removal, bounce or a counterspell at mana value 2 or
  less or free to cast), a stax piece (what the Meta panel counts as one, `metacombos.pool_stops`, or
  Tagger's `hatebear`), a creature and a haste creature. A card has several; removal, bounce and wipe
  exclude one another.
- **What a style wants,** with a clear fit weighing ×1.25 and a borderline one ×1.1:

  | Style | Favours | Finds off-plan |
  |---|---|---|
  | Turbo | ramp, tutors, draw ×1.25 | removal, bounce, counterspells, board wipes |
  | Stax | stax pieces, removal, bounce ×1.25 | |
  | Control | removal, bounce, cheap draw, counterspells ×1.25 | |
  | Tempo | removal, cheap interaction ×1.25; bounce ×1.1 | |
  | Midrange | counterspells, cheap interaction, tutors ×1.25 | |
  | Aggro | creatures ×1.1; haste creatures ×1.21 | |
  | Combo | tutors, protection ×1.1 | |

- **A card's weight:** a style counts the greatest weight among the traits it favours in the card, and the
  weights of the deck's styles multiply. A card is off-plan, and weighs ×0.8 whatever else favours it, when a
  style finds one of its traits off-plan, that style does not favour the card, and no other style of the deck
  favours that trait (Turbo beside Control keeps its counterspells). A card weighing more than one is marked
  as fitting.
- **Where it applies:** `cmdr meta` and the app sort the missing staples by share times weight and mark them
  "[fits turbo]" or "[off-plan for turbo]", the limit applying after the sort. The shares shown are
  unchanged. Sync takes each source's ideas in the same order, so a favoured card can beat a slightly more
  played one, and compiles no off-plan card (ADR 0028).
- **Racing,** as in ADR 0022: a Turbo deck without the Control tag does not count an unanswered meta combo as
  a gap. The report says "racing, not answering", the table shows "racing", and the Meta panel a muted badge.

## Consequences

- All seven tags now matter, and the order follows the data first and the plan second.
- The weights are judgement. The 1.25 and 1.1 are a nudge, not a ban, and the constants are at the top of the
  module.
- The roles come from Tagger, so a free counterspell with no counterspell tag is neutral, as before. The
  marks are hints for the owner, who still decides.
- A deck tagged both Turbo and Control gets the multiplied weights of both and keeps its unanswered combos as
  gaps.
