# 0010: Buying cards tracked through Archidekt card labels

**Status:** Accepted · **Date:** 2026-10-16

## Context

A swap saved to a deck ([ADR 0004](0004-deck-changes-through-archidekts-site-api.md)) often brings in a
card the owner doesn't have yet. The owner wants such cards marked "Don't Have", to mark them ordered,
and to see them arrive, from the app. Archidekt gives every deck entry one label, sent as
`"name,#colour"` in the same `modifyCards` request that saves swaps; `",#656565"` means none. Across the
eleven decks (2026-10-16) labels were rare: Proxy (16 cards), Getting (8), two others once each; no card
used "Don't Have".

Tried on the test deck 27461808 (2026-10-16): a label-only `modifyCards` is accepted, and unlike a
deck-tag edit it moves the deck's `updatedAt`, so `sync` notices labels changed on the website.

The owner chose (2026-10-16): the steps are Don't Have → Getting → no label; every card coming into a
deck gets Don't Have; each mark is written at once, per click.

## Options

1. **Track purchases locally** (a file per deck). No writes, but the website and other devices never
   see it, and labels set on Archidekt would be ignored.
2. **Archidekt labels, queued and saved together** like board decisions. One more step for a one-field
   change that changes nothing about the deck.
3. **Archidekt labels, written per mark.**

## Decision

Option 3, in `commander.labels`, used by the app (`Desk.mark`) and `cmdr toget`.

- **Steps:** a card saved into a deck gets "Don't Have" (`#f47373`); `ordered` sets "Getting"
  (`#2ccce4`, the colour already in use); `arrived` clears the label. The colours come from the palette
  Archidekt offers.
- **Saves:** `apply` and board saves send added cards with Don't Have. A card moved in from the
  Maybeboard gets it too, unless it already carries a label (Proxy, Getting), which is kept.
- **Marks:** each mark reads the account's labels (ADR 0012) and the deck from Archidekt, changes the label
  of the card's deck entries only, reads the deck back, rewrites the local copy and logs the mark in the deck's activity. No snapshot is
  taken first: a label never changes which cards are in the deck, and the activity log says what to set
  back. A mark that would change nothing is refused before any write.
- **Snapshots** keep each card's label name (snapshot version 3). Older snapshots read labels as unknown,
  so the next `sync` fetches each deck once.

## Consequences

- What to buy shows on Archidekt as well as in the app and `cmdr toget`, and labels set on the website
  come back with the next `sync`.
- Every card saved into a deck is labelled, owned or not; an owned one needs one `arrived` mark. The
  owner chose this over asking per card.
- Each mark is three reads (the account's labels, the deck, the deck again) and one write; marking many cards at once is slow but rare.
- Only Don't Have and Getting drive the workflow; other labels pass through untouched.
