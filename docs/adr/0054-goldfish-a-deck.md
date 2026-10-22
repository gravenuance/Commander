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
- **Everything else** is a spell with a mana cost. Cards the recognition cannot place, such as cost reducers
  and cards cast for an alternative cost, are listed under "Not modelled".

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
- The recognition misses cards. "Not modelled" lists them, so a deck with many cost reducers or alternative
  costs can be read with that in mind. A change to the policy or to the recognition raises `POLICY_VERSION`.
- Combat wins are not judged. A deck that wins fast by attacking is not warned about.
- Revisit the 25% threshold and the bracket turns if Wizards changes the brackets (the system is still a
  beta), and the keep rules if the opening-hand figures look wrong to the owner on real decks.
