# Changelog

## Unreleased

## 0.8.0 - 2026-10-21

### Fixed
- `sync` fetches TopDeck's events once, in the background, while it reads every deck's Archidekt and
  Spellbook sources, and only the decks at bracket 4 or 5, whose tournament lists need them, wait for them
  (ADR 0047). Before, the first deck of a first sync waited for the whole fetch, minutes for a 60-day window,
  before anything else was read, and every deck's merge waited for it.
- The sync's progress line names every read under way ("TopDeck: recent tournaments · Archidekt: recent
  Edgar Markov decks"), drops each as it ends and shows a wait for TopDeck's rate limit ("TopDeck: waiting 30 s
  (rate limit)"). Before, it showed the last one that started, and said Archidekt while TopDeck waited.
- Testing a TopDeck key no longer fails with HTTP 400: the check now names the game and format, which TopDeck
  requires.


### Changed
- Tournament lists now come from TopDeck.gg instead of EDHTop16, whose API has needed a key since 2026-10-19
  (ADR 0047). Each user needs a free TopDeck key: `cmdr topdeck` asks for it (never on the command line), or
  the app's new **TopDeck** button has the steps, a **Test key** and a **Save key**; it is kept in Windows
  Credential Manager. Without one, `meta`, `sync` and the app say why the tournament lists are missing and carry
  on with Archidekt's decks. Events of 16+ players are fetched seven days at a time, twelve seconds apart (TopDeck
  answers bulk requests at about five a minute, and a 429 is waited out for as long as it asks, up to five
  minutes a request), only for days not seen yet, and kept per event in the cache folder; the commander's
  lists and the most-played commanders and their shares are worked out from them, and player names and ids
  are never stored. `[meta] min_event_size` runs from 16 (also its default): a smaller value in the config is
  raised to 16 with a logged warning that says how to silence it. Every place the data shows, the app's Meta
  and Considering panels and the CLI output, credits and links TopDeck.gg.
- `sync` now deletes the folder of a deck deleted on Archidekt (ADR 0045), where it only reported it before: a
  signed-in sync does so when the deck is missing from the complete listing and its own page answers 404, and
  reports `removed: <name>`; `--dry-run` says `would remove`. Anything else keeps the folder and reports
  `missing`. The app closes the open deck with a notice if it was the one removed.
- A card in the deck is never in Considering (ADR 0046): `sync` closes every open card the deck's main list
  now holds as applied ("already in the deck") and prints `<deck> board: closed ...`, and the app hides such a
  card without waiting for the sync. Sideboard and Maybeboard cards, basic lands and cards a deck may run more
  than one of stay ideas. A queued card
  the deck holds stays queued for Review's "Mark N card(s) applied".
- The board file is version 3 (count changes); an older one is kept as `board.v2.bak` on its first save.
- The side panels' titles look like the Meta panel's sub-toggles, with the same markers on one line, and both
  turn green under the pointer instead of taking a background. The Activity and Mana rail is hidden while no
  deck is open.
- The log now records the `Retry-After` Archidekt sends with a 429 (or `none`) beside the URL, and, at DEBUG,
  how long each Archidekt request took, so the 180 s cooldown can be judged from real answers (ADR 0027). The
  cooldown itself is unchanged.
- Archidekt searches are refreshed every third day instead of daily, each commander and deck tag on a day of
  its own (a stable hash of its name), so a sync refreshes about a third of them instead of all, with a third of
  the paced Archidekt requests. The ideas built from them may be up to
  three days behind Archidekt (ADR 0027).
- A refresh of an Archidekt search pages only through the decks new or edited since the last one: the
  search lists the newest update first, so it stops at the first deck it already holds, and keeps the rest from
  the cached hits. Each commander and tag is still paged in full about once a week, on a day of its own, so
  view counts do not go stale. Cached searches from before this stay usable and are paged in full on their
  group's next refresh day, so they are not all searched at once (ADR 0027).

### Removed
- Spicy picks, the spice figure of `meta --card` and the `spice` fields of `meta --json`: TopDeck has no spice
  rating. The EDHTop16 client, its fixtures and its cached answers (deleted on the next run).

### Fixed
- A TopDeck window kept in the cache still answers on a later day when there is no key, TopDeck refuses it
  or is down: the lists come from the stored events, marked as a stale copy with the reason, where the first
  day after the last fetch showed nothing.
- After TopDeck fails (no key, a refused key, down), a sync or `meta` run no longer asks it again for every
  deck: the first failure stands for the rest of the run.
- `sync` now says when TopDeck refused the key, not only when there is none ("tournament lists skipped:
  missing or refused TopDeck key"), once per sync, and marks the decks incomplete either way.
- A TopDeck answer over the 20 MB cap no longer blocks the window for weeks: that span is asked for again in
  halves, down to one day, and a single day still too big is skipped with a logged warning.
- `cmdr board plan` keeps quantities and kept copies (a basic land swap printed plain names before).
- The Mana tab may suggest a land that only waits on the Maybeboard.
- A card change refused while a sync runs is logged with the sync's phase, and the message the app shows
  keeps to 12 words: "Wait for the sync to finish, then sign in." (the phase is no longer in it).
  That holds for a decision refused while the card data downloads too, which is logged once, at INFO.
  A deck with a long name of its own is called "this deck" there, so the message stays within 12 words.
- Card changes, settings and saves to Archidekt no longer wait for the whole sync, which on a first sync of
  many decks could take most of an hour: a deck's changes wait only while the sync may still rewrite that deck,
  which it pulls first when it is the one open, and changes that look cards up also wait while the card data
  downloads (ADR 0042). Each deck's new ideas are merged under that deck's lock, around what you changed
  meanwhile.
- The decklist's stats and + and − count a card Archidekt holds as two entries (two printings, or foil and
  not) by its copies in all: six Forests and four more were counted as four after a queued change, and a
  split Seven Dwarves offered + on both entries until the board refused.
