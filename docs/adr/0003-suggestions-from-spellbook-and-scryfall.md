# 0003: Suggestions from Commander Spellbook and Scryfall, not EDHREC

**Status:** Accepted · **Date:** 2026-10-15

## Context

`suggest` should propose cards to add to a deck and cards to cut, with EUR prices. The plan was to compare
each deck with its commander's page on EDHREC, read from the unofficial `json.edhrec.com/pages/commanders/
<slug>.json`. That file has what suggestions need: per-commander card lists with inclusion rates and
synergy scores. One fetch on 2026-10-15 returned 106 KB in 0.6 s, with no login.

EDHREC's terms of use (edhrec.com/terms, effective 2024-08-06) grant "a non-transferable, non-exclusive,
revocable, limited license to access the Site solely for your own personal, noncommercial use", say that
no part of the site may be "copied, reproduced, distributed, republished, downloaded" except as they
state, and forbid using "software or automated agents or scripts … to generate automated searches,
requests, or queries to the Site". EDHREC publishes no API terms that would override this.

Two sources the project already uses can stand in:

- Commander Spellbook's `POST /find-my-combos` takes the same deck request as `estimate-bracket` (ADR 0002)
  and returns the deck's combos (`included`) and those one piece short within its colours
  (`almostIncluded`), each with its bracket tag, cards with Scryfall oracle ids, described requirements
  ("Creature with Saddle 4 or less"), results and popularity. One call took 1.2 to 1.5 s and returned
  118 to 562 KB; across the user's 11 decks, 18 to 81 cards per deck would each complete a combo.
- Scryfall's bulk card objects carry `edhrec_rank`, EDHREC's overall popularity rank, republished under
  Scryfall's terms. With the role tags already in the card database (ADR 0001), it ranks the most played
  cards for a role within a colour identity. It knows nothing about a particular commander.

## Options

1. **Fetch EDHREC's JSON anyway**, once per deck a week, cached. The best ideas, specific to each
   commander, but against EDHREC's terms and the project's online-services standard, and the unofficial
   endpoint can change or block without notice.
2. **Read EDHREC pages the user saves by hand** into each deck folder, alongside option 3. The software
   never contacts EDHREC and the ideas are commander-specific, but it costs a manual step per deck and the
   saved file goes stale.
3. **Spellbook's near-misses plus Scryfall's tags and ranks.** Within every service's terms and needs no
   manual step, but the role ideas are generic staples rather than synergy picks.

## Decision

Option 3, chosen by the user on 2026-10-15. `suggest` lists, per deck:

- the cards that complete a near-miss combo, grouped by card (a card completing several combos first),
  leaving out combos tagged Banned, cards not legal in Commander, and near-misses whose missing piece is a
  described requirement rather than a card;
- for each role whose count of nonland cards is below its target (default ramp 10, draw 10, removal 8,
  board wipe 2, overridable in the config file's `[targets]` table), the most played legal nonland cards
  with that role inside the commanders' colour identity, by `edhrec_rank`;
- cut candidates: nonland, non-commander cards with none of the analysed roles and in no included combo,
  least played first.

An idea is marked when it would lift the deck above the bracket set on Archidekt: the combo's tag minimum
(ADR 0002's table), or the official rules from magic.wizards.com/en/formats/commander as read on
2026-10-15 (no game changers in brackets 1 and 2, at most three in bracket 3; no mass land denial below
bracket 4, per WotC's 2025-04-22 update). The extra-turn rules sit in page tabs that could not be read as
text, so extra turns are not part of the marker. `--within-bracket` hides marked ideas instead.

The combo answers are cached like the bracket estimates: per deck for 7 days in
`%LOCALAPPDATA%\Commander\spellbook\combos`, keyed by the request's SHA-256, at most four requests at a
time. The cache module was generalised so both commands share it; the estimate cache moved to format
version 2 and older files are fetched over.

## Consequences

- No request ever goes to EDHREC; its rank arrives inside Scryfall's bulk file, and the output credits
  both EDHREC and Spellbook.
- Role ideas are staples (Arcane Signet, Sol Ring), not cards chosen for a commander's theme. Cut
  candidates are a short list to look at, not advice: a theme card often fills no role on paper.
- Tag-based roles count broadly. Scryfall's ramp tags include some removal (Path to Exile is the third ramp
  idea for a white-red deck), and draw includes cards like Mind Stone. Counting only nonland cards keeps lands
  such as Ancient Tomb out of the role totals.
- The card database moved to schema version 2 (`edhrec_rank` and an index on it), so the next `sync`
  rebuilds it. Candidate queries read cards in rank order through that index and stop after the few they
  keep; a cached `suggest` over all 11 decks took 1.0 to 1.3 s on 2026-10-15, and one that asks Spellbook
  again 5.7 to 7.2 s.
- If EDHREC ever publishes an API or terms that allow tools, option 1 or 2 can be added behind its own
  provider module without changing the other lists.
- The write-back decision planned as ADR 0003 becomes ADR 0004.
