# Commander

`cmdr` tracks the user's Archidekt Commander decks (user `MAKKIS`) in `decks/` and supports research on upgrades,
EUR prices and brackets. The README covers usage; this file covers working on it.

## Commands (from the repo root)
- Full check, the same as CI would run: `uv run ruff check && uv run ruff format --check && pyright && uv run pytest`
- Pull decks and refresh card data: `uv run cmdr sync` (config is in `%APPDATA%\Commander\config.toml`, workspace `D:\Projects\Commander\decks`, card database `%LOCALAPPDATA%\Commander\cards.sqlite`)
- Research: `uv run cmdr analyze <deck> --details` and `uv run cmdr price <deck>`; set `PYTHONIOENCODING=utf-8` when piping output from Git Bash, because deck names contain emoji

## Conventions
- Each `decks/<id>-<slug>/` holds `deck.json` and `list.md`, which `sync` regenerates, and `notes.md`, which belongs to the user. Research findings and plans go in `notes.md`; never hand-edit the generated files.
- Commit deck syncs separately from code changes, with a subject like `Sync decks: <what changed>`, so the deck history stays readable.
- Archidekt and EDHREC are undocumented APIs: every response goes through `jsonshape` validation, and each service sits behind its own module.
- Test fixtures are trimmed real responses (`tests/fixtures/`), and expected values are hand-derived from them. `scryfall_cards.jsonl` holds real bulk lines for the fixture deck's cards; `scryfall_tags.jsonl` is hand-built on real role-tag UUIDs. The bulk readers refuse files under 10,000 cards, so `conftest.py` pads the fixtures with generated filler.
- Card roles map to Tagger tag UUIDs in `scryfall.ROLE_TAGS`; a change there, or to the database tables, needs `carddb.SCHEMA_VERSION` bumped so old databases get rebuilt.