- A queued count decrease means "at most that many" (ADR 0043): when the card was cut on Archidekt after
  "5 → 3" was queued, the save no longer adds three copies back. A queued increase means "at least". `sync`
  clears the count changes the deck already meets and prints `<deck> board: cleared ...`, so they no longer
  count as pending or block choosing the card as a cut.
- A queued count increase of a card the deck no longer holds is dropped like a decrease: when the card was cut
  on Archidekt after "3 → 5" was queued, the next save no longer adds five copies back (ADR 0043).
- A label change no longer holds the card data open across its Archidekt call, and a decision or a removal
  made while a first sync is still downloading the card data says to wait for it instead of "run cmdr sync"
  (ADR 0042).
- A deck folder deleted by `sync` is renamed to `.removing-<name>` under the board lock and then deleted, so a
  file held open by another program no longer leaves a half-deleted folder that is never looked at again: the
  next sync retries it. A folder that still cannot be deleted is only a warning naming it (reported as
  `missing`, logged at WARNING): `cmdr sync` still exits zero, and the app's status bar says "1 old deck folder
  couldn't be deleted; see the log" instead of counting it as a synced deck. A deck whose board the app is
  writing is kept for that sync (ADR 0045).
- The app closes a deck deleted on Archidekt even when its old folder could not be deleted yet; it kept the
  deck open before, and every action on it failed with "deck N is not in ...; press Sync".
- A deck listed twice by Archidekt's paged listing (one edited between two page reads) is fetched and
  reported once; the listing is read again from page 1 first, because the edit may also have moved another
  deck off every page read, and only a second drift keeps the first row (logged at INFO).

### Added
- − and + on the decklist's cards (ADR 0043): − takes one copy out (at one copy, the card), + adds one to a
  basic land or a card whose rules text allows more. The change is queued on the board, shown on the card
  ("×6 → 7", *Out*) and saved through Review. `cmdr board count <deck> <card> <copies>` does the same.
- Add a card without a cut: a clicked idea offers **Add without a cut** under one of the deck's categories,
  preset to its card type's, labelled Don't Have when saved; `cmdr board decide ... add --category NAME`.
  Cards queued to come in show in the decklist until saved.
- The Sideboard, Maybeboard and any other category Archidekt keeps outside the deck show under the decklist
  as groups of their own, with share pills, counted in no total (ADR 0044).
- Stats above the decklist: cards in the deck, Don't Have cards and their cost, and the average mana value of
  the spells; with changes queued, "saved → after Review".
- The log records each sync's start, phases, end and duration; every refused write; each Archidekt save and
  board proposal or decision; and errors on the app's page, so a handed-over log shows what went wrong.

## 0.7.1 - 2026-10-19

### Changed
- A suggested cut must be clearly less played than the idea (ADR 0041): at least 30 points less, or half the
  idea's share when that is smaller, so a 90% idea no longer suggests replacing an 80% card. Basic lands and
  the cuts you or Claude picked are unaffected; an idea left with no cut waits in *No cut yet*.

## 0.7.0 - 2026-10-19

### Added
- Grace cards (ADR 0039): a deck's `profile.toml` can name cards under `[grace]`, each with a factor over 1 and
  at most 10. A meta list or recent Archidekt deck holding them counts for more (the factors multiply, at most
  10) in that deck's percentages, cut targets and compiled ideas, for a commander that runs several different
  lists. Every deck's profile carries an empty `[grace]` table: `cmdr sync` adds it to existing profiles (a
  broken one is left alone) and writes the template, with the table, for a deck that has no profile. The meta
  breakdown is unchanged. `cmdr profile` lists the grace cards, and the percentage's breakdown in the app says
  which are set and how many lists hold one. The deck's Settings in the app list the grace cards too: add a
  card with its factor (3 to start), change a factor or remove a card, and *Save settings* writes `[grace]`
  with the rest, keeping your comments and the example, and refusing a card name that matches no card.
- `cmdr sync` and the app's Sync also pull your private and unlisted decks when a login is saved (ADR 0040): the
  deck list and each deck are read with it. Without a login, or with one Archidekt refuses, sync lists the
  public decks alone and warns that private and unlisted decks were not updated (without a login, only when
  such decks are already in the folder); those decks stay as they are and are not reported as missing.

### Changed
- Cut targets for an idea (ADR 0036): every candidate scores on one number and the best four are offered (were
  three, like-for-like cards first). A deck that holds more than the peers' median of ramp, draw, removal or
  tutors, or of a theme, offers those cards for ideas of another role or theme; a deck with lands to spare
  offers a basic land or a less played nonbasic land (one that only makes mana, with no role or theme) for a nonland idea. A role or
  theme surplus needs the deck 2 cards over the peers' median. The reason says which. A theme the peers
  were not counted for no longer lets a card go for an idea of another theme.
