# 0006: Deck boards in the repo, a pywebview desktop app over them

**Status:** Accepted · **Date:** 2026-10-15

## Context

Research on a card for a deck (why it fits, which card it should replace) and the user's decision on it
lived only in chat. The user asked for a place per deck where cards can sit as "considered" or
"suggested", where they decide to add or not, where Claude and the program can put cards on demand, and
from which decided changes can be saved to Archidekt. Claude must be able to catch up on what the user
did there. The user first accepted a local web page, then asked for something closer to an executable and
said no Node.js.

The user also keeps cards they are weighing on a deck's Sideboard or Maybeboard on archidekt.com, and
asked that a card swapped out be kept on the Maybeboard, so a swap can be tested both ways.

## Options

For the data:

- **Files in each deck folder, in git:** `board.json` for the current state and an append-only
  `activity.jsonl`. Offline, diffable, shared by the CLI, the app and Claude through the same code.
- **A database in `%LOCALAPPDATA%`:** transactional, but invisible to git and to Claude without tooling.
- **A claude.ai artifact with a shared store:** reachable from any device, but the data leaves the repo
  and `cmdr` can't read it.

For the interface:

- **Local web server (`cmdr serve`) in the browser:** no dependencies, but a localhost port needs a
  per-run token and Host checks against cross-site requests and DNS rebinding.
- **pywebview window:** the same HTML/CSS/JS in a native window drawn by WebView2 (part of Windows 11),
  calling Python directly; no port. Adds pywebview and pythonnet; packs into one exe with PyInstaller.
- **Qt (PySide6):** fully native and accessible, but more UI code and a 100-150 MB exe.
- **A separate C# app over `cmdr --json`:** the most native, but a second language and codebase.

## Decision

Boards are files in each deck folder. `BoardService` makes every change: it checks the card (exists, legal,
in the commander's colours, not already in the deck; a cut must be in the deck), takes a lock file
(`.board.lock`, created exclusively, stale after 60 s) around the read-modify-write, writes `board.json`
atomically and appends one line to `activity.jsonl` naming who acted (`you`, `claude`, `cmdr`,
`archidekt`). `cmdr board log --since` is how Claude catches up. A board save builds a plan and goes
through `apply.apply_plan`, the same path as `cmdr apply`, so snapshots, checks and read-back verification
are shared; cards are marked applied only when the read-back shows them.

The app is a pywebview window. Its page is `commander/web` (HTML, CSS, vanilla JavaScript, no build step)
inlined into one document, so no server runs, and it calls `desk.Desk`, whose methods answer JSON-ready
dicts and turn expected failures into `{"error": ...}`. The page builds every node with `textContent`,
because board text comes from people and Claude, and a Content-Security-Policy allows only inline code and
images from `cards.scryfall.io`. It polls cheap file stamps (modification times of the board, log,
snapshot and profile) every 2 s to follow changes from the CLI or Claude. PyInstaller packs it as
`Commander.exe`; pywebview 6.2.1 and PyInstaller 6.22.3 (build group) are pinned.

Sideboard and Maybeboard (cards outside the deck under a category of that name) mean "considering":
`sync` and every board save put them on the board as considered. Adding such a card moves its own entry
into the deck. A board cut moves the entry to the Maybeboard, with Maybeboard first and the card's own
categories kept behind it, so moving it back restores them; a card applied earlier and now back on the
Maybeboard is reopened on the board. Turning down a Sideboard or Maybeboard card drops it there with the
next save. Moves are `modifyCards` edits that send new categories; plans gained `maybeboard = true` on a
remove line and a `drop` list, but hand-written plans still delete unless they ask.

Live test, 2026-10-15, private test deck 27461808: one save moved Lightning Bolt to the Maybeboard (as
`Maybeboard, Burn`), moved Aggravated Assault in from the Maybeboard (filed as Enchantment) and dropped
Ruination; the read-back matched. The reverse swap moved Lightning Bolt back in under Burn.

## Consequences

- Every board change is in git history and readable without the app; the activity log grows without
  bound but is small (one line per action).
- The app needs WebView2 (present on Windows 11) and loads .NET through pythonnet, which takes a moment
  at start; the CLI doesn't import pywebview. The exe is about 17 MB and unsigned, so SmartScreen warns.
- Two writers on one board wait on the lock for up to 10 s; a crash leaves a lock that clears after 60 s.
- Polling every 2 s is a few `stat` calls per deck; a file watcher would cut it to zero if it ever matters.
- The Maybeboard grows with every swap until cards are turned down; turning down is the cleanup.
- Revisit the polling, and whether the page should move to a framework, if the app grows past the two
  lists, the review dialog and the activity column.
