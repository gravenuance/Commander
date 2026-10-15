# 0005: Deck profiles steer suggestions

**Status:** Accepted · **Date:** 2026-10-15

## Context

`suggest` (ADR 0003) judged every deck by the same generic targets: 10 ramp, 10 draw, 8 removal, 2 board
wipes, and cuts were the cards no Scryfall tag gave a role. Tried on two real decks, that failed in
opposite ways. Ellivere is a cEDH stax deck whose commander is its draw engine: it was told to add Mind
Stone, Solemn Simulacrum and board wipes, and to cut Suppression Field, Stony Silence and Containment
Priest, the stax core it is built around. Pantlaza is a bracket 2-3 Dinosaur deck that avoids flicker on
purpose: it was offered Vandalblast and nothing about Dinosaurs, and its cut list named Kinjalli's Caller
and Cream of the Crop, which the user files under Ramp and Draw on Archidekt.

What a deck is for (its archetype, what it must never run, what is untouchable) lives in the user's head
and in free-text `notes.md`, which a program can't read reliably. Scryfall's Tagger tags (bulk file
`oracle_tags`) describe what cards do in community-maintained terms: `hatebear`, `prevent-cast`,
`flicker` with sub-tags, `typal-dinosaur`. The card database kept only the ten role tags.

## Options

- **Free text only:** keep everything in `notes.md` and leave the judgement to whoever reads it. No new
  format, but `suggest` stays generic.
- **A structured section inside `notes.md`:** one file per deck, but the program would have to parse and
  rewrite a file the user writes prose in.
- **A separate hand-kept `profile.toml` per deck folder:** a small, validated TOML file next to
  `notes.md`, read by `suggest`. One more file, but nothing the user writes freely is ever parsed.
- **Per-commander data from EDHREC:** what similar decks play. Ruled out by EDHREC's terms (ADR 0003).

## Decision

A `profile.toml` in each deck folder, written by the user (`cmdr profile <deck> --init` starts it), with:
archetype, bracket ceiling, price limit, role targets, themes (type-line words, keywords, Tagger tags),
exclusions (the same three plus card names) and cards or categories kept from the cut list. It is
versioned (`version = 1`) with a migration seam, and every field is checked on load with a message
naming the file and key. Tags are written as slugs and resolved to Tagger ids against the card data;
unknown slugs are reported, not fatal.

The card database (schema 4) now stores every Tagger oracle tag with its parent-child edges, and each
card's rules text, keywords and power/toughness. Theme matching is SQL over those (a recursive query for
sub-tags), so `CardInfo` stays unchanged.

Two rules came out of trying it on the real decks:

- A card fills a role by its tag **or** by the deck's own Archidekt category of that name, for targets and
  for cuts alike. The user's filing is a deliberate judgement and outranks a community tag.
- Cards matching a theme are listed **last** among cut candidates, not left out. Leaving them out emptied
  Pantlaza's cut list, because nearly every card in a typal deck matches its theme. Untouchable cards go in
  `[keep]` (Ellivere keeps its Stax category).

## Consequences

- Ellivere's theme ideas became Grand Abolisher, Kutzil, Voice of Victory, Destiny Spinner and Charismatic
  Conqueror, and no stax piece is offered as a cut. Pantlaza's became Ghalta, Primal Hunger, Chimil and
  Etali, Primal Conqueror, with flicker cards excluded.
- The profile is a second place to keep in step with the deck. Tagger slugs can be renamed or removed;
  `cmdr profile` and `suggest` warn about unknown ones, and the warning is the cue to update the profile.
- Ranking is still by global EDHREC rank within the filters. A deck-specific popularity signal (for cEDH,
  tournament results; see the EDHTop16 item in the project backlog) would sharpen it and should be
  revisited once its terms are checked.
- The card database grew to about 74 MB and rebuilds on the next `sync` after the schema change.
