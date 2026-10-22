# Commander

`cmdr` is a command-line tool that tracks your Commander decks on [Archidekt](https://archidekt.com). It keeps a
copy of each deck in a local folder (make it a git repository of its own to keep the history; this repository
never holds decks) and gives you a place for research notes on each deck. It also analyses each deck (curve, types, colours, roles), prices it in EUR
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
uv run cmdr sync                                    # pull every deck, report what changed, compile ideas
uv run cmdr sync --no-ideas                         # the same, without compiling ideas from other players
uv run cmdr list                                    # show the decks saved locally
uv run cmdr analyze                                 # curve, lands and roles of every deck side by side
uv run cmdr analyze magda --details                 # one deck in full, by id or part of its name or commander
uv run cmdr price                                   # value of every deck in EUR
uv run cmdr price 10000001 --top 0                  # every card in one deck, most valuable first
uv run cmdr bracket                                 # estimated bracket of every deck beside the one on Archidekt
uv run cmdr bracket gator --details                 # one deck: game changers, tutors, combos with links
uv run cmdr suggest                                 # combo pieces and short roles of every deck side by side
uv run cmdr suggest magda --max-price 5             # one deck: cards to add up to EUR 5, cards to consider cutting
uv run cmdr goldfish magda                          # play a deck out 10,000 times: hands, commander, mana, combos
uv run cmdr goldfish magda --swap "Arcane Signet=Wild Growth"  # the same, and what that swap moves
uv run cmdr goldfish magda --cuts "Arcane Signet"   # a board card against each deck card it could replace
uv run cmdr meta                                    # every bracket 4 and 5 deck against the meta, side by side
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
uv run cmdr topdeck                                 # one-time: save your TopDeck.gg key for the tournament lists
uv run cmdr apply plan.toml                         # preview a plan of cards to add and remove
uv run cmdr apply plan.toml --save                  # make those changes on Archidekt
```

`cmdr --help` and `cmdr <command> --help` show every option. Every command that reports on decks takes `--json` for scripts.
A few options worth knowing: `sync --dry-run` shows what would change without writing, `sync --force` refetches
every deck and rebuilds the card data, `cmdr -v` (or `-vv`) prints more detail on stderr, `--config <file>` uses
another config file, `--workspace <folder>` another deck folder, `cmdr init --force` replaces an existing config,
and `cmdr login --as <name or email>` signs in with a name other than the username.

### Card data

`analyze` and `price` read a local card database built from Scryfall's bulk files. `sync` downloads them
(about 80 MB) the first time, and rebuilds the database when it is a week old or a new set that brings new
cards has come out since it was built (core, expansion, Masters, Commander and Universes Beyond sets, not
promos, boxed products or Un-sets). Scryfall rewrites the files every day for the prices, which no longer
starts a download, so a price is up to a week old. The sync report says why the data was rebuilt, or when it
is next due ("next refresh 2026-10-29 or with the next new set"); `sync --force` rebuilds it at once and
`sync --no-cards` skips the step ([ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md)). If the download
fails, the previous card data stays in use and `sync` exits with 1. `analyze` and `price` warn when the
Scryfall file the data was built from is more than eight days old, which means a weekly sync was missed.

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

A deck with no bracket set on Archidekt is read as the lowest bracket of its estimate
([ADR 0051](docs/adr/0051-an-unset-bracket-follows-the-estimate.md)): Ruthless as 4, Spicy and Powerful as 3,
Oddball and Core as 2, Exhibition as 1. With no estimate, or a Banned one, it stays unset. A bracket that is
set always wins. That decides which Archidekt brackets `sync` searches for the deck, whether it reads the
commander's tournament lists, and which meta the app and `meta` show. `sync` asks Spellbook for such a deck's
estimate (from the cache while under 7 days old); the app and `profile` only read the cache, however old, so a
deck view never waits for Spellbook. The bracket marks of `suggest`, `apply` and Review go by it too, but
`suggest --within-bracket` hides nothing on an estimate alone. The app's header still names only a bracket set
on Archidekt.

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

An idea marked `! bracket 3+ (game changer)` would lift the deck above the bracket set on Archidekt (or, with
none set, its estimate's lowest), by
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

`meta` compares a bracket 4 or 5 deck (set, or estimated for a deck with none set) with what performs in
tournaments: its commander's tournament lists from [TopDeck.gg](https://topdeck.gg) from the last two months
(top cuts first), and the most-viewed Archidekt decks for that commander updated in the same window (see
*Which Archidekt decks are read* below). The tournament lists need a free TopDeck key (see *TopDeck key*
below); without one, `meta` and the app say so and carry on with the Archidekt decks. A bracket 4 deck reads
only those two sources, so its report has no answers or combos from the most-played commanders. For bracket 5
it lists:

- **Staples you don't play:** cards in at least 40% of the lists read, or in half the Archidekt decks, that
  the deck lacks.
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
  it first, then removal for a piece, then counterspells. "no answer in the deck" flags the gaps. A combo is
  listed when it makes up 5% of the meta (at most 100, most exposed first); newer lists weigh more, and
  "rising" marks one that is twice as common in the last 30 days' lists as in the 30 days before. See
  [ADR 0037](docs/adr/0037-meta-combos-by-floor-and-recency.md). Combos that share at least two pieces (the
  commander counts) are listed together under a "Shared:" line naming those pieces, each combo showing only
  its other pieces ([ADR 0038](docs/adr/0038-meta-combos-grouped-by-shared-pieces.md)).

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
min_event_size = 16    # smallest tournament counted, 16 to 1000
peer_decks = 60        # most Archidekt decks read per commander, 0 to 60 (0 reads none)
meta_commanders = 10   # most-played commanders searched for answers and combos
meta_lists = 30        # best-placed lists read per meta commander
```

Tournament data is TopDeck.gg's: every output that shows it credits and links TopDeck.gg, and the stored
events stay on your machine. See [ADR 0007](docs/adr/0007-meta-from-edhtop16-archidekt-and-spellbook.md) for
the report and [ADR 0047](docs/adr/0047-tournament-lists-from-topdeck-with-a-key-per-user.md) for where the
tournaments come from.

### TopDeck key

TopDeck.gg's API needs a free key for each user. Issue yours at [topdeck.gg/developers](https://topdeck.gg/developers),
then either press **TopDeck** at the foot of the app's deck list (paste the key, **Test key**, **Save key**) or
run `uv run cmdr topdeck` and paste it when asked. TopDeck is asked whether it takes the key before it is
saved in Windows Credential Manager (service `Commander (cmdr) TopDeck`, user `api-key`); it never goes on a
command line, into a file or into the log. `cmdr topdeck --key-stdin` reads it from stdin for scripts,
`--no-check` saves it without asking TopDeck, and `--remove` forgets it. If TopDeck is busy when it is asked,
the check stops at once and says how long to wait ("TopDeck is busy; try again in 30 s").

Events of 16 players or more are fetched seven days at a time, twelve seconds apart (TopDeck allows 100 requests a
minute but answers bulk queries at about five a minute; a 429 is waited out as it asks), and each is kept in the cache folder (`topdeck\`) as the commander and the
cards of each deck with its finish, so a day later only the new days are asked for, and the last three days
again because an event that started then may not have finished. Players' names and ids are dropped as the
answer is read and never stored. The lists of a commander and the most-played commanders and their shares are
worked out from those events.

### Which Archidekt decks are read

The search looks at the deck's own bracket and the one above (bracket 5 has none above), most viewed first,
and pages down each bracket's list until it is under the bar: 1000 views, or 750 when fewer than ten decks
updated in the window reach 1000, or 500 when fewer than ten reach 750, and never below 500. Up to five pages
of 60 decks are read per bracket, and the log says when a busy commander hits that limit. It reads the decks
at or over the bar that were updated in the window, at most `peer_decks` of them, most viewed first; when
fewer than ten reach even 500 views, it reads those few, or none. Your own decks are never read. A bracket 4
deck also reads its commander's tournament lists, which bracket 5 decks always do. A deck with no bracket set
goes by its estimate's lowest bracket here too (see *Brackets*): estimated Powerful 3+, it searches brackets 3
and 4.

That is the search for the deck's own commander. The commanders a profile lists as alike and the deck tags are
searched less, three and two pages per bracket and 15 and 10 reads, and only three tags are searched (see
*Deck boards* below, [ADR 0053](docs/adr/0053-search-less-for-alike-commanders-and-deck-tags.md)).

Searches and deck reads are 6.5 seconds apart, just under the ten requests a minute Archidekt answers, and
after a 429 nothing goes out for a minute (or the `Retry-After`, if longer) before the request is tried once
more. The pull of your own decks is not paced, but a 429 holds it for the same minute and one retry, so its
last decks no longer fail. Each deck read is kept until its owner edits
the deck, so a later run reads only the changes. Each commander's (and each tag's) search answer is refreshed
every third day, on a day of its own, so a sync refreshes about a third of them and the ideas may lag Archidekt
by up to three days; the other answers are cached for a day. A refresh searches again, which keeps the view
counts current; the decks themselves are read again only after their owner edits them. A run that fetches
deletes the cached decks unused for 30 days, those last updated
before the window and those that no longer parse; a read from cache alone (the app) deletes nothing
([ADR 0027](docs/adr/0027-reading-other-players-archidekt-decks.md), searched by views since
[ADR 0050](docs/adr/0050-search-archidekt-by-view-count.md)).

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
tag_search = true            # also learn from decks carrying the deck's 3 main theme tags; off when missing

[grace]                      # lists holding these cards count for more in this deck's shares
"Basalt Monolith" = 3
"Rings of Brighthearth" = 2

[goldfish]                   # `cmdr goldfish` plays these as finishers, beside the Tagger-marked ones
finishers = ["Craterhoof Behemoth"]
not_finishers = ["Overrun"]  # Tagger-marked cards that do not win this deck
```

Every key is optional. `cmdr sync` writes this template for a deck that has no profile yet, and adds an empty
`[grace]` table, with a commented example, to a profile that has none, so it is there to fill in. `[exclude]`, `bracket_ceiling` and `max_card_eur` apply to the ideas `sync`
compiles as well as to `suggest`. `cmdr profile <deck>` shows the profile as `suggest` reads it and warns
about tags Tagger doesn't have. Flags still win over the profile: `--max-price` and `--within-bracket` override
`max_card_eur` and `bracket_ceiling`. Tagger tags are community-maintained, so a tag can be renamed or
removed; `cmdr profile` is the place to notice. See [ADR 0005](docs/adr/0005-deck-profiles.md).

**Grace cards.** Some commanders run several very different lists (Kinnan: little flips, big flips, no
Basalt Monolith), and the shares pooled over all of them describe none. Name the cards that mark your line
under `[grace]`, each with a factor over 1 and at most 10, and a meta list or recent Archidekt deck holding one
counts that many times more in this deck's weighted shares: the percentages on the deck list and in
Considering, the cut targets and the ideas `sync` compiles. A list holding several grace cards is weighed by
the product of their factors, at most 10. Names match whatever the case, by the full name or the front face
of a two-faced card; punctuation must match and a back face does not. The meta breakdown does not use grace.
A heavy grace leaves fewer lists doing the work, so the idea test is stricter for each card, while cards your
line plays can pass that would not have before; the percentage's breakdown in the app says which grace cards are set
and how many lists hold one, and `cmdr profile` lists them and flags names that match no card. Edit `[grace]`
in the file or in the app's Settings (*Grace cards*: add a card with its factor, change a factor, remove a card;
the app refuses a name that matches no card, and saves with the rest of the Settings
without touching your comments) ([ADR 0039](docs/adr/0039-grace-cards-weight-the-lists-like-yours.md)).

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

A deck's board holds the cards you are weighing for it, in one list, **Considering**. You, Claude, `suggest`
and `sync` put cards there. Each card carries why it might fit
and which deck cards it could replace. You decide each one: add it (choosing the card to cut, or none, and
the deck category it goes under), turn it down, or reopen it later. The board also queues count changes to
the deck's own cards: one more Forest, or a card taken out
([ADR 0043](docs/adr/0043-count-changes-and-adds-without-a-cut-on-the-board.md)). More copies than the deck
holds are only for a basic land or a card whose rules text allows them ("A deck can have any number of cards
named ...", "up to seven"). A count change and a queued swap never touch the same card. A decrease means at
most that many copies and an increase at least that many, so a change the deck already meets (it was edited on
Archidekt) sends nothing, and `sync` clears it. The board file is
`board.json` version 3; opening an older file keeps a copy as `board.v1.bak` or `board.v2.bak` before the first
save.

```sh
uv run cmdr board add ellivere "Sylvan Safekeeper" --reason "protects stax pieces" --replace "Compost=not stax"
uv run cmdr board decide ellivere "Sylvan Safekeeper" add --cut Compost
uv run cmdr board decide ellivere "Arcane Signet" add --category Ramp   # add without a cut
uv run cmdr board count ellivere Forest 12   # queue 12 Forests; 0 takes a card out
uv run cmdr board count ellivere Ponder 0 --category Maybeboard   # take a card off the Maybeboard
uv run cmdr board commit ellivere          # preview the queued changes against Archidekt
uv run cmdr board commit ellivere --save   # make them
```

Saving a board works like `apply --save`, with a snapshot first. A card you cut is removed from the deck;
if it passes the idea test on a later `sync` it shows up in Considering again like any other idea.
A cut the deck runs several copies of, such as a basic land, loses one copy instead.
A save leaves the deck's Sideboard and Maybeboard on Archidekt as they are, and so does `sync`, which no
longer reads them ([ADR 0035](docs/adr/0035-retire-the-maybeboard-and-sideboard.md)), except for a card you
queued to take off one: the save removes that entry and nothing else there
([ADR 0052](docs/adr/0052-take-a-card-off-the-sideboard-or-maybeboard.md)). Each `sync` tests the
open ideas `cmdr` compiled earlier again, and those taken from the Maybeboard by an earlier version too; one
that no longer passes the idea test below is turned down ("no longer makes the cut") and comes back only if
its share rises 20 points, unless you reopened it with Reconsider: that card is left alone until you decide
it. Reconsider applies to turned-down cards only (undoing a queued swap is a plain reopen). It labels a card
from the Maybeboard pick-up like a compiled idea, from the cached lists (the
source that plays it more, the meta on a tie; with no list playing it, only the reason says so), and each
`sync` does the same for the open ones that pass the test. `suggest --to-board` and `meta --to-board` put their ideas on the board,
and a card turned down there is not suggested again by `suggest`.

A card in the deck is never in Considering ([ADR 0046](docs/adr/0046-a-card-in-the-deck-is-never-in-considering.md)):
`sync` closes every open card the deck's main list now holds (a card you added on Archidekt, say) as applied,
"already in the deck", and prints `<deck> board: closed ...`; the app hides such a card at once, without
waiting for the sync. A card in the Sideboard or Maybeboard, or a basic land, still counts as an idea. A card
queued to add that the deck on Archidekt already holds stays queued: Review offers **Mark N card(s) applied**.

Each `sync` also compiles ideas from other players into Considering
([ADR 0028](docs/adr/0028-ideas-from-four-sources.md)), up to 50 per source, and only for cards the deck
lacks that its profile, bracket ceiling and budget allow:

- **meta** ("Tournament lists" in the app): the staples of the commander's TopDeck.gg lists, for decks at
  bracket 4 and 5.
- **archidekt decks** ("Archidekt: *commander* decks"): the staples of its most-viewed recent decks, at the
  deck's bracket and the one above.
- **adjacent decks** ("Archidekt: alike commanders"): for a deck that plays unlike most decks of its
  commander. List the commanders that play like it under `[ideas] commanders` in its profile (at most eight,
  a partner pair as its own list). Their recent decks form one pool, and cards at least half the pool plays
  become ideas. Each commander is searched over three pages per bracket (most viewed first, with the same view
  bars) and read for up to a quarter of `peer_decks` decks, 15 of 60. A name alone takes every deck that
  commander leads, with a partner or Background or without; a pair takes the decks led by exactly that pair
  ([ADR 0053](docs/adr/0053-search-less-for-alike-commanders-and-deck-tags.md)).
- **tagged decks** ("Archidekt: tagged *tag*"), only for a deck that sets `[ideas] tag_search = true` (the
  Settings switch *Search decks by tag*; off when missing): the deck's theme tags (the Archidekt deck tags
  that became themes, not play styles, roles or card types) are searched for recent decks of other
  commanders, but only the three that the most of the deck's main cards match (ties in Archidekt's tag order);
  no setting searches more. Each tag is a pool, read at the deck's bracket and the one above, two search pages
  per bracket (most viewed first, with the same view bars) and up to 10 decks. Cards at least half a pool
  plays become ideas, labelled with the tag. Write the tag under `[themes] ignore` to stop using it. It is off
  by default because the pools added almost nothing beyond the commander's own decks and each tag costs up to
  about a minute and a half on a first sync. Tagged ideas already on the board stay, but with the search off,
  or once their tag has left the deck or its three searched tags, each sync tests them by the commander's own
  lists instead of the pool: one that fails the idea
  test below is turned down, one that passes is relabelled to the source that plays it more. The same goes
  for alike ideas once the profile lists no commander. `sync`, `meta --to-board` and the app all follow the setting.

