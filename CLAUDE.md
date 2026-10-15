# Commander

`cmdr` tracks the user's Archidekt Commander decks (user `MAKKIS`) in `decks/` and supports research on upgrades,
EUR prices and brackets. The README covers usage; this file covers working on it.

## Commands (from the repo root)
- Full check, the same as CI would run: `uv run ruff check && uv run ruff format --check && pyright && uv run pytest`
- Pull decks: `uv run cmdr sync` (config is in `%APPDATA%\Commander\config.toml`, workspace `D:\Projects\Commander\decks`)

## Conventions
- Each `decks/<id>-<slug>/` holds `deck.json` and `list.md`, which `sync` regenerates, and `notes.md`, which belongs to the user. Research findings and plans go in `notes.md`; never hand-edit the generated files.
- Commit deck syncs separately from code changes, with a subject like `Sync decks: <what changed>`, so the deck history stays readable.
- Archidekt and EDHREC are undocumented APIs: every response goes through `jsonshape` validation, and each service sits behind its own module.
- Test fixtures are trimmed real responses (`tests/fixtures/`), and expected values are hand-derived from them.
