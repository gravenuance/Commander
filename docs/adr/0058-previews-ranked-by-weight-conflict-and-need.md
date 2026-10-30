# 0058: Previews ranked by weight, conflict and need

**Status:** Accepted · **Date:** 2026-10-30

## Context

ADR 0055 shows, per deck, the upcoming cards that fit it: those matching its three main themes or wanted by
its commander, ranked by how many distinct features they match, at most 6. Run on the owner's eleven decks
during the Star Trek season, it showed cards that made no sense and left out ones that did:

- **Cards that work against the commander ranked first.** Arabella counts creatures with power 2 or less;
  23 of her 60 fitting previews add +1/+1 counters or a lasting anthem, the first two among them (Captain
  Jean-Luc Picard: "Other creatures you control get +1/+1").
- **Commanders whose wants had no line.** Chatterfang's Tagger tags `synergy-token` and `token-increaser` say
  it wants token makers, but the commander table had no line for them, so only 5 of its 63 fits made tokens.
  Ellivere's `synergy-aura`, Glarb's `high-mana-value-matters` (it casts spells of mana value 4 or more from
  the library) and Vren's rules text (it profits when the opponents' creatures die) were read the same way.
- **Every feature counted one.** "A creature with power 2 or less" (about 9,600 legal cards) counted as much
  as Landfall (about 300), so small creatures crowded Arabella's list.
- **Cards revealed after the weekly card data had no tags.** The Needs of the Many (sacrifice a creature,
  your creatures gain hexproof and indestructible) has Tagger's `protection` tag on Scryfall, but the card data
  was built before it was revealed, so it had no role and matched nothing.
- **A role the deck lacks only broke ties.** A card filling a short role, but matching no theme, never
  showed: Glarb, Etali and Ellivere, short of removal, draw or board wipes, were shown none of the 2 to 33
  previews that do it.
- **6 was too few** for decks with 40 to 60 fitting previews.

## Options

1. **Hand-tune the order per deck in its profile.** Exact, but it moves the work to the owner and fixes
   nothing for the next deck.
2. **Rank by text similarity to the deck's cards with a local embedder.** Catches synergies no table names,
   but needs a model and the GPU in the app, and its picks are hard to explain on a tile.
3. **Extend the tables and weigh the features**: name what each commander wants and what works against it,
   what a theme needs beside its own cards, weigh a feature by how rare it is, let a short role show a card,
   and keep fresh tags for the previews. Every pick still says why it fits.

## Decision

Option 3, decided with the owner on 2026-10-30.

- **More commander wants** (`themewants.COMMANDER_WANTS_BY_TAG`): `synergy-token`, `token-doubler`,
  `token-increaser` and `token-replacer` want token makers (rules text that has you "create" a token, or
  investigate, amass or incubate; a card that has an opponent create tokens is none). `synergy-aura` wants
  Auras and `synergy-equipment` Equipment, as feeders of the theme too. `high-mana-value-matters` without an
  animate tag wants spells of mana value 4 or more. A commander's rules text can want something no tag says
  (`COMMANDER_WANTS_BY_TEXT`): "a creature an opponent controls would die" or "dies" wants creature removal.
- **Cards that work against the commander** (`CONFLICTS_BY_TAG`): `synergy-low-power` is worked against by
  cards that grow creatures for good, the `anthem` and `gives-pp-counters` tags or the text of a lasting
  "creatures you control get +N", a +1/+1 counter, or entering with an extra one; a pump until end of turn is
  none. The owner decided they still show, last, with a "Works against <deck>" chip.
- **What a theme needs beside its own cards** (`SUPPORT_BY_TAG`, previews only): a theme of
  `repeatable-creature-tokens` (Tokens, Go-wide) wants protection for your creatures, and one of
  `sacrifice-outlet`, `free-sacrifice-outlet` or `death-trigger` (Sacrifice, Aristocrats) wants token makers
  for fodder. The theme reading (ADR 0055's amendment) does not count them, so the Archidekt tag search is
  unchanged by them.
- **A short role shows a card.** A preview filling a role the deck runs short of (the suggest targets) shows
  though it matches no theme, and the role counts as a feature. The tile names it among what the card fits
  ("Fits: #Treasure #Ramp").
- **Weights.** A preview scores the sum of its distinct features: the themes' types, keywords, tags and
  wants, the commander's wants and the short roles it fills, each once. A feature weighs the log of all
  Commander-legal cards over those with it (at least 0.1), so Landfall weighs about 4.7 and a creature with
  power 2 or less about 1.2; what the commander wants weighs twice. A want's count is a scan of the legal
  cards, made once per card data build and kept with the build's other counts. Then, as before: wanted by
  the commander, filling a short role, coloured before colourless, sharing tags with the deck, the mana value
  nearest its average, the name.
- **Fresh tags for previews.** When the daily probe finds previews, it downloads that day's Oracle Tags file
  (about 6 MB, from `data.scryfall.io`, after one paced `/bulk-data` request) and keeps each preview's tags,
  with every tag above them, and its roles in `previews.json`, now version 3; a version 2 file is ignored and
  the next probe replaces it. The ranking reads them beside the card data's. A failed tag download keeps the
  cards found and the last tags of those still previewed, and is a sync warning. Off season nothing more is
  fetched. The Scryfall client keeps the file list and the parsed tags for the sync, so on a day the card
  data is rebuilt the probe takes the rebuild's tags rather than downloading them again.
- **The cap** is 12.

## Consequences

- On the owner's decks with the fresh tags: The Needs of the Many is Arabella's first preview, and 17
  cards that grow her creatures go last with the warning; Vren is shown removal and edicts, Glarb board
  wipes, sacrifice cards and big spells, Chatterfang landfall and token makers, Ellivere Auras and the board
  wipes and draw it lacks.
- A deck with no theme tags, or none a preview matches, now shows the previews filling its short roles.
- In a deck short of a rare role (board wipes weigh about 3.7), cards filling it can rank above single-theme
  cards: Etali's and Pantlaza's lists open with board wipes. Revisit the role weight if the owner finds that
  too much after the season.
- During a season the probe downloads about 6 MB a day more, and makes one more API request, except on a
  card data rebuild day, when it shares the rebuild's.
- The first ranking after a card data build, in each run of the app or CLI, scans the legal cards once for
  the wants' counts (0.9 s for six wants on the owner's machine); later rankings read them from the build's
  counts.
- The tables are short and were checked on 2026-10-30 against the card data; a commander tag or theme tag
  with no line keeps matching only what ADR 0055 matched.
