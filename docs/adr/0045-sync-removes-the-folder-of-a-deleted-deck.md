# 0045: Sync removes the folder of a deck deleted on Archidekt

**Status:** Accepted · **Date:** 2026-10-21 · Amends the "a shorter list is not a deletion" rule of
[ADR 0040](0040-sync-private-and-unlisted-decks.md)

## Context

`sync` has only reported a deck on disk that the listing lacks (`missing`) and kept its folder. The owner
wants the folder gone when the deck was deleted on Archidekt. The folder holds the owner's own files
(`notes.md`, `profile.toml`, `board.json`, `activity.jsonl`), so a wrong deletion loses work that is not on
Archidekt. A deck can also be absent from a listing without being deleted: the anonymous fallback of ADR 0040
lists public decks only, a login can expire partway through, and the API is undocumented.

## Options

1. **Keep reporting only.** Safe; stale folders pile up and the app lists decks that no longer exist.
2. **Archive the folder.** Recoverable, but the owner chose deletion over an archive.
3. **Delete on absence from the listing.** Simple, and wrong whenever the listing is short.
4. **Delete only when Archidekt confirms it.** The listing lacks the deck and a read of the deck's own page
   answers 404.

## Decision

Option 4, chosen by the owner, with these rules (all in `sync._settle_missing`):

- The client must be signed in. An anonymous listing cannot show private decks, and the page of a private deck
  may answer 404 or 403 to an anonymous read, so an anonymous 404 proves nothing. This covers the fallback
  after a refused or expired login (the client becomes anonymous), a login that was never saved, and a
  listing that failed partway (it falls back, or raises; nothing is deleted either way).
- `ArchidektClient.deck_is_gone` is True only for HTTP 404. A 401 or 403 (`SignInRequiredError`), 429, 5xx
  (after the fetcher's retries), a network error or a page that reads all keep the folder; the result stays
  `missing` with the reason in `error`.
- The deck's `owner` must be the sync's user; otherwise it is only reported.
- `Workspace.remove_deck` deletes only the folder `folder_for` finds by its `<id>-<slug>` name, and refuses
  a symlink, a junction or a folder whose resolved parent is not the workspace root.
- `dry_run` removes nothing and returns `WOULD_REMOVE`.
- The removal runs under the deck's `DeckLocks` lock (`PullWatch.deck_lock`), so it waits for a write in
  flight; a write that starts after fails with the existing "deck is not in the workspace; press Sync".
- Each removal is logged at INFO with the deck id, name and folder. The activity log lives inside the deck
  folder, so there is no workspace-level entry. The result is `SyncStatus.REMOVED`; the CLI prints it like
  other statuses and the app summary adds ", N removed".
- `PullWatch.on_removed` lets the app drop its per-deck caches and forget the open deck. The page closes an
  open deck that leaves the deck list, with a notice, instead of asking for it.
- Other per-deck data outside the folder is left alone: `views.json` scores for the id are ignored (the
  ideas phase walks the decks on disk) and decay out, and cached meta answers are keyed by commander and
  expire by age.

## Consequences

- The deletion is permanent. `decks/` is a local git repo of its own, so the owner can recover a removed
  folder from its history if they committed it; the README says so.
- A deck made private while the sync runs signed in is still listed, so it is never removed. A deck that
  Archidekt answers 404 for though it exists would be lost; the API is undocumented, so this is the residual
  risk, bounded by the signed-in and owner rules.
- Without a login nothing is ever deleted, as before.
- Revisit if Archidekt documents deletion semantics or the owner wants a trash folder.

## Amendment (2026-10-21): rename, then delete, under the board lock

A bare `rmtree` stops half-way at a file another program holds open (an indexer, antivirus, an editor on
`notes.md`). With `deck.json` already gone, the folder is no deck to `load_all`, so no later sync looked at it
again and nothing said so. `remove_deck` also ignored the board's cross-process lock, so `cmdr sync` could
delete a deck while the app wrote its board.

- `Workspace.remove_deck` takes the deck's board lock (`board_lock`, the lock `BoardService` uses) and renames
  the folder to `.removing-<folder name>`, then deletes that. A rename is atomic and fails whole when a file
  inside is held, leaving the deck folder intact for the next sync. `deck_folders`, `load_all` and
  `folder_for` read only names that start with the deck id, so the set-aside folder is never loaded as a
  deck and no grace table is written into it.
- A held lock skips the removal for this sync: the result stays `missing`, with "its board is in use" in its
  error and a warning in the log.
- A delete that fails after the rename is `failed`, names the set-aside folder, and does not call
  `on_removed`; the owner's files are still on disk under that name. Every sync first retries the leftovers
  (`Workspace.leftover_removals`): it reports `removed` and calls `on_removed` when the delete now works, a
  `failed` result naming the folder when it does not, and `would remove` on a dry run.
- A set-aside folder of the same name that is still there blocks a second removal with a message to delete
  it; the retry runs before any new removal, so that only follows a retry that failed.

## Amendment (2026-10-21): a folder that cannot be deleted yet is a warning

The owner decided that a set-aside `.removing-<name>` folder that cannot be deleted must not fail the sync: a
held file clears by itself, and the owner's files are safe in the folder meanwhile. A delete that fails after the
rename (`RemovalPendingError`), at the removal itself or in the retry of every later sync, is logged at WARNING
and reported as `missing` with the folder named in `error`, which `cmdr sync` prints; the exit code stays zero
and the next sync retries. `failed` is kept for a folder the workspace refuses (outside the root, a link, an
earlier leftover of the same name) and for a rename that fails. The retry and the app's `on_removed` are as
before.
