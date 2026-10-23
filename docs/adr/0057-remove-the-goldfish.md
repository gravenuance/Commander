# 0057: Remove the goldfish

**Status:** Accepted · **Date:** 2026-10-24 · Supersedes [ADR 0054](0054-goldfish-a-deck.md) (the goldfish) and
[ADR 0056](0056-goldfish-breaks-ties-between-cut-targets.md) (its tie-break between cut targets)

## Context

The goldfish (ADR 0054, with its amendments) deals and plays a deck 10,000 times by one fixed policy and
reports the opening hands, the commander, the mana, the colour screw and how soon combos and finishers land.
Its figures are only as good as its reading of the cards: each card's rules text is turned into mana, costs,
tutors and effects by recognising phrases, and a card the reading does not follow is either listed under
*Not modelled* or, worse, read as something it is not.

The owner went through the goldfish on his own decks and found the reading too narrow. Many cards are
unread, and others are silently misread, so a figure can look precise and still be wrong. The decks the
reading was tuned on are his own, so on decks of other commanders the figures would differ wildly from what
the cards do. Every amendment since the first release has added more cards and more phrases, and each new deck
shows more. Tools for goldfishing and drawing statistics already exist and are built for that.

## Options

1. **Keep growing the reading.** Each fix makes the next deck better, but the rules text of Magic has no end,
   and the work never reaches the point where a figure can be trusted without checking the cards.
2. **Keep it, but label the figures as rough.** Honest, but a number that is only roughly right does not help
   a decision about one card, and the *Goldfish pick* mark ordered cut targets by it.
3. **Remove it.** The app and the command line do less, and nothing in them claims to know how a deck plays.

## Decision

Option 3, decided by the owner on 2026-10-24 and released as 1.2.1. The code is deleted; it stays in git
history, in the tree at the tag `v1.2.0`.

- `cmdr goldfish` (with `--swap`, `--plan`, `--cuts` and `--json`), the bridge calls `goldfish`,
  `goldfish_swap`, `goldfish_review`, `goldfish_cuts` and `save_finishers`, and the finishers part of
  `settings` are gone.
- The app loses the Goldfish tab, the Goldfish fold of a queued idea, the Goldfish block of Review, the
  *Goldfish pick* mark and the Finishers section of Settings.
- Code that only the goldfish used goes with it: the cached read of a deck's own Spellbook combos
  (`metadata.cached_own_combos`), the dominant-card reading (`ideas.dominant_card_ids`,
  `ideas.dominant_tags`, `DeckReading.matching`), the score on a cut target, and the two-colour hybrid pips
  that `manabase.fixed_pips` kept for it. Fixes the goldfish led to in code that other features use stay
  (the tapped-land reading of the mana base, resolving a name to the real card).

## Consequences

- An idea's cut targets are in score order again (ADR 0036), with no tie-break and no call on selecting a
  tile; the page no longer asks the bridge anything when an idea is selected.
- `profile.toml` loses `[goldfish]` (`finishers`, `not_finishers`). A profile that still has the table, as
  every profile written by 1.2.0 does, reads as before: the table is ignored whatever it holds, and an edit
  of the settings leaves it in the file with the owner's comments. The template no longer writes it. A key
  the profile reader does not know is still an error, so the table is the one retired name it accepts.
- The card database is untouched, so `carddb.SCHEMA_VERSION` stays as it is.
- ADR 0055's one reading of a deck's themes now feeds the previews and the Archidekt tag search only.
- Reviving the goldfish would start from the tree at `v1.2.0` and need a reading of cards that reports what it
  cannot read instead of guessing, and a check against decks other than the owner's before any figure is
  shown or used to order anything.
