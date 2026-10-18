# 0028: Ideas for Considering from four sources, tested by the weighted share

**Status:** Accepted · **Date:** 2026-10-18 · Replaces [ADR 0014](0014-considered-compiled-on-every-sync.md) and
[ADR 0015](0015-ideas-from-commanders-that-play-alike.md)

## Context

ADR 0014 compiled ideas on every sync from two sources, the commander's EDHTop16 lists (bracket 5) and its
most-viewed recent Archidekt decks, each with its own flat threshold and at most 10 ideas. ADR 0015 added the
decks of commanders the profile names as alike. Since then the sample grew (ADR 0027), cards got one share
(ADR 0026), and a deck's play style became a weight (ADR 0031). Two things were still missing. A deck whose
theme is shared with other commanders (a landfall or sacrifice deck) learned from none of them unless the
owner named each commander. And the caps of 10 hid ideas nobody had looked at.

## Options

For decks with a theme:

1. **Name more commanders in the profile.** Exact, but the owner keeps the list up.
2. **Search Archidekt for the deck's theme tags,** and pool the decks of other commanders that carry one.
   Nothing to maintain, but a tag is coarse: any deck can wear it.

For the test on the commander's own sources:

1. **Flat thresholds** (40% of the lists, half the decks). Easy to read, but blind to sample size.
2. **The weighted share's Wilson bound** (ADR 0026). Honest about thin samples.

## Decision

Option 2 for both. `sync` compiles up to 50 ideas per source onto each deck's board under Considering
(`ideas.compile_ideas`), from four sources, each recorded in the card's `origin`:

- **meta:** the staples of the commander's EDHTop16 lists, for decks at bracket 4 and 5.
- **archidekt decks:** the staples of its most-viewed recent decks at the deck's bracket and the one above.
- **adjacent decks:** the commanders under `[ideas] commanders` in the profile (at most 8; a partner pair as
  one entry), their decks pooled. A pair is searched by its first name and only decks led by both are kept.
- **tagged decks:** the decks of other commanders carrying one of the deck's theme tags.

How a card qualifies and where it ranks:

- **meta and archidekt decks:** a card the deck lacks is an idea when the Wilson bound of its weighted share,
  over every list read, reaches 0.32 (ADR 0026). It goes to the source that plays it more, the meta on a
  tie. Below bracket 4 only the Archidekt decks exist.
- **adjacent and tagged decks:** the card is played by at least half of a pool of at least three decks (the
  rule as it stands; whether these pools move to the Wilson test is open).
- **Ranking:** each source takes its ideas in order of score, the source's share times the deck's play-style
  weight (ADR 0031), and off-plan cards are not compiled. Within budget comes before over budget (ADR 0025).
  Ideas already on the board fill places, so repeated syncs settle on the current top 50. A turned-down card
  takes none unless its share has risen 20 points (ADR 0026).
- **Filters,** as for `suggest`: the profile's exclusions, the bracket ceiling, the price limit and, below
  bracket 5, the deck's budget. An idea with no deck card to replace is put on the board all the same and
  shows under *No cut yet* in the app.

Tag search, in `commander.metadata`:

- Only theme tags count: deck tags that became a theme (ADR 0009), not play styles, roles or card types, and
  not categories. Each tag is searched once however it is spelled. Archidekt's `deckTagName` filter matches
  the whole name without regard to case.
- The search takes any commander's decks at the deck's bracket and the one above, three pages per bracket,
  and reads a quarter of `peer_decks` decks (15 of 60), most viewed first. Decks of the deck's own commander
  are left out. The answer is cached by tag and bracket, so every deck with the tag shares it.
- Each tag is a pool of its own. A card several pools play belongs to the pool playing it most, the first on
  a tie, and its reason says so ("in 7 of 15 recent decks tagged Landfall"); the board card keeps the tag
  (`originTag`).
- A commander or tag whose decks cannot be read is named on stderr, and the deck's other ideas still come.

The app labels an idea by its source: "Tournament lists", "Archidekt: <commander> decks", "Archidekt: alike
commanders" and "Archidekt: tagged <tag>". Considering lists the owner's cards first, then Claude's, then the
four sources in that order and the rest; within a group, over-budget cards last, then by weighted share.

## Consequences

- A deck with theme tags learns from every commander that plays alike. Each distinct tag costs up to two
  searches of three pages and 15 deck reads, paced as in ADR 0027 and cached for a day; decks sharing a tag
  share the answer.
- The tagged source is the loosest. It sorts after the others, and its pools are only as good as the tags;
  the badge names the tag so a bad pool can be spotted and the tag ignored under `[themes] ignore`.
- Raising the cap from 10 to 50 puts more on the board; the share pill and the order keep the best first.
- `sync --no-ideas` skips the step, `--dry-run` never runs it, and a deck whose sources all fail keeps its
  board as it was.
- EDHREC is still never asked (ADR 0003); its rank only orders what an idea could replace.