A card is an idea from the first two when the lower bound of its weighted share reaches 0.30
([ADR 0026](docs/adr/0026-one-weighted-share-per-card.md)); it goes to the source that plays it more. Each
source takes its ideas by its part of that share times the play-style weight (see *cEDH meta*), ideas within the deck's
budget before the rest. An idea with no deck card to replace is still put on the board, under *No cut yet*
in the app. A turned-down card comes back when its share has risen 20 points. EDHREC is never asked.

Searches and deck reads are paced and cached (see *Which Archidekt decks are read*), so the first sync of the
day takes a few minutes and the rest are quick. If a commander's or tag's decks cannot be read, sync names it
and carries on; if every source for a deck fails, its board stays as it was and the sync still succeeds.
`sync --no-ideas` skips this step.

Every deck's ideas are read at once, and Archidekt's requests go out in an order: the deck open in the app
first and the rest by how often you open them: each open adds 1 to a deck's score, and a score halves every 30
days. The scores are kept in `views.json` in `%LOCALAPPDATA%\Commander`, on your machine only; delete the file
to start again from the folder order. A deck you open while a sync runs moves to the front of the requests
still waiting, and its ideas are merged first when several are ready. The app opens no deck by itself, so the
main area stays blank until you pick one, and with none picked Sync goes by the scores alone.
While a sync runs each deck in the list shows its step beside it: waiting, fetching (several decks at once),
fetched, or a warning sign when a source failed
([ADR 0034](docs/adr/0034-fetch-the-decks-you-look-at-first.md), [ADR 0049](docs/adr/0049-sync-reads-decks-side-by-side-in-priority-order.md)).
You can keep working on a deck while a sync runs: proposing, deciding, the counts, the settings and saving to
Archidekt go through, and a deck's new ideas are merged around your changes. The sync pulls the deck open in
the app first, and the app asks you to wait only while the sync may still rewrite that deck ("Wait for
Ellivere to finish syncing"), for a change that looks cards up while the card data downloads (proposing, the
counts, the mana swaps, the settings and saving), and for signing in or out
([ADR 0042](docs/adr/0042-writes-wait-only-while-the-sync-writes-the-deck.md)).

