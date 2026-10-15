# Commander

`cmdr` is a command-line tool that tracks your Commander decks on [Archidekt](https://archidekt.com). It keeps a
copy of each deck in this repository, so every change shows up in git history, and gives you a place for
research notes on each deck. It also analyses each deck (curve, types, colours, roles) and prices it in EUR
from Cardmarket prices via [Scryfall](https://scryfall.com).

Planned: bracket checks, upgrade suggestions, and applying approved changes back to Archidekt.

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
uv run cmdr analyze                                 # curve, lands and roles of every deck side by side
uv run cmdr analyze magda --details                 # one deck in full, by id or part of its name or commander
uv run cmdr price                                   # value of every deck in EUR
uv run cmdr price 10004639 --top 0                  # every card in one deck, most valuable first
```

`cmdr --help` and `cmdr <command> --help` show every option. Every command takes `--json` for scripts.

### Card data

`analyze` and `price` read a local card database built from Scryfall's daily bulk files. `sync` downloads
them (about 80 MB) the first time and again only when Scryfall publishes newer ones, which it does once a
day; `sync --no-cards` skips that step. If the download fails, the previous card data stays in use and
`sync` exits with 1. `analyze` and `price` warn when the card data is more than three days old.

- **Prices** are Cardmarket trend prices in EUR, as Scryfall reports them. Each card is priced at the
  printing and finish (foil or not) in your deck. When that printing has no price, the card's cheapest
  printing stands in and `price` marks it with `~`. "Cheapest printings" is what the deck would cost
  built from the cheapest printing of every card.
- **Roles** (ramp, draw, removal, board wipes, tutors, protection, recursion, counterspells) are counted
  two ways: from your Archidekt categories with those names, and from Scryfall's community-maintained
  [Tagger](https://tagger.scryfall.com) tags. `analyze <deck> --details` lists the cards where the two
  disagree. Tags count broadly: ramp includes mana lands such as Ancient Tomb, and removal includes
  narrow answers such as Pyroblast. Land tutors like Farseek are counted as ramp, not tutors.
- **Curve and types** use a card's front face, so a spell with a land on its back counts as a spell.
  A card counts once for each of its types: an artifact creature is both.

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
| Card data folder | n/a | `CMDR_CACHE_DIR` | n/a | `%LOCALAPPDATA%\Commander` |

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
| Scryfall (`api.scryfall.com/bulk-data`, files on `data.scryfall.io`) | Card facts, EUR prices, role tags | One API call per `sync` for the file list; the files themselves have no rate limit. Scryfall updates prices once a day and asks for bulk files rather than per-card calls for lookups like these. Tag data is community-maintained and can change. See [ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md). |
