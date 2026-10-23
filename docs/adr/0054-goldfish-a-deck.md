# 0054: Goldfish a deck to see how it develops and how soon it wins

**Status:** Accepted · **Date:** 2026-10-22

## Context

The Mana tab (ADR 0019) judges each spell alone against Frank Karsten's tables: enough sources of a colour to
cast it on curve. It cannot say how ramp chains play out (Arcane Signet on turn two, Cultivate on turn three,
the commander on turn four), how often a hand is kept, whether two spells can be paid for together, or how
soon a deck's combo comes together once its tutors are counted. The owner wants those figures for each deck,
and wants to see how a swap on the board would move them before deciding on it.

The bracket question matters as much. Wizards' Commander Brackets Beta Update of 2025-10-21 says that in each
bracket "you should expect to be able to play at least N turns before you win or lose": nine turns in
bracket 1, eight in bracket 2, six in bracket 3 and four in bracket 4, while bracket 5 games "could end on any
turn". It adds that a combo which "could frequently come up" is not the best fit for a bracket. A deck that
assembles its combo early and often belongs in a higher bracket than it says.

Constraints: the app must not slow down, sync must not get longer, and no new data source is fetched. The
card database, the deck's cached Spellbook combos (ADR 0013) and its bracket or estimate (ADR 0051) are
enough.

## Options

1. **A rules engine** that plays the game out, as Forge and XMage do. It could judge any card, but it is years
   of work, and a game played against nobody says little about a four-player table.
2. **Closed-form odds**, as the Mana tab already does. Exact and instant for one question at a time, but
   ramp chains, mulligans, tutors and two spells at once are beyond it.
3. **A goldfish**: deal and play the deck many times by one fixed play policy, tracking mana, cards and
   casting, with no opponents and no combat. Cheap enough to run on demand, and it answers the questions
   above. The answers are only as good as the policy and the recognition of cards, which both have limits.
4. **A combat clock** on top of option 3: damage against an empty board, voltron commander damage, anthems
   and tokens. Rejected by the owner: with nothing to block or remove a creature, the clock is always
   optimistic, and recognising pump effects from rules text is an open-ended task.

## Decision

Option 3, without combat. Decided with the owner on 2026-10-22.

**Runs.** 10,000 games per deck, seeded from the deck id, so the same deck gives the same figures each time.
The run happens on demand, never at sync: when the Goldfish tab opens, or when a swap's figures are asked
for. Results are kept in memory, keyed by the deck's stamp, the card data's build, the bracket and the
policy's version (`POLICY_VERSION`, raised whenever the policy or the recognition changes).

**Game rules.** Commander as played in multiplayer: every player draws on turn one, and the first mulligan
is free (a fresh seven, nothing put back). After it, mulligans follow the London rule: draw seven and put one
card on the bottom for each mulligan past the free one. A hand of five is kept whatever it holds. The
commander starts in the command zone and, with nothing to remove it, is cast once.

**Keep rule, by bracket.** The deck's set bracket, else the lowest its estimate allows (ADR 0051). A deck with
neither uses the bracket 3 rule and gets no bracket verdict.

| Bracket | Keeps seven cards holding |
|---|---|
| 5 | 2 or more lands, and a way to the win: a combo piece or a tutor (fast mana counts as a mana source) |
| 4 | The same, with one more mana source |
| 3 | 2 to 4 lands (3 to 4 when the average mana value is over 3.5), and a card that matches one of the deck's dominant tags (`ideas.search_tags`) or a card with the Tagger role `draw` |
| 1 and 2 | 3 to 4 lands, and a dominant-tag or `draw` card |

On the London bottom, extra lands past four go first, then the most expensive spells; combo pieces, tutors
and finishers are kept.

**Play policy.** Each turn: untap, draw, play a land, then spend the mana in this order.

1. **Win:** if every named piece of a combo is on the battlefield, in the command zone or castable from hand
   this turn, with mana for all the pieces still in hand, the combo lands this turn.
2. **Mana:** land ramp, mana rocks and mana creatures, cheapest first. A creature's mana comes the next turn.
   A ritual is cast only if it pays for something else this turn.
3. **Commander**, if it is not out yet.
4. **Tutors:** for a combo piece that is missing, then a missing finisher, then a land when the tutor fetches
   only lands.
5. **Draw:** a one-shot draw spell draws its cards at once. A repeating draw engine draws one extra card each
   turn after it lands.
