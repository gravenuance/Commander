# 0011: The app as a deck grid with replacement targets, label flags and a meta panel

**Status:** Accepted · **Date:** 2026-10-16 · Superseded in part by
[ADR 0029](0029-replacement-targets-and-wider-cuts.md) (the targets and an idea without one),
[ADR 0026](0026-one-weighted-share-per-card.md) (the share behind the order and the return of turned-down
cards) and [ADR 0032](0032-one-considering-list-and-one-sync.md) (the one list and the Meta panel's refresh)

## Context

The first app ([ADR 0006](0006-deck-boards-and-desktop-app.md)) listed board cards in Suggested,
Considered and Decided columns. Each card carried a radio list of cuts, and the deck itself was not
shown. Every `suggest` idea carried the same generic cut list, so on Pantlaza all 13 ideas offered the same
three cuts. The Meta panel was a set of tables, and labels ([ADR 0010](0010-buying-cards-through-archidekt-labels.md))
had no UI.

The owner drew a new layout in Figma (2026-10-16) and decided:

- The deck shows as an Archidekt-style card grid, grouped by card type, with counts and EUR totals.
- One Considered list holds every idea, whatever its source (meta, Claude, Archidekt, Scryfall, EDHREC
  rank), and each idea must have at least one valid card to replace. Hovering or clicking an idea lights
  its targets in the grid; clicking a target queues the swap; × turns the idea down.
- A turned-down card can come back if the meta takes to it later.
- A corner flag on each grid card opens a label menu, as on Archidekt.
- Below Considered, a Meta section for bracket 5 decks only: each key deck card with the meta cards that
  stop it, then each meta combo with the deck cards that stop it, both tagged with the meta decks that
  play them.
- The Decided list goes. Activity folds to a rail.

## Options

For the targets:

1. **Keep the cut lists the tools write.** No new logic, but most ideas share one generic list.
2. **Hand-picked targets only.** Each target is precise, but every tool idea would need research before
   it showed.
3. **Hand-picked when given, else computed from roles.** Every idea gets a target, and research can still
   override the computed ones.

For bringing back turned-down cards:

1. **Never.** Simple, but the owner wants meta shifts to count.
2. **On any rise in meta share.** Noisy with two months of thin data.
3. **On a rise of 20 points or more since the card was turned down.**

## Decision

Targets option 3 and return option 3. The owner chose both, along with the layout above.

- **Targets** (`commander.targets`): the replace options on a board card count as picks only when you or
  Claude wrote them, and only while the card is still in the deck. Otherwise the targets are deck cards
  that share a role with the idea. An idea with no role matches cards with no role, and lands match only
  lands. Commanders, the profile's kept cards and the idea itself are never targets. The weakest come first:
  lowest meta share (bracket 5 only), then unranked on EDHREC, then the higher EDHREC rank. At most three
  targets are offered. An idea with no target does not show.
- **Return** (`board.feed_ideas`): a turned-down card keeps its meta share at that moment (`rejectedShare`
  in `board.json`; the board stays version 1 because the field is optional). When a meta refresh offers the
  card again and its share has risen 20 points or more, it reopens, with the rise in its reason.
- **Meta refresh in the app** puts the meta's staples on the board, as `cmdr meta --to-board` does.
- **Labels**: snapshots keep each label's colour (version 4; older snapshots fetch each deck once). The
  menu offers no label, Don't Have, Getting and every other label the deck uses. A choice is written to
  Archidekt at once, the same way as a `toget` mark.
- **Grid**: commanders first, then Creature, Planeswalker, Battle, Instant, Sorcery, Artifact,
  Enchantment, Land. Cards are sorted by mana value, then name. Each card also carries its Archidekt
  category, for a later toggle.

## Consequences

- Every idea has somewhere to go, and choosing a cut takes one click on the card it replaces.
- Computed targets are only as good as the Tagger roles behind them. A card whose job no role captures,
  such as a stax piece, may get odd targets until Claude or you pick some.
- Meta shares depend on the cached EDHTop16 answers. Without a cache, bracket 5 targets fall back to
  EDHREC rank.
- Ideas without a valid target are hidden rather than shown with a warning. They stay on the board and
  in `cmdr board show`.
- Turned-down cards no longer show in the app. They return through the meta, or when proposed again.
