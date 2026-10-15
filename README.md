# Commander

`cmdr` is a command-line tool that tracks your Commander decks on [Archidekt](https://archidekt.com). It keeps a
copy of each deck in this repository, so every change shows up in git history, and gives you a place for
research notes on each deck. It also analyses each deck (curve, types, colours, roles), prices it in EUR
from Cardmarket prices via [Scryfall](https://scryfall.com), estimates its bracket with
[Commander Spellbook](https://commanderspellbook.com), suggests cards to add and to cut, and makes the
changes you approve on Archidekt.

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
uv run cmdr bracket                                 # estimated bracket of every deck beside the one on Archidekt
uv run cmdr bracket gator --details                 # one deck: game changers, tutors, combos with links
uv run cmdr suggest                                 # combo pieces and short roles of every deck side by side
uv run cmdr suggest magda --max-price 5             # one deck: cards to add up to EUR 5, cards to consider cutting
uv run cmdr login                                   # one-time: sign in so apply can change decks
uv run cmdr apply plan.toml                         # preview a plan of cards to add and remove
uv run cmdr apply plan.toml --save                  # make those changes on Archidekt
```

`cmdr --help` and `cmdr <command> --help` show every option. Every command that reports on decks takes `--json` for scripts.

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

### Brackets

`bracket` sends each deck's card list to Commander Spellbook's bracket estimator and shows its answer beside
the bracket set on Archidekt, marking a deck with `!` when the Archidekt bracket is lower than the estimate.
Spellbook answers with a tag that fits a range of brackets:

| Tag | Brackets | Meaning, per Spellbook's [syntax guide](https://commanderspellbook.com/syntax-guide/) |
|---|---|---|
| Ruthless | 4+ | A relevant, fast two-card combo; infinite turns, mass land denial or control of opponents; or four or more game changers |
| Spicy | 3-4 | Could be Ruthless, but needs a third card, gives no relevant result, or stalls the game |
| Powerful | 3+ | A game changer, or a slow but relevant two-card combo |
| Oddball | 2-3 | Could be Powerful, but needs a third card or gives no relevant result |
| Core | 2+ | An extra-turn card without an extra-turn result, or a two-card combo too fast for bracket 1 |
| Exhibition | 1+ | None of the above |

The combo list comes from the same answer. A combo marked `2-card` needs only two cards from the deck,
which can mean three named cards when one is your commander; `2-card?` is Spellbook's "arguably two cards".
Game changers come from Scryfall's card data, and tutors, mass land denial and extra turns from the tags,
so those lists work offline.

Each answer is kept for 7 days in `%LOCALAPPDATA%\Commander\spellbook`, and asked again sooner when the deck's
cards change or with `--refresh`. If Spellbook cannot be reached, an older answer for the same cards is
shown with a warning; with none, the estimate shows as unavailable and `bracket` exits with 1.

### Suggestions

`suggest` gives each deck three lists. Prices are each card's cheapest printing in EUR.

- **Cards that complete a combo:** Commander Spellbook's combos that the deck is exactly one card short of,
  within its colours. A card that completes several combos comes first. Some combos also need a piece
  that Spellbook describes rather than names ("also needs: a sacrifice outlet"). Combos with a banned
  card are left out.
- **Roles below target:** for each role whose count of nonland cards falls short of its target, the most
  played cards with that role that fit the commander's colours and are legal, by
  [EDHREC](https://edhrec.com)'s popularity rank as Scryfall publishes it. The ideas are not specific to
  your commander. The default targets are ramp 10, draw 10, removal 8 and board wipes 2; change them in
  the config file:

  ```toml
  [targets]
  ramp = 12
  board_wipe = 3
  counterspell = 4   # any role analyze counts can have a target; 0 turns a default off
  ```

- **Cards to consider cutting:** nonland cards that fill none of the roles and are in no combo, least
  played first. These are only candidates: a theme card often fills no role on paper.

An idea marked `! bracket 3+ (game changer)` would lift the deck above the bracket set on Archidekt, by
the official rules (no game changers in brackets 1 and 2, at most three in bracket 3, no mass land denial
below bracket 4) or by the tag of the combo it completes. `--within-bracket` leaves these out, and
`--max-price 5` leaves out cards over EUR 5 and cards with no price. `-n` sets how many ideas each list
shows (default 5).

EDHREC's own pages are not used: its terms of use forbid automated requests. See
[ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md).

### Changing decks

`apply` makes the changes in a plan file to one deck on Archidekt. Write the plan as TOML:

```toml
deck = "magda"   # deck id, or part of its name as in other commands
add = ["Arcane Signet", {name = "Sol Ring", set = "c21", category = "Ramp"}]
remove = ["Wild Growth", {name = "Mountain", keep = 10}]
```

A plan says how the deck should end up. `add` means the deck ends with at least that many copies
(`quantity`, default 1); `remove` means it ends with none, or with `keep` copies. Lines the deck already
meets are skipped, so running a plan twice is safe. A new card gets its cheapest EUR printing unless
`set` names one, and goes under its card type (Land, Creature, Instant and so on) unless `category`
names another. Cards on the maybeboard are left alone.

Whether an added card counts in the deck follows the category's "in deck" setting on Archidekt,
which each deck sets for itself. A category the deck doesn't have yet is created by the save: Archidekt
creates Maybeboard outside the deck and every other name inside it, Sideboard included, so `apply`
warns before it creates a Sideboard; untick it on Archidekt to keep those cards out.

```sh
uv run cmdr login                      # once: asks for your Archidekt password
uv run cmdr apply plan.toml            # preview the changes; nothing is written
uv run cmdr apply plan.toml --save     # make them on Archidekt
```

The preview reads the deck as it is on Archidekt now and lists every change, the card count before and
after, and the lines it skips. It stops with a problem (exit code 2) when a card doesn't exist, isn't legal
in Commander or is outside the commander's colours; a card that would lift the deck above its bracket is
marked as in `suggest`. `--save` first saves a snapshot of the deck on Archidekt called
`before cmdr apply <date> <time> UTC`, then sends all changes at once, reads the deck back to check each
one, and rewrites the local copy if the deck is in your deck folder. To undo a save, restore that snapshot
on Archidekt. `apply -` reads the plan from stdin, and `--json` prints the result as JSON.

`cmdr login` keeps only Archidekt's login token in Windows Credential Manager (under
`Commander (cmdr) Archidekt`), never your password. When the token expires, `apply --save` asks you to run
`cmdr login` again; `cmdr logout` removes it. For scripts, `cmdr login --password-stdin` reads the password
from stdin.

Archidekt's written terms forbid automated requests, but its co-founder has said on the Archidekt forum
that the API is open to use. `apply` relies on that and keeps requests few. See
[ADR 0004](docs/adr/0004-deck-changes-through-archidekts-site-api.md).

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
| Card data and Spellbook answers folder | n/a | `CMDR_CACHE_DIR` | n/a | `%LOCALAPPDATA%\Commander` |
| Role targets for `suggest` | n/a | n/a | `[targets]` table | ramp 10, draw 10, removal 8, board_wipe 2 |
| Archidekt login for `apply --save` | `cmdr login` | n/a | n/a | none; the token is kept in Windows Credential Manager |

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
| Archidekt (`archidekt.com/api`) | Reading decks; changing them with `apply --save` | Public decks need no login; changes need `cmdr login`, whose token lives in Windows Credential Manager and is refreshed on each save. The API is undocumented, so every response's shape is checked and a clear message names what changed. A save makes a handful of requests, never retried. Its written terms forbid automated requests; its co-founder has said on its forum that the API is open to use. See [ADR 0004](docs/adr/0004-deck-changes-through-archidekts-site-api.md). |
| Scryfall (`api.scryfall.com/bulk-data`, files on `data.scryfall.io`) | Card facts, EUR prices, role tags | One API call per `sync` for the file list; the files themselves have no rate limit. Scryfall updates prices once a day and asks for bulk files rather than per-card calls for lookups like these. Tag data is community-maintained and can change. See [ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md). |
| Commander Spellbook (`backend.commanderspellbook.com`: `estimate-bracket`, `find-my-combos`) | Bracket estimates, combos, and combos one card short | One request per deck and command, at most four at a time, and none while a cached answer is under 7 days old. Spellbook asks tools to name themselves in the User-Agent, keep to a few calls per user action, stay under about 80 requests a minute, handle 429, and credit and link back to commanderspellbook.com, which `bracket` and `suggest` do in their output. Only card names and quantities are sent. See [ADR 0002](docs/adr/0002-bracket-estimates-from-commander-spellbook.md) and [ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md). |
| EDHREC (via Scryfall's `edhrec_rank`) | Popularity ranks for `suggest` | Never contacted: the rank comes inside Scryfall's bulk files. EDHREC's terms forbid automated requests to its site. |
