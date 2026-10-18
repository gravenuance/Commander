# 0017: Key cards found from the deck

**Status:** Accepted · **Date:** 2026-10-16

## Context

`cmdr meta` and the app's Meta section check the meta's answers against a deck's key cards
([ADR 0007](0007-meta-from-edhtop16-archidekt-and-spellbook.md)). Until now those were the commander and
whatever the profile's `[keep]` named, so most decks showed only their commander. The owner pointed out
what that misses: Glarb plays Protean Hulk, which a whole archetype is named after, and Magda plays
Gogo, Mysterious Mime for its ladder combo. Neither was named anywhere.

## Options

1. **Name them in the profile:** accurate, but every deck needs upkeep, and nothing is found by itself.
2. **Found each time, the profile adjusting:** rules over the deck and its peers, each with a reason.

Rules tried on the owner's four bracket 5 decks and rejected:

- *Cards the commander's lists play and the rest of the meta does not:* Magda got 50, its whole archetype.
- *Leaning cards no peer deck plays:* noisy (Vren's cantrips, Bello's Leylines).
- *Peer deck names that name a card:* rarely (only "Magda Turbo Clock" and "Croak of Doomsday" did).
- *Spellbook's almost-included combos:* none hold Hulk or Gogo.

## Decision

Option 2, chosen by the owner. A nonland deck card is key when any of these holds, and each one it meets
is a reason shown with it:

- It is the commander.
- It is a piece of one of the deck's own Spellbook combos.
- The owner files it under a category naming the plan: one holding combo, wincon, finisher, payoff,
  keycard or keypiece, ignoring case, spaces and punctuation ("Combo Pieces", "WinConEnablers").
- At least 2 of the recent Archidekt decks with the commander file it under such a category. The peer
  read keeps those cards in its cache. Gogo: 3 of 10 Magda decks.
- It costs at least 5 and at least two standard deviations above the mean mana value of the deck's
  nonland cards other than the commander (the deviation is the population's, not a sample's). Cards Tagger tags as cast for less (alternative-cost, cheaper-than-mv, manaless-value)
  are left out, so pitch spells and split cards don't count. Hulk costs 7 against Glarb's 1.8 average.
- The profile keeps it under `[keep]`.

`[key_cards] ignore` in the profile drops a card by name. The commander comes first, then the cards
with the most reasons.

## Consequences

- On 2026-10-16:
  - **Magda:** 19 key cards, including Gogo, Clock of Omens and Portal to Phyrexia.
  - **Glarb:** 8, including Protean Hulk and its Thassa's Oracle lines.
  - **Etali:** 15, its Dualcaster and Breach lines and its costly cheat targets.
  - **Ellivere:** 20, mostly its profile's stax pieces, plus both Elesh Norns.
- Peer caches written before this have no categories, and count none until their next refresh.
- Key cards protect nothing from cuts. Only `[keep]` does that, as before.
