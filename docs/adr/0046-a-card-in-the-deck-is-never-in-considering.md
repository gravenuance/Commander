# 0046: A card in the deck is never in Considering

**Status:** Accepted · **Date:** 2026-10-21 · Extends [ADR 0028](0028-ideas-from-four-sources-tested-by-the-weighted-share.md)
and [ADR 0042](0042-writes-wait-only-while-the-sync-writes-the-deck.md); keeps the mark-applied path of
commit 14cac59 (Review)

## Context

An open card can be on the board while the deck's main list already holds it: the owner added it on Archidekt
after it was proposed, a save put it in under another route, or a Maybeboard card was moved into the deck.
Ideas are compiled only from cards the deck lacks (ADR 0028), and `propose` refuses a card the deck holds, so
this arises only when the deck changes after the card reached the board. Considering then listed a card the
deck runs, with a cut to make room for a copy that is already in. The owner decided (2026-10-21): a card in
the main deck is never in Considering.

Two states must be told apart. An *open* card is an idea; it has no business there once the deck holds it. A
*queued* card (status add) is a decision the owner made; when the deck on Archidekt already holds it, Review
cannot send anything. Commit 14cac59 handles that: the review dialog says why nothing is sent and offers
**Mark N card(s) applied** (`mark_applied` without a snapshot, activity "found in the deck on Archidekt").
Only the main deck counts as holding: the Sideboard and Maybeboard are outside it and stay suggestable
(ADR 0044).

## Options

For an open card the deck holds:

1. **Turn it down** ("already in the deck"). Hides it, but a turned-down card returns only when its share
   rises 20 points (ADR 0026), so a card cut from the deck later would not come back as an idea.
2. **Remove it from the board.** Leaves no trace of why it vanished.
3. **Close it as applied,** with the note "already in the deck". `feed_ideas` already brings an applied card
   back at once when the deck no longer holds it ("back: no longer in the deck"), and the activity log
   records the closing.

For where to enforce it:

1. **At sync only.** Simple, but between a save or a pull and the next sync (minutes to an hour, ADR 0042)
   Considering still shows the card; `sync --no-ideas` never closes it.
2. **In the app only.** Always right on screen, but the board keeps stale open cards and the CLI
   (`board show`) lists them.
3. **Both.** The sync closes them under the deck lock; the app filters on the deck it holds now.

## Decision

Option 3 for the closing status and option 3 for where.

- **Sync:** in the ideas-phase merge, under the deck's lock and before the re-judge (so a held card is
  closed, not turned down for its share), `BoardService.close_held` closes every open card whose
  `card_key` the deck's main list holds: status applied, note "already in the deck", one activity line per
  card ("closed", by cmdr). It does so whatever the card's source (you, Claude or cmdr) and also for a card the
  owner reopened. `SyncOutcome.closed` lists them per deck, and the CLI prints
  `<deck> board: closed <cards> (already in the deck)` beside the existing `turned down` line.
- **App:** `Desk.deck` marks each board card `held` from the deck as stored now (`board.held_keys`), and the
  page's Considering and *No cut yet* lists skip an open card with `held`. A save writes the deck back
  locally, so a card a save added is held at the next view, before any sync. The deck rows' open count
  skips held cards too.
- **Queued cards are untouched.** A queued card the deck holds stays in Considering and keeps Review's
  mark-applied path (14cac59); sync does not close it, because the owner decided it and the preview, which
  reads the live deck, is the word on what a save would do.
- **Not counted as held:** the Sideboard and Maybeboard (outside the deck, ADR 0044), and every card a deck
  may run more than one of: basic lands and cards whose rules text allows more copies (`copy_limit`, as
  Relentless Rats and Seven Dwarves). The owner adjusts their counts from the mana tab and the decklist, so
  such a card on the board stays open (owner's call, 2026-10-21).

## Consequences

- Considering and the board agree with the deck after the next sync, and Considering agrees with it
  immediately in the app. The CLI board listing still shows an open held card until a sync runs.
- A card closed this way that leaves the deck comes back at the next sync if it is still an idea
  (`feed_ideas`, "back: no longer in the deck"); the owner's own proposal comes back only if the lists still
  play it.
- A pull that moves a card into the deck mid-sync is closed by that same sync when the deck's merge comes
  after its pull write; otherwise by the next one.
- `held` costs one pass over the deck's cards and one card-data lookup of their rules texts per view; the
  closing reads the board once and writes once per closed card. Before the first sync there is no card
  data, and the deck list counts every open card.
- Revisit if the owner wants a queued held card marked applied by the sync instead of by Review.
