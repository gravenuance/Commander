# 0024: Suggesting how the mana base should change

**Status:** Accepted · **Date:** 2026-10-17

## Context

The Mana tab (ADR 0019) says where a deck falls short of Frank Karsten's numbers but not what to do about
it. The owner asked for a suggestion under the analysis for decks that don't play by cEDH standards, and
for ideas on what a cEDH version would look like. Karsten's articles say their models don't fit cEDH, so
bracket 5 gets no suggestion from them.

The owner chose (2026-10-17):

- **Scope:** the land count and the split of basics first; where basics can't close a gap, named untapped
  lands; and untapped replacements for lands that always enter tapped.
- **Action:** one button puts the swaps on the deck's board under Considering, each with its cut, so they go
  through the usual Review and save. Nothing is written to Archidekt by the button.
- **Land count:** Karsten's formula is the target, with the other lists as a check.

## Decision

`manabase.suggest_mana` builds the plan in four steps, each the smallest change that helps.

1. **Land count.** It moves only when the deck is more than one land off Karsten's formula *and* outside the
   range of the recent Archidekt lists of the commander (when there are any). More lands name the costliest
   spells as room, leaving out cards that make mana or fetch lands: the formula already weighs ramp. Fewer
   lands come out of the basics of the colour with most to spare.
2. **Basics.** Each colour's need is the largest number of its basics any card asks for at the new land
   count (Karsten's table, scaled, with the gold extra and turn one's untapped need), less the sources that
   aren't its basics. Cards that would need more basics than the deck has are left out of the need, so one
   unreachable card doesn't drain the other colours. Basics then move one at a time to the shortest colour
   from the colour with most to spare, never making a colour short.
3. **Named lands.** What basics can't close, untapped lands in the commander's identity close, at most four,
   each in place of a basic of another colour it makes. A fetch land may replace only a basic of a colour
   that meets its needs, since it finds nothing once those basics are gone; a land that makes both colours
   itself keeps them level. Lands whose mana casts only some spells (Cavern of Souls), lands already in the
   deck and lands turned down on the board are never named.
4. **Tap-lands.** Up to three lands that always enter tapped and do nothing else (Guildgates, tri-lands, gain
   lands) each get the most played untapped land making the same deck colours. A tap-land with another use
   (bounce lands, scry lands, cycling lands) stays.

### Budget

The owner set it (2026-10-17): casual decks have budgets, and a deck shows its budget in what it already
runs. A shock land in an Ashling list whose dearest card is €7.16 makes no sense.

- **Working budget:** the deck's average card price, weighted by copies, basics and unpriced cards left out.
- **Ceiling:** 25% above the deck's dearest card, so a few options beyond the working budget remain.
- **Order:** a land within the working budget comes before a more played one over it; one over it is named
  only where none within fits. The tab shows the swaps in two groups, within budget and over budget.
- The profile's price limit, when set, caps both.

The plan is checked by running the analysis again on the deck as it would leave it; the tab shows each
colour's short cards now and after, and what is still short.

### On the board

A basic swap moves several copies at once, so a board card now has `copies` (1 by default). The save adds
that many and takes as many copies of the chosen cut; choosing a cut the deck holds fewer copies of is
refused. A basic land can be put on the board when the deck already runs it.

### Fixes to the analysis found on the way

- Shock lands ("you may pay 2 life") count as untapped.
- Lands that put the land they find "onto the battlefield tapped" (Evolving Wilds) count as tapped.
- Reveal lands ("if you don't") and Fabled Passage ("untap that land") count as sometimes tapped.

## cEDH: ideas, not built

Bracket 5 decks get no suggestion. Karsten's models assume games that last; cEDH decks win early, run fast
mana and rituals, and fetch for duals more than they cast basics. A cEDH suggestion would follow the
commander's top-cut lists (ADR 0007) instead:

- **Land count:** the top-cut median and range, adjusted for how many fast mana pieces and rituals the deck
  runs against those lists (each one a list runs that the deck doesn't argues for a land, and the reverse).
- **The land base itself:** the lands the top cuts run most, by share, that the deck lacks (fetches, duals,
  shocks, Ancient Tomb, City of Brass), with the least shared lands in the deck as cuts. This is the same
  idea feed as Considered (ADR 0014) narrowed to lands.
- **Fetchability:** each fetch's targets in the deck (typed duals and basics), and the colours a fetch can't
  find for lack of them.
- **Untapped on turn one:** every land should enter untapped; any tap-land at all is flagged.
- **Fast mana:** Sol Ring, Mana Crypt, Chrome Mox, Mox Diamond, Lotus Petal and the deck's rituals against
  how many the top cuts run.
- **Colour weight by tempo:** the colours of the deck's one- and two-drops and of its win line weigh more
  than the colours of late cards.

What stops it now: the top-cut lists give counts of lands but not the opening hands or turn one plays, and
their shares can't be published (EDHTop16's terms), so the suggestion would name lands and give no tables.

## Consequences

- On 2026-10-17, across the owner's seven decks below bracket 5: Chatterfang runs four lands above Karsten
  and other lists and short of black, so three Forests become Swamps and four go; Bello is short of red, so
  three Forests become Mountains and three red-making lands are named; Ashling (five colours) can't meet the
  table at 42 lands, so shock lands replace basics and thirteen cards stay short.
- The owner wants the same budget rule (within, then over the deck's average card) for every card under
  consideration below bracket 5, not only lands: compiled ideas (ADR 0014), `suggest` and Considering. That
  is open.
- The plan is a threshold reading of Karsten's numbers, as ADR 0019 says: a card a few tenths short is close
  to his line, and the owner decides each swap on the board.
