# 0004: Deck changes through the API archidekt.com's own pages use

**Status:** Accepted · **Date:** 2026-10-15

## Context

`apply` should make planned changes (cards to add and remove) on Archidekt, so the user doesn't have to
copy `suggest`'s ideas into the deck editor by hand. Archidekt publishes no API documentation and no API
terms. Reading public decks already works without a login (`sync`, step 2); changing a deck needs the
user's login.

Archidekt's terms of use (archidekt.com/terms, effective 2018-09-07) grant a licence for "personal,
noncommercial use", forbid reverse engineering "any part of the Site", and forbid using "software or
automated agents or scripts … to generate automated searches, requests, or queries to the Site". That is
the clause that ruled out EDHREC in ADR 0003. robots.txt disallows only `/login?`, `/partialCompare`,
`/playtester-v2/` and `/sandbox?`.

Archidekt's co-founder has written about the API on its forum twice. In 2019 (thread 40353): "our API is
open and public (as far as reading is concerned). If you want to reverse engineer how to use it by messing
with some filters and watching your chrome network tab, there's nothing preventing you from doing so",
with a request to link back to Archidekt when posting its data publicly, and a warning that the API would
be locked down if it got "constantly hammered". In 2022 (thread 2832338): "You're more than welcome to use
our API for whatever you want (just know that you might hit our rate limiter if you hit it too hard)",
and "we're always happy to help answer questions if you're doing something with our API". Neither post
mentions changes made with a user's own login.

The calls come from the JavaScript archidekt.com serves (read 2026-10-15), so no browser capture with the
user's session was needed:

- `POST /api/rest-auth/login/` with `username` or `email` and `password` returns a short-lived JWT
  (`token`), a `refresh_token` and the `user`.
- `POST /api/rest-auth/token/refresh/` with `refresh` returns a new JWT as `access` (or `token`).
- Every signed-in call sends `Authorization: JWT <token>`.
- `GET /api/cards/v2/?name=<exact name>&exact&edition=<set>` finds Archidekt's id for a printing; each
  result carries the Scryfall id as `uid`. A `uid` filter is ignored, so the name and set are needed.
- `POST /api/decks/<id>/snapshots/` with `name` and `description` saves a restore point of the deck.
- `PATCH /api/decks/<id>/modifyCards/v2/` with `cards` sends every change at once. Each entry has
  `action` (`add`, `modify` or `remove`), `cardid`, `categories`, a client-chosen `patchId`, the entry's
  `deckRelationId` for modify and remove, and `modifications` (`quantity`, `modifier`, `customCmc`,
  `companion`, `flippedDefault`, `label`). The deck JSON's `cards[].id` is the `deckRelationId`.

## Options

1. **Automated, cautious write-back.** `apply --save` signs in and makes the changes. It relies on the
   co-founder's statements rather than the written terms, so Archidekt could limit or suspend the account.
2. **Paste-in text.** `apply` prints the new deck list in Archidekt's import format and the user pastes
   it. No login and no terms question, but a manual step per deck.
3. **Ask Archidekt first.** Build option 2 now and add option 1 only if Archidekt agrees.

## Decision

Option 1, chosen by the user on 2026-10-15, kept small and careful:

- **Login.** `cmdr login` asks for the password once (or reads it with `--password-stdin`), swaps it for
  the refresh token and keeps only that, in Windows Credential Manager through `keyring`, under the
  service name `Commander (cmdr) Archidekt` and the lower-cased username. The password is never stored,
  logged or put in an error message. The login is refused unless Archidekt says it belongs to the username
  cmdr is set up for; `apply` also refuses decks that someone else owns. A refreshed token that Archidekt
  rotates replaces the stored one. `cmdr logout` deletes it.
- **Plans state the end result.** `add` means the deck ends with at least that many copies, `remove` that
  it ends with none or with `keep` copies. Lines the deck already meets are skipped, so applying a plan
  twice changes nothing the second time, and a failed run can simply be repeated.
- **Checked before anything is sent.** `apply` reads the deck from Archidekt (not the local copy) and
  checks every line against the card database: the card exists, is legal in Commander, and fits the
  commanders' colours. Any problem stops the run with exit code 2. An added card gets its cheapest EUR
  printing unless the plan names a set, and the category Archidekt's editor would give it (its first card
  type, lands first) unless the plan names one. Game changers and mass land denial that lift the deck
  above its Archidekt bracket are marked, as in `suggest`.
- **Preview by default.** Without `--save`, nothing is written and no login is needed (a saved login is
  used only to read a private deck). `--save` looks up every new card's Archidekt id first and stops
  before any write if one is missing; then saves a snapshot named `before cmdr apply <UTC time>`; then
  sends all changes in one `modifyCards` request; then reads the deck back and reports any change that
  did not land. A deck in the deck folder gets its local copy rewritten.
- **Few requests, never repeated.** A save costs one token refresh, one card lookup per new card, one
  snapshot, one change request and two deck reads. Writes go out once and are never retried; a timeout
  says the write may or may not have landed. 401 and 403 ask the user to sign in again, 400 shows the
  first 300 bytes of Archidekt's answer (its reason for rejecting the request), and 429 says to wait. If
  the read-back fails, the message says the changes were accepted. The User-Agent names the tool, as for
  every other request.
- **Repeats by another name.** A plan's names are checked for repeats as written, and the resolver also
  catches one card listed twice under its full and front-face names, or both added and removed.

## Consequences

- Each modify and remove sends back the entry's own label, modifier, companion and custom mana value, so a
  change of copies keeps them; new cards get the editor's default grey label (`,#656565`).
- A card that sits in several entries can be removed whole, but not trimmed with `keep`; that is left to
  the deck editor.
- Archidekt can change these calls without notice. Every answer is shape-checked, and the read-back after
  saving catches a change that is accepted but not applied. The snapshot is the way back.
- The terms risk stays with the user's account. If Archidekt asks tools to stop, or publishes API terms,
  this decision is revisited; option 2 needs no login and can replace `--save`.
- Tests run against a fake Archidekt that implements these calls as described above, with an in-memory
  keyring, so no test touches the real Credential Manager or network. The first save against the real
  site is made on a private throwaway deck.