Every change lands in the deck's activity log with who made it (you, Claude or cmdr; logs written by an
earlier version may also show Archidekt, for cards it took from the Maybeboard).
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
step it is on beneath), **TopDeck** (the key the tournament lists need, with its steps, **Test key** and
**Save key**; the Meta and Considering panels credit TopDeck.gg) and **Sign out**, which removes the saved
login. Without an account the button stays,
off, with "Sign in to sync" beside it. Signing in again finds the same folder. The page lists no decks until a sign-in succeeds: at start without a
signed-in account the sign-in dialog opens over a blurred, dimmed page, and leaving it any other way (**Not
now**, Escape) or signing out leaves "Sign in to see your decks" with a **Sign in** button, while the config
and `cmdr` keep working on the decks.

A sync and a save, a label change, the Mana tab's swaps, a board edit (proposing, deciding or removing a
card, or + and −), saving the Settings or a sign-in or sign-out never run together: the one
that starts second is refused with a message ("Wait for the save to finish, then sync.") and can be tried
again when the first ends. A sign-in or sign-out waits for no deck's write: it is refused only while a sync
runs, and a save already under way finishes for the account it began with
([ADR 0042](docs/adr/0042-writes-wait-only-while-the-sync-writes-the-deck.md)). The app only reads the
cache of other players' data; Sync is the one way to refresh it
([ADR 0032](docs/adr/0032-one-considering-list-and-one-sync.md)).

