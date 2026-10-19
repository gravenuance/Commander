# 0039: Grace cards weight the lists like yours

**Status:** Accepted · **Date:** 2026-10-19 · Extends [ADR 0026](0026-one-weighted-share-per-card.md) (one
weighted share per card); the setting lives in the deck profile of [ADR 0005](0005-deck-profiles.md)

## Context

Some commanders in the meta run several very different lists. Kinnan is the case that raised this: one line
flips little creatures, another flips big ones, and a third plays no Basalt Monolith at all. A player of one
of them reads percentages and ideas pooled over all of them (ADR 0026), so a card the player's own line runs
in every list shows as 40% and an idea that only the other lines play can pass the idea test. The shares are
right for "the commander" and diluted for "my version of it".

The player already knows which cards mark their line. The app has no reliable way to find that out itself:
clustering lists by similarity would be a design of its own, and would guess at what the user can simply say.

## Options

1. **Leave the shares as they are.** Nothing to build; the dilution stays.
2. **Filter: count only the lists that hold the deck's cards.** Simple to explain, but a hard cut: a list
   missing one card is out, and with 20 or 30 lists the sample collapses. It also needs a threshold.
3. **Cluster the lists by similarity and pick the deck's cluster.** Needs no input, but is a clustering
   design with its own failure modes, and it is hard to explain why a list was left out.
4. **Grace cards: the deck names cards, and lists holding them weigh more.** A soft weight, one setting the
   user controls, and it composes with the weights ADR 0026 already has.

## Decision

Option 4.

- **Profile only.** `profile.toml` gets a `[grace]` table mapping a card name to a factor. The app never writes
  it: the user edits the file, as for the other keys that are not in the Settings dialog. `cmdr profile` shows
  the grace cards.
- **A per-card factor, multiplied, capped.** A factor is a number over 1 and at most 10. A list's or deck's
  weight is multiplied by the product of the factors of the grace cards it holds, and the product is capped at
  `MAX_GRACE = 10`, so a list holding every grace card cannot become the whole sample. A factor of 1 or less
  is a profile error, not a way to down-weight: that would be a different feature.
- **Same step as the other weights.** The factor multiplies the list weight (`_list_weight`, `_deck_weight`)
  before each source's weights are scaled to a mean of one, so a list is still about one vote and the pooling
  of the two sources by list count is unchanged. A source whose lists all get the same factor product is
  unchanged. Names match by `card_key`: case does not matter, and a name is the full name or the front face
  of a two-faced card; punctuation must match and a back face does not. `weighted_shares` takes the mapping as a parameter; it does not read profiles.
- **Card shares only.** The deck list's percentages and Considering pills, the replacement targets and the
  ideas `sync` compiles use the deck's grace. The meta breakdown (combos, key cards, field, stoppers) does
  not: it describes the meta, not the player's slice of it, and weighting it would hide what the field really
  does.
- **Shown in the app.** The deck view carries the grace cards and how many of the lists read hold at least one,
  and the share's breakdown says so ("Weighted toward lists with Basalt Monolith ×3: 12 of 30 lists hold
  one"), so a changed percentage can be traced to its cause.

## Consequences

- A player of one line of a split commander gets the percentages and ideas of that line, and can tune the
  strength per card.
- Heavy grace shrinks the effective sample. The shares stay weighted shares, and the Kish effective sample size
  and the Wilson lower bound that already decide what is an idea (ADR 0026) absorb it: with a few lists
  carrying most of the weight, the bar is stricter for any one card, while cards the grace lists play can
  clear it where they would not have, which is the honest answer. The raw counts
  (`12 of 30 lists`) stay the number of lists read.
- A grace card that no list holds changes nothing, and with no `[grace]` table the shares are exactly what they
  were.
- The user has to know which cards mark their line, and a wrong or too heavy choice makes the shares describe
  the grace cards' lists only. `cmdr profile` and the breakdown line show what is set.
- Editing `profile.toml` refreshes what the app shows through the board stamp, which already includes the
  profile; the next `sync` recompiles the ideas with it. There is no setting in the app's dialog, and none is
  planned until the file proves awkward.
- The ideas already on a board are judged again at the next sync (`rejudge_ideas`) with the grace in force,
  so an idea that only passed on the other lines is turned down then, with the usual reason.
