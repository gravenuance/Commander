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
