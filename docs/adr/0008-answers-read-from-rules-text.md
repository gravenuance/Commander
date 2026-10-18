# 0008: Combo and threat answers read from rules text, not only tags

**Status:** Accepted · **Date:** 2026-10-16

## Context

ADR 0007 matched answers to combo pieces and key pieces by Tagger tags alone: a tag kind (`removal-bounce`,
`removal-nonland`, `sweeper`) stood for a fixed reach, such as any nonland permanent or any creature. The
first live runs on 2026-10-16 showed what that gets wrong:

- Dead // Gone carries `removal-bounce`, so it was listed against Underworld Breach and Lion's Eye
  Diamond; Gone only returns a creature.
- Pyroblast and Red Elemental Blast carry `removal-nonland`, so they "removed" pieces that aren't blue.
- Damage ignored toughness: Bloodfire Dwarf, Dead and Lightning Bolt answered a 7/8 Hullbreaker Horror, and
  Tinder Wall, which only hits a creature it blocks, counted as removal.
- Vexing Bauble counted against Thassa's Oracle, though it only counters spells cast without mana.
- Stax matching read too little: Torpor Orb answered Hullbreaker because the combo's results say "ETB",
  though nothing triggers on entering, and Talon Gates, a land. Grafdigger's Cage answered Talon Gates put
  in from the hand, and Eternal Witness returning Snap to the hand, because Tagger files it as graveyard hate.

The card database held colour identity but not colours, which calls a Talisman blue.

## Options

1. **More tags.** Map finer Tagger tags (`hate-blue`, `burn-creature`) to reaches. Cheap, but tags carry
   no amounts or conditions, so damage against toughness and "mana value 1 or greater" stay out of reach.
2. **Read each answer's rules text into modes**, with tags still deciding which cards are answers. Handles
   colours, toughness and conditions, but a phrase reader misses unusual wording.
3. **Hand-kept answer lists per combo.** Exact, but goes stale as the meta moves, and nobody keeps it.

## Decision

Option 2. `commander.answers` reads each sentence of a removal or counter card into a mode: an effect
(remove, damage N, toughness loss N, counter), the types its target names, colour limits ("blue",
"if it's blue", "nonblack"), and conditions (mana value, power or toughness N or less, no mana spent, no
flying). A mode is checked against a piece's type, colours, toughness, flying and mana value from the card
database. Combat-only targets, cards in graveyards and exile-then-return flickers answer nothing. A fact the
data lacks rules nothing out. A card with no sentence the reader follows keeps its tags' reach,
so nothing silently drops out. An edict is read from its text and carries the catch that the opponent
chooses what to sacrifice; an upkeep tax such as Kataki's answers nothing, since it is paid before the
combo.

Stax matching reads the steps more closely: an enter-trigger lock needs a "When X enters" step (or Spellbook's
"X enters the battlefield, causing …") on a card of the type the lock names; a "can't enter" lock covers
only the zones and types its sentence names; graveyard hate needs text that exiles from graveyards. Pieces
the combo casts for {0}, or that cost nothing, count as cast free.

The card database stores each card's own colours (schema 5), read from Scryfall's `colors`, else the front
face's, else the coloured symbols of the mana cost.

## Consequences

- Answer lists are shorter and closer to what each card can do; Etali's lack of stax and counters
  against Breach loops shows up instead of hiding behind wrong removal.
- The reader is phrase-based. New wording can slip through it, and such a card falls back to its tags'
  reach, which can be too wide. Add a test with the card's real text when one turns up.
- The next `sync` rebuilds the card database.