- Meta combos (ADR 0037): every combo making up at least 5% of the meta is listed, most exposed first, at most
  100 (was the top 15). A list weighs less the older its tournament and more the bigger the event, as in the
  card shares, so a combo spreading now counts at its present share. `cmdr meta` and the app's Meta panel
  mark a combo "rising" when its share of the last 30 days' lists is twice that of the 30 days before
  (or two lists hold it where none did); the JSON has `rising`.
- Meta combos that share at least two pieces, the commander included, are listed together (ADR 0038): a
  "Shared:" line names the pieces and each combo shows only its others, in `cmdr meta` and the app's Meta panel.
  The app's answer carries them as `comboGroups`.
- A land whose mana ability can make more than one mana (Ancient Tomb, Gaea's Cradle, Cabal Coffers, Nykthos,
  Urza's Tower) is a utility land, never offered as a spare land (ADR 0036).
- The app's main panel titles (Considering, Meta) are a step smaller in size than the sections inside them, but
  in bold capitals, so they read as the larger heading. They line up with the sub-sections and lost their
  hover background. Each category under *What stops you* folds, staying as you left it.

## 0.6.0 - 2026-10-18

### Added
- One weighted share per card (ADR 0026): the share of the commander's EDHTop16 lists and recent Archidekt
  decks that play it, each list weighing more the more recent and, for a tournament entry, the better placed
  and the bigger the event. It shows as a pill on every grid card but the commander, with the raw count and
  each source's part in the tooltip, and on each Considering card, where it opens a breakdown (see *Changed*).
  Cut targets, the order within Considering and the return of turned-down cards use it.
- Ideas from tagged decks (ADR 0028), for decks that turn on *Search decks by tag*: each of a deck's theme tags
  is searched on Archidekt for recent decks of other commanders, and the cards at least half of a tag's decks
  play become ideas, named "Archidekt: tagged *tag*" in the breakdown. Considering also names the other sources by where they come from: "Tournament
  lists", "Archidekt: *commander* decks" and "Archidekt: alike commanders".
- A per-deck setting *Search decks by tag* in the app's Settings, off by default (`[ideas] tag_search = true`
  in `profile.toml`, shown by `cmdr profile`). While it is off, `cmdr sync`, `cmdr meta --to-board` and the
  app's update make no tag search for the deck, because tag pools added almost nothing beyond the commander's
  own decks and each tag costs about 1.5 to 2 minutes on a first sync. Tagged ideas already on a board stay
  until the next sync tests them by the commander's own lists (the re-judging entry under *Changed*).
- Ideas with no deck card to replace are put on the board as well and listed under *No cut yet* in the app,
  where clicking one offers every deck card but the commander.
- Cuts of another role or theme (ADR 0029): when like for like leaves a slot free, a deck card of another role
  or theme may be offered while the deck keeps what other players of the commander run (the 25th percentile
  of each role, the median of each theme; an other-theme cut also needs the idea 20 points more played). Roles
  are counted by Tagger tag alone, as the peers' are. A cut of another role scores 3 points per card a role it
  fills runs over the peers' median, so an over-stocked role gives up cards first. The reason says what allows
  it.
- Every suggested cut is less played than the idea, a card no list plays counting as 0; without list data
  there is no such gate. An idea left with no cut goes to the *No cut yet* panel. A cut you pick yourself is
  never removed.
- A swap that takes a land out or puts one in is offered only while the land count ends within one of
  Karsten's, closer to it than before, or inside the range of three or more other lists, and every colour
  keeps the sources its spells need (ADR 0030).
- Tempo, Midrange, Aggro and Combo deck tags weigh the meta's staples beside Turbo, Stax and Control, as
  multipliers on the share (ADR 0031): 1.25 for a clear fit, 1.1 for a borderline one, 0.8 for off-plan.
- Sync fetches ideas for the deck open in the app first, then for the rest by how often you open them (ADR 0034):
  each open adds 1 to the deck's score, which halves every 30 days and is kept in `views.json` in
  `%LOCALAPPDATA%\Commander`, on this machine only. A deck opened during a sync goes next, once the commander
  being fetched is done. `cmdr sync` goes by the same scores. While a sync runs, each deck in the list shows
  its step: waiting, fetching, fetched, or incomplete when a source failed.
- A save that could not read the account's labels and used Archidekt's default colours says so in the app,
  and after a sign-in the focus moves to the first deck, or to Sync.

### Changed
- The Sideboard and Maybeboard are retired as a second home for ideas (ADR 0035). `sync` no longer puts their
  cards on a board and writes nothing to them; a cut removes the card from the deck instead of moving it to
  the Maybeboard. A save (Review, `board commit --save`, `apply --save`) leaves the cards on the deck's
  Sideboard and Maybeboard alone. A cut card returns as an idea like any card not in the deck, an applied
  board card the deck no longer holds included.
- On every sync, the open ideas `cmdr` compiled earlier (from the meta, the commander's Archidekt decks, alike
  commanders or tagged decks), and the open cards an earlier version took from the Maybeboard, are tested
  again like new ideas (ADR 0028). Those that fail are turned down as "no longer makes the cut" (activity
  line by `cmdr`) and return only if their share rises 20 points; the rest stay. An alike or tagged idea is
  judged by its pool only when that pool was read in the sync. One whose source is gone is tested by the
  commander's own lists instead: an alike idea once the profile lists no commander, a tagged one while the
  deck's tag search is off or after its tag left the deck. A card that fails is turned down, one that passes
  is relabelled to the source that plays it more, with a compiled idea's reason. Nothing is judged without
  share data, and cards by you or Claude, queued, applied or turned-down cards, and cards you reopened are
  not touched.
