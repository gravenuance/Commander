# 0028: Ideas for Considering from four sources, tested by the weighted share

**Status:** Accepted · **Date:** 2026-10-18 · Its tournament lists now come from TopDeck.gg
([ADR 0047](0047-tournament-lists-from-topdeck-with-a-key-per-user.md)) · Replaces [ADR 0014](0014-considered-compiled-on-every-sync.md) and
[ADR 0015](0015-ideas-from-commanders-that-play-alike.md) · Extended by
[ADR 0034](0034-fetch-the-decks-you-look-at-first.md) (the order a sync fetches in)

## Context

ADR 0014 compiled ideas on every sync from two sources, the commander's EDHTop16 lists (bracket 5) and its
most-viewed recent Archidekt decks, each with its own flat threshold and a cap on the ideas. ADR 0015 added the
decks of commanders the profile names as alike. Since then the sample grew (ADR 0027), cards got one share
(ADR 0026), and a deck's play style became a weight (ADR 0031). One thing was still missing: a deck whose
theme is shared with other commanders (a landfall or sacrifice deck) learned from none of them unless the
owner named each commander.

## Options

For decks with a theme:

1. **Name more commanders in the profile.** Exact, but the owner keeps the list up.
2. **Search Archidekt for the deck's theme tags,** and pool the decks of other commanders that carry one.
   Nothing to maintain, but a tag is coarse: any deck can wear it.

For the test on the commander's own sources:

1. **Flat thresholds** (40% of the lists, half the decks). Easy to read, but blind to sample size.
2. **The weighted share's Wilson bound** (ADR 0026). Honest about thin samples.

## Decision

Option 2 for both. `sync` compiles up to 50 ideas per source (the cap since 0.4.0) onto each deck's board under Considering
(`ideas.compile_ideas`), from four sources, each recorded in the card's `origin`:

- **meta:** the staples of the commander's EDHTop16 lists, for decks at bracket 4 and 5.
- **archidekt decks:** the staples of its most-viewed recent decks at the deck's bracket and the one above.
- **adjacent decks:** the commanders under `[ideas] commanders` in the profile (at most 8; a partner pair as
  one entry), their decks pooled. A pair is searched by its first name and only decks led by both are kept.
- **tagged decks:** the decks of other commanders carrying one of the deck's theme tags, for decks whose
  profile sets `[ideas] tag_search = true` (see *Tag search is opt-in* below).

How a card qualifies and where it ranks:

- **meta and archidekt decks:** a card the deck lacks is an idea when the Wilson bound of its weighted share,
  over every list read, reaches 0.30 (ADR 0026). The candidates are the cards of every list read, not only
  the top cuts, and the reason counts them ("in N of M lists"). It goes to the source whose weighted share
  of it is larger, the meta on a tie. Below bracket 4 only the Archidekt decks exist.
- **adjacent and tagged decks:** the card is played by at least half of a pool of at least three decks. A
  pool keeps this rule while its source is on; the owner chose it over the Wilson test, which is for the
  commander's own lists.
- **Ranking:** each source takes its ideas in order of score, the source's weighted share times the deck's
  play-style weight (ADR 0031; adjacent and tagged ideas have no weighted share and use the pool's plain
  share), and off-plan cards are not compiled. Within budget comes before over budget (ADR 0025).
  Only ideas cmdr put on the board for that source fill its places, so repeated syncs settle on the current
  top 50; cards by you or Claude, and ideas of another source, take none. A turned-down card takes none
  unless its share has risen 20 points (ADR 0026).
- **Filters,** as for `suggest`: the profile's exclusions, the bracket ceiling, the price limit and, below
  bracket 5, the deck's budget. An idea with no deck card to replace is put on the board all the same and
  shows under *No cut yet* in the app.

Re-judging, in `ideas.rejudge_ideas`, before each sync compiles a deck's new ideas:

- Every open board card whose source is `cmdr`, or `archidekt` (the old Maybeboard pick-up, ADR 0035), is
  tested again. A card by `you` or `claude`, and any card queued, applied or turned down, is never touched.
- **meta and archidekt decks:** a card whose origin is `meta` or `archidekt decks`, and an `archidekt` card
  with no origin, must still pass `ShareTable.is_idea`, the test new ideas pass. A meta idea is judged only
  when the EDHTop16 lists were read in this sync, a decks idea only when the Archidekt decks were, and nothing
  when no list was read at all (a failed source must not lower the shares and turn cards down).
