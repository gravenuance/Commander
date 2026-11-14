# 0069: An MCP server that runs the command line in-process

**Status:** Accepted · **Date:** 2026-11-14

## Context

Claude works on the decks through `cmdr` commands run in a shell: `analyze`, `board add`, `apply` and the
rest, with the output read as text. The owner wants those as tools of an MCP server, so a client can
pre-approve the reads, prompt on the writes and show a job's progress, without a shell for each call (the
user's decision of 2026-11-14). `cmdr` already prints JSON for most commands (`--json`) and takes every
outside dependency by injection (`cli.main`), which the tests rely on.

## Options

- **Call the modules directly from the tools.** No parsing, but every command's wiring (workspace lookup,
  stale-data warning, exit codes, the `cmdr: ...` messages) would be written twice and drift.
- **Run `cmdr` as a subprocess per call.** Reuses everything, but pays a Python start-up and a card-database
  open for each call, and cannot be tested with the injected fakes.
- **Call `cli.main` in-process** with captured stdout and stderr, `--json` where the command has it. One
  code path for the command line and the server; the tests use the same fakes as `test_cli*.py`.

## Decision

The third option, in `commander.mcp`, started by `cmdr-mcp` (official `mcp` SDK 2.3.0, stdio).

- **Tools.** One tool per command and use. Reads (`readOnlyHint`; the owner pre-approves them): `list_decks`,
  `analyze`, `price`, `card`, `tags`, `tokens`, `profile`, `board_show`, `board_log`, `toget_list`, `bracket`,
  `suggest` (never `--to-board`), `board_plan`, `board_commit_preview`, `apply_preview`, and `job_status`
  and `start_meta`. Local writes (the client asks): `board_add`, `board_research`, `board_decide`,
  `board_count`, `board_remove`, `suggest_to_board`, `meta_to_board`, `profile_init`, `start_sync`. Writes to
  the owner's real decks on Archidekt (`destructiveHint`): `board_commit_save`, `apply_save`, `toget_mark`.
  Left out, so no tool can read or write a secret: `login`, `logout`, `topdeck`, `init`, `app` and
  `toget --cardmarket`; stdin is never wired to a password or key, and the prompt callback refuses.
- **Answers.** The parsed JSON as `result` with the command's stderr lines as `warnings` (a stale card
  database, a source that failed), or the command's text for the few commands with no JSON form. A non-zero
  exit is a tool error carrying the `cmdr: ...` message, then whatever the command still printed (a save
  whose read-back check fails has already written; a plan with problems lists them on stdout), then the exit
  code.
- **One command at a time.** `cli.main` calls `logging.basicConfig(force=True)` on every run, so two commands
  at once would take each other's stderr handler, and one would drop the server's own. Commands take turns on
  a lock, and the root logger is put back as it was after each. The server's log file
  (`<cache>/logs/commander-mcp.log`, rotating, never stdout) hangs on the `commander.mcp` logger, which the
  root reset never reaches.
- **Jobs.** `sync` and `meta` take minutes, so `start_sync`, `start_meta` and `meta_to_board` return a job id
  at once; `job_status` gives the state, the progress lines (the command's stderr, made to look like a
  terminal so it prints them), the result and the warnings, and with `wait_seconds` holds the call and sends
  MCP progress notifications meanwhile. One job at a time, and while it runs every other tool answers
  "busy" at once instead of waiting minutes: a sync rewrites the deck folders (ADR 0032). Jobs live in
  memory and are forgotten when the server restarts.
- **Third-party text.** Deck, card and tag names and notes come from Archidekt, Scryfall and other players.
  Every description says so, and every string leaving the server loses control characters and bidirectional
  overrides (newline and tab stay). Arguments that `cmdr` could read as a flag (a leading `-`), or that
  hold control characters, are refused, and flag values are passed as `--name=value`.
- **Board changes are by `claude`.** The activity log and the app show who acted, and the server is Claude
  acting for the owner; `board_decide` says in its description that it records the owner's decision, so it
  runs only after the owner has made it.
- **Packaging.** A PyInstaller one-folder build, `CommanderMcp.spec`, for the owner's MCP hub, which starts
  servers from an `.exe` under `<Tools>/CommanderMcp/`.

## Consequences

- A new command needs a tool, or none: the list of tools is the allow-list, and `test_mcp_client` pins it
  with its hints.
- While a job runs the other tools refuse, so a long `sync` is waited out through `job_status`.
- `board_commit_save`, `apply_save` and `toget_mark` stay behind the client's prompt; their descriptions say
  to run them only on the owner's say-so, after the preview (ADR 0004).
