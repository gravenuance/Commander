# 0016: Replacements like for like, and what a list leans into

**Status:** Accepted · **Date:** 2026-10-16

## Context

Considered offers each idea up to three deck cards to replace ([ADR 0011](0011-app-deck-grid-and-replacement-targets.md)).
They were the weakest cards in the same role, by meta share and EDHREC rank, so they never looked at
themes. Every Pantlaza idea offered to cut Ghalta the Unstoppable, Dinosaurs on a Spaceship and Palani's
Hatcher: in a Dinosaur deck, the cards that fill no role are mostly Dinosaurs.

The owner also pointed out that a list's own direction is easy to lose. Most Arabella decks go wide with
tokens; the owner's adds untappers and extra combats, and an update should keep that.

## Options

1. **Theme cards last among targets, with a warning:** they could still be offered, after everything else.
2. **Like for like:** a card serving a theme or leaning is offered only for an idea serving one of the
   same, and such swaps come first.

## Decision

Option 2, chosen by the owner.

- A card's themes are the deck's themes ([ADR 0009](0009-themes-from-archidekt-deck-tags-and-categories.md))
  it matches, the profile's named one word at a time ("Dinosaur", "Discover"), plus its leanings.
- **Leanings** compare the deck with the recent Archidekt decks of its commander at its bracket, which
  `sync` already reads. A Scryfall Tagger tag (sub-tags counting for their parents) is a leaning when the
  deck's nonland cards other than the commander hold at least 3 cards with it, at least 2 more and twice
  as many as the peer decks on average, and fewer than a fifth of the peer decks play as many. Tags with
  fewer than 8 or more than 3000 cards, printed cycles and Tagger's trivia (card names, vanilla flavours,
  unique type lines or mana costs) are skipped. A wider tag whose cards are mostly a narrower one's folds
  into it. At most 6, widest gap first; at least 3 peer decks are needed. The thresholds were tuned on
  the owner's 11 decks against 10 recent decks each.
- A leaning holds only nonland cards; `[themes] ignore` in the profile drops one by its tag slug.
- Targets rank by the share of the meta's lists at bracket 5, else of the recent decks, then by EDHREC
  rank. Each target's reason names the theme it shares with the idea.
- `cmdr profile` and the deck's header in the app list the leanings with their cards.

## Consequences

- Pantlaza's Dinosaur ideas swap for Dinosaurs only; its protection ideas no longer offer Akroma's Will.
  Arabella's untappers and extra combats are cut only for ideas that do the same.
- An idea with nothing like it in the deck has no target and leaves Considered: on 2026-10-16 that was
  5 of 154 open ideas (Pantlaza: Chimil, Breath of Fury, Repercussion; Etali: Crop Rotation, Roaming
  Throne). Its board entry stays.
- Leanings found so far include Chatterfang's landfall and extra lands, Glarb's creature sacrifice
  outlets and tutors to the battlefield (its Protean Hulk package), Arabella's untappers and combat ramp,
  and Edgar's +1/+1 counters. A few are noise (Magda's burn-with-set-s-mechanic) and can be ignored.
- The app works the deck's rules out once and keeps them until its snapshot, profile or cached meta
  answers change: the first view of a deck takes about a second, later ones as before.
