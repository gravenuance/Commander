# 0021: Why a deck's bracket estimate disagrees, by Spellbook's own rules

**Status:** Accepted · **Date:** 2026-10-17

## Context

A deck has up to three brackets: the one set on Archidekt, the profile's `bracket_ceiling` (ADR 0005), and
Commander Spellbook's estimate (ADR 0002). `bracket` flagged a deck whose Archidekt bracket was below the
estimate, but did not say which cards caused it, and ignored the ceiling. The owner asked for the cards that
cause a disagreement to be highlighted, before deciding where each deck belongs.

Spellbook's backend is open source (MIT). Its `estimate_bracket` (`backend/spellbook/models/variant.py`,
read 2026-10-17) derives the deck's tag from flags its `estimate-bracket` answer already returns. Each card
carries `banned`, `gameChanger`, `massLandDenial` and `extraTurn`. Each combo carries `speed`, `relevant`,
`borderlineRelevant`, `definitelyTwoCard`, `arguablyTwoCard` and flags for land denial, extra turns, locks,
skipped turns and control of opponents. The rules, highest tag first:

| Tag | Any of |
|---|---|
| Banned | a banned card |
| Ruthless | more than 3 game changers; 2 or more extra-turn cards; mass land denial; a combo with extra turns, land denial or control of every opponent; a relevant, definitely two-card combo at speed 4+ |
| Spicy | at speed 4+, a relevant combo arguably of two cards or a borderline one definitely of two; a lock, skipped turns or control of an opponent |
| Powerful | a relevant, definitely two-card combo at speed 3+; a game changer |
| Oddball | a borderline, arguably two-card combo at speed 3+ |
| Core | an extra-turn card; a relevant, definitely two-card combo at speed 2+ |

Speed runs from the mana a combo needs: 5 for none, 4 up to 4 mana, 3 up to 6, 2 up to 8, 1 beyond, plus
one when that mana is no sure minimum.

## Options

1. **Our own rules** from the official bracket guidance. They would drift from the estimate we show, so a
   cause list could fail to explain the tag beside it.
2. **Spellbook's rules, mirrored** over the flags in its answer. Each cause is exactly what lifted the tag.
3. **Highlight every flagged card and two-card combo** without rules. Simple, but a bracket 3 deck would see
   its one game changer flagged as though it were the problem.

## Decision

Option 2, the owner's choice. `bracketcheck.estimate_causes` mirrors the rules, and a combo is named once,
under the highest rule it meets. `check_bracket` takes the Archidekt bracket, the ceiling and the estimate,
and returns:

- **Notes**, one line each: the estimate above the Archidekt bracket, the estimate above the ceiling, a
  ceiling different from the Archidekt bracket, a deck set above the highest bracket its tag fits (Spicy 3-4,
  Oddball 2-3), and cards that are not legal.
- **Causes:** those whose tag starts above the lower of the Archidekt bracket and the ceiling. Banned cards
  are always a cause.
- **A safeguard:** when the mirrored rules give a different tag from Spellbook's, a note says the causes may
  be incomplete and a warning is logged. That means Spellbook changed its rules.

Tutors are context only, never a cause. Spellbook doesn't count them, and the owner agreed. Third-party
guides report that the October 2025 bracket update dropped the tutor limits; the official page was not read.

The estimate answer now keeps each combo's speed and flags and the flagged cards (cache version 3, so each
deck is fetched once more). `cmdr bracket` prints the notes and causes, its table marks a deck with `!`
whenever there is a note, and `--json` carries both. The app's `bracket` bridge call reads the same cache and
asks Spellbook only when the answer is missing or a week old. The page shows an Estimate chip, amber with
the notes and causes behind it when something disagrees, and an amber badge on each causing card.

## Consequences

- On 2026-10-17 the mirrored rules gave Spellbook's tag for all 11 decks. Two decks have causes: Arabella
  (set 3, estimate 4+: two fast Kiki-Jiki combos) and Ashling (set 2, estimate 3-4: one fast combo of about
  two cards). Pantlaza has only a note: its ceiling 3 is above its Archidekt bracket 2.
- A Spellbook rule change shows up as the safeguard note rather than as a wrong explanation. The rules then
  need reading again and the mirror updating.
- The app makes a Spellbook request the first time it opens a deck with no cached answer under a week old.
  That is one request per deck and week, within Spellbook's terms (ADR 0002).