6. **Combo pieces that are permanents**, when mana is left over. Instants and sorceries among the pieces are
   held for the turn that wins.
7. **Other spells,** the most expensive castable first.

The land played is an untapped one that pays a colour needed this turn, else a tapped one if the turn's
spending does not need it. A modal double-faced card with a land face is played as a land only when no other
land is in hand. A finisher is held, never cast, and its turn is the first on which it could be cast.

**Recognising cards.** Once per deck, each card becomes a small record the game loop reads, so no rules text
is parsed during play. The Mana tab's reading of lands, colours, tapped lands, ramp and two-part cards
(`manabase`) is reused.

- **Lands:** colours and whether they enter tapped (`manabase.Source`).
- **Mana sources:** an artifact or creature with a mana ability (amount and colours), a ritual (net mana that
  turn), or land ramp (a land searched to the battlefield, tapped or not, or to hand).
- **Tutors:** the Tagger role `tutor` plus the card type the text searches for (land, basic land, creature,
  artifact, enchantment, instant or sorcery, any card) and where the card goes (hand, battlefield, top of the
  library, which is drawn next turn).
- **Draw:** the Tagger role `draw`; the count from "draw two cards" and the like, else one; a permanent with
  a repeating draw is an engine.
- **Combo pieces:** the named cards of the deck's Spellbook combos. A combo that needs a piece in the
  graveyard or exile is left out, as nothing here puts it there. A combo that also needs a described card
  ("a creature with power 5 or more") counts on its named pieces, and the tab shows the requirement beside it.
- **Finishers:** cards with the Tagger tags `overrun`, `alternate-win-condition` or `theft-mass`, plus the
  ones the owner adds in the deck's Settings ("Finishers" chips, stored in `profile.toml`), minus the ones the
  owner removes there. A finisher with X in its cost counts as castable once X can be 10 or more.
- **Everything else** is a spell with a mana cost. Cards the recognition cannot place, such as cards cast for
  an alternative cost or cost reducers worded in ways it does not follow, are listed under "Not modelled".

**Figures.** The turns shown follow the bracket: turns 1 to 5 in bracket 5, else the two turns either side of
the bracket's turn (2 to 6 in bracket 4, 4 to 8 in bracket 3, 6 to 10 in bracket 2, 7 to 11 in bracket 1).

- Opening hand: the share of games kept on seven (the free mulligan included), on six, and on five or fewer.
- The commander, each of a pair alone: cast by each turn shown, and the median turn.
- Mana available on each turn shown: the median and the worst tenth of games.
- Colour screw: the share of games in which, by the middle turn shown, a spell in hand had enough mana but
  not the colours.
- **Ways to win**, shown only for a deck with a Spellbook combo or a finisher: each combo landed by each turn
  shown, and how often a tutor fetched one of its pieces; each finisher castable by each turn shown. A deck
  without either runs none of this.
- **Faster than its bracket:** a warning when a combo lands by the bracket's turn (9, 8, 6 and 4 for brackets
  1 to 4) in 25% of games or more, a rate the owner chose so that the warning marks a deck that does it
  consistently rather than by luck. Bracket 5 gets no warning, nor do finishers, which do not win by
  themselves.

**Swaps.** A swap is judged on the same games: the incoming card takes every slot the cut card held in each
shuffle, so only games that reached that slot, or in which a tutor could have fetched either card, can
differ, and only those are played again. Figures are compared game by game, and a change is shown only when
it is larger than twice its paired standard error (a median or a worst tenth: when it differs at all).

- A **Considering card with a chosen cut**, once selected, shows a Goldfish fold listing only the figures that
  moved. A swap that moves nothing shows nothing at all: no fold and no message.
- **Review** shows the same for every queued change together, above Save, again only when something moved.

**Where it shows.** A Goldfish tab on the right rail beside Mana, loaded like the Mana tab only while it is
open. `cmdr goldfish <deck> [--swap "<in>=<out>"] [--plan <plan.toml>] [--json]` gives the same figures on
the command line.

## Consequences

- Each deck gets figures the Mana tab could not give, and a swap is judged by what it does to them, on the
  same draws, without noise passing for a change.
- Nothing runs until asked, and no request goes out, so sync time and app start do not change. Opening the
  tab costs a run of about a second; a swap costs a fraction of that.
- The figures describe one fixed policy against an empty table. Real games with interaction are slower, so
  a turn shown here is the earliest a deck gets there, not a promise. The tab says which keep rule ran.
