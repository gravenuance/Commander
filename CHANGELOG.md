# Changelog

## Unreleased

### Added
- Cards to get (ADR 0010): cards a save brings into a deck get the Archidekt label "Don't Have";
  `cmdr toget` lists each deck's Don't Have and Getting cards and marks one `--ordered` (Getting) or
  `--arrived` (no label) on Archidekt at once. The app's bridge has the same (`toGet` in the deck view,
  `mark`). Deck snapshots keep each card's label (version 3; the next `sync` fetches each deck once).

- App redesign (ADR 0011): the deck shows as a card grid by card type, with counts and EUR totals.
  Considered holds every idea that has a card to replace; hovering or clicking one lights its targets in
  the grid, a click on a target queues the swap, and × turns the idea down. Targets are the ones you or
  Claude picked, else the weakest cards in the same role by meta share and EDHREC rank. Each grid card
  has a corner flag that sets its Archidekt label. The Meta section (bracket 5) shows key cards with the
  meta cards that stop them and meta combos with your answers, tagged with the meta decks playing them,
  and a refresh puts the meta's staples on the board. A turned-down card returns when its meta share
  rises 20 points. Deck snapshots keep label colours (version 4; the next `sync` fetches each deck once).

- Card labels come from your Archidekt account (ADR 0012): every deck offers the account's labels in
  their colours, and Don't Have and Getting are written in the account's colours. A signed-in `sync`
  keeps the list in `labels.json` beside the deck folders; each label change and save reads it first.

- `cmdr meta` and the app's Meta section check every bracket 5 deck's own combos against the meta (ADR 0013):
  each combo from Commander Spellbook with the cards the top commanders' lists commonly play that stop it
  (stax, removal, counters), tagged with the commanders playing them. Etali's Food Chain loop, Glarb's
  Thassa's Oracle lines and Magda's artifact loops now show what threatens them.

- `cmdr sync` fills Considered from other players (ADR 0014): the staples each deck misses from its
  commander's EDHTop16 lists (bracket 5) and from the most-viewed recent Archidekt decks at its bracket,
  up to 10 per source, only cards with something to replace. EDHREC is never asked. The app's badge
  shows where an idea came from. `sync --no-ideas` skips it; `--json` lists the new ideas per deck.

### Fixed
- Combo answers read locks more closely (ADR 0013): Grafdigger's Cage stops casting from graveyards and
  libraries only, not from exile; Deafening Silence and Ranger-Captain of Eos no longer stop a loop that
  casts only creatures; and a lock that holds only during its owner's turn (Grand Abolisher) answers no
  combo. These apply to the meta combos' answers from your deck too.
- Desktop app: the "Thin data" and status badges no longer float at the bottom of the window.

### Changed
- Desktop app: the Suggested and Decided lists are gone (suggestions join Considered; decided cards
  leave the app), the activity column folds to a rail, and ideas from `suggest` no longer all offer the
  same generic cuts.
- Desktop app: the open Meta panel updates by itself when `cmdr meta` (or anything else) saves new meta
  answers to the cache, instead of only when the panel is opened or the deck changes.
- `cmdr meta --to-board` and the app's Refresh meta add the same ideas as `sync`: at most 10 per source,
  each with a deck card to replace.

## 0.3.0 - 2026-10-16

The cEDH meta, answers read from rules text, and deck themes from Archidekt.

### Added
- Deck themes from Archidekt: `suggest` reads each deck's deck tags and its own categories as themes, beside
  the profile's (ADR 0009). Ideas name the themes they fit, themed ideas that fit more of them come first,
  and `cmdr profile` lists every theme with its source. `[themes] ignore` in a profile drops one. Deck
  snapshots keep the deck tags (version 2; the next `sync` fetches each deck once), and `sync` notices
  changed deck tags, which Archidekt makes without changing a deck's edit time. Deck tags that say how a
  deck plays (cEDH, Tempo, Commander Matters, ETB Effects, Draw) make no theme; `cmdr profile` lists them
  as such.
- `cmdr meta` for bracket 5 decks: compares a deck with its commander's performing EDHTop16 lists from the
  last two months and the most-viewed recent Archidekt decks, and lists the staples it misses, spicy picks,
  its own picks, the most-played commanders' answers to its key pieces, and the meta's most-played combos
  (from Commander Spellbook) with the deck's answers: stax pieces, removal and counterspells. `--card`
  checks one card, `--json` prints the report, `--to-board` puts the missing staples on the deck's board,
  and with no deck it shows every bracket 5 deck side by side. Sample sizes are set in the config's `[meta]`
  table; answers are cached for a day, combos for a week.
- `cmdr meta` answers come from each card's rules text as well as its tags: bounce, damage and colour hosers
  reach only what they can hit (types, colours, toughness, mana value), counters follow their conditions, and
  stax pieces must match what the combo's steps do (ADR 0008). The card database stores card colours (schema 5);
  the next `sync` rebuilds it.
- A counter answers a storm spell such as Brain Freeze only when it meets every copy (Flusterstorm,
  Mindbreak Trap); an {X} piece such as Walking Ballista no longer counts as cast free; and a tag alone no
  longer makes an instant or sorcery a stax piece (Mindbreak Trap is a counter, Silence still a lock).