- Reconsider on a turned-down card from the old Maybeboard pick-up (or one `cmdr` compiled) labels it again from
  today's cached lists: the source that plays it more, the meta on a tie, and a compiled idea's wording, so it
  takes that source's colour and group in Considering. A card no list plays keeps its neutral label and the
  reason "reconsidered; no list plays it". A reconsidered card is not turned down again by the sync's re-judge
  until you decide it (`reconsidered` on the board card, optional), and each sync labels the open cards from the
  Maybeboard pick-up that pass the test the same way (ADR 0035).
- A Considering card shows its combined share as a pill coloured by the source the idea is credited to
  (tournament lists, the commander's Archidekt decks, alike commanders, tagged decks), in place of the source
  badge and the plain count. Clicking the pill opens a breakdown: the combined share with its count, then each
  source's share and count. For an alike or tagged idea the pill shows the share of the pool of decks it was
  found in, and those two groups are ordered by it. The pill reads only the percentage ("60%"); its label and
  tooltip say what it is of ("60% of Landfall-tagged decks"). The board card keeps the pool's numbers
  (`originPool`) and the next sync adds them to ideas already there.
- *No cut yet* is a side panel of its own between Considering and Meta, shown only while it holds ideas, in
  place of the fold inside Considering.
- The app no longer opens the first deck by itself: the main area is blank until you pick one.
- The app's page shows no one until a sign-in succeeds: at start without a signed-in account, after **Sign out**,
  and after leaving the sign-in dialog any other way (Not now, Escape). The dialog blurs and dims the app
  behind it, and the page shows "Sign in to see your decks" with a **Sign in** button, no deck list and no
  account name, and Sync off. The config, the saved login and the CLI are untouched. So Sync is off on a
  signed-out page even when a username is set.
- The app's **Update from Archidekt** button is **Sync**. While it runs it shows a spinner and the step it is
  on. The Meta panel shows the deck's cached answers during a sync, says it is updating only while that deck
  is queued or being fetched, and rereads them when that fetch ends. Without an account the button stays
  visible, off, with "Sign in to sync" beside it.
- Other players' Archidekt decks are read more widely and more carefully (ADR 0027). The search covers the
  deck's own bracket and the one above, up to 20 pages, and keeps the decks with 1000 views, stepping down to
  750, 500 and then every deck until ten pass. `peer_decks` is now the cap on decks read per commander:
  default 60, 0 to 60 (was 10, 0 to 25). Searches and deck reads are five seconds apart (was two), and a 429
  holds every request for three minutes or the `Retry-After`; a second one ends Archidekt for the rest of the
  run, which uses its cached answers. A deck that answers 403, 404 or 410 (made private or deleted) is skipped,
  not counted as a failed source. Each deck read is cached until its owner edits it, and a fetching run
  deletes cached decks unused for 30 days or last updated before the window.
- Decks at bracket 4 read their commander's EDHTop16 lists too, and a report uses the top-cut lists whenever
  one list made the cut (was five).
- Ideas (ADR 0028): a card from the commander's lists or decks is an idea when the Wilson lower bound of its
  weighted share reaches 0.30 (was 40% of the lists or half the decks), judged over every list read, not
  only the top cuts (the reason reads "in N of M lists"). Each source takes its ideas
  by the source's weighted share times the play-style weight, and only cmdr's own ideas of that source count
  towards its cap of 50; cards by you or Claude take no place.
- `cmdr meta --to-board` compiles the deck's ideas from all four sources as `sync` does, not the meta's
  staples alone.
- Play-style weights replace the three bands of ADR 0022: staples sort by share times weight, and the report
  says "weighed for turbo" where it said "turbo plan first". Stax now favours removal and bounce, where it
  favoured cheap interaction. A report with no top-cut list says "no top cuts" where it said "few".
- A deck has one board list, **Considering**, in place of Considered and Suggested; `board.json` is version 2
  and a version 1 file is copied to `board.v1.bak` before its first save.
- A turned-down card returns once its weighted share has risen 20 points, at every bracket (was the share of
  the top-cut lists at bracket 5 only).
- Reconsider applies to turned-down cards only: undoing a queued swap is a plain reopen, and the sync never
  turns down a card you reopened or one by you or Claude.
- Cut targets are ranked by the weighted share, then by EDHREC rank.
- The Mana tab works out how often a check or reveal land enters tapped from the deck's own lands: the
  chance that none of the nine cards seen by turn three has a basic land type the land asks for (ADR 0033).
  Fast lands and Fabled Passage stay at a quarter. The mana plan counts a land as untapped when it enters
  tapped a quarter of the time or less.
- `cmdr profile` says whether a broad deck tag is a play style, a role or a card type ("a role, not a theme").
- The account's labels are read once more after three seconds when a save cannot read them, then
  Archidekt's default colours are used.
- The app page's behaviour is tested by running its scripts under Node against a fake DOM; the tests skip when
  `node` is not on the PATH, and Node stays a test-time tool.

### Removed
- The Meta panel's **Refresh meta** button: Sync refreshes the meta answers, as `cmdr sync` does.
- `cmdr board add --suggested` and `board research --move`: there is one list.
- Plan files' `maybeboard = true` and `drop = [...]`, and the Meta report's mark on staples that sit on the
  Sideboard or Maybeboard (`onSideboard` in `meta --json`). Old boards and logs with source `archidekt` still read.
- Adding a card to the Maybeboard or Sideboard from a plan (`add` with either category is refused), and
  `--by archidekt` on the board commands.

### Fixed
- Fellwar Stone counts as half a source, as ADR 0019 says; the code gave it three quarters.
- The app's Sync refuses while a save, a label change, the swaps, a board edit, saving the Settings or a sign-in
  or sign-out is running ("Wait for the save to finish, then sync."), as those already refuse while a sync runs, so the two can no longer touch the same
  deck files at once.
- One malformed EDHTop16 entry, or a test event dated in the future, no longer fails a commander's whole read:
  the entry is logged and skipped, and later-dated events are left out.
- `cmdr board log --since` compares a UTC time like `2026-10-15T18:00` as the time it is, and refuses any other
  format; it used to compare raw text.
- The Meta panel counts a sweeper as reaching creatures only when it is a board wipe, as ADR 0020 says.
- The sign-in password field is cleared however the call ends, not only when it returns.

## 0.5.0 - 2026-10-17

### Added
- Cardmarket wants list: the app's "Copy cards to buy" button and `cmdr toget --cardmarket` give every deck's
  main-deck cards labelled "Don't Have" as `<quantity> <name>` lines to paste into Cardmarket's "Add Decklist"
  field. Copies add up across decks; decks whose labels are unknown are named and left out.

## 0.4.0 - 2026-10-17

### Added
- Below bracket 5 every idea keeps to the deck's budget (ADR 0025): up to 25% over its average card is
  within budget, and 25% above its dearest card is the ceiling. `suggest`, the ideas compiled on sync and the Mana tab put ideas
  within budget first and offer none above the ceiling; Considering marks the rest "Over budget".
  `cmdr suggest --any-price` ignores the deck's budget.
- Considering lists your ideas first, then Claude's, the meta's, the Archidekt decks' and the rest; within
  each, over-budget ideas last and the others by how many lists play them. Each sync now compiles up to 50
  ideas per source (was 10).
- The app's log records signing in and out, with the account and its deck folder (never the password).
- The Mana tab suggests how the mana base should change below bracket 5 (ADR 0024): the land count when
  it is off Karsten's formula and outside other lists' range, the basics moved from colours with sources
  to spare, named untapped lands for gaps basics can't close, and untapped twins for plain tap-lands.
  Named lands keep to the deck's budget: within its average card first, then up to 25% over its dearest
  card, shown apart. One button puts the swaps on the board; a board card can now add several copies of a
  basic.
- Previews show the bracket after the changes: `apply`, `board commit` and the app's Review dialog ask
  Commander Spellbook about the deck as the plan leaves it and show its estimate ("3-4 (Spicy), was 4+"),
  with the notes and causes of `bracket` when it lands above the Archidekt bracket or the profile's ceiling.
  `apply --json` gains `bracketAfter`.
- Cards to get (ADR 0010): cards a save brings into a deck get the Archidekt label "Don't Have";
  `cmdr toget` lists each deck's Don't Have and Getting cards and marks one `--ordered` (Getting) or
  `--arrived` (no label) on Archidekt at once. The app's bridge has the same (`toGet` in the deck view,
  `mark`). Deck snapshots keep each card's label (version 3; the next `sync` fetches each deck once).

