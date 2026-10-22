# 0056: The goldfish breaks ties between cut targets

**Status:** Accepted · **Date:** 2026-10-22 · Builds on [ADR 0036](0036-surplus-cuts-on-one-score.md) (the
score and the four targets) and [ADR 0054](0054-goldfish-a-deck.md) (the swap figures)

## Context

An idea offers up to four deck cards it could replace, ordered by one score (ADR 0036): the idea's share
less the card's, plus a bonus for what allows the cut. Often two or three targets score almost alike, and
the order among them is then luck: a surplus point, a rank on EDHREC. The goldfish can say what each of those
cuts does to the deck's mana, its commander and its colours, which the score cannot see. Since ADR 0054 it
can do so for a swap, and now that a swap of some copies is played, also for the basic land that is so often
the best-scoring cut.

The goldfish knows nothing of interaction, tutors that answer a threat or a card that protects the deck, so
it must not decide which cards are cut, only which of two near-equal ones is cut first.

## Options

1. **Rank all targets by the goldfish.** Simple to explain, but it lets a number that ignores interaction
   override the meta's score, and may put a card with a clearly worse score first.
2. **Break ties only.** Targets within a small margin of each other are ordered by the goldfish; the others
   keep the score's order. The goldfish never promotes a card the meta ranks clearly lower, and never
   offers a card the meta did not.
3. **Show the figures beside every target and leave the order.** Nothing is hidden, but four folds per idea
   are too much to read, and the owner would do the comparison by eye.

## Decision

Option 2. Decided with the owner on 2026-10-22.

- **Tied.** Targets are ordered by score, best first. A tie group starts at a target and takes the targets
  after it that score at most 0.10 below it, so no two in a group differ by more than 0.10. The margin is the
  owner's; it is the size of `LIKE_FOR_LIKE_BONUS`. Picked targets (ADR 0029) carry no score, belong to no
  group and keep their places. `Target.score` carries the score to the page and the CLI.
- **Order within a group.** Each target's swap, the idea for that deck card, is played against the games
  already played (ADR 0054). The target whose swap makes fewer headline figures worse goes first; of those,
  the one that makes more of them better; otherwise the meta's order stays. "Worse" and "better" are the
  direction the Goldfish fold already shows, and only headline figures that moved count. A figure with no
  better direction (a combo landed by the bracket's turn) counts as neither.
- **Never beyond the meta's targets.** The goldfish orders the targets the score offered and offers no
  other. Interaction moves no figure, so a card the meta kept for what it does against opponents is not
  judged on that.
- **The page.** When an idea's tile is selected, one bridge call, `goldfish_cuts(deck, card)`, plays the
  targets that have a tie and answers all of them in order, each with whether it moved up and the figure that
  decided it. The tile shows the meta's order until the answer comes, then reorders once, keeping keyboard
  focus on a button that moved. A target that moved up carries a small mark, "Goldfish pick", whose tooltip
  names the figure in a few words. The answer is kept like the swap fold's: by deck, card, targets, the
  view's stamp and the settings' epoch.
- **The command line.** `cmdr goldfish <deck> --cuts "<card in>"` plays the card against each of its
  targets from the board and prints their figures, with the order the app shows. A card the board does not
  hold, or one with no targets, is an error with a plain message and a non-zero exit. `--json` gives the
  figures.

## Consequences

- A selected idea costs up to four paired comparisons when it has a tie, about a second the first time for
  the deck's games and a fraction of that for each swap; they are kept per swap, so the swap fold of the
  chosen cut is free afterwards. An idea with no tie plays nothing.
- A tie group of one needs no games, and neither does a list of picked targets.
- Most ties will resolve to "nothing moved either way", and the meta's order stays. The mark appears only
  when the goldfish has a reason.
- The margin of 0.10 and the use of the headline figures alone are the owner's first choices. Revisit them
  if the marks look arbitrary on real decks.

## Amendment (2026-10-22): what the command line plays, and plans that keep copies

- `cmdr goldfish <deck> --cuts "<card in>"` plays the card against every target, tied or not, and prints
  each target's figures; the page plays only the targets of a tie. The order is the same in both, because a
  target outside a tie keeps the score's place whatever its figures say. A card the board does not hold and
  a card with no targets are usage errors and exit with code 2.
- A target whose swap cannot be played (one the deck holds fewer copies of than the card brings, or one no
  longer in the deck) has no figures. It keeps its place in a tie, the others are sorted around it, and it
  is never a Goldfish pick; the command line prints why it cannot be played. Counting it as "nothing moved"
  would put it ahead of any played target with a figure made worse.
- A target the deck holds several copies of (a basic land, most often) gives up only as many copies as the
  incoming card brings, the rest staying in the deck, as the amendment to ADR 0054 on a swap of some copies
  sets out. By the same rule, a remove in a plan given to `cmdr goldfish --plan` cuts the copies beyond its
  `keep`, and the plan's add must bring as many; the owner kept this rule on 2026-10-22. Before, a plan's
  `keep` was refused.
- A target is marked as moved up when it goes ahead of any target the score ranked above it, even if its
  own place does not change: from A, B, C to C, B, A, both C and B are marked.
