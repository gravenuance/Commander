# 0041: A computed cut is clearly less played than the idea

**Status:** Accepted · **Date:** 2026-10-19 · Amends [ADR 0036](0036-surplus-cuts-on-one-score.md) (the gate on
every computed cut)

## Context

ADR 0036 lets a deck card be a computed cut for an idea when it is less played than the idea by any margin, and
ranks the allowed cuts on one score. The owner found an idea played in 90% of lists offered one cut, a card
played in 80%, while the deck held several cards at 0–15%. Those cards were kept out by the role, theme and
leaning gates, so the near-staple was the best cut left. Swapping a near-staple for a staple gains a few
points of share and loses a card the meta also plays, so the target was more noise than advice.

## Options

1. **Keep any positive gap.** The ranking already puts the least played allowed card first; a near-staple is
   offered only when nothing else is allowed.
2. **A fixed minimum gap.** Every computed cut must be some points less played than the idea. An idea played in
   fewer lists than the gap gets no computed cut at all, which hits the owner's off-meta ideas most.
3. **A minimum gap scaled to the idea.** The cut must be `MIN_GAP` less played, or half the idea's share when
   that is smaller.
4. **A ceiling on the cut's share.** No computed cut above a fixed share, whatever the idea's share.
5. **Loosen the role and theme gates.** More low-share cards of other roles get through; the gates keep the
   deck's ramp and draw density, so this trades one wrong target for another.

## Decision

Option 3, chosen by the owner, with `MIN_GAP = 0.30` (the owner's pick: a 90% idea may replace cards at 60%
or less). A 30% idea needs cuts at 15% or less, a 10% idea cuts at 5% or less.

- The gate (`_clearly_less_played`) applies to every computed cut: like-for-like, role and theme surplus
  cuts, and nonbasic spare lands. Basic lands stay exempt, as every list plays them.
- Without any shares there is no gate, as before.
- Cuts that you or Claude picked keep the plain gate of ADR 0029: a cut you picked stays however played, and
  one Claude picked must still be less played than the idea. Claude's picks carry their own reasons.
- The theme surplus keeps its own `MIN_THEME_GAP = 0.20`, which still bites for ideas under 40%.
- An idea left with no cut goes to the *No cut yet* panel, where you pick one.

## Consequences

- More ideas land in *No cut yet*, above all well-played ideas in decks whose weak cards are kept by the role
  and theme gates. That is the intent: no target is better than a weak one.
- `MIN_GAP` joins the provisional numbers of ADR 0036, to retune from the owner's accept and reject choices.
