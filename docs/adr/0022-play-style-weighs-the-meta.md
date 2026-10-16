# 0022: A deck's play style weighs what the meta says it misses

**Status:** Accepted · **Date:** 2026-10-17

## Context

`cmdr meta` lists the staples a bracket 5 deck misses by share of the commander's top-cut lists, and flags
every meta combo the deck has no answer to. Both read as gaps whatever the deck is trying to do. Etali
(Archidekt deck tag Turbo) aims to win on turn two; its notes say that few answers to Underworld Breach loops
"is the turbo plan, not a gap". Yet the report listed Force of Vigor and Pyrokinesis as staples to add,
counted the unanswered combos, and `sync` compiled those staples into Considering.

Deck tags already tell how a deck plays. ADR 0009 sets play styles apart from themes, and the owner asked to
weigh them when researching replacements. Among the four bracket 5 decks: Etali is Turbo; Ellivere is Stax
and Tempo; Magda and Glarb are Midrange.

## Decision

The owner chose the rules and their reach.

- **Styles:** the deck tags Turbo, Stax and Control, by casefolded name. Every other play style is neutral.
- **What a staple is**, from the card data:
  - *speed*: the ramp, tutor or draw role;
  - *interaction*: the removal, counterspell or board wipe role;
  - *cheap interaction*: interaction at mana value 2 or less, or free to cast (the Mana tab's alternative-cost
    tags);
  - *stax piece*: what the Meta panel counts as one (`metacombos.pool_answers`: Tagger's lock tags and rules
    text), or Tagger's `hatebear` tag. Tagger has no general stax tag.
- **Fit:** a staple is *favoured* when a style wants it (Turbo speed, Stax stax pieces and cheap
  interaction, Control interaction). It is *off-plan* when the deck races, meaning Turbo without Control, and
  it is interaction that no style favours. Anything else is neutral.
- **Order and marks:** the missing staples sort favoured, neutral, off-plan, then by share as before, and the
  limit applies after sorting. The CLI marks "[fits turbo]" and "[off-plan for turbo]". Shares are unchanged.
- **Racing:** a racing deck's unanswered meta combos read "no answer in the deck (turbo: racing, not
  answering)". The table shows "racing" in place of the count, and the app's Meta panel shows a muted
  "Racing" badge where it showed "No answer".
- **Ideas:** off-plan staples are not compiled into Considering, from the meta lists or from adjacent
  commanders' decks.

## Consequences

- On 2026-10-17 data, Etali's staples open with fast mana and tutors, and Fury, Force of Vigor and
  Pyrokinesis close the list as off-plan. Ellivere's open with Linvala, Null Rod, Eiganjo, Rest in Peace and
  Dauntless Dismantler. Magda and Glarb are unchanged.
- Off-plan ideas already on Etali's board stay until they are turned down. `sync` only stops adding them.
- The roles come from Tagger, so a free counterspell with no counterspell tag counts as neutral. The marks
  are hints for the owner, who still decides.
