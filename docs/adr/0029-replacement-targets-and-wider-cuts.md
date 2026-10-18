# 0029: Replacement targets: like for like first, then cuts of another role or theme

**Status:** Accepted · **Date:** 2026-10-18 · Replaces the targets and the return of turned-down cards in
[ADR 0011](0011-app-deck-grid-and-replacement-targets.md), and the targets of
[ADR 0016](0016-replacements-like-for-like.md)

## Context

An idea needs deck cards it could replace (ADR 0011), and a card serving a theme is cut only for one serving
the same (ADR 0016). Strictly like for like leaves some ideas with nothing to cut. A removal spell has few
removal spells to replace in a deck that already runs the peers' count, while the deck may run twice the
peers' ramp. An idea of a theme the deck lacks has only cards of no theme to replace. ADR 0011 hid such
ideas altogether. ADR 0011 also said unranked cards on EDHREC come before ranked
ones; the code has had them last.

Like-for-like cuts were the weakest of the role or theme however strong: an idea played in 39% of the
lists was offered three cards in 83% of them. A cut should never be the more played card.

## Options

1. **Keep like for like,** and show an idea with no target under a heading of its own. Safe, but the owner
   picks every cut for those ideas.
2. **Offer any weakest card.** Every idea gets targets, but the deck could lose its removal for a ramp card.
3. **Offer cards of another role or theme only while the deck keeps what other players of the commander run,**
   after the like-for-like ones.

## Decision

Option 3, in `commander.targets` and `commander.peercounts`, with option 1's heading as well.

**Every cut is less played than the idea:** its weighted share (ADR 0026) must be higher than the deck
card's. A card no list plays counts as 0, so it can always go; a card as played as the idea cannot. This holds
for each kind of cut below, land swaps included, except the cuts you wrote on a card yourself. Without any
shares (no list was read) there is no gate. A deck card that was offered once but is more played than the
idea now is simply not offered, and an idea left with none is listed under *No cut yet*.

Targets come in this order, at most three:

1. **Picked:** the cuts you or Claude wrote on the card, while still in the deck. Claude's must pass the gate
   above and give way to the computed ones when they do not; yours stay however played they are, as does the
   cut you chose when queueing the idea (`BoardCard.choice`, which is stored, never computed).
2. **Like for like:** deck cards of the idea's role (an idea with none matches cards with none, a land only
   lands), and a card serving a theme or leaning only for an idea serving one of the same (ADR 0016). Cards
   sharing a theme come first, then the least played: the smallest weighted share (ADR 0026), then the
   ranked cards on EDHREC, least played first (the highest rank number), and unranked cards last.
   Commanders, the profile's kept cards and the idea itself are never offered.
3. **Wider cuts,** only for the slots left and only when at least three recent decks of the commander were
   read. The peers' counts come from `peercounts`: per role the 25th percentile (the floor) and the median,
   per theme the median. A card of another role or theme may go when:
   - each role it fills that the idea does not still has at least the floor after the cut;
   - if it is of a theme the idea does not serve, the idea's share is at least 20 points above the card's,
     and each of the card's themes still has at least the peers' median count after the cut. A theme with no
     peer count, one of the profile's own words, may lose one card.

   Cuts of another role come before cuts of another theme, cards sharing a theme with the idea first. They
   rank by the idea's share less the card's, plus a bonus for each card by which the role the cut empties runs
   over the peers' median (0.03 per card: among peer decks each card of a role past the median is about 3 points less played), then by
   weakness. The reason says "other role" or "other theme" and gives the
   over-run or the gap in points.
4. **Land swaps** of all three kinds pass the mana base guard (ADR 0030).

An idea with no target is no longer hidden: the app lists it under *No cut yet*, where clicking it offers
every deck card but the commander. A turned-down card returns by the rule of ADR 0026.

## Consequences

- More ideas have a cut to start from, and the cut says why it is allowed. The owner still decides each swap.
- An idea no list plays (one you proposed by hand) has no card less played than it, so it is listed under
  *No cut yet* until some list plays it; clicking it still offers every deck card.
- With three peer decks the floors and medians are rough. They only widen the cuts, never narrow the
  like-for-like ones, and nothing wider appears below three decks.
- A theme the roles do not capture (a stax piece, a lock) can still get odd like-for-like targets; Claude or
  the owner can pick the cut.
- The thresholds (`MIN_THEME_GAP`, `OVERRUN_BONUS`, the percentile) are named constants and the over-run
  bonus is provisional, to retune once more
  data shows how often a wider cut is taken.
