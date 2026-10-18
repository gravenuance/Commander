# 0026: One weighted share per card from the lists other players run

**Status:** Accepted · **Date:** 2026-10-18

## Context

Four places asked "how many of the commander's lists play this card?" and each answered it differently. The
cut targets of ADRs 0011 and 0016 ranked by the share of the top-cut lists at bracket 5, of the recent
Archidekt decks elsewhere, then by EDHREC rank. The turned-down return of ADR 0011 watched only the bracket 5
share. Considering ordered a meta idea by
its tournament share and any other by its share of recent Archidekt decks. The ideas of ADR 0014 had a flat
threshold per source (40% of the lists, half of the decks).

Raw counts treat a list from two months ago like one from last week, and a tournament win like a last place
that still fell in the window. They also leave out the other source: a card played by a third of the lists
and a third of the decks was an idea in neither.

## Options

1. **Keep a share per source.** Nothing to retune, but the four places stay inconsistent and a card between
   the thresholds of both sources is lost.
2. **One share, equal weight per list.** One number to rank by, but old lists and last places count in full.
3. **One share, each list weighted by how recent and how well placed it is,** the sources pooled by their
   list counts.

## Decision

Option 3, in `commander.weightedshare`.

- **Which lists:** every EDHTop16 list with a decklist in the window (not only the top cuts) and every
  Archidekt deck read for the commander (ADR 0027). A card counts once per list.
- **A list's weight:**
  - *Recency:* it halves for every 30 days since the tournament, or since the Archidekt deck was last
    updated.
  - *Placement* (EDHTop16 lists only): the share of the field the entry beat, times a size factor of
    1 + log2(players / 16), floored at 0.25 so a small event stays a small voice. An entry with no standing
    counts as the middle of the field.
- **Pooling:** within each source the weights are scaled to a mean of one, so a list is still about one vote
  and the weights only reorder lists inside their source. A card's share is the scaled weight of the lists
  playing it over the number of lists, both sources together. The raw count (`12/35`) and each source's part
  are kept beside it.
- **The idea test:** a card is an idea when the lower bound of its share's Wilson score interval (95%) is at
  least 0.32. The interval is taken on the Kish effective sample size of the weights, (sum w)^2 / sum w^2, so
  a few heavy lists count as the few they are. With every list weighing the same, 60 decks need a share of
  about 44% and 10 decks about 61%. The bound of 0.32 was the best F1 of a backtest.
- **Where it is used:** the idea test (ADR 0028), the ranking of cut targets and the cuts across roles and
  themes (ADR 0029), the return of a turned-down card once its share has risen 20 points (`RETURN_RISE`, at
  every bracket now), the order within a group of Considering, the order each source's ideas are taken in (the source's part of
  it, times the play-style weight), and the share pill on each grid and board card
  with the sources in its tooltip. The commander carries none.
- **Where it is not:** `cmdr meta` keeps the raw shares and the 40% and half-of-the-decks thresholds it
  reports, and the pools of ADR 0028's adjacent and tagged decks keep their half-of-the-pool rule.

## Consequences

- One number explains why a card is an idea, a cut or back on the board, and the app shows it.
- The constants are judgement: the half-life, the size factor, the floor and the bound are named at the top of
  `weightedshare.py` and tested, so retuning is a change of one value.
- Recency moves with the clock, so the app recomputes a deck's shares when the meta cache changes or the day
  turns.
- A commander with few lists gets few ideas: the interval is wide, and nothing is invented to fill the gap.
- The shares come from EDHTop16 data, so they stay on this machine and out of git (ADR 0007).