- Meta combo answers name every piece a card reaches ("removes Underworld Breach or Lion's Eye Diamond") and
  show their catch in the CLI, the JSON (`catch`) and the app: soft counters ("unless they pay {2}"),
  Counterbalance's reveal, and edicts, which are now read from their text ("they choose what to sacrifice").
  Kataki's upkeep tax no longer counts as removal, and graveyard hate must seal graveyards, exile whole ones
  or keep exiling from an activated ability; Dawnbringer Cleric's one-card exile no longer counts.
- Desktop app: a Meta panel per deck with the same report, a Meta combos table included; it opens from
  cache and fetches on Refresh meta.

## 0.2.0 - 2026-10-15

Deck profiles, deck boards and the desktop app.

### Added
- `cmdr card <name>...`: a card's rules text, type, power/toughness, price, EDHREC rank, roles and Scryfall
  Tagger tags; `cmdr tags <text>` finds Tagger tags by name with their card counts.
- Deck profiles: a hand-kept `profile.toml` in a deck's folder tells `suggest` what the deck wants: its
  archetype, a bracket ceiling, a price limit, role targets, themes (type-line words, keywords, Tagger tags),
  cards and themes never to suggest, and cards or categories never to offer as cuts. `cmdr profile <deck>`
  shows it and flags unknown tags; `--init` writes a commented template.
- `cmdr suggest` lists the most played cards matching a deck's themes, ranks themed ideas first within each
  role, and lists themed deck cards last among the cuts.
- Deck boards: `cmdr board` keeps the cards considered or suggested for a deck in its folder (`board.json`),
  with the research on each (why it fits, which deck cards it could replace), decisions (add with a chosen
  cut, turn down, reopen) and an activity log (`activity.jsonl`) of every change and who made it.
  `cmdr board log` catches up on changes; `cmdr board plan` prints the queued cards as a plan;
  `cmdr board commit` previews them against Archidekt and `--save` makes them.
- `cmdr suggest <deck> --to-board` puts its ideas on the board as suggested, with the deck's cut candidates as
  cards to replace; cards turned down on the board are never suggested again.
- Desktop app: `cmdr app`, or `Commander.exe` built with PyInstaller, opens a window over every deck's
  board (WebView2 through pywebview; no web server). Propose cards with name search, pick the card to cut,
  add or turn down, review the queued changes against Archidekt and save them; it follows changes made from
  the CLI or by Claude as they land. It logs to `%LOCALAPPDATA%\Commander\logs\commander.log`.
- Sideboard and Maybeboard as "considering": `cmdr sync` puts the cards you keep there on Archidekt on the
  deck's board as considered. Adding one moves its own entry into the deck, printing and all. A card cut on
  the board moves to the Maybeboard instead of being deleted, keeping its categories behind it, and comes
  back on the board as considered, so a swap can be weighed the other way round. Turning down a Sideboard
  or Maybeboard card takes it off there with the next save.
- Plan files: a remove line takes `maybeboard = true` to move the card instead of deleting it, and
  `drop = [...]` takes cards off the Sideboard or Maybeboard.

### Changed
- The card database (schema 4) keeps each card's rules text, keywords and power/toughness, and every Tagger
  oracle tag with its sub-tags, not only the role tags. The next `cmdr sync` rebuilds it.
- `cmdr suggest` counts a card towards a role when its tags give it the role or the deck files it under a
  category of that name, so cards you filed under Ramp or Draw are no longer offered as cuts.

## 0.1.0 - 2026-10-15

First version.

### Added
- `cmdr init`, `cmdr sync` and `cmdr list`: save your Archidekt decks as `deck.json`, `list.md` and `notes.md`
  under `decks/`, and report what changed since the last sync.
- `cmdr analyze`: mana curve, card types, colour pips, game changers and role counts (ramp, draw, removal,
  board wipes, tutors, protection, recursion, counterspells), from your Archidekt categories and from
  Scryfall tags side by side.
- `cmdr price`: deck value in EUR (Cardmarket prices via Scryfall) for the printings in each deck and for
  the cheapest printings, leaving out non-tournament ones (gold or silver borders, oversized, memorabilia).
- `cmdr sync` keeps a local card database from Scryfall's daily bulk files up to date, rebuilding it when its
  format changes; `--no-cards` skips it.
- `cmdr bracket`: Commander Spellbook's bracket estimate for each deck beside the bracket set on Archidekt,
  with its combos, plus the deck's game changers, tutors, mass land denial and extra-turn cards. Answers are
  cached for 7 days.
- `cmdr suggest`: per deck, the cards that would complete a combo it is one card short of (Commander
  Spellbook), the most played cards for roles below target (Scryfall tags, EDHREC ranks via Scryfall), and
  cards to consider cutting, with EUR prices. Ideas that would lift a deck above its Archidekt bracket are
  marked; `--within-bracket` hides them and `--max-price` caps the price. Role targets can be set in the
  config file's `[targets]` table.
- `cmdr apply`: preview a plan file's cards to add and remove against the deck on Archidekt, and with
  `--save` make the changes there, after saving a snapshot of the deck on Archidekt. Plans state the end
  result, so applying one twice is safe.
- `cmdr login` and `cmdr logout`: keep or remove the Archidekt login token for `apply --save` in Windows
  Credential Manager; the password is never stored.
