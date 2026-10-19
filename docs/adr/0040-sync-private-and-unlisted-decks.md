# 0040: Sync private and unlisted decks

**Status:** Accepted · **Date:** 2026-10-19 · Extends [ADR 0004](0004-deck-changes-through-archidekts-site-api.md)
(the login and the `Authorization` header) and [ADR 0023](0023-accounts-and-sync-in-the-app.md) (sync)

## Context

`sync` listed `GET /api/decks/v3/?ownerUsername=<user>&pageSize=50` and read each deck, both without a login, so
it saw only public decks. The owner has a private deck (the test deck 27461808) and an unlisted one
(27586769) that it never pulled.

A read-only probe on 2026-10-19 showed:

- Without a login the listing for `MAKKIS` held the 11 public decks. With `Authorization: JWT <access token>`,
  the header `ArchidektEditor` already sends, it held 13: the private and the unlisted deck were added. No
  other parameter was needed.
- Every listing entry and the single-deck read `GET /api/decks/{id}/` carry the booleans `private`, `unlisted`
  and `theorycrafted`.
- Reading a private deck needs the header. An unlisted deck reads without it, but the anonymous listing leaves
  it out.

The API is undocumented. The same informal permission as for the rest of it applies (ADR 0004).

## Options

1. **Keep syncing public decks only.** Nothing to build; the private and unlisted decks stay outside the local
   history, research and ideas.
2. **Sign in for every sync and fail without a login.** Simple, but an expired login would stop the sync of
   the public decks too, and the CLI worked without a login until now.
3. **Sign in when a login is saved and fall back without it.** Private and unlisted decks sync when they can,
   and everything else carries on as before, with a warning.

## Decision

Option 3, chosen by the owner.

- **Auth.** `run_sync` takes an access token for the saved login before the decks (`ArchidektAuth`, the
  refresh-token swap of ADR 0004) and hands it to `ArchidektClient(token=...)`. The client sends
  `Authorization: JWT <token>` with every page of the listing and every deck read. The same token then serves
  the card-label read of ADR 0012, so a sync swaps the refresh token once. Without a token the client's
  requests are the ones it always sent. The token is never logged, stored or put in a message; the status
  error that Archidekt's refusal raises names the URL and the status only. The header goes to the listing's
  first URL and to `next` links, which `parse_deck_page` already rejects when they leave `archidekt.com/api`,
  and the HTTP client does not follow redirects, so the token cannot reach another host. The fetcher keeps no
  response cache, and the one cache that reads Archidekt decks (the peer-deck cache of ADR 0027) belongs to
  `ArchidektSearch`, which stays anonymous, so a signed-in answer cannot be served to an anonymous read.
- **The test deck is synced like any other deck.** There is no skip list.
- **Flags.** The listing entry and the deck read are parsed with `opt_bool` through `jsonshape` into
  `DeckSummary` and `Deck` (`private`, `unlisted`, false when absent). The snapshot (`deck.json`) keeps them as
  two optional keys, so the snapshot version stays 4: a snapshot without them reads as a public deck, which
  every earlier one was. A deck whose flags differ from the listing's is fetched again even when its edit
  time is unchanged. The app and the CLI show no labels for them.
- **Fallback.** Sync lists and reads anonymously, with a warning, when
  - the login cannot be used (`AuthError`, `CredentialError`, or a network or shape error on the token
    refresh): "Archidekt login not usable (<reason>): private and unlisted decks were not updated";
  - the signed-in listing answers 401 or 403: "Archidekt refused the login: private and unlisted decks were
    not updated" (the client raises `LoginRefusedError`, and `sync_decks` lists again anonymously);
  - there is no login at all, but a deck on disk has one of the flags (an earlier signed-in sync put it
    there): "Not signed in to Archidekt: private and unlisted decks were not updated". With no login and no
    such deck there is no warning.
- **A shorter list is not a deletion.** `sync` has always only reported a deck missing from the listing as
  `missing` and kept its folder; it never removes or edits one. An anonymous listing cannot show private or
  unlisted decks, so those on disk are now left out of the missing report whenever the listing was
  anonymous. A signed-in listing that lacks one still reports it missing, as for a deleted public deck.

The app's Sync calls the same `run_sync`, so it behaves the same, and shows the warning in its sync summary.

## Consequences

- Private deck data sits only in the local `decks/` repository, which is never pushed. Its boards and
  ideas follow the same paths as any deck's. Bracket 5 decks send their card names and quantities to Commander
  Spellbook for their own combos (ADR 0002), and every deck's commander name goes into searches of Archidekt
  and EDHTop16, so a private deck's commander and list are no longer private to Archidekt. This is the
  owner's call, as for the public decks.
- The API is undocumented. Archidekt can change the listing's visibility rules or the flags without notice;
  the answers are shape-checked, a wrong type in a flag fails that deck's read with a clear message, and a
  listing that stops including hidden decks shows up as the fallback's warning or, with a login, as
  `missing`, never as a deletion.
- An expired or revoked login shrinks the listing without that being a deletion; the warning says so and
  `cmdr login` fixes it. A deck made private on Archidekt while sync runs signed out is reported `missing`,
  because nothing on disk says it was ever hidden; the next signed-in sync finds it again.
- A signed-in sync makes the same requests as before, plus one more listing in the fallback: the token swap
  moved before the decks instead of after them.
- A dry-run sync also signs in, to list the private decks, so it refreshes, and may rotate, the saved Archidekt
  login token like a real sync does.
- Revisit if Archidekt documents the API, or if the owner wants hidden decks left out of the ideas and meta
  steps.
