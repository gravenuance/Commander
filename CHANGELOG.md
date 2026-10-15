# Changelog

## Unreleased

### Added
- `cmdr init`, `cmdr sync` and `cmdr list`: save your Archidekt decks as `deck.json`, `list.md` and `notes.md`
  under `decks/`, and report what changed since the last sync.
- `cmdr analyze`: mana curve, card types, colour pips, game changers and role counts (ramp, draw, removal,
  board wipes, tutors, protection, recursion, counterspells), from your Archidekt categories and from
  Scryfall tags side by side.
- `cmdr price`: deck value in EUR (Cardmarket prices via Scryfall) for the printings in each deck and for
  the cheapest printings.
- `cmdr sync` keeps a local card database from Scryfall's daily bulk files up to date; `--no-cards` skips it.
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

### Changed
- The card database now stores EDHREC popularity ranks and marks non-tournament printings (gold or silver
  borders, oversized, memorabilia), so the first `cmdr sync` after updating rebuilds it. Cheapest prices
  and the printing `apply` picks leave those printings out.
