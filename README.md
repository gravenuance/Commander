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
uv run cmdr suggest pantlaza --to-board             # put suggest's ideas on the deck's board
uv run cmdr board show ellivere                     # cards considered and suggested for one deck
uv run cmdr board log --since 2026-10-15T18:00      # what changed on the boards since then
uv run cmdr app                                     # the desktop app over every deck's board
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

`suggest` gives each deck three lists. Prices are each card's cheapest printing in EUR.

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

EDHREC's own pages are not used: its terms of use forbid automated requests. See
[ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md).

`cmdr card <name>` prints a card's rules text, type, power and toughness, price, EDHREC rank, roles and
[Scryfall Tagger](https://tagger.scryfall.com) tags, from the local card data. Name several cards to see
them together.

### cEDH meta

`meta` compares a bracket 5 deck with what performs in cEDH: its commander's tournament lists on
[EDHTop16](https://edhtop16.com) from the last two months (top cuts first), and the most-viewed Archidekt
decks for that commander updated in the same window. It lists:

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

A deck's play-style deck tags weigh the report ([ADR 0022](docs/adr/0022-play-style-weighs-the-meta.md)):

| Deck tag | Staples first (marked "fits") | Also |
|---|---|---|
| Turbo | ramp, tutors, draw | Interaction is "off-plan": listed last, never put on the board as an idea. A meta combo without an answer is no gap ("racing, not answering"), and the table shows "racing". |
| Stax | stax pieces (found as the Meta panel finds them, or Tagger's hatebear tag) and cheap interaction (mana value 2 or less, or free to cast) | |
| Control | interaction | Unanswered combos stay gaps, even beside Turbo. |

Other play styles weigh nothing. The shares stay as they are; only the order and the marks change.

With fewer than five top-cut lists every list in the window is read, and with fewer than ten lists the
report says the data is thin: read its shares as hints. Answers are read from Scryfall Tagger tags and
rules text, so check them before acting on one. `--card <name>` shows how lists use one card,
`--to-board` puts the missing staples on the deck's board, and the app's Meta panel shows the same report
by meta card: under *What stops you*, each card the meta plays against your combos or key cards appears
once, grouped as stax, removal or counters, with chips for what of yours it stops and the decks that play
it ([ADR 0018](docs/adr/0018-meta-panel-by-meta-card.md)).

Answers are cached for a day (Spellbook combos for a week, the deck's own until its list changes); `--refresh` fetches again. A first run takes
a few minutes, because requests are spaced out. The sample sizes are set in the config file:

```toml
[meta]
window_days = 60       # 7 to 90
min_event_size = 16    # smallest tournament counted
peer_decks = 10        # Archidekt decks read per commander
meta_commanders = 10   # most-played commanders searched for answers and combos
meta_lists = 30        # best-placed lists read per meta commander
```

EDHTop16's terms forbid redistributing its statistics, so meta reports stay on your machine. See
[ADR 0007](docs/adr/0007-meta-from-edhtop16-archidekt-and-spellbook.md).

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
  plays, which matters when weighing a replacement rather than for picking cards in general.
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

A deck's board holds the cards you are weighing for it, in two lists: **considered** cards are ones you,
Claude or your Archidekt Sideboard and Maybeboard raised; **suggested** cards come from `suggest` or from
research. Each card carries why it might fit and which deck cards it could replace. You decide each one:
add it (choosing the card to cut), turn it down, or reopen it later.

```sh
uv run cmdr board add ellivere "Sylvan Safekeeper" --reason "protects stax pieces" --replace "Compost=not stax"
uv run cmdr board decide ellivere "Sylvan Safekeeper" add --cut Compost
uv run cmdr board commit ellivere          # preview the queued changes against Archidekt
uv run cmdr board commit ellivere --save   # make them
```

Saving a board works like `apply --save`, with a snapshot first. A card you cut moves to the deck's
Maybeboard instead of being deleted, keeping its categories behind the Maybeboard, and comes back on the
board as considered: if the new card disappoints, add the old one back and the swap runs the other way.
A cut the deck runs several copies of, such as a basic land, loses one copy instead.
Cards you keep on a deck's Sideboard or Maybeboard on Archidekt show up on its board as considered after
each `sync`; adding one moves that entry into the deck, printing and all, and turning one down takes it
off the Sideboard or Maybeboard with the next save. `suggest --to-board` puts its ideas on the board, and
a card turned down there is never suggested again.

Each `sync` also compiles ideas from other players into Considering
([ADR 0014](docs/adr/0014-considered-compiled-on-every-sync.md)). A bracket 5 deck gets the staples it
misses from its commander's EDHTop16 lists ("meta") and from the most-viewed recent Archidekt decks for
its commander ("archidekt decks"); other decks get the Archidekt ones, from decks at their own bracket. Each
source adds at most 10, most played first, and only cards with a deck card to replace. EDHREC is never
asked. Answers are cached for a day with `meta`'s, so the first sync of the day takes a few minutes and
the rest are quick. If every source for a deck fails, its board stays as it was and the sync still
succeeds. `sync --no-ideas` skips this step.

A deck that plays unlike most decks of its commander can learn from commanders that play like it: list
them under `[ideas] commanders` in its profile, with a partner pair as its own list. Their recent
Archidekt decks at the deck's bracket form one pool, and cards at least half the pool plays become up to
10 "adjacent decks" ideas ([ADR 0015](docs/adr/0015-ideas-from-commanders-that-play-alike.md)).

Every change lands in the deck's activity log with who made it (you, Claude, cmdr or Archidekt).
`cmdr board log` shows every deck's log, oldest first; `--since` keeps what is newer. The log is how
Claude catches up on what you decided in the app. `cmdr board show`, `research`, `remove` and `plan` cover
the rest; `cmdr board --help` lists them.

### Desktop app

The app works for any Archidekt account ([ADR 0023](docs/adr/0023-accounts-and-sync-in-the-app.md)).
Without one it opens on **Sign in to Archidekt**: the username and password go to Archidekt's login only,
and the login token is kept in Windows Credential Manager, as `cmdr login` keeps it. Each account has its
own deck folder, remembered in the config's `[workspaces]` table. Your own keeps the folder `cmdr init` set,
and a new account gets a sibling folder named `decks-<username>`. Signing in as a new account syncs its
decks at once. The foot of the deck list shows who is signed in, with **Sync decks** (the same as
`cmdr sync`, card data and ideas included, its progress shown beneath) and **Sign out**, which removes the
saved login. Signing in again finds the same folder.

`cmdr app` opens a window over every deck's board. Pick a deck on the left to see it as a card grid,
grouped by card type with counts and EUR totals. S, M and L above the grid set the card size. Click a
card to see it large (× or Esc closes it). Export, next to the commander's name, copies the saved deck
in TopDeck.gg's decklist format (`~~Commanders~~`, then `~~Mainboard~~`, one `1 Name` a line).
Settings, beside it, edits two lists in the deck's `profile.toml` with autocomplete: the commanders
that play alike (`[ideas] commanders`; "+ partner" on one makes a pair) and the themes to ignore
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

- **Considering**, on the right, lists every idea for the deck: yours, Claude's, the meta's and the
  tools'. Hover over an idea or click it to light the deck cards it could replace. Click one of them to
  queue the swap; once an idea is clicked, any other deck card but the commander can be clicked too.
  × turns the idea down, with a reason if you like: turned-down cards wait under *Rejected* (closed until
  opened) with their reasons, and Reconsider brings one back. Considering, Meta and each Meta section
  fold, and stay as you left them. The suggested targets are the cards you or Claude picked, or else the
  weakest cards in the same role ([ADR 0011](docs/adr/0011-app-deck-grid-and-replacement-targets.md)),
  like for like: a card serving one of the deck's themes or leanings is offered only for an idea serving
  the same ([ADR 0016](docs/adr/0016-replacements-like-for-like.md)).
  An idea with nothing to replace stays on the board but does not show. Each idea's badge says who
  added it, or for a compiled idea where it came from ("meta", "archidekt decks", "adjacent decks").
- **Labels**: the flag in a grid card's top-left corner sets its Archidekt label. Every deck offers your
  account's labels in their colours, read on each signed-in `sync` and before each label change
  ([ADR 0012](docs/adr/0012-card-labels-from-the-archidekt-account.md)). A change is written to
  Archidekt at once.
- **Meta** (bracket 5 decks) lists each key card with the meta cards that stop it, then the meta's combos
  with your answers, each tagged with the meta decks that play them. **Refresh meta** fetches new data
  and puts the meta's staples into Considering. A card you turned down comes back once its meta share
  rises 20 points.
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
uv run --group build pyinstaller --noconfirm --clean --onefile --windowed --name Commander --add-data "src/commander/web;commander/web" src/commander/app_main.py
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

```sh
uv run cmdr toget                                          # every deck's cards still to get
uv run cmdr toget ellivere --json                          # one deck, as JSON
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
| `board.json` | Cards considered and suggested, their research and your decisions | `cmdr board`, the app, `suggest --to-board`, `sync` |
| `activity.jsonl` | Every change to the board, who made it and when, oldest first | Whatever changes the board; never rewritten |

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
| Sample sizes for `meta` | n/a | n/a | `[meta]` table | 60 days, events of 16+, 10 Archidekt decks, 10 commanders, 30 lists each |
| Archidekt login for `apply --save` | `cmdr login` | n/a | n/a | none; the token is kept in Windows Credential Manager |
| App log detail | n/a | `CMDR_LOG_LEVEL` | n/a | `INFO` |

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
| Scryfall (`api.scryfall.com/bulk-data`, files on `data.scryfall.io`; card images from `cards.scryfall.io` in the app) | Card facts, rules text, EUR prices, Tagger tags for roles and profiles, card art | One API call per `sync` for the file list; the files themselves have no rate limit. Scryfall updates prices once a day and asks for bulk files rather than per-card calls for lookups like these. Tag data is community-maintained and can change. See [ADR 0001](docs/adr/0001-scryfall-bulk-card-data.md). |
| Commander Spellbook (`backend.commanderspellbook.com`: `estimate-bracket`, `find-my-combos`) | Bracket estimates, combos, and combos one card short | One request per deck and command, at most four at a time, and none while a cached answer is under 7 days old. Spellbook asks tools to name themselves in the User-Agent, keep to a few calls per user action, stay under about 80 requests a minute, handle 429, and credit and link back to commanderspellbook.com, which `bracket` and `suggest` do in their output. Only card names and quantities are sent. See [ADR 0002](docs/adr/0002-bracket-estimates-from-commander-spellbook.md) and [ADR 0003](docs/adr/0003-suggestions-from-spellbook-and-scryfall.md). |
| EDHTop16 (`edhtop16.com/api/graphql`) | Tournament lists, spice and the most-played commanders for `meta` and `sync`'s ideas | Requests 1.5 s apart, none while a cached answer is under a day old. Its terms offer the API as-is and forbid excessive automated requests, implying endorsement and redistributing its statistics; reports credit it and stay local. See [ADR 0007](docs/adr/0007-meta-from-edhtop16-archidekt-and-spellbook.md). |
| Archidekt deck search (`archidekt.com/api/decks/v3/`) | Other players' recent decks for `meta` and `sync`'s ideas | Searches 2 s apart and a few deck reads per commander, cached for a day; your own decks are left out. Under the same informal permission as the rest of Archidekt's API (ADR 0004). |
| Commander Spellbook (`find-my-combos`, for `meta`) | The combos the most-played commanders' lists run | One request per meta commander, 1 s apart, cached for 7 days. |
| EDHREC (via Scryfall's `edhrec_rank`) | Popularity ranks for `suggest` | Never contacted: the rank comes inside Scryfall's bulk files. EDHREC's terms forbid automated requests to its site. |
