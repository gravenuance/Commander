# 0020: A board wipe clears creatures

**Status:** Accepted · **Date:** 2026-10-16

## Context

The board-wipe role was every card under Scryfall Tagger's `sweeper` tag and its one sub-tag,
`sweeper-one-sided`: 954 cards. That covers anything that removes a group of permanents, so `suggest`
offered Vandalblast to Pantlaza as a board wipe (ADR 0005), and the role counts included Bane of Progress,
Back to Nature and Armageddon. The owner does not count Vandalblast as a board wipe. In Commander the term
means a reset of the creatures on the battlefield, as Blasphemous Act or Cyclonic Rift do.

Dropping `sweeper-one-sided` was considered and rejected. Its 246 cards include Cyclonic Rift, Plague Wind
and In Garruk's Wake, which are board wipes by any reading.

## Decision

A sweeper is left out of the board-wipe role when its Tagger removal tags name only noncreature permanents
and none name creatures. The cards then count only if their rules text says "each creature".

- **Reach creatures:** `removal-creature` and its sub-tags (`removal-toughness`, `removal-nonland`,
  `removal-permanent` and others), `removal-fight`, `removal-token`.
- **Only other permanents:** `removal-artifact`, `-aura`, `-battle`, `-enchantment`, `-equipment`,
  `-land`, `-noncreature`, `-planeswalker`, `-spacecraft`, `-vehicle` and their sub-tags.
- **Untagged sweepers stay.** Blasphemous Act carries no removal tag at all.
- **The text check:** the tags miss creature damage on land and artifact sweepers (Wildfire, Incendiary
  Command, Star of Extinction), so "each creature" in the text puts a card back. The looser "creature" would
  keep Bane of Progress, whose text says "this creature".

Tags are named by id in `scryfall.py`, as `ROLE_TAGS` are. Tagger's removal tags form a graph, not a tree:
`removal-nonland` sits under both `removal-artifact` and `removal-creature`. A card tagged with it counts as
reaching creatures.

The tags and the text are read at different stages. `scryfall.role_members` applies the tag rule, and
`role_gates` hands the left-out cards to `write_card_db`, which keeps those whose text holds the phrase. The
card database schema went to 7 so that existing databases are rebuilt.

## Consequences

- About 90 cards leave the role. On 2026-10-16 data: Vandalblast, Bane of Progress, Back to Nature,
  Shatterstorm and Armageddon are out (Armageddon still counts as mass land denial). Cyclonic Rift,
  Blasphemous Act, Culling Ritual and Wildfire stay.
- None of the owner's eleven decks changes its board-wipe count, because none runs a noncreature-only
  sweeper. `suggest` stops offering them to fill the role.
- A rename on Tagger keeps working, since the tags are found by id. A removed tag shrinks a set, and the
  `sweeper` tag itself still logs a warning when it is gone.

## Amendment (2026-10-27): land tutors and removal that ramps the opponent

Two more roles were wider than their names, found in a review of what the roles hold:

- **Tutor held 244 land tutors.** The role left out the `tutor-land` subtree, but Tagger files Cultivate, Land
  Tax, Kodama's Reach, Expedition Map and Ash Barrens also under `tutor-to-hand` or `tutor-to-battlefield`
  (the `tutor-to` subtree, which says only where the fetched card goes), and that kept them in. Ellivere's
  tutor count listed Land Tax and Yasharn, Implacable Earth. `RoleTag.exclude_alongside` names `tutor-to`: its
  tags do not count on a card that also has a land tutor tag, so a card with another tutor tag (Natural Order's
  `tutor-creature`) stays, and so do Unmarked Grave and the other `tutor-to` cards with no land tag.
- **Ramp held removal that gives the opponent a land.** Path to Exile, Erode, Settle the Wreckage, Emergency
  Eject, Cityscape Leveler and five more carry Tagger's `swap-removal` and also `ramp`; Pantlaza's `analyze`
  listed Path to Exile as ramp, and the surplus cuts and the Turbo play style counted it. The role now drops
  every card tagged `swap-removal` (`RoleTag.drop_cards`). The `donate-rampant-growth` tag, which the review
  first suggested, would also have dropped 15 cards that ramp the caster too (Tempt with Discovery,
  Collective Voyage, Fertilid, Hithlain Rope, Veteran Explorer): ramp stays ramp where it helps the caster.

On 2026-10-22 data, 244 of the 814 legal tutors leave the role and 10 of the 2,153 legal ramp cards. The card
database schema went to 9 (ADR 0001) so that existing databases are rebuilt.
