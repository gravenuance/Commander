# 0036: Surplus cuts: every candidate on one score, four targets

**Status:** Accepted · **Date:** 2026-10-19 · Amends [ADR 0029](0029-replacement-targets-and-wider-cuts.md)
(the floor gate on cuts of another role becomes a median gate, one score replaces "like for like first", four
targets) and [ADR 0030](0030-land-swaps-guarded-by-the-mana-base.md) (a nonland idea may replace a land
under the guard). [ADR 0016](0016-replacements-like-for-like.md)'s protection of leanings stands.
Amended in part by [ADR 0041](0041-a-cut-clearly-less-played-than-the-idea.md) (a computed cut must be clearly
less played than the idea), [ADR 0062](0062-cedh-land-floor-from-the-top-cut-gap.md) (the spare-land floor
at bracket 5), [ADR 0063](0063-the-spare-land-by-the-fetch-ratio.md) (how a spare land scores, and one
place for the spare lands) and [ADR 0064](0064-a-fetcher-is-added-against-a-cap.md) (a mana-only land may
replace a fetch land).

## Context

ADR 0029 offered the deck cards of the idea's role or theme first, and let cards of another role or theme
fill only the slots those left empty. In practice the like-for-like cards took every slot whatever their
share, so a deck holding twice the peers' ramp, or a land too many, rarely saw those cards offered for the
idea that most needed their place. The gate on a cut of another role was the peers' 25th percentile, which
lets a deck cut down to a count that most peer decks run more than. A nonland idea could never replace a
land, though a deck 38 lands strong with a computed need of 37 has one to spare.

## Options

1. **Keep the order** and widen only the gates. Small, but the best cut is still hidden behind the first
   three like-for-like ones.
2. **One score for all candidates** and a surplus as the reason a card of another role, theme or kind may go.
   Every cut says what allows it, and the owner sees the best four of all kinds.

## Decision

Option 2, in `commander.targets` and `commander.landswap`.

- **One score, four targets.** `MAX_TARGETS = 4`. Every candidate scores the idea's weighted share less its own
  (ADR 0026), plus a bonus for what allows it; the highest four are offered. Scores equal to nine digits tie and go to the bigger
  surplus (cards over the median, or spare lands) first, then the less popular card (the higher EDHREC rank
  number first, unranked last), then the name. Picked targets still come first and
  keep their rules (ADR 0029). Every cut but the user's own is still less played than the idea (gap above 0),
  except that without any shares there is no gap and no wider cut, as before.
- **Like for like** (same role, or a theme or leaning shared, as in ADR 0029) adds `LIKE_FOR_LIKE_BONUS = 0.10`.
- **Role surplus.** For `SURPLUS_ROLES` (ramp, draw, removal, tutor), a card of another role may go when the
  deck holds at least `SURPLUS_MARGIN = 2` cards over the peers' median of every such role the card fills, and
  still at least the median after the cut (one over is within the noise of a median over a few decks). Counts are the Tagger tags alone, as the peers'. The bonus is `SURPLUS_BONUS = 0.03` per card over
  the median of the role over-run most. No theme gap is asked. A card of another role without a role in
  `SURPLUS_ROLES` (protection, recursion and so on) keeps ADR 0029's floor, and a card filling no role goes
  as before.
