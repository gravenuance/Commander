# 0061: List the tokens each deck makes

**Status:** Accepted · **Date:** 2026-10-31

## Context

The owner wants to know every token a deck can make: the Treasures, Vampires and Soldiers, but also the
emblems and the helper cards that stand for a game state (the Monarch, Day // Night, a dungeon, the marker
for a token that copies another), to have the right pieces on the table and see which cards need them.

Scryfall's bulk file already says. Each card printing has `all_parts`, the related objects with the id of one
printing of each. The card data (ADR 0001) dropped the token, double-faced token and emblem objects as things
that never sit in a deck, and read nothing of `all_parts`. Looking at the real file turned up four facts:

- A token part has `component` `token`, but emblems, the Monarch, Day // Night, The Ring and the dungeons
  are filed as `combo_piece`, like the real combo partners of a card.
- A part names a token by one printing's id, never its oracle id, and the printings of one card differ: Magda
  names three different Treasure printings, and Caller of the Claw's Spanish promo names none at all.
- Some token objects have another layout (`flip`, `reversible_card`, `normal`) and a type line starting
  `Token`, and the dungeons of Adventures in the Forgotten Realms are `normal` cards typed `Dungeon`.
- The Copy marker (type `Token`, named Copy) is linked by about a thousand cards, but not by every card whose
  text makes a token copy (Stormsplitter, Rootcast Apprenticeship, populate cards).

## Options

For where the list comes from:

1. **Scryfall's links alone.** Exact for what Scryfall links, blind to a token copy and to anything it left
   out.
2. **The cards' rules text alone.** Reaches every card, but reading "create two 2/2 green Cat creature
   tokens" back into a token is the phrase-matching the goldfish was removed for (ADR 0057).
3. **The links, with the text read for two things only:** whether a card copies a token, and whether a card
   creates one that is not linked.

For where the links live:

1. **Read them from the bulk file on each use.** 80 MB and ten seconds for a list.
2. **Store them in the card database,** with the tokens, so a read is a query.

For when `list.md` gets its section:

1. **At every save.** Needs the card data in every place that saves a deck, and the card data may still be
   downloading while the decks sync.
2. **After the card data is in,** in one pass of the sync, and carried over by saves made elsewhere.

## Decision

Option 3, with the links stored in the card database and `list.md` written in a pass of its own.

**Reading the bulk file.** `parse_card` keeps its cards as they are and also keeps, for each printing, the ids
its `all_parts` name as `token` parts and as `combo_piece` parts (without the card's own id). A new
`parse_token` reads a token, emblem or helper object (skipping digital printings) into the oracle id (the front
face's for a card without one), the name (both faces for a two-faced object), the type line, power, toughness,
colours, the rules text of both faces, a printing for the image and its release date. An object is a token
when its layout is `token`, `double_faced_token` or `emblem`, or when its type line starts with `Token` or
`Dungeon`; those no longer also enter the card tables. One pass over the file serves both, so the download is
read once.

**The card database** (`SCHEMA_VERSION` 10) gains `token(oracle_id, name, kind, type_line, power, toughness,
colors, oracle_text, scryfall_id)` and `card_token(oracle_id, token_oracle_id)`, the second `WITHOUT ROWID`.
A token's image is its latest regular printing (a regular one beats a later gold, silver or oversized one; the
first seen wins a tie). After the file has been read, each card's part ids from all of its printings are turned
into token oracle ids, so the card has the union of what its printings name; a `combo_piece` that is not a
token falls away there. A `token`-component id that matches no token in the file is dropped and counted in one
log line at info level (the user cannot act on it). `CardDb.tokens_of` answers the tokens of a set of cards and `token_named` finds the Copy marker.

**Kinds.** `TokenKind` is `emblem` for the `emblem` layout or a type line starting `Emblem`; `token` when the
front type line starts `Token` and a space ("Token Creature — Rat"); `helper` for the rest: typed `Card` or
`Dungeon`, plain `Token` (the Copy marker), or `Creature` (the face-down reminder cards Morph, Manifest and
A Mysterious Creature, and Cyberman).

**Checklists.** The checklist cards that mark a double-faced card in a library ("Ixalan Checklist") are named
as parts by the double-faced cards they cover, but no deck makes them. A token object whose name ends
` Checklist` or ` Checklist N`, or whose text starts "(You can mark this card to represent a" or "(Use this
card to represent a double-faced", is not read at all. The other non-game cards typed `Card` (World
Championships ads, player bios and decklists, Blank Card, minigames, Bounty cards) are linked from no card
and stay in the table unused.

