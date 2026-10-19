# 0037: Meta combos listed by an exposure floor and weighed by recency, with a rising mark

**Status:** Accepted · **Date:** 2026-10-19 · Amends [ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md)
(the 15-combo cut and the unweighted share of lists)

## Context

The meta report lists the combos that top commanders' lists run (ADR 0007). A combo counts for a commander
when a fifth of its lists hold every piece, its exposure is the sum over those commanders of the share times
the commander's meta share, and the report keeps the 15 most exposed. Two things in that rule hide a newer
combo that is spreading:

- **Every list in the window weighs the same.** A line that took off in the last few weeks (the user's
  example: the Shang-Chi and Squirrel Girl lines) is held by few of the lists from the two-month window and
  by many of the latest ones, and a count over all of them shows it at a fraction of its present share. It
  can stay under a fifth, and so not count at all, while it is the combo to answer.
- **A fixed top 15 cuts by rank, not by size.** A busy meta has more than 15 combos worth knowing about, and
  a combo just below the cut is as exposed as one just above it. When the field is quiet, the same cut keeps
  combos that almost no list plays.

The report also gives no sign that a combo is gaining. The reader sees a share, not its direction.

ADR 0026 already weighs lists by age and event size for cards: `weightedshare` halves a list's weight every
30 days and multiplies it by a factor for the event's size. Combos can use the same weights.

## Options

1. **Keep the top 15 and the unweighted share.** Nothing to change, and the problems above stay.
2. **Raise the cap only.** Shows more combos, but a spreading combo under a fifth of the window still does
   not count, and a quiet field still fills the list with combos nobody plays.
3. **Floor, recency weights and a rising mark.** List every combo that clears a share of the meta, weigh each
   list as the card shares do, and mark a combo whose share is growing. Costs a clock in the report, longer
   reports, and a weighted share that is no longer a plain fraction of lists.
4. **Weigh by placement as well.** Cards use the placing too (ADR 0026), but a combo's presence in a list
   does not depend on how well the list did, so the placing adds noise here.

## Decision

Option 3, as the user decided on 2026-10-19.

- **Floor and cap:** a combo is listed when its exposure, as a share of all top commanders' meta shares (the
  figure the CLI prints), is at least `MIN_COMBO_EXPOSURE` (5%). The report sorts by exposure and shows at
  most `MAX_COMBOS` (100, so in practice the floor decides). The total counts every top commander, whether or not it has lists in the window.
- **Weighted share:** each list weighs `event_weight` from `weightedshare`: half per `HALF_LIFE_DAYS` since its
  tournament, times the event-size factor, without the placing. A commander's share of a combo is the weight
  of the lists holding every piece over the weight of all its lists, and the fifth (`COMBO_SHARE`) is tested
  on that. The commander's own meta share stays as it was.
- **Rising mark:** the lists of the last 30 days are compared with those of the 30 days before them, by
  plain counts. A combo is `rising` when its share of the recent lists is at least `RISING_FACTOR` (2.0) times
  its share of the earlier ones, or when none of the earlier lists hold it and at least two recent ones do.
  A commander with fewer than three lists in either half does not judge; the counts pool over the commanders
  that do and where the combo counts. The CLI prints "rising" after the players, the JSON carries `rising`,
  and the app's Meta panel shows a "Rising" badge beside the combo.
- **Unchanged:** the deck's own combos against the meta (ADR 0013) keep their limit of 15; the cap above is for
  the meta's combos.

## Consequences

- A combo that has spread in the last weeks counts at its present share, and one that has faded counts for
  less. The report can run longer, as the floor rather than the cap decides how many combos it lists.
- The Spellbook requests are unchanged: they ask for each commander's core, which this does not touch.
- The cached answers hold the entries, not the combos, so no cache version changes: the combos are worked out
  from them at each report.
- A commander with a small sample (fewer than three lists in a half) never shows a rising combo of its own,
  and the mark can lag by up to a month for a combo that starts spreading before the latest 30 days.
- The shares shown for a combo are weighted, so they are no longer a whole number of lists out of the lists
  read. The 5% floor, the factor of 2 and the three-list minimum were chosen by the user, not fitted to data;
  revisit them after a few weeks of reports, and the 100 cap if the list reads as too long.