`cmdr app` opens a window over every deck's board. Pick a deck on the left to see it as a card grid,
grouped by card type with counts and EUR totals. The deck's Sideboard, Maybeboard and any other category
Archidekt keeps outside the deck follow as groups of their own, marked *Not in deck*
([ADR 0044](docs/adr/0044-the-sideboard-and-maybeboard-shown-again.md)): their cards carry pills like the
others but count in no total, and ideas may still suggest them. Hovering one shows −, which queues it to
come off that category with the next save, and + undoes that. Above the grid stand the deck's card count,
its Don't Have cards and what they cost, and the average mana value of its spells (lands left out), for the
deck as saved; where queued changes would alter one, it reads "saved → after Review". S, M and L beside them
set the card size. Hovering a deck card shows − (one copy fewer; at one copy it takes the card out) and, on a
basic land or a card allowed in multiples, +. These and the cards queued to come in show in the grid until
saved: "×6 → 7" or *Out* on the copies badge, a dashed outline and *New · category* on a card coming in,
whose − takes it off the queue. Each card but
the commander carries a pill with its weighted share of the lists other players run with the commander, and
its sources in the tooltip. Click a card to see it large (× or Esc closes it). Copy decklist, next to the commander's name, copies the saved deck
in TopDeck.gg's decklist format (`~~Commanders~~`, then `~~Mainboard~~`, one `1 Name` a line).
Settings, beside it, edits the deck's `profile.toml`: the commanders
that play alike (`[ideas] commanders`, with autocomplete; "+ partner" on one makes a pair), the *Search decks by tag* switch
(`[ideas] tag_search`) and the themes to ignore
(`[themes] ignore`: the deck's tags, categories and leanings are one click away, and any Tagger tag
can be typed, with autocomplete), the grace cards (`[grace]`, below; type the card's name, no autocomplete)
and the *Finishers* the goldfish counts as ways to win (`[goldfish]`: one chip each, the Tagger-marked ones
labelled "tagged"; × takes one out, "+ name" puts a marked one back, and the box adds a deck card, with
autocomplete). Your comments and everything
else in the file stay as they are.

The right-hand rail has three tabs. **Activity** lists what changed on the board. **Goldfish** plays the
deck out 10,000 times when opened and shows what `cmdr goldfish` reports (see [Goldfish](#goldfish));
when the deck cannot be played, **Play the deck again** tries once more. **Mana** checks the mana
base against Frank Karsten's published numbers
([ADR 0019](docs/adr/0019-mana-base-from-karstens-numbers.md)):
- **Lands:** the land count, beside his land count formula and the median of other lists of the commander
  (top-cut tournament lists at bracket 5, recent Archidekt decks below).
- **Colour sources:** each colour's sources, with every card short of what his table asks to cast it on
  curve.
- **Turn one:** one-drops without enough untapped lands.
- **Two-part cards** are judged by one part in all of these and in the suggested changes: an adventure
  (Bonecrusher Giant // Stomp) by its creature, at the card's mana value, so Stomp does not make it a
  one-drop; a split card (Dead // Gone, Fire // Ice) by its most expensive half, both halves when they tie;
  any other card by its front face.
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
  or tagged idea the pill shows the share of the decks it was found in instead, the figure the idea was
  picked by (its label and tooltip read "60% of Landfall-tagged decks"), and those two groups are ordered by it; an idea synced before
  the board kept those numbers shows the combined share until the next sync. Click the pill for
  the breakdown: the combined share with its count ("Combined 34% · 12 of 35"), then each source's share
  and count ("Archidekt: alike commanders", "Archidekt: tagged *tag*" and so on).
  Hover over an idea or click it to light the deck cards it could replace. Click one of them to queue the
  swap; once an idea is clicked, any other deck card but the commander can be clicked too. Ideas that name
  no card to replace wait in their own *No cut yet* panel below Considering, shown only while there are any:
  click one, then the deck card it replaces. A clicked idea also offers **Add without a cut**, under one of
  the deck's categories (its card type's first); like every add it is labelled Don't Have when saved.
  × turns the idea down, with a reason if you like: turned-down cards wait under *Turned down* (closed until
  opened) with their reasons, and Reconsider brings one back. Considering, Meta, each Meta section and each
  category under *What stops you* fold, and stay as you left them.
  While a set is being previewed, a folded *Previews* panel below *No cut yet* shows up to 6 of its announced
  cards that suit the deck: in its colours, matching the most of its three main Archidekt tags, then filling
  a role it runs short of. They are only to look at: a preview cannot be added or priced, and it leaves the
  panel on its release day, after which the usual ideas can bring it. The sync fetches them from Scryfall at
  most once a day, and only while a set is upcoming
  ([ADR 0055](docs/adr/0055-show-upcoming-cards-that-fit-a-deck.md)).
  The suggested targets are the cards you or Claude picked, or else the four best scoring deck cards
  ([ADR 0029](docs/adr/0029-replacement-targets-and-wider-cuts.md),
  [ADR 0036](docs/adr/0036-surplus-cuts-on-one-score.md),
  [ADR 0041](docs/adr/0041-a-cut-clearly-less-played-than-the-idea.md)). Every candidate scores the idea's
  share less its own, plus a bonus for what allows the cut. Every cut suggested is at least 0.30 less played
  than the idea, or half the idea's share when that is smaller (a 90% idea replaces cards at 60% or less, a
  10% idea cards at 5% or less), a card no list plays counting as 0 (basic lands excepted); a cut Claude
  picked need only be less played; without list data there is no such gate. An idea left with no cut goes to the
  *No cut yet* panel, and a cut you pick yourself is never removed. Cards in the idea's role, and a card
  serving one of the deck's themes or leanings only for an idea serving the same, get +0.10. A card of another
  role scores +0.03 per card over the peers' median when the deck holds at least 2 more than that median
  of every role of ramp, draw, removal and tutors it fills (other roles keep the peers' 25th percentile). A
  card of another theme goes the same way when the deck runs 2 over the peers' median of every theme it
  serves and the idea is 0.20 more played; a leaning, or a card that would empty one of those four roles, never goes
  as a theme surplus. A nonland idea may replace a land the mana base can spare, +0.03 per spare land
  (lands above the greater of Karsten's count less one and the peers' median land count): the basic land of
  the colour with most sources to spare, and nonbasic lands that only make one mana at a time (no fetch, copy, cycling or other ability, and none that can make more than one mana, like Ancient Tomb or Gaea's Cradle), fill no role, serve no theme and are less played
  than the idea. Surplus cuts need at least three recent decks of the commander read, and the reason says
  which kind allows each cut. A swap that takes a land out or puts one in is offered only if the land count
  ends within one of Karsten's, closer to it than before, or inside the range of three or more other lists,
  and every colour keeps its sources
  ([ADR 0030](docs/adr/0030-land-swaps-guarded-by-the-mana-base.md)).
  When an idea is clicked and some of its targets tie on score, the goldfish plays it against each of them
  and reorders those once its answer comes; a target it moved up carries a *Goldfish pick* mark whose tooltip
  names the figure that decided it ([ADR 0056](docs/adr/0056-goldfish-breaks-ties-between-cut-targets.md)).
  A queued idea with a cut, once clicked, shows a folded *Goldfish* section with the headline figures its
  swap moves (each commander cast, the worst tenth of mana and each combo by the bracket's turn, and colour
  screw), and nothing at all when none moved.
- **Labels**: the flag in a grid card's top-left corner sets its Archidekt label. Every deck offers your
  account's labels in their colours, read on each signed-in `sync` and before each label change
  ([ADR 0012](docs/adr/0012-card-labels-from-the-archidekt-account.md)). A change is written to
  Archidekt at once.
- **Meta** (bracket 4 and 5 decks, set or estimated) lists the deck's key cards. For bracket 5 each comes with
  the meta cards that stop it, then the meta's combos with your answers, each tagged with the meta decks that
  play them; a bracket 4 deck shows the key cards and the number of tournament lists read, as the
  most-played commanders are not read for it. The panel reads the cache, during a sync
  too; **Sync** fetches new data (the panel says it is updating only while this deck is queued or being
  fetched, then rereads) and puts the meta's staples into Considering. A card you turned down
  comes back once its weighted share has risen 20 points.
- **Estimate**, beside the bracket under the deck's name, is Spellbook's estimate. When it, the bracket and
  the profile's ceiling disagree, it turns amber; click it for the notes and causes, each combo linked to
  Spellbook. The cards behind a cause carry the bracket they lift the deck to in an amber badge beside the
  label flag, with the reasons on hover. The estimate comes from the same 7-day cache as `bracket`.
- **Review changes** checks the queued swaps, adds and count changes against the deck on Archidekt and
  saves them. Above Save, a *Goldfish* block lists the headline figures the queued swaps move together, only
  when something moved.

The window follows changes made with the CLI or by Claude within a couple of seconds. The rail on the
right opens the open deck's activity, goldfish and mana base; it is hidden while no deck is open.

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
remove = ["Wild Growth", {name = "Mountain", keep = 10}, {name = "Ponder", category = "Maybeboard"}]
```

A plan says how the deck should end up. `add` means the deck ends with at least that many copies
(`quantity`, default 1); `remove` means it ends with none, or with `keep` copies. A `remove` with a
`category` outside the deck, such as the Maybeboard, takes the card off that category and leaves the deck's
own copies alone. Lines the deck already meets are skipped, so running a plan twice is safe. A new card
gets its cheapest EUR printing, leaving out gold-bordered and other non-tournament printings, unless `set`
names one; it goes under its card type (Land, Creature, Instant and so on) unless `category` names another.
Saving a plan leaves the deck's Sideboard and Maybeboard alone but for its own removals, and an `add` into
either category is refused
([ADR 0035](docs/adr/0035-retire-the-maybeboard-and-sideboard.md)). The old `maybeboard = true` and
`drop` keys are gone; a plan that still has them is refused.

Whether an added card counts in the deck follows the category's "in deck" setting on Archidekt,
which each deck sets for itself. A category the deck doesn't have yet is created by the save and counts in
the deck.

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

A card a save brings into the deck gets the Archidekt label "Don't Have"; see [Cards to get](#cards-to-get).

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

### Goldfish

`cmdr goldfish <deck>` deals and plays the deck 10,000 times against an empty table, by one fixed play
policy, and reports the opening hands kept, the turn each commander is cast, the mana on each turn (median and
worst tenth), how often a spell had the mana but not the colours, and, for a deck with a Commander Spellbook
combo or a finisher, how soon each lands, with a warning for a combo that comes up by the bracket's turn in a
quarter of the games or more. The turns shown follow the deck's bracket, which also picks the mulligan rule.
It reads the card data and the caches alone and fetches nothing; the same deck always gives the same figures
(the seed is the deck id). The figures are the earliest a deck gets there, not a promise: nothing interacts,
and `Not modelled` lists the cards the reading could not place.

`--swap "<card in>=<card out>"` (repeated for more) or `--plan plan.toml` (the plan `apply` reads: its adds
paired with its removes, in order) plays the same games again with the swaps made and lists every figure that
moved, before and after; a figure counts as moved only when the change is larger than twice its paired
standard error. A swap takes out one copy of the card out and plays the rest, so Cultivate for one of ten
Forests plays nine Forests and Cultivate; in a plan, a remove takes out the copies beyond its `keep`, and
its add brings as many. `--json` gives the figures for scripts, and `[goldfish]` in the profile (above)
sets the finishers. See
[ADR 0054](docs/adr/0054-goldfish-a-deck.md).

`--cuts "<card>"` plays a card on the deck's board against each deck card it could replace and prints the
headline figures each swap moves, in the order the app shows the targets. Targets scoring at most 0.10 below
the first of a run tie with it, and a tie is ordered by those figures: fewer made worse first, then more made
better, else the score's order. A target that moved up names the target it went ahead of and the figure
that put it there, and one whose swap cannot be played says why and keeps its place. The goldfish never
offers a card the score did not ([ADR 0056](docs/adr/0056-goldfish-breaks-ties-between-cut-targets.md)).

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

When a deck is renamed on Archidekt, `sync` renames its folder to match, under the deck's board lock; if a
board write holds that lock, the deck is skipped and the next sync renames it
([ADR 0045](docs/adr/0045-sync-removes-the-folder-of-a-deleted-deck.md)). When a deck is deleted on
Archidekt, a signed-in `sync` deletes its folder too (notes, profile and board included, so commit `decks/` first
if you keep its history) and reports it as `removed`; `sync --dry-run` says `would remove` and deletes nothing.
It does so only when the deck is missing from a complete signed-in listing and Archidekt answers 404 for the
deck itself ([ADR 0045](docs/adr/0045-sync-removes-the-folder-of-a-deleted-deck.md)). Any other case (not signed
in, a refused login, a private deck, a network error or any other status) reports the deck as `missing` and keeps
the folder. In the app, a removed open deck closes with a short notice. The folder is first renamed to
`.removing-<name>`, and a deck whose board the app is writing is kept for that sync; if the delete then fails
(a program holds a file open), the renamed folder keeps your files, the sync warns about it by name (reported as `missing`;
the sync still succeeds) and the next sync retries.

#### Private and unlisted decks

With a saved login (`cmdr login`, or signing in to the app), `sync` lists and reads your decks with it, so your
private and unlisted decks are synced like the public ones
([ADR 0040](docs/adr/0040-sync-private-and-unlisted-decks.md)). Their data lands in the deck folder only.
Without a login, or when Archidekt refuses it (it expired, or the credential store cannot be read), `sync`
lists your public decks alone and warns, ending in "private and unlisted decks were not updated". The start
says why:

- "Not signed in to Archidekt": there is no saved login. The warning appears only when private or unlisted
  decks are already in the folder.
- "Archidekt login not usable (<reason>)": a login is saved, but a token could not be got from it.
- "Archidekt refused the login": the token was refused when the decks were listed.

Those decks stay as they are and are not reported as disappeared; run `cmdr login` again to update them.

### Settings

Settings resolve in this order: command-line flags, then environment variables, then the config file, then
defaults.

| Setting | Flag | Variable | Config key | Default |
|---|---|---|---|---|
| Archidekt username | `--user` | `CMDR_USER` | `user` | none (required) |
| Deck folder | `--workspace` | `CMDR_WORKSPACE` | `workspace` | none (required) |
| Config file | `--config` | `CMDR_CONFIG` | n/a | `%APPDATA%\Commander\config.toml` |
| Cache folder: card data, meta answers, other players' decks, view scores (`views.json`), logs, the app window's profile (`webview`, with the card images) | n/a | `CMDR_CACHE_DIR` | n/a | `%LOCALAPPDATA%\Commander` |
| Role targets for `suggest` | n/a | n/a | `[targets]` table | ramp 10, draw 10, removal 8, board_wipe 2 |
| Sample sizes for `meta` and `sync` | n/a | n/a | `[meta]` table | 60 days, events of 16+, up to 60 Archidekt decks (a quarter of that for an alike commander), 10 commanders, 30 lists each |
| Archidekt login for `apply --save`, and for `sync` to read private and unlisted decks | `cmdr login` | n/a | n/a | none; the token is kept in Windows Credential Manager |
| TopDeck.gg API key for the tournament lists | `cmdr topdeck`, or the app's TopDeck button | n/a | n/a | none; the key is kept in Windows Credential Manager |
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
| Archidekt (`archidekt.com/api`) | Reading decks; changing them with `apply --save` | Public decks need no login; changes, and `sync` of your private and unlisted decks, need `cmdr login`, whose token lives in Windows Credential Manager and is refreshed on each save and each sync. The API is undocumented, so every response's shape is checked and a clear message names what changed. A save makes a handful of requests, never retried. Its written terms forbid automated requests; its co-founder has said on its forum that the API is open to use. See [ADR 0004](docs/adr/0004-deck-changes-through-archidekts-site-api.md). |
| Scryfall (`api.scryfall.com/bulk-data`, `/sets` and `/cards/search`, files on `data.scryfall.io`; card images from `cards.scryfall.io` in the app) | Card facts, rules text, EUR prices, Tagger tags for roles and profiles, card art, previews of upcoming sets | One API call per `sync` for the set list, and one for the file list when the card data is rebuilt (weekly, or when a new set has come out); the files themselves have no rate limit. While a set is upcoming, one card search per set and page of 175 cards, at most once a day, 200 ms apart (Scryfall asks for no more than 10 requests a second; [ADR 0055](docs/adr/0055-show-upcoming-cards-that-fit-a-deck.md)). Scryfall updates prices once a day and asks for bulk files rather than per-card calls for lookups like these. The app keeps the card images it has loaded in its window profile on disk, so each is fetched once. Tag data is community-maintained and can change. See [ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md). |
| Commander Spellbook (`backend.commanderspellbook.com`: `estimate-bracket`, `find-my-combos`) | Bracket estimates, combos, and combos one card short | One request per deck and command, at most four at a time, and none while a cached answer is under 7 days old. Spellbook asks tools to name themselves in the User-Agent, keep to a few calls per user action, stay under about 80 requests a minute, handle 429, and credit and link back to commanderspellbook.com, which `bracket` and `suggest` do in their output. Only card names and quantities are sent. See [ADR 0002](docs/adr/0002-bracket-estimates-from-commander-spellbook.md) and [ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md). |
| Archidekt deck search (`archidekt.com/api/decks/v3/`) | Other players' popular recent decks for `meta` and `sync`'s ideas | Searches and deck reads 6.5 s apart, after a 429 a pause of at least a minute, and a second 429 ends Archidekt for the run; at most 5 search pages (60 decks each) per bracket and `peer_decks` (60) deck reads for a deck's own commander; 3 pages and a quarter of that (15) for each alike commander; and, for decks that search by tag, 2 pages and 10 reads for each of the deck's 3 main tags. Each search answer is refreshed every third day (staggered per commander or tag), by a new search by views, and each deck read until its owner edits it. Your own decks are left out. Under the same informal permission as the rest of Archidekt's API (ADR 0004). See [ADR 0027](docs/adr/0027-reading-other-players-archidekt-decks.md), [ADR 0048](docs/adr/0048-pace-archidekt-by-its-own-limits.md), [ADR 0050](docs/adr/0050-search-archidekt-by-view-count.md) and [ADR 0053](docs/adr/0053-search-less-for-alike-commanders-and-deck-tags.md). |
| TopDeck.gg (`topdeck.gg/api/v2/tournaments`) | Commander tournaments with decklists, for the commander's lists and the most-played commanders in `meta` and `sync`'s ideas | A free key for each user (`cmdr topdeck`, or the app), sent in the `Authorization` header and kept in Windows Credential Manager; it does not expire unless TopDeck revokes it. 100 requests a minute, fewer for bulk queries: events of 16+ players are asked for seven days at a time, 12 s apart (a 429 is waited out for the `Retry-After` it names), only for days not seen yet, and kept per event in the cache folder; malformed events and decks are skipped and logged. Player names and ids are dropped as the answer is read. Any output that shows the data credits and links TopDeck.gg, as its terms ask. See [ADR 0047](docs/adr/0047-tournament-lists-from-topdeck-with-a-key-per-user.md). |
| Commander Spellbook (`find-my-combos`, for `meta`) | The combos the most-played commanders' lists run | One request per meta commander, 1 s apart, cached for 7 days. |
| EDHREC (via Scryfall's `edhrec_rank`) | Popularity ranks for `suggest` | Never contacted: the rank comes inside Scryfall's bulk files. EDHREC's terms forbid automated requests to its site. |