- App redesign (ADR 0011): the deck shows as a card grid by card type, with counts and EUR totals.
  Considered holds every idea that has a card to replace; hovering or clicking one lights its targets in
  the grid, a click on a target queues the swap, and × turns the idea down. Targets are the ones you or
  Claude picked, else the weakest cards in the same role by meta share and EDHREC rank. Each grid card
  has a corner flag that sets its Archidekt label. The Meta section (bracket 5) shows key cards with the
  meta cards that stop them and meta combos with your answers, tagged with the meta decks playing them,
  and a refresh puts the meta's staples on the board. A turned-down card returns when its meta share
  rises 20 points. Deck snapshots keep label colours (version 4; the next `sync` fetches each deck once).

- Card labels come from your Archidekt account (ADR 0012): every deck offers the account's labels in
  their colours, and Don't Have and Getting are written in the account's colours. A signed-in `sync`
  keeps the list in `labels.json` beside the deck folders; each label change and save reads it first.

- `cmdr meta` and the app's Meta section check every bracket 5 deck's own combos against the meta (ADR 0013):
  each combo from Commander Spellbook with the cards the top commanders' lists commonly play that stop it
  (stax, removal, counters), tagged with the commanders playing them. Etali's Food Chain loop, Glarb's
  Thassa's Oracle lines and Magda's artifact loops now show what threatens them.

- `cmdr sync` fills Considered from other players (ADR 0014): the staples each deck misses from its
  commander's EDHTop16 lists (bracket 5) and from the most-viewed recent Archidekt decks at its bracket,
  up to 10 per source, only cards with something to replace. EDHREC is never asked. The app's badge
  shows where an idea came from. `sync --no-ideas` skips it; `--json` lists the new ideas per deck.

- Ideas from commanders that play alike (ADR 0015): a profile's `[ideas] commanders` names other
  commanders, or partner pairs, whose recent Archidekt decks at the deck's bracket `sync` pools; cards
  half the pool plays become up to 10 "adjacent decks" ideas. `cmdr profile` lists them.

