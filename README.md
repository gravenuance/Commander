# Commander

`cmdr` is a command-line tool that tracks your Commander decks on [Archidekt](https://archidekt.com). It keeps a
copy of each deck in this repository, so every change shows up in git history, and gives you a place for
research notes on each deck. It also analyses each deck (curve, types, colours, roles), prices it in EUR
from Cardmarket prices via [Scryfall](https://scryfall.com), estimates its bracket with
[Commander Spellbook](https://commanderspellbook.com), suggests cards to add and to cut, and makes the
changes you approve on Archidekt.

## Download

The desktop app is a single `Commander.exe` on the
[Releases](https://github.com/gravenuance/Commander/releases) page, built by GitHub Actions from the release's
tag. It is not code-signed, so Windows SmartScreen warns on the first launch ("More info", then "Run
anyway"). Check the download against `SHA256SUMS.txt` from the same release
(`Get-FileHash Commander.exe`), or verify its build provenance with
`gh attestation verify Commander.exe --repo gravenuance/Commander`.

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
uv run cmdr init --user <archidekt-user> --workspace decks   # one-time: save your username and the deck folder
uv run cmdr sync                                    # pull every public deck, report what changed, compile ideas
uv run cmdr sync --no-ideas                         # the same, without compiling ideas from other players
uv run cmdr list                                    # show the decks saved locally
uv run cmdr analyze                                 # curve, lands and roles of every deck side by side
uv run cmdr analyze magda --details                 # one deck in full, by id or part of its name or commander
uv run cmdr price                                   # value of every deck in EUR
uv run cmdr price 10004639 --top 0                  # every card in one deck, most valuable first
uv run cmdr bracket                                 # estimated bracket of every deck beside the one on Archidekt
uv run cmdr bracket gator --details                 # one deck: game changers, tutors, combos with links
uv run cmdr suggest                                 # combo pieces and short roles of every deck side by side
uv run cmdr suggest magda --max-price 5             # one deck: cards to add up to EUR 5, cards to consider cutting
uv run cmdr meta                                    # every bracket 5 deck against the cEDH meta, side by side
uv run cmdr meta magda                              # one deck: staples it misses, meta threats, meta combos
uv run cmdr meta ellivere --card "Sylvan Safekeeper" # how performing lists use one card
uv run cmdr card "Sylvan Safekeeper"                # a card's rules text, price, roles and Tagger tags
uv run cmdr tags hatebear                           # find Tagger tags by name, for deck profiles
uv run cmdr profile ellivere --init                 # start a deck profile: themes, exclusions, targets
uv run cmdr suggest pantlaza --to-board             # put suggest's ideas on the deck's board (Considering)
uv run cmdr board show ellivere                     # cards under consideration for one deck
uv run cmdr board log --since 2026-10-15T18:00      # what changed on the boards since then
uv run cmdr app                                     # the desktop app over every deck's board
uv run cmdr login                                   # one-time: sign in so apply can change decks
uv run cmdr apply plan.toml                         # preview a plan of cards to add and remove
uv run cmdr apply plan.toml --save                  # make those changes on Archidekt
```

`cmdr --help` and `cmdr <command> --help` show every option. Every command that reports on decks takes `--json` for scripts.
A few options worth knowing: `sync --dry-run` shows what would change without writing, `sync --force` refetches
every deck and rebuilds the card data, `cmdr -v` (or `-vv`) prints more detail on stderr, `--config <file>` uses
another config file, `--workspace <folder>` another deck folder, `cmdr init --force` replaces an existing config,
and `cmdr login --as <name or email>` signs in with a name other than the username.

### Card data

`analyze` and `price` read a local card database built from Scryfall's daily bulk files. `sync` downloads
them (about 80 MB) the first time and again only when Scryfall publishes newer ones, which it does once a
day; `sync --no-cards` skips that step. If the download fails, the previous card data stays in use and
`sync` exits with 1. `analyze` and `price` warn when the card data is more than three days old.

- **Prices** are Cardmarket trend prices in EUR, as Scryfall reports them. Each card is priced at the
  printing and finish (foil or not) in your deck. When that printing has no price, the card's cheapest
  printing stands in and `price` marks it with `~`. "Cheapest printings" is what the deck would cost
  built from the cheapest printing of every card. Here "printing" means one sanctioned play accepts:
  gold- or silver-bordered, oversized and memorabilia printings (such as the World Championship decks)
  are left out unless a card has no other price.
- **Roles** (ramp, draw, removal, board wipes, tutors, protection, recursion, counterspells) are counted
  two ways: from your Archidekt categories with those names, and from Scryfall's community-maintained
  [Tagger](https://tagger.scryfall.com) tags. `analyze <deck> --details` lists the cards where the two
  disagree. Tags count broadly: ramp includes mana lands such as Ancient Tomb, and removal includes
  narrow answers such as Pyroblast. Land tutors like Farseek are counted as ramp, not tutors.
- **Curve and types** use a card's front face, so a spell with a land on its back counts as a spell.
  A card counts once for each of its types: an artifact creature is both.

### Brackets

`bracket` sends each deck's card list to Commander Spellbook's bracket estimator and shows its answer beside
the bracket set on Archidekt and the profile's `bracket_ceiling`. A deck is marked `!` when they disagree.
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

`bracket <deck>` says what disagrees and why ([ADR 0021](docs/adr/0021-bracket-discrepancy-causes.md)):

```text
estimate: bracket 4+ (Ruthless), Commander Spellbook 2026-10-16
  ! the estimate (4+) is above the Archidekt bracket (3)
  because of:
    Ruthless   a fast two-card combo that wins: Kiki-Jiki, Mirror Breaker + Village Bell-Ringer
```

The causes follow Spellbook's own rules, so they are exactly what lifted its tag, checked against the lower of
the Archidekt bracket and the ceiling. Tutors are never a cause, since Spellbook doesn't count them. A ceiling
that differs from the Archidekt bracket, or a deck set above everything its cards fit (bracket 5 with a Spicy
3-4 estimate), gets a note without cards. If Spellbook's tag ever differs from what its rules give, the note
says the causes may be incomplete.

Each answer is kept for 7 days in `%LOCALAPPDATA%\Commander\spellbook`, and asked again sooner when the deck's
cards change or with `--refresh`. If Spellbook cannot be reached, an older answer for the same cards is
shown with a warning; with none, the estimate shows as unavailable and `bracket` exits with 1.

### Suggestions

`suggest` gives each deck four lists. Prices are each card's cheapest printing in EUR.

- **Cards that complete a combo:** Commander Spellbook's combos that the deck is exactly one card short of,
  within its colours. A card that completes several combos comes first. Some combos also need a piece
  that Spellbook describes rather than names ("also needs: a sacrifice outlet"). Combos with a banned
  card are left out.
- **Roles below target:** for each role whose count of nonland cards falls short of its target, the most
  played cards with that role that fit the commander's colours and are legal, by
  [EDHREC](https://edhrec.com)'s popularity rank as Scryfall publishes it. A card counts for a role when
  Scryfall's tags give it the role or you filed it under a category of that name on Archidekt. The ideas
  are not specific to your commander unless the deck has themes (see Deck themes). The default
  targets are ramp 10, draw 10, removal 8 and board wipes 2; change them for every deck in the config
  file, or for one deck in its profile:

  ```toml
  [targets]
  ramp = 12
  board_wipe = 3
  counterspell = 4   # any role analyze counts can have a target; 0 turns a default off
  ```

- **Cards that fit the deck's themes:** cards the deck lacks that match its themes, those fitting the
  most themes first, then the most played. Each says which themes it fits: `Grand Abolisher (fits
  profile, Hatebears, Stax)`, where profile means the profile's own themes. A theme counts once: profile
  words a deck tag already names go under the deck tag, and deck tags meaning the same share a label
  (`Power Matters/Stompy`).
- **Cards to consider cutting:** nonland cards that fill none of the roles and are in no combo, least
  played first, with cards matching the deck's themes last. These are only candidates: a theme card often
  fills no role on paper.

An idea marked `! bracket 3+ (game changer)` would lift the deck above the bracket set on Archidekt, by
the official rules (no game changers in brackets 1 and 2, at most three in bracket 3, no mass land denial
below bracket 4) or by the tag of the combo it completes. `--within-bracket` leaves these out, and
`--max-price 5` leaves out cards over EUR 5 and cards with no price. `-n` sets how many ideas each list
shows (default 5).

Below bracket 5 ideas also keep to the deck's own budget
([ADR 0025](docs/adr/0025-deck-budget-below-bracket-5.md)): up to 25% over the deck's average card price is
within budget, and nothing more than 25% above its dearest card is offered. Ideas within budget come first; the
rest are marked "(over budget)", and in the app's Considering list "Over budget". `--any-price` ignores the
deck's budget.

EDHREC's own pages are not used: its terms of use forbid automated requests. See
[ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md).

`cmdr card <name>` prints a card's rules text, type, power and toughness, price, EDHREC rank, roles and
[Scryfall Tagger](https://tagger.scryfall.com) tags, from the local card data. Name several cards to see
them together.

### cEDH meta

`meta` compares a bracket 5 deck with what performs in cEDH: its commander's tournament lists on
[EDHTop16](https://edhtop16.com) from the last two months (top cuts first), and the most-viewed Archidekt
decks for that commander updated in the same window (see *Which Archidekt decks are read* below). It lists:

- **Staples you don't play:** cards in at least 40% of the lists read, or in half the Archidekt decks, that
  the deck lacks. Cards on your Sideboard or Maybeboard are marked.
- **Spicy picks:** cards EDHTop16 rates 3 or 4 for spice (new or rarely played) in at least two lists,
  with their best finish.
- **Your own picks:** deck cards under 10% of the lists play; cards the profile keeps are marked.
- **Your combos and what the meta stops them with:** the deck's own combos, from Commander Spellbook, each
  with the cards the ten most-played commanders' lists commonly play against it: stax pieces first, then
  removal for a piece, then counterspells, tagged with the commanders whose lists play them. A combo nothing
  stops is flagged. See [ADR 0013](docs/adr/0013-the-decks-own-combos-against-the-meta.md).
- **Your key cards** and the meta's answers to them: the cards the ten most-played commanders' lists
  would use against them, weighted by how much each commander is played. Key cards are found each time,
  each with why: the commander, the deck's combos, cards you or other players of the commander file
  under a combo or win category, and cards costing far above the deck's curve (Glarb's Protean Hulk).
  `[keep]` in the profile adds more; `[key_cards] ignore` leaves one out. See
  [ADR 0017](docs/adr/0017-key-cards-found-from-the-deck.md).
- **Meta combos and your answers:** the combos those lists run most, from
  [Commander Spellbook](https://commanderspellbook.com), each with the deck's answers: stax pieces that stop
  it first, then removal for a piece, then counterspells. "no answer in the deck" flags the gaps.

A deck's play-style deck tags weigh the report ([ADR 0031](docs/adr/0031-play-styles-as-score-multipliers.md)).
Each tag favours some kinds of card, and the staples sort by their share times a weight: 1.25 for a clear fit,
1.1 for a borderline one, 0.8 for a card the plan finds off-plan. A card marked "fits" has a weight above 1.
The shares shown stay as they are; only the order and the marks change, and the limit applies after the sort.

| Deck tag | Favours (weight) | Off-plan |
|---|---|---|
| Turbo | ramp, tutors, draw (1.25) | removal, bounce, counterspells, board wipes |
| Stax | stax pieces (found as the Meta panel finds them, or Tagger's hatebear tag), removal, bounce (1.25) | |
| Control | removal, bounce, cheap draw, counterspells (1.25) | |
| Tempo | removal, cheap interaction (1.25); bounce (1.1) | |
| Midrange | counterspells, cheap interaction, tutors (1.25) | |
| Aggro | creatures (1.1); haste creatures (1.21) | |
| Combo | tutors, protection (1.1) | |

Cheap interaction is removal, bounce or a counterspell at mana value 2 or less, or free to cast. With several
tags the weights multiply, and a style that favours a card keeps it from being off-plan for another.
Off-plan staples are never compiled into Considering. A Turbo deck without the Control tag is racing: a meta
combo it has no answer to is no gap ("racing, not answering"), and the table shows "racing". Beside Control
the unanswered combos stay gaps.

When at least one list made the top cut, the report uses the top-cut lists; when none did, it reads every list
in the window and says so. With fewer than ten lists it says the data is thin: read its shares as hints.
Answers are read from Scryfall Tagger tags and rules text, so check them before acting on one.
`--card <name>` shows how lists use one card, `--to-board` compiles the deck's ideas onto its board as `sync` does, and
the app's Meta panel shows the same report by meta card: under *What stops you*, each card the meta plays
against your combos or key cards appears once, grouped as stax, removal or counters, with chips for what of
yours it stops and the decks that play it ([ADR 0018](docs/adr/0018-meta-panel-by-meta-card.md)).

Answers are cached for a day (Spellbook combos for a week, the deck's own until its list changes); `--refresh` fetches again. A first run takes
a few minutes, because requests are spaced out. The sample sizes are set in the config file:

```toml
[meta]
window_days = 60       # 7 to 90
min_event_size = 16    # smallest tournament counted
peer_decks = 60        # most Archidekt decks read per commander, 0 to 60 (0 reads none)
meta_commanders = 10   # most-played commanders searched for answers and combos
meta_lists = 30        # best-placed lists read per meta commander
```

EDHTop16's terms forbid redistributing its statistics, so meta reports stay on your machine. See
[ADR 0007](docs/adr/0007-meta-from-edhtop16-archidekt-and-spellbook.md).

### Which Archidekt decks are read

The search looks at the deck's own bracket and the one above (bracket 5 has none above), newest update first,
up to 20 pages per commander. It keeps decks with at least 1000 views, or 750, or 500 when fewer than ten
reach the bar, or all of them when fewer than ten reach 500, and reads at most `peer_decks` of them, most
viewed first. Your own decks are never read. A bracket 4 deck also reads its commander's EDHTop16 lists, which
bracket 5 decks always do.

Searches and deck reads are five seconds apart, and after a 429 nothing goes out for three minutes (or the
`Retry-After`, if longer) before the request is tried once more. Each deck read is kept until its owner edits
the deck, so a later run reads only the changes; the search results are cached for a day like the other
answers. A run that fetches deletes the cached decks unused for 30 days, those last updated before the
window and those that no longer parse; a read from cache alone (the app) deletes nothing
([ADR 0027](docs/adr/0027-reading-other-players-archidekt-decks.md)).

The lists feed one weighted share per card, which the app shows on each card: recent lists and well-placed
tournament entries count for more, and a card is an idea for Considering when the lower bound of its share
is high enough for the number of lists read
([ADR 0026](docs/adr/0026-one-weighted-share-per-card.md)). `meta` itself keeps showing plain shares.

### Deck profiles

A profile tells `suggest` what one deck is for, so its ideas fit the deck rather than Commander in general.
Start one with `cmdr profile <deck> --init`, which writes a commented `profile.toml` into the deck's folder,
then fill it in:

```toml
version = 1
archetype = "Dinosaur battlecruiser: big dinos and discover, no flicker"
bracket_ceiling = 3          # leave out ideas that would lift the deck above bracket 3
max_card_eur = 20            # leave out cards over EUR 20, and cards with no price

[targets]
protection = 6
board_wipe = 0               # 0: the deck wants none, so wipes are never suggested

[themes]                     # ideas matching any of these come first
types = ["Dinosaur"]         # words of the type line
keywords = ["Discover"]
tags = ["typal-dinosaur"]    # Tagger tags; `cmdr tags dinosaur` finds them
ignore = ["Tokens"]          # leave out the themes read from these deck tags or categories

[exclude]                    # never suggested
tags = ["flicker"]           # sub-tags count too: flicker-creature, flicker-self and so on
cards = ["Skullclamp"]

[keep]                       # never offered as cuts
cards = []
categories = ["Stax"]        # every card under this Archidekt category

[key_cards]                  # found each time; `cmdr meta` says why
ignore = ["Pyrokinesis"]     # found, but not key

[ideas]                      # commanders that play like this one: `sync` learns from their decks too
commanders = ["The Gitrog Monster", ["Tevesh Szat, Doom of Fools", "Kodama of the East Tree"]]
tag_search = true            # also learn from decks carrying the deck's theme tags; off when missing
```

Every key is optional. `[exclude]`, `bracket_ceiling` and `max_card_eur` apply to the ideas `sync`
compiles as well as to `suggest`. `cmdr profile <deck>` shows the profile as `suggest` reads it and warns
about tags Tagger doesn't have. Flags still win over the profile: `--max-price` and `--within-bracket` override
`max_card_eur` and `bracket_ceiling`. Tagger tags are community-maintained, so a tag can be renamed or
removed; `cmdr profile` is the place to notice. See [ADR 0005](docs/adr/0005-deck-profiles.md).

### Deck themes

A deck's themes also come from what you wrote on Archidekt, so a deck needs no profile to have them:

- **Deck tags** (Dwarves, Landfall, Stax...) become type words, keywords and Tagger tags of the same name;
  a few names that match nothing go through a table (Aristocrats, Blink, +1/+1 Counters). Play styles,
  roles and card types (cEDH, Commander Matters, Draw, Creatures) make no theme: they say how the deck
  plays. The play styles Turbo, Stax, Control, Tempo, Midrange, Aggro and Combo weigh the meta's staples
  instead (see *cEDH meta*); `cmdr profile` says which kind each such tag is.
- **Your own categories** of five cards or more (Leyline, Sac Outlet) become the Tagger tags and
  subtypes most of their cards share but few other cards have. Categories for roles, card types, the
  commander and the Maybeboard are skipped, as is whatever the deck tags already say.

`cmdr profile <deck>` lists each theme, where it came from and what it became, along with deck tags that
match nothing and categories whose cards share nothing distinctive. The profile's own themes come first;
`[exclude]` and `[themes] ignore` take out what doesn't fit. `sync` picks up changed deck tags even though
Archidekt keeps the deck's edit time. See [ADR 0009](docs/adr/0009-themes-from-archidekt-deck-tags-and-categories.md).

It also lists what the deck **leans into**: Scryfall Tagger tags its cards hold far more of than the recent
Archidekt decks with its commander that `sync` reads, such as Arabella's untappers next to a field of token
decks. The app shows them in the deck's header. Replacements keep them like for like, and `[themes] ignore`
drops one by its tag slug ([ADR 0016](docs/adr/0016-replacements-like-for-like.md)).

### Deck boards

A deck's board holds the cards you are weighing for it, in one list, **Considering**. You, Claude, your
Archidekt Sideboard and Maybeboard, `suggest` and `sync` put cards there. Each card carries why it might fit
and which deck cards it could replace. You decide each one: add it (choosing the card to cut), turn it down,
or reopen it later. The board file is `board.json` version 2; opening a version 1 file (which had two lists)
keeps a copy as `board.v1.bak` before the first save.

```sh
uv run cmdr board add ellivere "Sylvan Safekeeper" --reason "protects stax pieces" --replace "Compost=not stax"
uv run cmdr board decide ellivere "Sylvan Safekeeper" add --cut Compost
uv run cmdr board commit ellivere          # preview the queued changes against Archidekt
uv run cmdr board commit ellivere --save   # make them
```

Saving a board works like `apply --save`, with a snapshot first. A card you cut moves to the deck's
Maybeboard instead of being deleted, keeping its categories behind the Maybeboard, and comes back on the
board under Considering: if the new card disappoints, add the old one back and the swap runs the other way.
A cut the deck runs several copies of, such as a basic land, loses one copy instead.
Cards you keep on a deck's Sideboard or Maybeboard on Archidekt show up on its board after each `sync`;
adding one moves that entry into the deck, printing and all, and turning one down takes it off the Sideboard
or Maybeboard with the next save. `suggest --to-board` and `meta --to-board` put their ideas on the board,
and a card turned down there is not suggested again by `suggest`.

Each `sync` also compiles ideas from other players into Considering
([ADR 0028](docs/adr/0028-ideas-from-four-sources.md)), up to 50 per source, and only for cards the deck
lacks that its profile, bracket ceiling and budget allow:

- **meta** ("Tournament lists" in the app): the staples of the commander's EDHTop16 lists, for decks at
  bracket 4 and 5.
- **archidekt decks** ("Archidekt: *commander* decks"): the staples of its most-viewed recent decks, at the
  deck's bracket and the one above.
- **adjacent decks** ("Archidekt: alike commanders"): for a deck that plays unlike most decks of its
  commander. List the commanders that play like it under `[ideas] commanders` in its profile (at most eight,
  a partner pair as its own list). Their recent decks form one pool, and cards at least half the pool plays
  become ideas.
- **tagged decks** ("Archidekt: tagged *tag*"), only for a deck that sets `[ideas] tag_search = true` (the
  Settings switch *Search decks by tag*; off when missing): the deck's theme tags (the Archidekt deck tags
  that became themes, not play styles, roles or card types) are searched for recent decks of other
  commanders. Each tag is a pool, read at the deck's bracket and the one above, three search pages per
  bracket and up to a quarter of `peer_decks` decks. Cards at least half a pool plays become ideas, labelled
  with the tag. Write the tag under `[themes] ignore` to stop using it. It is off by default because the
  pools added almost nothing beyond the commander's own decks and each tag costs about 1.5 to 2 minutes on
  a first sync. Turning it off never removes tagged ideas already on the board. `sync`, `meta --to-board`
  and the app all follow the setting.

A card is an idea from the first two when the lower bound of its weighted share reaches 0.32
([ADR 0026](docs/adr/0026-one-weighted-share-per-card.md)); it goes to the source that plays it more. Each
source takes its ideas by its part of that share times the play-style weight (see *cEDH meta*), ideas within the deck's
budget before the rest. An idea with no deck card to replace is still put on the board, under *No cut yet*
in the app. A turned-down card comes back when its share has risen 20 points. EDHREC is never asked.

Searches and deck reads are paced and cached (see *Which Archidekt decks are read*), so the first sync of the
day takes a few minutes and the rest are quick. If a commander's or tag's decks cannot be read, sync names it
and carries on; if every source for a deck fails, its board stays as it was and the sync still succeeds.
`sync --no-ideas` skips this step.

The ideas come deck by deck, the deck open in the app first and the rest by how often you open them: each
open adds 1 to a deck's score, and a score halves every 30 days. The scores are kept in `views.json` in
`%LOCALAPPDATA%\Commander`, on your machine only; delete the file to start again from the folder order. A deck
you open while a sync runs goes next, once the commander being fetched is done. The app opens no deck by
itself, so the main area stays blank until you pick one, and with none picked Sync goes by the scores alone.
While a sync runs each deck in the list shows its step beside it: waiting, fetching, fetched, or a warning
sign when a source failed ([ADR 0034](docs/adr/0034-fetch-the-decks-you-look-at-first.md)).

Every change lands in the deck's activity log with who made it (you, Claude, cmdr or Archidekt).
`cmdr board log` shows every deck's log, oldest first; `--since` keeps what is newer than a UTC time like
`2026-10-15T18:00` and refuses any other format. The log is how
Claude catches up on what you decided in the app. `cmdr board show`, `research`, `remove` and `plan` cover
the rest; `cmdr board --help` lists them.

### Desktop app

The app works for any Archidekt account ([ADR 0023](docs/adr/0023-accounts-and-sync-in-the-app.md)).
Without one it opens on **Sign in to Archidekt**: the username and password go to Archidekt's login only,
and the login token is kept in Windows Credential Manager, as `cmdr login` keeps it. Each account has its
own deck folder, remembered in the config's `[workspaces]` table. Your own keeps the folder `cmdr init` set,
and a new account gets a sibling folder named `decks-<username>`, or `Documents\Commander\decks-<username>`
when no folder was set. Signing in as a new account syncs its decks at once. The foot of the deck list shows
who is signed in, with **Sync** (the same as `cmdr sync`, card data and ideas included, with a spinner and the
step it is on beneath) and **Sign out**, which removes the saved login. Without an account the button stays,
off, with "Sign in to sync" beside it. Signing in again finds the same folder. The page lists no decks until a sign-in succeeds: at start without a
signed-in account the sign-in dialog opens over a blurred, dimmed page, and leaving it any other way (**Not
now**, Escape) or signing out leaves "Sign in to see your decks" with a **Sign in** button, while the config
and `cmdr` keep working on the decks.

A sync and a save, a label change, the Mana tab's swaps, a board edit (proposing, deciding or removing a
card), saving the Settings or a sign-in or sign-out never run together: the one
that starts second is refused with a message ("Wait for the save to finish, then sync.") and can be tried
again when the first ends. The app only reads the cache of other players' data; Sync is the one way to
refresh it ([ADR 0032](docs/adr/0032-one-considering-list-and-one-sync.md)).

`cmdr app` opens a window over every deck's board. Pick a deck on the left to see it as a card grid,
grouped by card type with counts and EUR totals. S, M and L above the grid set the card size. Each card but
the commander carries a pill with its weighted share of the lists other players run with the commander, and
its sources in the tooltip. Click a card to see it large (× or Esc closes it). Copy decklist, next to the commander's name, copies the saved deck
in TopDeck.gg's decklist format (`~~Commanders~~`, then `~~Mainboard~~`, one `1 Name` a line).
Settings, beside it, edits the deck's `profile.toml` with autocomplete: the commanders
that play alike (`[ideas] commanders`; "+ partner" on one makes a pair), the *Search decks by tag* switch
(`[ideas] tag_search`) and the themes to ignore
(`[themes] ignore`: the deck's tags, categories and leanings are one click away, and any Tagger tag
can be typed). Your comments and everything else in the file stay as they are.

The right-hand rail has two tabs. **Activity** lists what changed on the board. **Mana** checks the mana
base against Frank Karsten's published numbers
([ADR 0019](docs/adr/0019-mana-base-from-karstens-numbers.md)):
- **Lands:** the land count, beside his land count formula and the median of other lists of the commander
  (top-cut tournament lists at bracket 5, recent Archidekt decks below).
- **Colour sources:** each colour's sources, with every card short of what his table asks to cast it on
  curve.
- **Turn one:** one-drops without enough untapped lands.
- **Suggested changes** (below bracket 5, [ADR 0024](docs/adr/0024-mana-base-suggestion.md)): how many lands
  to run, how many of each basic, and untapped lands to name where basics can't close a gap, with
  untapped twins for plain tap-lands. Named lands are split into within budget (up to 25% over the deck's
  average card) and over budget (up to 25% above its dearest card). **Put swaps on the board** lists them
  under Considering, each with its cut, for the usual Review and save.

- **Considering**, on the right, lists every idea for the deck in this order: yours, Claude's, the meta's,
  the Archidekt decks' for the deck's commander, alike commanders', tagged decks', then the rest. Within each
  group ideas over the deck's budget come last, and the others by the share their pill shows ("34%"), the
  combined weighted share. The pill's colour says which source the idea is credited to: tournament lists, the
  commander's Archidekt decks, alike commanders' or tagged decks' (grey for yours and Claude's). For an alike
  or tagged idea the pill shows the share of the decks it was found in instead ("60% of Landfall-tagged
  decks"), the figure the idea was picked by, and those two groups are ordered by it; an idea synced before
  the board kept those numbers shows the combined share until the next sync. Click the pill for
  the breakdown: the combined share with its count ("Combined 34% · 12 of 35"), then each source's share
  and count ("Archidekt: alike commanders", "Archidekt: tagged *tag*" and so on), the primary one marked.
  Hover over an idea or click it to light the deck cards it could replace. Click one of them to queue the
  swap; once an idea is clicked, any other deck card but the commander can be clicked too. Ideas that name
  no card to replace wait in their own *No cut yet* panel below Considering, shown only while there are any:
  click one, then the deck card it replaces.
  × turns the idea down, with a reason if you like: turned-down cards wait under *Turned down* (closed until
  opened) with their reasons, and Reconsider brings one back. Considering, Meta and each Meta section
  fold, and stay as you left them.
  The suggested targets are the cards you or Claude picked, or else the weakest cards like the idea
  ([ADR 0029](docs/adr/0029-replacement-targets-and-wider-cuts.md)). Like for like comes first: cards in the
  same role, and a card serving one of the deck's themes or leanings only for an idea serving the same, the
  least played first and unranked cards last. Every cut suggested is less played than the idea, a card no
  list plays counting as 0; without list data there is no such gate. An idea left with no cut goes to the
  *No cut yet* panel, and a cut you pick yourself is never removed. Slots left over go to cards of another
  role or theme, but only
  while the deck keeps what other players of the commander run (the peers' 25th percentile of each role, the
  median of each theme), and an other-theme cut needs the idea to be 20 points more played. The reason names
  what makes each cut allowed. A swap that takes a land out or puts one in is offered only if the land count
  stays within one of Karsten's or inside the other lists' range, and every colour keeps its sources
  ([ADR 0030](docs/adr/0030-land-swaps-guarded-by-the-mana-base.md)).
- **Labels**: the flag in a grid card's top-left corner sets its Archidekt label. Every deck offers your
  account's labels in their colours, read on each signed-in `sync` and before each label change
  ([ADR 0012](docs/adr/0012-card-labels-from-the-archidekt-account.md)). A change is written to
  Archidekt at once.
- **Meta** (bracket 5 decks) lists each key card with the meta cards that stop it, then the meta's combos
  with your answers, each tagged with the meta decks that play them. The panel reads the cache; **Sync** fetches new data (the panel
  shows it updating, then rereads) and puts the meta's staples into Considering. A card you turned down
  comes back once its weighted share has risen 20 points.
- **Estimate**, beside the bracket under the deck's name, is Spellbook's estimate. When it, the bracket and
  the profile's ceiling disagree, it turns amber; click it for the notes and causes, each combo linked to
  Spellbook. The cards behind a cause carry the bracket they lift the deck to in an amber badge beside the
  label flag, with the reasons on hover. The estimate comes from the same 7-day cache as `bracket`.
- **Review changes** checks the queued swaps against the deck on Archidekt and saves them.

The window follows changes made with the CLI or by Claude within a couple of seconds. The rail on the
right opens each deck's activity.

The app draws its page with Microsoft Edge WebView2, part of Windows 11, through
[pywebview](https://pywebview.flowrl.com); it opens no network port. It logs to
`%LOCALAPPDATA%\Commander\logs\commander.log` (`CMDR_LOG_LEVEL=DEBUG` for more), and `--debug` allows
the web inspector.

To build a single `Commander.exe` that needs no Python:

```sh
uv run --group build pyinstaller --noconfirm --clean --onefile --windowed --name Commander --icon src/commander/web/commander.ico --add-data "src/commander/web;commander/web" src/commander/app_main.py
```

The exe lands in `dist/`. It is not code-signed, so Windows SmartScreen warns the first time it runs;
choose **More info**, then **Run anyway**. It reads the same config, decks and login as `cmdr`.
See [ADR 0006](docs/adr/0006-deck-boards-and-desktop-app.md).

### Changing decks

`apply` makes the changes in a plan file to one deck on Archidekt. Write the plan as TOML:

```toml
deck = "magda"   # deck id, or part of its name as in other commands
add = ["Arcane Signet", {name = "Sol Ring", set = "c21", category = "Ramp"}]
remove = ["Wild Growth", {name = "Mountain", keep = 10}, {name = "Swords to Plowshares", maybeboard = true}]
drop = ["Path to Exile"]   # take off the Sideboard or Maybeboard
```

A plan says how the deck should end up. `add` means the deck ends with at least that many copies
(`quantity`, default 1); `remove` means it ends with none, or with `keep` copies, and with
`maybeboard = true` the card moves to the Maybeboard instead of being deleted. `drop` takes cards off the
Sideboard or Maybeboard. Lines the deck already meets are skipped, so running a plan twice is safe. Adding
a card that waits on the Sideboard or Maybeboard moves that entry into the deck. A new card gets its
cheapest EUR printing, leaving out gold-bordered and other non-tournament printings, unless `set` names
one; it goes under its card type (Land, Creature, Instant and so on) unless `category` names another.
`remove` leaves Sideboard and Maybeboard copies alone.

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
marked as in `suggest`. Below the changes comes Commander Spellbook's bracket estimate for the deck as the
plan leaves it, beside the current one when that is cached, with the same notes and causes as `bracket`
when it lands above the Archidekt bracket or the profile's ceiling. The estimate is cached on its own, so a
preview never replaces the deck's. `board commit` and the app's Review dialog show it too. `--save` first saves a snapshot of the deck on Archidekt called
`before cmdr apply <date> <time> UTC`, then sends all changes at once, reads the deck back to check each
one, and rewrites the local copy if the deck is in your deck folder. To undo a save, restore that snapshot
on Archidekt. `apply -` reads the plan from stdin, and `--json` prints the result as JSON.

A card a save brings into the deck gets the Archidekt label "Don't Have", unless it comes from the
Maybeboard with a label already (Proxy, for one); see [Cards to get](#cards-to-get).

`cmdr login` keeps only Archidekt's login token in Windows Credential Manager (under
`Commander (cmdr) Archidekt`), never your password. When the token expires, `apply --save` asks you to run
`cmdr login` again; `cmdr logout` removes it. For scripts, `cmdr login --password-stdin` reads the password
from stdin.

Archidekt's written terms forbid automated requests, but its co-founder has said on the Archidekt forum
that the API is open to use. `apply` relies on that and keeps requests few. See
[ADR 0004](docs/adr/0004-deck-changes-through-archidekts-site-api.md).

### Cards to get

Cards still to buy carry the Archidekt label "Don't Have"; once ordered, "Getting"; once they arrive, no
label. `toget` lists them and marks them; each mark is written to Archidekt at once, without a snapshot,
and logged in the deck's activity. In the app, the flag on each grid card sets the same labels.
`toget --cardmarket` and the app's "Copy cards to buy" button give every deck's Don't Have cards, summed,
as a Cardmarket wants list to paste into its "Add Decklist" field; cards already Getting are left out.
`--cardmarket` takes no deck, card, `--json`, `--ordered` or `--arrived`.

```sh
uv run cmdr toget                                          # every deck's cards still to get
uv run cmdr toget ellivere --json                          # one deck, as JSON
uv run cmdr toget --cardmarket                             # Don't Have cards of all decks, as a Cardmarket wants list
uv run cmdr toget ellivere "Sylvan Safekeeper" --ordered   # label it Getting
uv run cmdr toget ellivere "Sylvan Safekeeper" --arrived   # clear its label
```

Labels set on the Archidekt website come back with the next `sync`. See
[ADR 0010](docs/adr/0010-buying-cards-through-archidekt-labels.md).

### Deck folders

`sync` writes one folder per deck, `decks/<archidekt-id>-<name>/`, containing:

| File | Contents | Written by |
|---|---|---|
| `deck.json` | Normalised deck snapshot (cards, quantities, categories, Scryfall IDs) | `sync`, every time |
| `list.md` | Readable deck list grouped by category | `sync`, every time |
| `notes.md` | Your research notes and plans | `sync` creates it once and never overwrites it |
| `profile.toml` | What the deck wants from `suggest` | You, starting from `cmdr profile <deck> --init` |
| `board.json` | Cards under consideration, their research and your decisions | `cmdr board`, the app, `suggest --to-board`, `meta --to-board`, `sync` |
| `activity.jsonl` | Every change to the board, who made it and when, oldest first | Whatever changes the board; never rewritten |

`decks/` is gitignored, because it holds your own decks and notes. To keep their history, make it a git
repository of its own (`git init decks`) and commit there after each sync.

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
| Sample sizes for `meta` and `sync` | n/a | n/a | `[meta]` table | 60 days, events of 16+, up to 60 Archidekt decks, 10 commanders, 30 lists each |
| Archidekt login for `apply --save` | `cmdr login` | n/a | n/a | none; the token is kept in Windows Credential Manager |
| App log detail | n/a | `CMDR_LOG_LEVEL` | n/a | `INFO` |

## Test

```sh
uv run ruff check
uv run ruff format --check
npx --yes pyright@1.1.414
uv run pytest
```

Tests use recorded fixture data and never touch the network.

## External services

| Service | Used for | Notes |
|---|---|---|
| Archidekt (`archidekt.com/api`) | Reading decks; changing them with `apply --save` | Public decks need no login; changes need `cmdr login`, whose token lives in Windows Credential Manager and is refreshed on each save. The API is undocumented, so every response's shape is checked and a clear message names what changed. A save makes a handful of requests, never retried. Its written terms forbid automated requests; its co-founder has said on its forum that the API is open to use. See [ADR 0004](docs/adr/0004-deck-changes-through-archidekts-site-api.md). |
| Scryfall (`api.scryfall.com/bulk-data`, files on `data.scryfall.io`; card images from `cards.scryfall.io` in the app) | Card facts, rules text, EUR prices, Tagger tags for roles and profiles, card art | One API call per `sync` for the file list; the files themselves have no rate limit. Scryfall updates prices once a day and asks for bulk files rather than per-card calls for lookups like these. Tag data is community-maintained and can change. See [ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md). |
| Commander Spellbook (`backend.commanderspellbook.com`: `estimate-bracket`, `find-my-combos`) | Bracket estimates, combos, and combos one card short | One request per deck and command, at most four at a time, and none while a cached answer is under 7 days old. Spellbook asks tools to name themselves in the User-Agent, keep to a few calls per user action, stay under about 80 requests a minute, handle 429, and credit and link back to commanderspellbook.com, which `bracket` and `suggest` do in their output. Only card names and quantities are sent. See [ADR 0002](docs/adr/0002-bracket-estimates-from-commander-spellbook.md) and [ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md). |
| EDHTop16 (`edhtop16.com/api/graphql`) | Tournament lists, spice and the most-played commanders for `meta` and `sync`'s ideas | Requests 1.5 s apart, none while a cached answer is under a day old; malformed entries are skipped and future-dated events left out. Its terms offer the API as-is and forbid excessive automated requests, implying endorsement and redistributing its statistics; reports credit it and stay local. See [ADR 0007](docs/adr/0007-meta-from-edhtop16-archidekt-and-spellbook.md). |
| Archidekt deck search (`archidekt.com/api/decks/v3/`) | Other players' recent decks for `meta` and `sync`'s ideas | Searches and deck reads 5 s apart, after a 429 a pause of at least 3 minutes; at most 20 search pages and `peer_decks` (60) deck reads per commander, and a quarter of that per deck tag, for decks that search by tag. Search results are cached for a day, each deck read until its owner edits it. Your own decks are left out. Under the same informal permission as the rest of Archidekt's API (ADR 0004). See [ADR 0027](docs/adr/0027-reading-other-players-archidekt-decks.md). |
| Commander Spellbook (`find-my-combos`, for `meta`) | The combos the most-played commanders' lists run | One request per meta commander, 1 s apart, cached for 7 days. |
| EDHREC (via Scryfall's `edhrec_rank`) | Popularity ranks for `suggest` | Never contacted: the rank comes inside Scryfall's bulk files. EDHREC's terms forbid automated requests to its site. |
