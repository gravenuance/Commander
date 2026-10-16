# 0013: The deck's own combos checked against the meta

**Status:** Accepted · **Date:** 2026-10-16

## Context

`cmdr meta` ([ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md)) looked at the deck's weak
spots in one way only: meta cards that remove or counter a key piece, where key pieces are the commander
plus the cards a profile keeps. Etali has no profile, so only Etali counted, and nothing was said about its
Food Chain loop: what in the meta stops it, and who plays that. The owner asked for this on every bracket 5
deck, with the meta scanned for the deck's current combos.

Combo answers ([ADR 0008](0008-answers-read-from-rules-text.md)) already read, for each meta combo, which
deck cards stop it. The same reading works the other way round: the deck's combos against the cards the
meta plays.

Checking Etali by hand showed three answers that are wrong in both directions:

- Grafdigger's Cage stopped Squee + Food Chain. Its tag counted it as a lock on every zone, but its text
  covers graveyards and libraries, and Squee is cast from exile.
- Deafening Silence and Ranger-Captain of Eos stopped the same loop. They lock noncreature spells, and the
  loop casts Squee, a creature. The scope reader also took "noncreature" for "creature".
- Grand Abolisher stopped it by its activation tag. Its lock holds only during its owner's turn, and a
  combo is played on its owner's turn.

## Options

1. **Ask the owner to list key pieces in every profile.** No new code, but combos are more than their
   pieces: a lock on casting from exile hits no piece and still stops the loop.
2. **Check the deck's own combos against the meta's cards, using the existing combo-answer reader.**

## Decision

Option 2, for every bracket 5 deck.

- `gather` asks Commander Spellbook for the deck's own combos (`find-my-combos` on its list), cached for a
  week under the deck's id; a changed list asks again.
- The meta's pool is every card at least 30% of a top commander's lists play: the pool the key-piece
  threats already used, now shared. `pool_answers` builds stax, removal and counter answers from any pool,
  not only a deck.
- Each combo lists its stoppers as stax, then removal, then counters, each most played first, tagged with
  the commanders whose lists play them. Combos are shown fewest pieces first, at most 15.
- The lock reader gains three rules, which apply to both directions:
  - A lock's zones come from its text ("can't cast spells from graveyards or libraries"). The rule for
    casting outside the hand matches only combos that cast from one of those zones.
  - A "noncreature" casting lock misses a combo whose cast pieces are all creatures.
  - A card whose lock begins "During your turn, your opponents can't" stops no combo.

## Consequences

- Etali, Glarb and Magda show each combo with what threatens it. Ellivere has no combos and shows none.
- One more Spellbook request per deck per week, paced with the meta's.
- The lists are long: most counterspells and bounce reach some piece. The app shows the most-played four
  of each kind and folds the rest.
- Stifle-style answers to a combo's triggers or activations are still not read (declined earlier as low
  value).
