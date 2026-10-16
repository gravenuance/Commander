# 0012: Card labels come from the Archidekt account

**Status:** Accepted · **Date:** 2026-10-16 · Replaces the label colours and label choices of
[ADR 0010](0010-buying-cards-through-archidekt-labels.md) and [ADR 0011](0011-app-deck-grid-and-replacement-targets.md)

## Context

Archidekt keeps card labels per account, not per deck: every deck the owner has, or makes later, can use
the same set. The owner's account has six (2026-10-16): Considering change `#0de9fa`, Not quite there
`#fa0d96`, To be changed `#fa890d`, Don't Have `#fa0d11`, Getting `#0d9ffa` and Proxy `#f445f4`.

ADRs 0010 and 0011 offered only no label, Don't Have and Getting in Archidekt's default colours
(`#f47373`, `#2ccce4`), plus labels a deck already used. A label the owner had never put on a card in a
deck could not be chosen, and Don't Have and Getting were written in colours other than the account's.

The deck API does not list the account's labels. Archidekt's own site reads them with a signed-in
`GET /api/decks/colorTags/`, which returns `[{id, name, color}]` in the account's order. Read on the
owner's account on 2026-10-16; no write was made.

## Options

1. **Hard-code the owner's six labels.** Nothing to fetch, but labels added on the website never show.
2. **Read the account's labels at every deck view.** Always current, but it needs a login and a network
   call each time the app shows a deck.
3. **Read them with each signed-in write, and on `sync`, and keep a copy** for the app to show offline.

## Decision

Option 3.

- `ArchidektEditor.account_labels()` reads the list. Odd entries are skipped: a missing name, a colour
  that is not `#rrggbb`, or a repeated name.
- The copy lives in `labels.json` beside the deck folders (`{"version": 1, "labels": [{name, colour}]}`).
  An unreadable or unknown version reads as never read.
- `sync` refreshes it when a login is saved. Without one it reads nothing, and a failed read is reported
  without failing the sync. Each label change from the app or `toget` reads the list first, so a label
  just made on the website is on offer, and refreshes the copy.
- The label menu offers no label, then the account's labels in the account's order, then Don't Have and
  Getting if the account lacks them, and last any other label a deck already uses.
- Writes use the account's colour for a label's name. A save labels incoming cards Don't Have in that
  colour, with one more signed-in read per save. Archidekt's default colours remain the fallback.
- The grid shows a card's label in the account's colour when the account has a label of that name.
  Archidekt matches labels by name, so cards labelled earlier in another colour are left as they are.

## Consequences

- Every deck, existing or new, offers the same labels as the website.
- Until the first signed-in `sync` or label change, the app offers the fallback set.
- A save makes one more request.
- Labels deleted on the website stay on offer until the next refresh.