- The recognition misses cards. "Not modelled" lists them, so a deck with many alternative costs can be read
  with that in mind. A change to the policy or to the recognition raises `POLICY_VERSION`.
- Combat wins are not judged. A deck that wins fast by attacking is not warned about.
- Revisit the 25% threshold and the bracket turns if Wizards changes the brackets (the system is still a
  beta), and the keep rules if the opening-hand figures look wrong to the owner on real decks.

## Amendment (2026-10-22): after the first figures on real decks

The first run on the owner's decks showed four figures off; the owner decided:

- **Colour screw** counts a game only when the colours cost a play: by the middle turn shown, a turn ended
  with mana unspent while a spell the policy would cast, or the uncast commander, cost no more than that
  turn's mana but could not be paid in its colours. Cards the policy holds (finishers, combo instants and
  sorceries, rituals, spirit guides) are left out. The first definition, any spell in hand short of colours,
  gave 29 to 62% and said nothing.
- **Keep rule, brackets 4 and 5:** a finisher is a way to win too. A deck with neither a combo nor a finisher
  uses the bracket 3 clause in its place (a dominant-tag card or a draw card), with its own bracket's land and
  source counts. Before, a stax deck with few tutors (Ellivere) went to five cards in half of its games. The
  dominant-tag clause is still waived for a deck that has no such card at all.
- **Fetch lands** that search for basic land types give the colours of the deck's own lands with those types
  (Flooded Strand finds Watery Grave and Breeding Pool), within the commander's identity, not only the types'
  colours.
- **A commander that is a combo piece** counts only once it is on the battlefield or can be cast that turn
  with the other pieces, unless Spellbook lists it as needed only in the command zone.

The dominant tags (`ideas.dominant_tags`) are read whatever the profile's tag search setting says; that
setting only decides whether Archidekt is searched by tag. `POLICY_VERSION` stays 1, since nothing has been
cached yet.

## Amendment (2026-10-22): headline figures for a swap

One ramp swap on a real deck moved 20 figures, every turn of every mana column among them, which is too much
for a fold on a card. The owner decided that the Goldfish fold on a Considering card and the block in Review
show only these headline figures, and of them only the ones that moved:

- each commander cast by the bracket's turn;
- the worst tenth of mana on the bracket's turn;
- colour screw;
- each combo landed by the bracket's turn.

The bracket's turn is 9, 8, 6 and 4 for brackets 1 to 4, and turn 3 for bracket 5. The Goldfish tab and
`cmdr goldfish --swap` still give every figure. When the bracket's turn is not among the turns shown, the
nearest turn shown stands in for it.

## Amendment (2026-10-22): the finishers setting and partial swaps

- The owner's finishers are stored in `profile.toml` as a `[goldfish]` table: `finishers` for cards the owner
  adds, `not_finishers` for Tagger-marked ones the owner takes out. A card in both lists is refused. The table
  is optional, so older profiles stay valid and `PROFILE_VERSION` stays 1.
- A swap takes the place of every copy of the card cut, as the engine plays it. A queued card whose copies
  differ from the copies the deck holds of its cut (one Forest out of ten), whose cut is not in the deck's
  library or whose card the deck already holds is left out of the figures, and `cmdr goldfish` refuses it
  with the reason.

## Amendment (2026-10-22): engine fixes after review

- A spirit guide is exiled from the hand for its mana and is not cast, so its own coloured pip is not paid.
- A generic cost is paid with colourless mana first, then with the units that pay the fewest colours, so a
  Wastes or a Mind Stone pays for a rock before a Plains does.
- A two-colour hybrid pip ({W/U}, {C/W}) needs a unit of either colour, so Oona is not cast from colourless
  mana. `manabase.fixed_pips` carries these pips under their symbol ("W/U"); the Mana tab reads colour
  letters only and shows the same as before. A generic hybrid ({2/W}) and a Phyrexian pip stay generic
  mana, which can cost more than the cheapest way to pay them.
- A sacrificed creature takes away what it gave, whatever kind it is: its mana, the extra land drops it granted
  and the extra card its draw engine drew each turn. A sacrificed commander stays cast, as it is cast once.
- A shuffle takes eight bytes a card from the game's random stream and is a Fisher-Yates from the front, carried
  out only as far as the game reads: a mulligan costs the seven cards dealt, not the whole library, and a
  search settles the rest. The stream a game uses is still fixed by its seed and index, so a swap is played
  on the same shuffles as before. The cards a seed deals are not the ones it dealt before.