- **adjacent and tagged decks:** the card must still be played by at least half of its pool of at least three
  decks. An alike idea is judged only when every commander group's decks were read, a tagged idea only when
  its own tag's pool was read; otherwise it is left alone. Once the source is gone for the deck, the commander's
  own test applies instead: an alike idea when the profile lists no commander group, a tagged one while the
  deck's tag search is off or when its tag is no longer among the deck's theme tags. It must pass `ShareTable.is_idea` on the combined share like a meta idea (judged
  only when a list was read); a failing card is turned down, a passing one is relabelled (activity
  "relabelled") to the source that plays it more, the meta on a tie, with the reason a compiled idea has and
  its tag and pool cleared. With several groups listed the alike pool stays one pool, so a card of a removed
  group is judged by the pool rule until the last group goes.
- A card that fails is turned down as `cmdr` with the reason "no longer makes the cut", remembering its
  weighted share now (0 for a card no list plays), so it comes back only if the share rises 20 points
  (ADR 0026). The turn-down checks under the board lock that the card is still open, not reopened by the user and by
  `cmdr` (or the old pick-up).
- A card the user reopened (`reconsidered` on the board card) is never re-judged until they decide it
  (ADR 0035).

Tag search, in `commander.metadata`:

- Only theme tags count: deck tags that became a theme (ADR 0009), not play styles, roles or card types, and
  not categories. Each tag is searched once however it is spelled. Archidekt's `deckTagName` filter matches
  the whole name without regard to case.
- The search takes any commander's decks at the deck's bracket and the one above, three pages per bracket,
  and reads a quarter of `peer_decks` decks (15 of 60), most viewed first. Decks of the deck's own commander
  are left out. The answer is cached by tag and bracket, so every deck with the tag shares it.
- Each tag is a pool of its own. A card several pools play belongs to the pool playing it most, the first on
  a tie, and its reason says so ("in 7 of 15 recent decks tagged Landfall"); the board card keeps the tag
  (`originTag`) and, for tagged and alike ideas, the pool's numbers (`originPool`: `count` and `total`), an
  optional field that a sync adds to ideas already on the board.
- A commander or tag whose decks cannot be read is named on stderr, and the deck's other ideas still come.

The app names an idea's source in the breakdown behind its share pill, which is coloured by that source:
"Tournament lists", "Archidekt: <commander> decks", "Archidekt: alike commanders" and "Archidekt: tagged
<tag>". The breakdown of an alike or tagged idea gives its pool's share and size, from the board card's `originPool`.
The pill of an alike or tagged idea shows that pool share, the figure the idea was picked by (the pill
reads "60%", its label and tooltip "60% of Landfall-tagged decks"), and the combined share stays in the
breakdown; the other sources' pills show the combined share.
Considering lists the owner's cards first, then Claude's, then the four sources in that order and the rest;
within a group, over-budget cards last, then by the share the pill shows (the pool share for alike and tagged
ideas).

## Tag search is opt-in per deck

The tagged source is off unless the deck's profile sets `[ideas] tag_search = true`, the Settings switch
"Search decks by tag" in the app; a missing key means off. While it is off, `gather_idea_pools`
passes no tags to `gather_adjacent`, so no tag request goes out, whether from `sync`, `meta --to-board` or
the app. Alike-commander search is unchanged: it runs for the commanders the profile lists. Tagged ideas
already on a board stay until the next sync judges them by the commander's own test (see *Re-judging*).

Why: an analysis of cached decks found that tag pools add almost nothing beyond the commander's own decks, and each
tag costs about 1.5 to 2 minutes of paced requests on a first sync. The owner opts a deck in when its theme
is broad enough to want the pools.

## Consequences

- A deck that searches by tag learns from every commander that plays alike. Each distinct tag costs up to two
  searches of three pages and 15 deck reads, paced as in ADR 0027 and cached for a day; decks sharing a tag
  share the answer.
- The tagged source is the loosest. It sorts after the others, and its pools are only as good as the tags;
  the breakdown names the tag so a bad pool can be spotted and the tag ignored under `[themes] ignore`.
- Up to 50 ideas per source put more on the board; the share pill and the order keep the best first.
- `sync --no-ideas` skips the step, `--dry-run` never runs it, and a deck whose sources all fail keeps its
  board as it was.
- EDHREC is still never asked (ADR 0003); its rank only orders what an idea could replace.
