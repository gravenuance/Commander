# 0064: A swap that adds a fetcher is held to a cap, not to the bar

**Status:** Accepted · **Date:** 2026-11-04 · Amends [ADR 0063](0063-the-spare-land-by-the-fetch-ratio.md)
(how the guard judges a swap that adds a fetcher, which swaps it judges, and what the Mana tab shows) and
[ADR 0036](0036-surplus-cuts-on-one-score.md) (`_fits_land`: a mana-only land may replace a fetch land)

## Context

ADR 0063 left four gaps in its Consequences. The owner decided each of them.

- Adding a fetcher always lowers the targets per fetcher. A deck at or under its bar was refused every fetch
  idea that did not also bring a target. Glarb has nine fetches and four targets, a ratio of 0.44, below
  every list of its field, so it could never be offered one.
- A mana-only land could not replace a fetch land. Glarb's field runs Bayou, Tropical Island and Underground
  Sea in all 13 lists, and the guard could not offer them for its fetches.
- Only swaps involving a land were guarded. Farseek or Land Tax replacing a spell was never checked for what
  it could find.
- The Mana tab showed neither the land floor nor the fetches, so the owner could not see what the guard
  worked to.

On 2026-11-04 the cached top-cut lists were checked again. Lists commonly run more fetchers than targets: 148
of 238 do. The targets do not rise with the fetchers. No list strands a fetcher. So a ratio held to the
median says little about whether one more fetcher is too many; the most fetchers a list runs does.

## Options

For a swap that adds a fetcher:

1. **Not under the field's lowest ratio.** Rejected: Glarb is already below it, so every fetch idea for
   Glarb would stay refused.
2. **The bar, as in ADR 0063.** Rejected for the same reason, and for every deck at or under its bar.
3. **No loss and a cap.** The swap may not strand a fetcher or cut a target, and may not bring the deck over
   the most fetchers any list of the field runs.

## Decision

Option 3, chosen by the owner, with the other three changes.

- **A swap that adds a fetcher** (the fetchers after it outnumber those before) skips the bar. It is refused
  when a fetcher is newly stranded, when the targets fall, or when the fetchers end above the cap. Every
  other swap keeps ADR 0063's rules.
- **The cap** is the most fetchers any list runs. `fetch_standard` computes it with the bar from the same
  lists (the compared lists, or at bracket 5 the top commanders' lists with as many colours), so the two
  cannot disagree. Without lists for a bar there is no cap.
- **A land that does only make mana may replace a fetch land** when another fetcher the deck keeps finds it,
  that is, the land has a basic land type that fetcher searches for (`ManaGuard.found_by_other_fetch`, which
  reuses `manabase.fetcher` and `finds_land`). The guard still judges the swap afterwards.
- **A fetcher spell that replaces a spell** goes through the fetch check alone (`ManaGuard.allows_fetcher`).
  The land count and the colour sources are left out: a spell for a spell moves neither, and the sources
  check would refuse a Farseek for the green pip it adds. A spell that is no fetcher is not guarded, as before.
- **The Mana tab** shows the land floor when it is the calibrated one of ADR 0062 (bracket 5, with enough
  top-cut lists). When the deck runs a fetcher it also shows its fetchers, the lands they find, the targets
  per fetcher, the fetchers that find nothing, and the bar and cap when there are any. A deck without a
  fetcher shows no fetch line and so no bar: Magda's bar comes from the few lists of its field that fetch,
  and would mislead.

## Consequences

- A deck under its bar may be offered a fetch land or a fetcher spell that loses no target and keeps within
  the cap. The ratio falls with each one, down to the cap and no further.
- Glarb may be offered Bayou, Tropical Island and Underground Sea for its fetches, while another fetcher of
  the deck finds them. A lone fetcher is never replaced by a land, since nothing else would find it.
- A nonland idea that fetches is refused when nothing it finds is in the deck, and when it would take the
  deck over the cap. A spell that is cut for a spell is still not guarded, a fetcher spell cut among them
  included: cutting one can lose targets, and nothing checks it.
- `Comparison` carries the most fetchers of the compared lists, and `ManaGuard` the cap. The Mana tab's
  payload gains `landFloor` and `fetches`, taken from the guard the swaps are judged by.
- The guard's answers are remembered by the pair of cards and whether only the fetches were judged.
