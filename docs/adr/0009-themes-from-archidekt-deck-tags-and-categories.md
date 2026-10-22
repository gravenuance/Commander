# 0009: Deck themes read from Archidekt deck tags and categories

**Status:** Accepted · **Date:** 2026-10-16

## Context

`suggest` ranks ideas by a deck's themes, but only a hand-written `profile.toml` ([ADR 0005](0005-deck-profiles.md))
could name them, and only two of the eleven decks had one. The owner already describes every deck on
Archidekt in two ways: deck tags from Archidekt's fixed list (at most ten per deck: Dwarves, Landfall,
Stax, cEDH...) and card categories, many of them their own (Leyline, Sac Outlet, Artifact Dwarf).
Scryfall's bulk data and Tagger tags, already in the card database, can turn either into the same
search as `otag:hatebear order:edhrec`, with no extra requests.

The owner chose (2026-10-16): derived themes merge with the profile and the profile wins; categories
become themes by what their cards share; deck tags map by name with a small override table.

## Options

1. **Profiles only.** Nothing new to get wrong, but most decks stay without themes.
2. **Map deck tags and category names by name alone.** Simple, but a category name is the owner's own
   word ("Counters" means counterspells in one deck and +1/+1 counters in another), and many names match
   no tag.
3. **Deck tags by name, categories by their cards.** A category stands for the Tagger tags and subtypes
   most of its cards share but few other cards have.

## Decision

Option 3, in `commander.deckthemes`.

- **Deck tags** map to a type word (Dwarves → Dwarf), a keyword (Landfall), and Tagger tags with the
  same slug or its `typal-`/`synergy-` forms, unless `OVERRIDES` names them (Stax, Aristocrats,
  +1/+1 Counters...). Play styles (cEDH, Tempo), roles (Draw, Ramp) and card types (Creatures) are too
  broad to pick cards by and make no theme. A tag nothing matches is reported, not guessed.
- **Categories** with at least five known cards, other than roles, card types and deck parts, get a
  greedy cover. Each step takes the feature that adds the most cards, squared, times ln(lift). Lift is
  how much commoner the feature is in the category than among all Commander-legal cards. A feature
  needs a lift of at least 8, at least 20% of the category and at least two new cards. The cover stops
  at three features or 70% of the category, and stands for nothing below 50%. A card belongs to every
  tag above its own tags. Trivia tags (symmetrical, alliteration, cycle-...) and the umbrella `typal`
  never count. What the deck tags already name, and the tags above theirs, is left out, so a category
  in a typal deck says something besides the tribe. A category the deck tags fully explain is skipped
  quietly.
- **Merging:** the profile's `[themes]` come first, then the derived ones, each value once.
  `[exclude]` removes matching parts of a derived theme. The new `[themes] ignore = [...]` drops derived
  themes by deck tag or category name.
- **Relevance:** each idea names the themes it fits ("fits Stax, Hatebears"). Themed ideas that fit more
  of the deck's themes come first, then by popularity, because popularity alone let staples of a broad
  theme crowd out the deck's own (Pantlaza showed no Dinosaurs).
- **Sync:** snapshots keep the deck tags (snapshot version 2). Archidekt keeps a deck's `updatedAt` when
  only its tags change, so sync compares the tags in the deck list too.

The thresholds were tuned on the owner's eleven decks against what their categories hold.

## Consequences

- Every deck gets themes from what its owner already wrote on Archidekt; `cmdr profile` lists each one,
  where it came from, and what was left out.
- The cover is a heuristic. A category can come out thin (Arabella's Tokens: attacking-matters,
  synergy-commander); `ignore` drops it.
- Tagger has no tag for some deck tags. ETB Effects matches nothing and is reported. The override table
  needs a line when a new deck tag misses.
- Override slugs can go stale as Tagger changes; `suggest` warns about a "deck theme tag" Tagger lacks.
- Each `suggest` and `profile` run spends about 0.1 to 0.7 s per deck on the derivation.

## Amendment (2026-10-22): payoff tags and the dominant tags

`OVERRIDES` still maps a deck tag to the Tagger tags of its payoffs (Artifacts → `synergy-artifact`), and
`merge_themes` and everything `suggest` does with themes are unchanged. The cards that feed such a tag, the
commander's own tags, and which three deck tags are dominant (none that match no deck card, tags with one
spec as one) are in ADR 0055's amendment of this date; `commander.themereading` holds the reading.