- **Theme surplus.** A card of a theme the idea does not share may go when the deck holds at least
  `SURPLUS_MARGIN` cards over the peers' median of every theme it serves and at least the median after the cut, and the idea is
  `MIN_THEME_GAP = 0.20` more played than the card (kept from ADR 0029: the surplus says the deck can spare the
  theme, not that this card is the one to lose). The bonus is `SURPLUS_BONUS` per card over the median of the
  theme over-run most. A theme with no peer count (the profile's own words) is no surplus. A card that would
  empty a role in `SURPLUS_ROLES` is judged by role surplus alone, never by theme surplus, so nothing counts
  twice; one that holds such a role only in common with the idea (a draw card for a draw idea) is judged by
  theme surplus like any other.
  A leaning is never cut as a surplus of either kind unless the idea serves it too.
- **Land surplus.** A nonland idea may replace a land, only with the peers' counts, the shares and the mana data
  read, and only when `ManaGuard.allows(idea, land)` passes with the idea counted in (ADR 0030). The guard
  counts the spare lands: how many lands the deck could lose before it falls below its floor, the greater of
  Karsten's count less `LAND_TOLERANCE` and the median land count of the compared lists (Karsten's alone
  without `MIN_PEER_LISTS` lists), on the deck as it stands. The lists' lowest count is no floor, nor is
  coming closer to Karsten's count than now, or a deck far over the count would report lands to spare well
  past the count it should run. None to spare, no land target. The basic land of the colour with the most
  sources to spare (the colour's sources less what its most demanding spell needs) is one candidate; other
  basics are not offered, not even when the guard refuses that one. A basic needs no gap to the idea, since
  nearly every list runs basics (about 90% shares in real data). A nonbasic land is a candidate only when it does nothing but make mana (`manabase.mana_only_land`: every line
  of its front face's oracle text, reminder text left out, is a mana ability of one mana, an enters-tapped line, check, fast,
  slow, reveal or shock, or empty; unknown text is not mana-only, nor is a land with a half that is no land, such as
  Midgar's Adventure), carries no role (Tagger tag or the deck's
  category), serves no theme or leaning, and is less played than the idea. Both tests apply because mana-only
  is not stricter than the role test: a mana-only land can carry a ramp tag. A utility land (Vesuva and other
  copy lands, Field of the Dead, Mirrorpool, fetches, cycling, bounce lands, man-lands, and any land whose mana ability can
  make more than one mana: Ancient Tomb's "Add {C}{C}", "for each" as in Gaea's Cradle and Cabal Coffers, X or
  "an amount equal to" as in Nykthos, Urza's Tower) or a theme land is never spare. A filter land, which pays a mana for two,
  nets one and stays mana-only. A basic scores `SURPLUS_BONUS` per spare land; a nonbasic that
  plus its own gap.
- **Reasons** say which kind: "surplus draw: 13 against a median of 10 in recent decks", "surplus theme
  (Tokens): 6 against a median of 4 in recent decks; idea 30 points more played", "spare land: the mana base
  holds with 2 fewer lands". Like-for-like reasons are unchanged. The share and the EDHREC rank follow.
- Protected cards (the profile's kept cards, and the pieces of every combo Commander Spellbook finds inside the
  deck) and commanders are cut by no path. Combo is no counted role: no peer count exists for it. Without three recent decks, or
  without shares, only like-for-like targets remain, now up to four.

## Consequences

- A deck that over-runs a role or a theme, or holds a land to spare, offers those cards for the ideas that
  rank above them, whatever the idea's own role. A like-for-like card still wins when it is played little
  enough: its bonus of 0.10 is worth a little over three cards over the median.
- The score is one provisional number set: `LIKE_FOR_LIKE_BONUS`, `SURPLUS_BONUS`, `SURPLUS_MARGIN`, `MIN_THEME_GAP`. Retune
  them once the owner's accept and reject choices show which kinds of target get taken.
- The swap itself is still judged by `allows`, one land at a time, so a deck far over its count may lose lands
  the spare count already gives up on its way down.
- Two kinds of card are no longer offered for ideas of another role or theme: a card of a role in
  `SURPLUS_ROLES` that the deck holds less than `SURPLUS_MARGIN` over the median of, and a card of a theme with
  no peer count (the profile's own words). Both stay offered for ideas that share the role or theme.
- The margin and the floors came from a review of the surplus scores on the owner's 11 decks, which were
  inflated: a median over a few decks moves by a card, and a lowest count is what one deck ran, not what to
  aim for. `SURPLUS_MARGIN` and `LIKE_FOR_LIKE_BONUS` are the numbers to retune next.
- The page shows four targets in the space of three; the buttons wrap.
- Combo pieces are protected only when Spellbook's answer for the deck was read (`MetaData.own_combos`);
  without it, only the profile's kept cards are, and `deck_cut_rules` logs that at info level for the deck.
- Lands with conditional extras (City of Brass, the Verges, the Thriving and gain lands, Glimmervoid) count as
  utility lands, since their text goes beyond a mana line. That fails safe: a utility land is never cut. Artifact
  lands and Dryad Arbor count as mana-only, so in an artifact or creature deck they are spared only by their
  role and theme tags.

## Amendment (2026-10-28): what is protected

A review of the cuts offered on the owner's eleven decks found the deck's engine cards offered as cuts: below
bracket 5 Spellbook's own answer for the deck is not read, so the combo pieces were protected for three decks
of eleven. The protected set (`targets.deck_cut_rules`) is now:

- the cards the profile keeps, as before;
- the pieces of the deck's combos: Spellbook's own answer, else the combos of the deck's cached bracket
  estimate (`bracket.cached_estimate`, cache only, however old), so a bracket 4 deck is covered too;
- the deck's key cards ([ADR 0017](0017-key-cards-found-from-the-deck.md), amended the same day);
- the profile's grace cards ([ADR 0039](0039-grace-cards-weight-the-lists-like-yours.md)): the owner weighs the
  lists holding them more because the deck is built around them.

None of these is a computed cut for any idea. A cut picked by hand still can be one, and the desk rebuilds its
cut rules when the deck's estimate file changes.

The same review changed three rules of the surplus score (`targets._wider_cut`):

- **A card of a surplus role keeps its theme protection.** "Judged by role surplus alone" meant to count the
  bonus once, but it also dropped ADR 0016's rule that a theme card is cut only for an idea of the same
  theme: Chatterfang's Fecundity (Aristocrats) was offered for 23 ideas, Edgar's Dusk Legion Duelist for 16. A
  card that would empty a role of `SURPLUS_ROLES` and serves a theme the idea does not share now needs the
  role surplus and the theme surplus; the larger of the two sets the bonus and the reason, so nothing counts
  twice. A card whose themes the idea shares, or with none, still goes by role surplus alone.
- **The deck's identity is no theme surplus.** A theme that holds a word of the profile's own themes
  (`CutRules.profile_themes`; the deck tag Dinosaurs for the word "Dinosaur"), or is one of the deck's
  `CORE_THEMES = 3` largest by cards held (leanings left out), is never a theme surplus: a Dinosaur deck
  offered its Dinosaurs for ramp spells because it ran more than its peers. Whether a deck is unlike its peers
  in what it is built on is no sign that it can spare it.
- **The bonus stops at three cards.** `MAX_SURPLUS_CARDS = 3` caps the cards over the median that count
  (`SURPLUS_BONUS * min(over, 3)`, also for spare lands), so a surplus never scores 0.10 or more and never
  outranks a like-for-like cut on size alone; Bello's Life's Legacy led 16 of 30 ideas and Chatterfang's
  Corrupted Conviction 34 of 52 at up to +0.27. The tie-break by the bigger surplus is not capped.

A land idea is held to "a utility land is never spare" as well (`targets._fits_land`). It had applied to
nonland ideas only, so a dual land was offered Vesuva, Command Beacon, Urborg or Maze of Ith to replace: a land
that does nothing but make mana (`ManaGuard.mana_only`) may be replaced only by a land idea of the same kind,
and a utility land idea may replace any land. A basic land may be replaced by a land idea that makes one of its
colours (`ManaGuard.makes_colour_of`), by one that makes no coloured mana for any basic; ADR 0041's amendment
says how basics score. Without the mana base's data (no guard) every land fits, as before. Verges, Tainted
lands and fetches count as utility lands here (see the consequence above), so the rule errs towards keeping
lands.