**`tokens.py`,** the one place the rest calls, turns a deck's resolved main cards into `DeckTokens`: the
tokens, each with the deck cards that make it (sorted by name), the one with most makers first and then by
name; and the cards to check by hand.

- A card whose rules text matches `COPIES_A_TOKEN` ("a token that's a copy", "tokens that are copies", "a
  token copy of", "a copy of ... token", "the copy becomes a token", and the keywords populate and myriad)
  makes the Copy marker, even when Scryfall does not link it. The deck has one Copy row, the linked and the
  matched makers together, except that a card which links a token of its own name (embalm, eternalize) makes
  that token and no Copy. Without a Copy record in the card data the row is listed without an image and a
  warning is logged.
- A card is returned in `check_by_hand` when its text creates a token (`creates_a_token`: "create ... token"
  in one sentence, not a token doubler's "would create" or "create or sacrifice") and it links no token and is
  no copy maker; or when its text matches a row of a table of wordings (`_HELPER_WORDINGS`) and none of its
  linked tokens is what the wording makes: "become(s) the monarch" (The Monarch), "get(s) an emblem" (an
  emblem), "take(s) the initiative" and "venture into" (a dungeon), "the Ring tempts you" (The Ring) and the
  ascend keyword (City's Blessing). Scryfall misses some of these links (12 of the 60 cards that make you the
  monarch).

**Where it shows.**

- `cmdr tokens [deck]` prints a ledger of every distinct token once, with its type, a line of its text and
  "Deck (makers)" per deck; with a deck, that deck's tokens and makers. `--json` gives both.
- `list.md` ends with `## Tokens (N)`, a line per token such as
  `- W 1/1 Vampire (Lifelink): Charismatic Conqueror, Preacher of the Schism` (a creature token with text gets
  its text without reminder text or full stops, lines joined by semicolons and cut at 40 characters; emblems
  and helpers are just their name), then `- Check by hand: ...` when there are such cards. A deck that makes
  no token has no section. The section follows a `<!-- cmdr:tokens -->` line, which is how a save finds it: a
  deck category named Tokens has the same heading. `Workspace.save` carries over the section its list had;
  `run_sync` rewrites it for every deck once the card data is in, under the deck's lock, through
  `Workspace.write_list`. Without card data the sections stay as they were, and a list written before the
  marker has no section to carry until the next sync writes one.
- The app's deck grid ends with a group of the tokens' images, `kind: "tokens"`, `inDeck: false`, in no total
  and with no counts, labels or swaps; a tooltip and the screen-reader text name the cards that make a token.
  The page draws it with `textContent` only.

## Consequences

- The card data is rebuilt once after the upgrade, because the schema changed: the first `sync` downloads the
  80 MB file again.
- Everything Scryfall links appears: the Foretell and Manifest cards, for one, are linked as parts of their
  cards and are listed as helpers. If that is noise, the filter belongs in `tokens.py`, not the build.
- The lists depend on the community-kept links; a card Scryfall has not linked yet shows up in "check by
  hand" if its text says it creates a token, and nowhere if it does not. The two text rules are phrase
  matches and will misjudge a few cards ("a copy of target creature, then create a Treasure token" in one
  sentence reads as a copy); each is one regular expression with its tests.
- Tokens are not read from Scryfall's per-card API, so a token printed this week is known only after the card
  data is next rebuilt.
- A save from the board, `apply` or a label change keeps the section it found, which can be a sync behind the
  deck until the next `sync` writes it.
- Odd-layout tokens and dungeons no longer enter the card tables. No deck can hold them, so no query changes.
- Four `combo_piece` links in the card data are wrong and are listed as they are: Throne Warden names The
  Monarch, Eye of Vecna and Hand of Vecna name Vecna, and Adrix and Nev, Twincasters names the Copy marker.
  They are four cards among thousands, and a filter for them would be a list of exceptions to keep.
