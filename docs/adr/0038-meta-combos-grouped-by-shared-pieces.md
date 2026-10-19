# 0038: Meta combos grouped by the pieces they share

**Status:** Accepted · **Date:** 2026-10-19 · Amends [ADR 0037](0037-meta-combos-by-floor-and-recency.md)
(the flat list of meta combos)

## Context

The meta report lists every combo that makes up 5% of the meta (ADR 0037), which in a busy field is a long
flat list. Many of its combos are one core with a different last piece or two: the same commander and the same
engine, ending in another card. Reading them one by one repeats the shared pieces in every row, hides which
rows are really one line of play, and shows the same answers again and again, because an answer to the shared
pieces answers all of them.

## Options

1. **Keep the flat list.** Nothing to change; the repetition stays.
2. **Group by the commander only.** Easy, but a commander has many unrelated combos, and combos of two
   commanders can share an engine.
3. **Group by shared pieces.** Combos that hold the same core of pieces sit together under that core. Costs an
   algorithm for overlapping cores, and a combo can fit more than one.
4. **Group in the page.** Keeps the Python small, but the command line would then print the flat list or need
   its own copy of the rule.

## Decision

Option 3, in Python (`commander.metacombos.group_combos`), so that `cmdr meta` and the app share one rule.

- **Cores.** A core is a set of at least `MIN_SHARED_PIECES = 2` pieces that is the intersection of two or more
  combos. Commanders count as pieces, as Spellbook lists them.
- **Assignment.** A combo joins the group of the largest core it holds. A tie goes to the core whose combos
  (all that hold it) add up to the more exposure, then to the core's names in alphabetical order. A group left
  with one combo, because the others went to larger cores, is no group: its combo stands on its own.
- **Singles.** A combo that shares fewer than two pieces with every other combo stays on its own.
- **Order.** Groups and singles come by their best exposure, ties in the order the combos came in. Inside a
  group the combos keep the order they had.
- **Display.** A group has a heading naming the shared pieces ("Shared: Deadeye Navigator, Kinnan,
  Shang-Chi", pieces in alphabetical order) and its combos under it, each showing only the pieces outside the
  core, with its exposure, results, players, answers and rising mark as before. A combo made of the shared
  pieces alone says so. The app's answer carries `comboGroups`, each `{shared, combos}` with a combo row plus its
  `others`; a combo on its own has `shared` empty. The command line prints the same groups, the heading on a
  line of its own and the combos indented under it. `--json` keeps the flat `combos`.

## Consequences

- A busy meta reads as lines of play rather than a list of near-copies, and the pieces that matter (the
  shared ones) are named once.
- A combo that fits two cores is shown under one; the other core's heading never lists it, though it holds
  those pieces. The larger core is the more specific line of play, which is why it wins.
- The tie-break by total exposure counts every combo that holds a core, not only those that end up in its group.
- The grouping looks at piece names only, not at how the combos work, so two combos sharing two incidental
  pieces (Sol Ring and a commander) group as readily as two sharing an engine. Raise `MIN_SHARED_PIECES` if
  that proves noisy in real reports.
- The 100-combo cap and the 5% floor of ADR 0037 are untouched; grouping changes the order only through the
  groups' best exposure.
- A combo that is the core of larger combos can still show as a single. With A = {x, y}, B = {x, y, z} and
  C = {x, y, z, w}, B and C share the larger core {x, y, z} and group under it, which leaves A the only combo
  of {x, y}, so A stands on its own though it shares those pieces with both.
