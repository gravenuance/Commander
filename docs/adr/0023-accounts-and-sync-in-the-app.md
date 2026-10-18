# 0023: Accounts and sync in the app

**Status:** Accepted · **Date:** 2026-10-17

## Context

The app read its Archidekt username from the config that `cmdr init` writes. Signing in, needed for saves
and labels, was `cmdr login` only, and the app had no way to fetch decks. Without a configured deck folder it
did not open at all. The owner wants other players to use the app, which means signing in, keeping each
account's decks apart and syncing, all from the window.

## Decision

The owner chose the following.

- **Sign-in screen.** An app without an account opens on *Sign in to Archidekt*, with username and
  password. The password goes to Archidekt's login only. The bridge call that carries it logs no arguments,
  even at DEBUG level, and the page clears the field after each try. The refresh token goes to Windows
  Credential Manager, as `cmdr login` stores it. The username becomes the config's `user`.
- **A deck folder per account, with nothing moved.** The config gains a `[workspaces]` table mapping
  casefolded usernames to deck folders. The account signed in so far keeps its folder there. A returning
  account gets its folder back, and a new one gets a sibling of the current folder named `decks-<username>`,
  or `Documents\Commander\decks-<username>` when no folder was set. Moving the owner's decks into
  `decks/<user>/` was considered and dropped: it changes what `--workspace` means and every path in the docs
  and tests. Config edits go through tomlkit, so comments, `[meta]` and `[targets]` stay.
- **Sync from the app.** `cmdr sync`'s steps (decks, card data, labels, Sideboard and Maybeboard cards,
  ideas) move into `syncrun.run_sync`. It returns its warnings rather than printing them, and the CLI and the
  app both call it. The app runs one sync at a time on its own thread, with progress under the account at
  the foot of the deck list and a one-line summary when it ends. A first sign-in, with an empty folder,
  syncs at once. Signing in or out is refused with a message while a sync runs (ADR 0032).
- **Sign out** removes the saved login and the config's `user`, keeps every remembered folder, and reopens
  the sign-in screen.
- **No deck folder** no longer stops the app: it opens with an empty list until someone signs in.

## Consequences

- The CLI is unchanged: `--workspace`, `CMDR_WORKSPACE` and the config's `workspace` still name the deck
  folder. After someone else signs in through the app, the CLI follows the config to their folder until the
  owner signs in again.
- Another account's first sync downloads the card data if this machine has none, which takes minutes. Its
  progress shows under the account.
- Saving, labels and meta refreshes keep needing a login, now available from the window.