- What a deck leans into (ADR 0016): the Tagger tags its cards hold far more of than recent decks with
  its commander, listed by `cmdr profile` (and `--json`) and in the app's deck header.

- The app's Meta panel lists each meta card once (ADR 0018): *What stops you* merges "Your combos" and
  "Against your key cards" into one row per meta card, grouped as stax, removal or counters, with chips
  for the combos and key cards it stops. Key cards show as chips that say why on hover. Shorter headers:
  Key cards, What stops you, Meta combos.

- App: a Mana tab beside Activity (ADR 0019) checks each deck's mana base against Frank Karsten's land
  count formula and colour source table, and compares the land count with the commander's top-cut lists
  (bracket 5) or recent Archidekt decks. Card data schema 6 keeps the mana each card makes; peer caches
  keep how many of each basic land a deck runs.
- App: Settings for each deck edits the profile's commanders that play alike and themes to ignore, with
  autocomplete for commanders, the deck's own themes and leanings, and Tagger tags. The file keeps its
  comments (new dependency: tomlkit 0.15.1).
- App: larger grid cards by default, with S/M/L sizes; clicking a card shows it large (× or Esc
  closes it); a clicked idea in Considering can replace any deck card, not only the suggested ones;
  Export copies the saved deck in TopDeck.gg's decklist format.
- The app's Considered panel is now Considering. Turning a card down asks why (optional); turned-down cards
  list under *Rejected*, closed until opened, with their reasons and a Reconsider button. Panels and the
  Meta sections fold and remember it.

- Bracket discrepancies come with their causes (ADR 0021): when the Archidekt bracket, the profile's
  `bracket_ceiling` and Commander Spellbook's estimate disagree, `cmdr bracket <deck>` and the app say how
  and name the cards and combos that lift the estimate, by Spellbook's own rules. The app shows an Estimate
  chip under the deck's name and badges the causing cards in the grid. Spellbook answers are fetched once
  more (cache version 3).

- A theme counts once in idea labels and ranking: profile themes a deck tag already names go under the
  deck tag, and deck tags meaning the same share a label ("Power Matters/Stompy"). `cmdr profile` lists the
  profile themes the deck tags already name.

- Play-style deck tags weigh `cmdr meta` (ADR 0022): Turbo puts speed staples first, marks interaction
  off-plan and stops counting unanswered meta combos as gaps ("racing"); Stax puts stax pieces and cheap
  interaction first; Control puts interaction first. Off-plan staples are no longer compiled as ideas, and
  the app's Meta panel shows a Turbo deck's unanswered combos as "Racing".