## Amendment (2026-10-22): a swap of some copies

The owner decided that a swap of some copies of a card is played, which lifts the limit the amendment on
partial swaps set. Cultivate in for one of ten Forests plays nine Forests and Cultivate.

- The incoming card takes the slots of the copies cut (the last ones the card holds), so the paired games
  still line up by slot. A queued card's copies are the copies cut and the copies brought; more than the deck
  holds, or none, is refused with the reason. A cut with no add and an add with no cut stay out of the figures,
  and Review keeps only swaps with a cut.
- A game is played again if it drew a slot that changed, found by dealing its shuffles again from its own
  random stream (the drawn card numbers say only that some copy of the card was drawn). Games whose searches
  looked at a land or at a card a tutor wants are played again when the swap puts one in or takes one out, as
  before. The card put in some slots is numbered after all the others, so the cards already there keep their
  numbers; the figures equal those of the swapped deck played whole.
- `cmdr goldfish --plan` pairs a remove with its add by count: the remove takes out the copies the deck holds
  beyond its `keep`, and the add must bring as many.

## Amendment (2026-10-23): one-shot mana, tutors, Treasures and searches on death

The owner's pass over three bracket 5 decks (Etali, Magda, Ribbit) found fast mana undercounted, tutors and
Treasure engines left out, and Hermit Druid landing a turn early. The owner decided how each is played; the
reading and the policy now follow, and `POLICY_VERSION` is 5.

- **One-shot mana.** A creature or artifact that sacrifices itself for more mana than it cost, a spell that
  makes more Treasures than it costs, Rite of Flame and Jeska's Will are rituals; Jeska's Will counts an
  opponent's hand as seven cards. A spell that nets no mana (Manamorphose, Deadly Dispute with its one
  Treasure) is no ritual: it is cast for its card, gives back the mana it makes and leaves its Treasures in
  play. A permanent that nets no mana (Wild Cantor, Reckless Barbarian) is cast only with the mana a turn
  leaves over, once its other plays are chosen, paid from the turn's ordinary mana alone (no ritual,
  Treasure, life payment or burst), and gives nothing as it comes; it then holds its own mana in play as a
  Treasure does, without being an artifact. It is spent under the ritual policy below and leaves play when it
  is, and until then it is a creature that may attack, be tapped by a Drum or a crew, or be sacrificed (its
  mana gone with it). Mana that may be spent only on some kinds of spell (Geosurge, Open the Omenpaths) pays
  only for those and is gone once that spell is paid. Mana that needs a creature to tap (Springleaf Drum) or
  three artifacts (Mox Opal) comes while they are there, and a Drum or a crew taps no creature that is
  tapped for its own mana or whose own ability needs it to tap (a summoning-sick mana creature can be
  tapped, as it cannot tap for itself); mana paid for with life (Treasonous Ogre) is used down to 10 life;
  Orcish Lumberjack sacrifices a Forest, and two of them need a Forest each. City of Traitors is sacrificed
  when the next land is played. "Any combination of colors" (Manamorphose, Sage of the Maze, Dawn's
  Reflection) is the deck's colours. A nonland card's mana that needs a condition the game cannot follow
  ("Activate only if ...", Fanatic of Rhonas' ferocious mana; "Activate only once each turn", Kozilek's
  Translator) is left out, so Fanatic makes one mana and Circle of Elders none; a land's conditional second
  colour still counts (Hushwood Verge).
- **Ritual policy.** Rituals, Treasures and the like are spent only on the commander, a mana source, a combo
  piece, a finisher or a tutor that fetches a piece; Lion's Eye Diamond only on a combo landing or the
  commander, as it discards the hand. Life payments pay for anything.
- **Tutors.** A search for a colour (Natural Order: green), for a creature with power up to a number
  (Imperial Recruiter) or for a subtype (Goblin Matron) finds only such cards; a subtype is read only if a
  card of the deck has it, unless it is the other choice beside a card type ("an artifact or Dragon card").
  A changeling has every creature type and no other subtype, so a search for an Equipment does not find it.
  A tutor to the hand or the top takes the piece the mana could pay for
  by the last turn shown, a land a turn, before a dearer one. A combo piece whose ability taps it and makes no
  mana (Hermit Druid) must have been in play since the turn began.
