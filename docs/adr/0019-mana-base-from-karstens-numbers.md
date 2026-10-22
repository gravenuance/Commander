# 0019: The mana base, checked against Frank Karsten's numbers

**Status:** Accepted · **Date:** 2026-10-16

## Context

The owner asked for a Mana tab next to Activity that analyses each deck's mana base. They asked for it to
rest on researched best practice, not general knowledge. The most cited published work is Frank Karsten's
(Pro Tour Hall of Fame; his tables are the usual reference). It has four articles on TCGplayer, read in
full through TCGplayer's content API, since the pages render in the browser:

- **S1:** [How Many Sources Do You Need to Consistently Cast Your Spells? A 2022 Update](https://www.tcgplayer.com/content/article/How-Many-Sources-Do-You-Need-to-Consistently-Cast-Your-Spells-A-2022-Update/dc23a7d2-0a16-4c0b-ad36-586fcca03ad8/), 2022-08-02.
- **S2:** [How Many Lands Do You Need in Your Deck? An Updated Analysis](https://www.tcgplayer.com/content/article/How-Many-Lands-Do-You-Need-in-Your-Deck-An-Updated-Analysis/cd1c1a24-d439-4a8e-b369-b936edb0b38a/), 2022-07-29.
- **S3:** [What's an Optimal Mana Curve and Land/Ramp Count for Commander?](https://www.tcgplayer.com/content/article/What-s-an-Optimal-Mana-Curve-and-Land-Ramp-Count-for-Commander/e22caad1-b04b-4f8a-951b-a41e9f08da14/), 2022-07-15.
- **S4:** [How to Build Commander Mana Curves: Game Length, Ramp Cost and Competitiveness](https://www.tcgplayer.com/content/article/How-to-Build-Commander-Mana-Curves-Game-Length-Ramp-Cost-and-Competitiveness/50566e8d-bc0b-457a-bffb-dbb1d5872b7c/), 2023-08-11.

### Land count (S2)

- **Formula:** lands = 31.42 + 3.13 × average mana value − 0.28 × cheap spells.
  - The average is over the nonland cards.
  - Cheap spells are the cheap card draw and cheap mana ramp spells, by his rules-text definitions. Mostly
    that means mana value 2 or less with set phrases ("draw a card", "add", a land search), plus anything
    that cycles for one mana.
  - A land/spell double-faced card counts as 0.38 of a land, or 0.74 when mythic.
- **How it was made:** fitted on 95,143 winning 60-card tournament decks (R² 0.395), then ported to 99
  cards with an adjustment for Commander's free mulligan and first-turn draw.
- **His caveats:** it is "not perfect", and Commander results were hard to find.

### Colour sources (S1)

- **The table:** a 99-card table of how many sources of a colour a spell needs to be cast on curve. The
  condition is having enough sources by turn M at 89 + M % (M = mana value), given enough lands. Examples:
  one pip at 1 mana needs 19; 2CC needs 26; CC needs 30; CCC needs 36.
- **Its assumptions:** 41 lands. For other counts he scales the table with the land count.
- **Gold cards:** each colour is worked out separately, then one is added to each.
- **Partial sources:**
  - mana dorks: ½ of a source, for spells of 2 mana or more;
  - mana rocks: ¾, for 3 or more;
  - 2-mana land ramp: ¾, for 3 or more;
  - 3-mana ramp: ½, for 5 or more;
  - Exotic Orchard: ¾;
  - Fellwar Stone: ½;
  - Fabled Passage and Pathways: ⅔ in decks of three or more colours.
- **Fetch lands:** a full source of every colour they can fetch.
- **Turn one:** only untapped lands count. A land that enters tapped only sometimes is about a quarter of a
  tap-land.
- **Land/spell cards as sources:** 0.8, or 1 when mythic.
- **Alternative costs:** spells cast that way are left out when they're never hard-cast.

### Commander curves (S3, S4)

- **Casual decks:** with a 2–3 mana commander, 42 lands and Sol Ring do best. With a 4–6 mana commander,
  38–39 lands and 7–9 rocks.
- **Short games:** 38 lands.
- **Not for cEDH:** both articles say their models don't fit cEDH, where decks run rituals and win early.

## Options

1. **Karsten for every deck.**
2. **Karsten, with lists of the same commander alongside:** the commander's EDHTop16 top-cut lists at
   bracket 5, and the recent Archidekt decks with the same commander and bracket below it.

## Decision

Option 2, chosen by the owner.

### The tab

The app's right-hand rail has two tabs, Activity and Mana. The Mana tab shows three sections.

- **Lands:**
  - the deck's land count, with land/spell cards weighted;
  - the median and range of the other lists, and their nonland mana cards (rocks, dorks, rituals) beside
    the deck's own;
  - Karsten's formula with its inputs. At bracket 5 it is marked as made for casual games, and the
    tournament lists are named the better guide.
- **Colour sources:** per colour, the sources from lands, and every card short of what the table asks.
  - Each card is checked against the sources that count for it, scaled to the deck's land count.
  - The commander is included.
  - Gold cards get the extra source.
- **Turn one:** one-drops short of untapped lands, and how many lands enter tapped.

### Inputs and what is left out

- The card data now keeps the mana each card makes and its rarity (schema 6).
- Peer caches keep counts above one, so basic lands are counted. Older caches show no comparison until
  their next refresh.
- Hybrid, Phyrexian and X pips are left out of colour needs. So are cards Tagger tags as cast for an
  alternative cost (alternative-cost, manaless-value), which are listed as not counted.
- Treasure makers and cantrips' partial worth are not counted. They have no reliable count from card text.
- Fetches read their basic land types from the rules text; a basic-land search finds any colour.

## Consequences

- On 2026-10-16:
  - **Magda** runs 26.5 lands, the median of 74 top-cut lists (24–29.4). Karsten's formula would say 35,
    which shows why bracket 5 uses the lists.
  - **Pantlaza's** Swords to Plowshares, Path to Exile and Kinjalli's Caller are short of untapped white
    sources on turn one.
  - **Ellivere's** Finale of Devastation and Eternal Witness are short of green.
- Shortfalls are thresholds, not verdicts: a card a few tenths short is close to Karsten's line.

## Amendment (2026-10-22): how a card with two parts is judged

The card data keeps Scryfall's `layout` for each card (schema 8), and the Mana tab, its turn-one check and the
suggested changes (ADR 0024) judge a card's colour by it (`manabase.castings`). The owner decided:

- **An adventure** (Bonecrusher Giant // Stomp) by its creature: the front face's cost at the card's mana
  value. Its cheap adventure half no longer counts as a one-drop on turn one.
- **A split card** (Dead // Gone, Fire // Ice) by its most expensive half, at that half's own mana value,
  since the printed mana value is the halves added up (rule 202.3c). Halves of equal value both count, and a
  colour takes the harder of them.
- **An aftermath card** (Cut // Ribbons), whose layout is also `split`, by its first half at that half's own
  mana value, since its second half is cast only from the graveyard; its Aftermath line tells it apart.
- **Every other layout,** a modal double-faced card included, by its front face's cost at the card's mana
  value.

Before, a split card was judged at the combined mana value by its front half's pips alone, so Fire // Ice
asked for the sources of a four-mana spell. The curve and the average mana value keep the printed mana value.
- The tab reads only the snapshot, the card data and cached meta answers, and opens in about 0.1 s.
