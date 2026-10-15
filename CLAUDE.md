# Commander

`cmdr` tracks the user's Archidekt Commander decks (user `MAKKIS`) in `decks/` and supports research on upgrades,
EUR prices and brackets. The README covers usage; this file covers working on it.

## Commands (from the repo root)
- Full check, the same as CI would run: `uv run ruff check && uv run ruff format --check && pyright && uv run pytest`
- Pull decks and refresh card data: `uv run cmdr sync` (config is in `%APPDATA%\Commander\config.toml`, workspace `D:\Projects\Commander\decks`, card database `%LOCALAPPDATA%\Commander\cards.sqlite`)
- Research: `uv run cmdr analyze <deck> --details`, `uv run cmdr price <deck>`, `uv run cmdr bracket <deck> --details` and `uv run cmdr suggest <deck>`; set `PYTHONIOENCODING=utf-8` when piping output from Git Bash, because deck names contain emoji
- Change a deck: `uv run cmdr apply <plan.toml>` previews; `--save` writes to Archidekt and needs `uv run cmdr login` once (the user types the password; never ask for it or pass it on a command line)

## Conventions
- Each `decks/<id>-<slug>/` holds `deck.json` and `list.md`, which `sync` regenerates, and `notes.md`, which belongs to the user. Research findings and plans go in `notes.md`; never hand-edit the generated files.
- Commit deck syncs separately from code changes, with a subject like `Sync decks: <what changed>`, so the deck history stays readable.
- Archidekt's API is undocumented: every response from it, and from Scryfall and Commander Spellbook, goes through `jsonshape` validation, and each service sits behind its own module. EDHREC is never fetched: its terms forbid automated requests (ADR 0003); its popularity rank comes inside Scryfall's bulk data.
- Test fixtures are trimmed real responses (`tests/fixtures/`), and expected values are hand-derived from them. `scryfall_cards.jsonl` holds real bulk lines for the fixture deck's cards; `scryfall_tags.jsonl` is hand-built on real role-tag UUIDs. The bulk readers refuse files under 10,000 cards, so `conftest.py` pads the fixtures with generated filler. `spellbook_estimate.json` and `spellbook_combos.json` are trimmed real `estimate-bracket` and `find-my-combos` answers. The role taggings of the suggested cards (Arcane Signet, Chaos Warp and so on) are made up in `conftest.EXTRA_TAGGINGS`, and `test_scryfall` reads the real tag lines alone. Tests that run `bracket` or `suggest` copy the card database into their own cache folder (`own_cache`), because cached Spellbook answers would otherwise leak between tests.
- `apply --save` changes the user's real decks on Archidekt (ADR 0004). Try changes to it on a private throwaway deck first, never on a tracked deck. The login token lives in Windows Credential Manager (service `Commander (cmdr) Archidekt`); `conftest.memory_keyring` swaps in an in-memory keyring for every test, and `FakeArchidekt` implements the login, card lookup, snapshot and `modifyCards` calls.
- Card roles map to Tagger tag UUIDs in `scryfall.ROLE_TAGS`; a change there, or to the database tables, needs `carddb.SCHEMA_VERSION` bumped so old databases get rebuilt.