- **Treasures** wait in play as one-shot mana of any colour and count as artifacts. They come from a permanent
  as it enters (Plundering Barbarian's mode) and from attacks: after the first main phase every creature in
  play since the turn began that the phase left untapped attacks, for its own Treasure trigger and for one
  that a creature becoming tapped sets off (Magda: a Dwarf). A creature tapped for its own mana or a burst
  does not attack, and neither does one a Drum or a crew tapped, the summoning-sick ones tapped first and
  then those with the least to gain from attacking. A Treasure made only if an opponent is ahead (Beza's, on
  lands) is not counted. No combat damage is followed, so Ragavan and Professional Face-Breaker stay not
  modelled.
- **Searches paid for by a permanent.** Magda's "sacrifice five Treasures" search is used as each main phase
  begins while there are five, and Treasures are kept for it rather than spent on anything but a win or the
  commander. Loyalty is not in the card data, so a -N search is taken as payable once when N is at most the
  card's mana value (Tezzeret, Cruel Captain's -3).
- **Searches on death.** A creature that searches as it dies (Protean Hulk, Academy Rector) is sacrificed to a
  free outlet in play (Carrion Feeder, Phyrexian Tower, Ashnod's Altar), and its search happens whenever it
  dies, a sacrifice for a spell's cost too. Protean Hulk fetches the missing creature pieces, best first, up
  to six mana value in all. Flash is not modelled, and nothing tutors for the Hulk, so a deck without an
  outlet and seven mana does not see it.
- **Cost reducers.** The owner decided that cost reducers are counted, and `POLICY_VERSION` is 6. A spell is
  paid for at its printed cost less the generic mana the reducers take off, which never goes below the
  coloured pips or below 0, and the reductions of several reducers add up.
  - A permanent that says "<kind> spells you cast cost {N} less" takes N off each spell of the kind from
    the moment it is in play: a card type or "noncreature", a colour, a subtype (a changeling has every
    creature type), joined by "and" or "or" (Instant and sorcery, red or green) or together (white creature),
    or no word at all for every spell. A reducer in the hand does nothing, and a reducer never reduces its
    own cost. A creature that is sacrificed stops reducing.
  - A spell that says "costs {X} less, where X is" the greatest power or the total power of the creatures you
    control, or the greatest mana value among the creatures of a subtype you control (Sunderflock:
    Elementals), "{N} less for each creature on the battlefield" (the goldfish's own creatures, as nobody else
    has any) or "{N} less if you control a creature with power P or more" (Bolt Bend) takes that off when it
    is cast, from the creatures in play and their printed power (0 for a power that varies).
  - Every place that prices a card reads the reduced cost: whether a card can be picked, the plan of rituals
    and Treasures, the mana a ritual gains, the check that a tutor's pick is castable within the turns
    shown, the pieces of a combo cast together, a finisher's cost, the colour screw and the land played. A
    deck with no reducer or discount runs none of this.
  - "Creatures you control get -N/-N" (Heartless Summoning) takes N off their power in those counts. A
    creature whose printed toughness is N or less dies as it enters while the effect is in play, doing only
    what it does as it enters, and dies when the effect comes, with its mana, so a mana creature of toughness
    1 gives none under Heartless Summoning. A printed toughness of 0 is left alone: other cards set it.
  - Left out, and so still under "Not modelled": a chosen creature type (Herald's Horn, Urza's Incubator), a
    cost reduced for each counter (Animar), "historic" and "colorless" spells, a condition on the spell cast
    (Goreclaw: power 4 or greater), a sacrifice for the discount (Rottenmouth Viper), and the cost of an
    ability. A Class such as Artist's Talent keeps its reduction behind a level that nothing here pays for.
  - In a swap, two plain spells that play alike must also have the same card types, subtypes, power and
    toughness when the deck holds a reducer, a discount, such a shrinking or a permanent that makes a Treasure
    for each creature of a type becoming tapped (Magda), as those are what they read; a reducer or a discount
    is no plain spell.

On the three decks, 10,000 games each: Etali's commander by turn five rose from 25% to 59% and Magda's combos
by turn five from 9% to 55%; Ribbit's Hermit Druid line fell from 10.5% to 8.9% by turn five. The cards not
modelled went from 26, 16 and 14 to 12, 8 and 13. A run takes 1.8 to 2.6 seconds, up from 1.4 to 1.5.

## Amendment (2026-10-23): doublers, copies and the effects the reading does not follow

The owner's pass over the Magda, Gator Skin Boots and Ribbit decks found cards that double Treasures and
triggers, or copy a permanent, played as plain spells. The owner decided how each is played, and
`POLICY_VERSION` is 7 (the cost reducers above made it 6).

- **Treasures per event.** Xorn makes one Treasure more in every creation, and Parallel Lives and Mondrak
  twice as many. Ojer Taq (creature tokens only) and Academy Manufactor ("one of each", the Treasure too)
  leave the Treasures as they are. With both kinds in play the additions come first, then the multiples,
  the order that makes the most.
- **Trigger doublers.** A permanent that makes a trigger happen an additional time (Panharmonicon, Yarok,
  Windcrag Siege, Delney, Twinflame Travelers, Roaming Throne) repeats a permanent's arrival effects
  (Treasures, a search, lands fetched, cards drawn) and the Treasure triggers of an attack or a tap, once
  more for each doubler in play that names the cause and the permanent. A doubler of "another" creature
  (Twinflame Travelers, Roaming Throne) does not repeat its own triggers. Roaming Throne names the
  commander's creature type; of several, the type the deck's Treasure triggers gain most from; with no type
  of its own, the type most of the deck's creatures have. A doubler of deaths (Teysa Karlov) is not read.
- **A commander or land that becomes a copy of a creature until end of turn.** Gogo (at the beginning of
  combat), Hall of Echoes (for {5}) and Cursed Mirror (as it enters, with haste) become a copy of the
  creature that makes the most Treasures, and none that makes none. A copy of a legendary creature stays
  only where the legend rule does not apply (Gogo is renamed; Hall of Echoes lifts it), else it is not
  made. A Cursed Mirror cast after combat gains nothing from the attack it missed: it is a copy only for the
  Treasures the creature makes as it arrives.
- **A card that enters as a copy and stays one** (Sculpting Steel, Phantasmal Image, Mockingbird, Phyrexian
  Metamorph, Vesuva). It copies the permanent in play that is worth most for the mana, counting what the
  copy makes in the turns left (mana from its next turn if it is a creature, Treasures, cards, lands, land
  drops, a doubler), and only when that is more than the copy costs; nothing is copied that is legendary or a
  copy itself. Mockingbird copies a creature of up to the mana spent, paying X for it. Vesuva enters tapped
  as the land that makes the most of the colours the hand wants. A combo piece, a finisher or a commander
  is cast as itself.
- **Peter Parker's Camera** copies an ability of its controller's for {2}, a tap and one of its three
  counters, once a turn. It copies a search paid for with Treasures or loyalty for a second card, paid with
  the mana left over after the first main phase, or with Treasures when that costs less than the search
  does (or the search is paid by loyalty), and the largest Treasure trigger of combat when that makes more
  than the copy costs. Mana abilities cannot be copied, and arrival triggers are not.
- **With the cost reducers.** A card that enters as a copy is cast for the cost the reducers in play leave,
  and a Mockingbird pays X on top of that; what it is worth is weighed against what it costs. The copy has
  the features of the card it copies, its reductions included, so a copied Medallion that also makes mana
  reduces as the original does; a permanent that is a copy for one turn (Gogo, Hall of Echoes, Cursed
  Mirror) copies the creature that makes the most Treasures and none other, so it reduces nothing. The
  costs of abilities (Hall of Echoes' {5}, the Camera's {2}) are not reduced, as the cost reducers above
  leave them out. A creature of toughness 1 that enters under Heartless Summoning still has its arrival
  effects repeated by the doublers, as they would be for any arrival.
- **Not modelled.** A card the Tagger says doubles tokens or triggers, makes more mana or copies a permanent
  (tags `token-increaser`, `token-doubler`, `trigger-doubler`, `mana-increaser`, `clone`, `copy-creature`,
  `copy-artifact`, `copy-permanent`, `copy-ability`, `copy-land`) and whose effect the reading did not take
  is listed once the deck has what the effect works on: Treasures, a trigger the goldfish counts, a mana
  source or land, a permanent worth copying, another land. The figures of a swap list what the swapped deck
  would: a card swapped in is listed only if that deck has what its effect works on, and a card in the deck
  is no longer listed when the swap takes that away. This lists Kiki-Jiki, Chatterfang and the token copies of creatures; the goldfish still plays them
  as plain spells.

Still not followed: token copies of creatures (Kiki-Jiki, Ashling, myriad), copies of spells, doublers of
death triggers, doublers of mana, and the legend rule beyond what is above.
