# Commander

`cmdr` is a command-line tool that tracks your Commander decks on [Archidekt](https://archidekt.com). It keeps a
copy of each deck in this repository, so every change shows up in git history, and gives you a place for
research notes on each deck.

Planned: deck analysis, EUR pricing from Cardmarket via Scryfall, bracket checks, upgrade suggestions, and
applying approved changes back to Archidekt.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- Windows (other platforms should work but are untested)

## Build

```sh
uv sync
```

This creates `.venv` with the exact versions pinned in `uv.lock`.

## Run

```sh
uv run cmdr init --user MAKKIS --workspace decks   # one-time: save your username and the deck folder
uv run cmdr sync                                    # pull every public deck and report what changed
uv run cmdr list                                    # show the decks saved locally
```

`cmdr --help` and `cmdr <command> --help` show every option.

### Deck folders

`sync` writes one folder per deck, `decks/<archidekt-id>-<name>/`, containing:

| File | Contents | Written by |
|---|---|---|
| `deck.json` | Normalised deck snapshot (cards, quantities, categories, Scryfall IDs) | `sync`, every time |
| `list.md` | Readable deck list grouped by category | `sync`, every time |
| `notes.md` | Your research notes and plans | `sync` creates it once and never overwrites it |

When a deck is renamed on Archidekt, `sync` renames its folder to match. When a deck disappears from
Archidekt (deleted, or made private), `sync` reports it and keeps the local copy.

### Settings

Settings resolve in this order: command-line flags, then environment variables, then the config file, then
defaults.

| Setting | Flag | Variable | Config key | Default |
|---|---|---|---|---|
| Archidekt username | `--user` | `CMDR_USER` | `user` | none (required) |
| Deck folder | `--workspace` | `CMDR_WORKSPACE` | `workspace` | none (required) |
| Config file | `--config` | `CMDR_CONFIG` | n/a | `%APPDATA%\Commander\config.toml` |

## Test

```sh
uv run ruff check
uv run ruff format --check
pyright
uv run pytest
```

Tests use recorded fixture data and never touch the network.

## External services

| Service | Used for | Notes |
|---|---|---|
| Archidekt (`archidekt.com/api`) | Reading decks | Public decks need no login. The API is undocumented, so `sync` checks every response's shape and fails with a clear message if Archidekt changes it. |