- Accounts and sync in the app (ADR 0023): the app signs in to any Archidekt account, gives each account
  its own deck folder (the config's `[workspaces]`; yours stays where it is), syncs a new account's decks at
  once, and has Update from Archidekt and Sign out at the foot of the deck list. Without a config it opens on the
  sign-in screen instead of stopping. `cmdr sync` and the app share one sync (`syncrun.run_sync`).

- The app and `Commander.exe` have their own icon, a low-pixel card back with see-through corners
  (`tools/make_icon.py` draws it).

### Fixed
- Board saves plan from the deck as it is on Archidekt: a queued card the deck already holds is marked
  applied and its cut is not made, and nothing is written when every queued card is already there. Cuts
  reach a card split over several entries, the commander is never cut, a move into the deck brings the
  copies asked for, and a swap whose cut failed stays queued. `cmdr board plan` does the same and gains
  `--user`.
- A board left locked by a crashed run can be taken over safely, and a failed activity write is reported
  instead of lost. Old deck profiles are migrated; unreadable or oversized ones are refused with a message.
  A deck of free cards has no budget ceiling.
- Network and storage: a refused read asks you to sign in, and a dropped connection gives a plain error.
  A rotated Archidekt login is kept and an expired one is refreshed. `sync` skips decks owned by another
  account, still finishes the card data download when a deck fails, and names a card-data failure in its
  summary. Temporary files no longer collide or pile up, saves retry while Windows briefly holds a file
  open, and cache entries dated in the future count as stale.
- Research: staples are legal and in the deck's colours; peer decks must share every commander; hidden
  cards count once; meta caches keep each time window apart; a colourless deck keeps its land count with
  no Wastes (ADR 0024); a spell//land double-faced card is never a cut; the mana plan rounds halves away
  from zero; the Mana tab lists cards that cost only hybrid mana.
- Desktop app: the deck list loads when one deck's board is damaged and marks that deck with a red "!";
  Save is refused when the board changed after Review opened; saves, labels, marks and mana swaps wait
  for a running update; a button stays disabled while its request runs, so a double click acts once;
  quick deck switches and quick typing show only the latest answer; label text is black or white to suit
  the label's colour; messages name the app's buttons, not `cmdr` commands; a crash before the window
  opens reaches the log.
- CLI: an unexpected error prints one line and exits 1; board file, lock and "nothing queued" failures
  exit 1, and `cmdr init` over an existing config exits 2. A move into the deck of several copies says how
  many. Emoji deck names print when output is redirected on Windows.

- The Mana tab no longer counts the basics its named lands replace as basic cuts: Arabella's "Mountain 9 → 5"
  is now "Mountain 9 → 9 (−4 for the lands below)".
- The Mana tab counts shock lands as untapped, Evolving Wilds and its kind as tapped, and reveal lands
  and Fabled Passage as sometimes tapped.
- Saving a board that cuts a basic land takes out one copy; it used to move every copy to the Maybeboard.
- A board wipe clears creatures (ADR 0020): sweepers that remove only artifacts, enchantments, lands or
  other noncreature permanents (Vandalblast, Bane of Progress, Armageddon) no longer count as board
  wipes, unless their text deals with "each creature" (Wildfire). The card database is rebuilt on the next
  `sync` (schema 7).

- Ignoring a leaning tag no longer brings up the wider tag it had absorbed, such as gives-evasion when
  gives-flying is ignored.

- Key cards are found each time (ADR 0017), each with why: the deck's combos, cards you or other
  players of the commander file under a combo or win category, and cards costing far above the deck's
  curve, beside the commander and `[keep]`. `[key_cards] ignore` in the profile leaves one out. Peer deck
  caches keep each deck's combo and win cards; older caches count none until their next refresh.
- Replacements go like for like (ADR 0016): a card serving one of the deck's themes or leanings is offered
  only for an idea serving the same, and such swaps come first. Pantlaza's ideas no longer all offer its
  Dinosaurs as cuts. Below bracket 5, targets rank by how many recent decks play them.
- Combo answers read locks more closely (ADR 0013): Grafdigger's Cage stops casting from graveyards and
  libraries only, not from exile; Deafening Silence and Ranger-Captain of Eos no longer stop a loop that
  casts only creatures; and a lock that holds only during its owner's turn (Grand Abolisher) answers no
  combo. These apply to the meta combos' answers from your deck too.
- Desktop app: the "Thin data" and status badges no longer float at the bottom of the window.
- Ideas compiled by `sync`, `meta --to-board` and Refresh meta follow the deck's profile: its exclusions,
  bracket ceiling and price cap (Pantlaza got Ephemerate though its profile excludes flicker).

### Changed
- Releases are built on GitHub from the version tag: `Commander.exe` with `SHA256SUMS.txt` and build
  provenance (`gh attestation verify`). CI runs the full check on every push.
- `decks/` is no longer part of the repository: it holds your own decks and notes; keep their history in
  a git repository of its own.
- Escape in the app's card search closes the list of matches first and keeps what you typed.
- `cmdr tags --json` writes non-ASCII characters as they are, like the other commands.
- Code layout: `cli.py` is split into `cli_analysis` and `cli_suggest`, the app page into one script per
  area, and the mana plan into `manaplan.py`.
- App usability: ideas that name no card to replace (suggestions, Sideboard and Maybeboard cards, proposals
  matching nothing) show under *No cut yet* instead of not at all; a save says plainly how many changes went
  to Archidekt; errors stay until dismissed; *Sync decks* is *Update from Archidekt*, *Rejected* is
  *Turned down*, *Export* is *Copy decklist*; grid cards zoom from the keyboard; colours, radii, type sizes
  and focus rings all come from the design tokens.
- Commander short names skip a leading "The" and keep the first word: "Gitrog", "Tevesh".
- Desktop app: the Suggested and Decided lists are gone (suggestions join Considered; decided cards
  leave the app), the activity column folds to a rail, and ideas from `suggest` no longer all offer the
  same generic cuts.
- Desktop app: the open Meta panel updates by itself when `cmdr meta` (or anything else) saves new meta
  answers to the cache, instead of only when the panel is opened or the deck changes.
- `cmdr meta --to-board` and the app's Refresh meta add the same ideas as `sync`: at most 10 per source,
  each with a deck card to replace.

## 0.3.0 - 2026-10-16

The cEDH meta, answers read from rules text, and deck themes from Archidekt.

### Added
- Deck themes from Archidekt: `suggest` reads each deck's deck tags and its own categories as themes, beside
  the profile's (ADR 0009). Ideas name the themes they fit, themed ideas that fit more of them come first,
  and `cmdr profile` lists every theme with its source. `[themes] ignore` in a profile drops one. Deck
  snapshots keep the deck tags (version 2; the next `sync` fetches each deck once), and `sync` notices
  changed deck tags, which Archidekt makes without changing a deck's edit time. Deck tags that say how a
  deck plays (cEDH, Tempo, Commander Matters, ETB Effects, Draw) make no theme; `cmdr profile` lists them
  as such.
- `cmdr meta` for bracket 5 decks: compares a deck with its commander's performing EDHTop16 lists from the
  last two months and the most-viewed recent Archidekt decks, and lists the staples it misses, spicy picks,
  its own picks, the most-played commanders' answers to its key pieces, and the meta's most-played combos
  (from Commander Spellbook) with the deck's answers: stax pieces, removal and counterspells. `--card`
  checks one card, `--json` prints the report, `--to-board` puts the missing staples on the deck's board,
  and with no deck it shows every bracket 5 deck side by side. Sample sizes are set in the config's `[meta]`
  table; answers are cached for a day, combos for a week.
- `cmdr meta` answers come from each card's rules text as well as its tags: bounce, damage and colour hosers
  reach only what they can hit (types, colours, toughness, mana value), counters follow their conditions, and
  stax pieces must match what the combo's steps do (ADR 0008). The card database stores card colours (schema 5);
  the next `sync` rebuilds it.
- A counter answers a storm spell such as Brain Freeze only when it meets every copy (Flusterstorm,
  Mindbreak Trap); an {X} piece such as Walking Ballista no longer counts as cast free; and a tag alone no
  longer makes an instant or sorcery a stax piece (Mindbreak Trap is a counter, Silence still a lock).
- Meta combo answers name every piece a card reaches ("removes Underworld Breach or Lion's Eye Diamond") and
  show their catch in the CLI, the JSON (`catch`) and the app: soft counters ("unless they pay {2}"),
  Counterbalance's reveal, and edicts, which are now read from their text ("they choose what to sacrifice").
  Kataki's upkeep tax no longer counts as removal, and graveyard hate must seal graveyards, exile whole ones
  or keep exiling from an activated ability; Dawnbringer Cleric's one-card exile no longer counts.
- Desktop app: a Meta panel per deck with the same report, a Meta combos table included; it opens from
  cache and fetches on Refresh meta.

## 0.2.0 - 2026-10-15

Deck profiles, deck boards and the desktop app.

### Added
- `cmdr card <name>...`: a card's rules text, type, power/toughness, price, EDHREC rank, roles and Scryfall
  Tagger tags; `cmdr tags <text>` finds Tagger tags by name with their card counts.
- Deck profiles: a hand-kept `profile.toml` in a deck's folder tells `suggest` what the deck wants: its
  archetype, a bracket ceiling, a price limit, role targets, themes (type-line words, keywords, Tagger tags),
  cards and themes never to suggest, and cards or categories never to offer as cuts. `cmdr profile <deck>`
  shows it and flags unknown tags; `--init` writes a commented template.
- `cmdr suggest` lists the most played cards matching a deck's themes, ranks themed ideas first within each
  role, and lists themed deck cards last among the cuts.
- Deck boards: `cmdr board` keeps the cards considered or suggested for a deck in its folder (`board.json`),
  with the research on each (why it fits, which deck cards it could replace), decisions (add with a chosen
  cut, turn down, reopen) and an activity log (`activity.jsonl`) of every change and who made it.
  `cmdr board log` catches up on changes; `cmdr board plan` prints the queued cards as a plan;
  `cmdr board commit` previews them against Archidekt and `--save` makes them.
- `cmdr suggest <deck> --to-board` puts its ideas on the board as suggested, with the deck's cut candidates as
  cards to replace; cards turned down on the board are never suggested again.
- Desktop app: `cmdr app`, or `Commander.exe` built with PyInstaller, opens a window over every deck's
  board (WebView2 through pywebview; no web server). Propose cards with name search, pick the card to cut,
  add or turn down, review the queued changes against Archidekt and save them; it follows changes made from
  the CLI or by Claude as they land. It logs to `%LOCALAPPDATA%\Commander\logs\commander.log`.
- Sideboard and Maybeboard as "considering": `cmdr sync` puts the cards you keep there on Archidekt on the
  deck's board as considered. Adding one moves its own entry into the deck, printing and all. A card cut on
  the board moves to the Maybeboard instead of being deleted, keeping its categories behind it, and comes
  back on the board as considered, so a swap can be weighed the other way round. Turning down a Sideboard
  or Maybeboard card takes it off there with the next save.
- Plan files: a remove line takes `maybeboard = true` to move the card instead of deleting it, and
  `drop = [...]` takes cards off the Sideboard or Maybeboard.

### Changed
- The card database (schema 4) keeps each card's rules text, keywords and power/toughness, and every Tagger
  oracle tag with its sub-tags, not only the role tags. The next `cmdr sync` rebuilds it.
- `cmdr suggest` counts a card towards a role when its tags give it the role or the deck files it under a
  category of that name, so cards you filed under Ramp or Draw are no longer offered as cuts.

## 0.1.0 - 2026-10-15

First version.

### Added
- `cmdr init`, `cmdr sync` and `cmdr list`: save your Archidekt decks as `deck.json`, `list.md` and `notes.md`
  under `decks/`, and report what changed since the last sync.
- `cmdr analyze`: mana curve, card types, colour pips, game changers and role counts (ramp, draw, removal,
  board wipes, tutors, protection, recursion, counterspells), from your Archidekt categories and from
  Scryfall tags side by side.
- `cmdr price`: deck value in EUR (Cardmarket prices via Scryfall) for the printings in each deck and for
  the cheapest printings, leaving out non-tournament ones (gold or silver borders, oversized, memorabilia).
- `cmdr sync` keeps a local card database from Scryfall's daily bulk files up to date, rebuilding it when its
  format changes; `--no-cards` skips it.
- `cmdr bracket`: Commander Spellbook's bracket estimate for each deck beside the bracket set on Archidekt,
  with its combos, plus the deck's game changers, tutors, mass land denial and extra-turn cards. Answers are
  cached for 7 days.
- `cmdr suggest`: per deck, the cards that would complete a combo it is one card short of (Commander
  Spellbook), the most played cards for roles below target (Scryfall tags, EDHREC ranks via Scryfall), and
  cards to consider cutting, with EUR prices. Ideas that would lift a deck above its Archidekt bracket are
  marked; `--within-bracket` hides them and `--max-price` caps the price. Role targets can be set in the
  config file's `[targets]` table.
- `cmdr apply`: preview a plan file's cards to add and remove against the deck on Archidekt, and with
  `--save` make the changes there, after saving a snapshot of the deck on Archidekt. Plans state the end
  result, so applying one twice is safe.
- `cmdr login` and `cmdr logout`: keep or remove the Archidekt login token for `apply --save` in Windows
  Credential Manager; the password is never stored.
