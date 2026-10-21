"""The app page's behaviour: its real scripts run under Node against a fake DOM (`pagerun`)."""

import json

import pytest

from pagerun import Answer, needs_node, page

pytestmark = needs_node

# Shared by the programs: a board card as the bridge sends it, and the share objects its pill reads.
PRELUDE = """
const card = (over = {}) => ({
  id: "c1", name: "Arcane Signet", copies: 1, source: "cmdr", origin: "meta", originTag: null, status: "open",
  typeLine: "Artifact", manaCost: "{2}", priceCents: null, image: null, text: "", reason: "", note: "",
  choice: null, targets: [{ name: "Sol Ring", reason: "r", source: "cmdr" }], gameChanger: false,
  withinBudget: null, share: null, pool: null, updatedAt: "",
  ...over });
const part = (share, count, total) => ({ share, count, total });
const combined = (share, count, total, parts = {}) => ({
  share, count, total, label: `${Math.round(share * 100)}%`, meta: null, archidekt: null, ...parts });
const texts = (node, selector) => node.querySelectorAll(selector).map((n) => n.textContent);
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_tile_with_a_combined_share_shows_it_as_a_pill_button_that_opens_the_breakdown() -> None:
    tile = run("""
    const idea = card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) });
    const tile = buildTile(idea);
    const pill = tile.querySelector(".share-pill");
    const pop = tile.querySelector(".share-pop");
    return {
      pill: { tag: pill.tagName, text: pill.textContent, type: pill.getAttribute("type"),
        expanded: pill.getAttribute("aria-expanded"), controls: pill.getAttribute("aria-controls"),
        label: pill.getAttribute("aria-label"), title: pill.title, class: pill.className },
      pop: { id: pop.id, hidden: pop.hidden, role: pop.getAttribute("role"),
        label: pop.getAttribute("aria-label") },
      badges: tile.querySelectorAll(".badge.cmdr").length,
      total: texts(pop, ".share-total"),
      rows: texts(pop, ".share-row .share-source").concat(texts(pop, ".share-row .share-numbers")),
    };
    """)
    assert tile["pill"] == {
        "tag": "BUTTON",
        "text": "60%",
        "type": "button",
        "expanded": "false",
        "controls": "share-pop-c1",
        "label": "60% played; mostly tournament lists",
        "title": "60% played; mostly tournament lists: show breakdown",
        "class": "badge share-pill origin-meta",
    }
    assert tile["pop"] == {
        "id": "share-pop-c1",
        "hidden": True,
        "role": "group",
        "label": "Arcane Signet: share breakdown",
    }
    # The origin text badge is replaced by the pill; the counts are in the breakdown.
    assert tile["badges"] == 0
    assert tile["total"] == ["Combined 60% · 3 of 5"]
    assert tile["rows"] == ["Tournament lists", "60% · 3 of 5 lists"]


def test_a_tile_without_any_share_names_where_the_idea_came_from() -> None:
    tile = run("""
    const tile = buildTile(card({ origin: "archidekt decks" }));
    state.view = { short: "Magda" };
    return { pills: tile.querySelectorAll(".share-pill").length,
      badge: tile.querySelector(".badge.cmdr").textContent };
    """)
    assert tile["pills"] == 0
    assert tile["badge"] == "Archidekt: this commander decks"


def test_an_alike_or_tagged_pill_shows_the_pool_share_and_says_so() -> None:
    pills = run("""
    const alike = buildTile(card({ id: "a", origin: "adjacent decks", share: combined(0.9, 9, 10),
      pool: { share: 0.5, count: 2, total: 4 } })).querySelector(".share-pill");
    const tagged = buildTile(card({ id: "t", origin: "tagged decks", originTag: "Storm",
      share: combined(0.9, 9, 10), pool: { share: 0.25, count: 1, total: 4 } })).querySelector(".share-pill");
    const plain = buildTile(card({ id: "p", origin: "archidekt decks", share: combined(0.4, 2, 5) }))
      .querySelector(".share-pill");
    state.view = { short: "Magda" };
    const named = buildTile(card({ id: "m", origin: "archidekt decks", share: combined(0.4, 2, 5) }))
      .querySelector(".share-pill");
    return [alike, tagged, plain, named].map((p) => [p.textContent, p.getAttribute("aria-label")]);
    """)
    assert pills == [
        ["50%", "50% of alike commanders' decks"],
        ["25%", "25% of Storm-tagged decks"],
        ["40%", "40% played; mostly this commander Archidekt decks"],
        ["40%", "40% played; mostly Magda Archidekt decks"],
    ]


def test_the_breakdown_lists_each_source_and_asks_for_the_next_sync_when_a_pool_has_no_numbers() -> None:
    rows = run("""
    const both = card({ id: "b",
      share: combined(0.7, 7, 10, { meta: part(0.8, 4, 5), archidekt: part(0.6, 3, 5) }) });
    const alike = card({ id: "a", origin: "adjacent decks", share: combined(0.7, 7, 10),
      pool: { share: 0.5, count: 2, total: 4 } });
    const old = card({ id: "o", origin: "tagged decks", originTag: "Storm", share: combined(0.7, 7, 10) });
    const rowsOf = (c) => buildTile(c).querySelector(".share-pop").querySelectorAll(".share-row")
      .map((row) => [texts(row, ".share-source")[0], texts(row, ".share-numbers")[0]]);
    return { both: rowsOf(both), alike: rowsOf(alike), old: rowsOf(old) };
    """)
    assert rows["both"] == [
        ["Tournament lists", "80% · 4 of 5 lists"],
        ["Archidekt: this commander decks", "60% · 3 of 5 decks"],
    ]
    assert rows["alike"] == [["Archidekt: alike commanders", "50% · 2 of 4 decks"]]
    assert rows["old"] == [["Archidekt: tagged Storm", "Counted on the next sync"]]


def test_ideas_sort_by_group_then_budget_then_the_share_the_pill_shows() -> None:
    order = run("""
    const ideas = [
      card({ id: "alike-low", origin: "adjacent decks", pool: { share: 0.2, count: 1, total: 5 } }),
      card({ id: "alike-high", origin: "adjacent decks", pool: { share: 0.6, count: 3, total: 5 } }),
      card({ id: "meta-over", origin: "meta", withinBudget: false, share: combined(0.9, 9, 10) }),
      card({ id: "meta-low", origin: "meta", share: combined(0.3, 3, 10) }),
      card({ id: "meta-high", origin: "meta", share: combined(0.5, 5, 10) }),
      card({ id: "claude", source: "claude", origin: null }),
      card({ id: "you", source: "you", origin: null }),
      card({ id: "plain", origin: "tagged decks" }),
    ];
    return sortSuggestions(ideas).map((c) => c.id);
    """)
    assert order == [
        "you",
        "claude",
        "meta-high",
        "meta-low",
        "meta-over",
        "alike-high",
        "alike-low",
        "plain",
    ]


@pytest.mark.parametrize(
    ("origin", "key"),
    [
        ("meta", "meta"),
        ("archidekt decks", "decks"),
        ("adjacent decks", "adjacent"),
        ("tagged decks", "tagged"),
        (None, "none"),
    ],
)
def test_the_share_pill_takes_the_colour_key_of_the_origin_the_idea_is_credited_to(
    origin: str | None, key: str
) -> None:
    classes = run(
        f"const idea = card({{ origin: {json.dumps(origin)}, share: combined(0.5, 1, 2),"
        " pool: { share: 0.5, count: 1, total: 2 } });"
        'return buildTile(idea).querySelector(".share-pill").className;'
    )
    assert classes == f"badge share-pill origin-{key}"


def test_one_breakdown_is_open_at_a_time_and_a_click_inside_it_does_not_select_the_tile() -> None:
    seen = run("""
    setupSharePopover();
    renderConsidering([
      card({ id: "one", name: "One", share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) }),
      card({ id: "two", name: "Two", share: combined(0.4, 2, 5, { meta: part(0.4, 2, 5) }) }),
    ]);
    const pills = $("list-considering").querySelectorAll(".share-pill");
    const pops = $("list-considering").querySelectorAll(".share-pop");
    const open = () => pills.map((p) => p.getAttribute("aria-expanded"));
    const shown = () => pops.map((p) => !p.hidden);
    await fire(pills[0], "click");
    const first = { open: open(), shown: shown(), selected: state.selected };
    await fire(pills[1], "click");
    const second = { open: open(), shown: shown(), selected: state.selected };
    await fire(pops[1].querySelector(".share-total"), "click");
    const inside = { open: open(), selected: state.selected };
    await fire(pills[1], "click");
    const again = { open: open(), shown: shown() };
    return { first, second, inside, again };
    """)
    assert seen["first"] == {"open": ["true", "false"], "shown": [True, False], "selected": None}
    assert seen["second"] == {"open": ["false", "true"], "shown": [False, True], "selected": None}
    assert seen["inside"] == {"open": ["false", "true"], "selected": None}
    assert seen["again"] == {"open": ["false", "false"], "shown": [False, False]}


def test_escape_closes_the_breakdown_and_refocuses_its_pill_before_the_selection_lets_go() -> None:
    seen = run("""
    setupSharePopover();
    setupPanels();
    renderConsidering([card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) })]);
    const pill = $("list-considering").querySelector(".share-pill");
    const pop = $("list-considering").querySelector(".share-pop");
    state.selected = "c1";
    await fire(pill, "click");
    await fire(document, "keydown", { key: "Escape" });
    const afterFirst = { hidden: pop.hidden, expanded: pill.getAttribute("aria-expanded"),
      focus: document.activeElement === pill, selected: state.selected };
    await fire(document, "keydown", { key: "Escape" });
    return { afterFirst, selectedAfterSecond: state.selected };
    """)
    assert seen["afterFirst"] == {"hidden": True, "expanded": "false", "focus": True, "selected": "c1"}
    assert seen["selectedAfterSecond"] is None


def test_a_click_outside_the_breakdown_closes_it_without_moving_focus() -> None:
    seen = run("""
    setupSharePopover();
    renderConsidering([card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) })]);
    const pill = $("list-considering").querySelector(".share-pill");
    await fire(pill, "click");
    await fire($("main"), "click");
    return { expanded: pill.getAttribute("aria-expanded"), focused: document.activeElement === pill };
    """)
    assert seen == {"expanded": "false", "focused": False}


def test_clicking_a_tile_outside_its_controls_selects_it() -> None:
    selected = run("""
    renderConsidering([card({ share: combined(0.6, 3, 5) })]);
    await fire($("list-considering").querySelector(".card"), "click");
    return state.selected;
    """)
    assert selected == "c1"


def test_no_cut_yet_ideas_fill_their_own_panel_and_hide_it_when_there_are_none() -> None:
    shown = run("""
    const queued = card({ id: "q", status: "add", choice: "Sol Ring" });
    const waiting = card({ id: "w", name: "Waiting", targets: [] });
    renderConsidering([queued, waiting]);
    const withIdeas = { panel: $("uncut").hidden, count: $("count-uncut").textContent,
      uncut: $("list-uncut").children.length, considering: $("list-considering").children.length };
    renderConsidering([queued]);
    return { withIdeas, none: { panel: $("uncut").hidden, count: $("count-uncut").textContent } };
    """)
    assert shown["withIdeas"] == {"panel": False, "count": "(1)", "uncut": 1, "considering": 1}
    assert shown["none"] == {"panel": True, "count": ""}


def test_considering_and_no_cut_yet_never_list_a_card_the_deck_holds() -> None:
    shown = run("""
    renderConsidering([
      card({ id: "a", name: "Held With Cut", held: true }),
      card({ id: "b", name: "Held No Cut", held: true, targets: [] }),
      card({ id: "c", name: "Sideboard Card", held: false }),
      card({ id: "d", name: "Waiting", held: false, targets: [] }),
    ]);
    return { considering: texts($("list-considering"), ".card-name"),
      uncut: texts($("list-uncut"), ".card-name"),
      count: $("count-considering").textContent, uncutCount: $("count-uncut").textContent };
    """)
    assert shown == {
        "considering": ["Sideboard Card"],
        "uncut": ["Waiting"],
        "count": "(1)",
        "uncutCount": "(1)",
    }


def test_a_queued_card_the_deck_holds_stays_in_considering_for_review_to_mark_applied() -> None:
    names = run("""
    renderConsidering([card({ id: "q", name: "Queued", status: "add", choice: "Sol Ring", held: true })]);
    return texts($("list-considering"), ".card-name");
    """)
    assert names == ["Queued"]


def test_a_card_leaves_considering_when_the_reloaded_view_has_it_in_the_deck() -> None:
    seen = run("""
    renderConsidering([card({ id: "a", name: "Bolt", held: false })]);
    const before = texts($("list-considering"), ".card-name");
    renderConsidering([card({ id: "a", name: "Bolt", held: true })]);
    return { before, after: texts($("list-considering"), ".card-name"),
      empty: $("empty-considering").hidden };
    """)
    assert seen == {"before": ["Bolt"], "after": [], "empty": False}


def test_opening_another_deck_shows_only_its_own_no_cut_yet_ideas() -> None:
    """Deck A's no-cut tiles must not stay in the panel once deck B is open."""
    shown = run("""
    state.current = 1;
    renderConsidering([card({ id: "a1", name: "Damping Sphere", targets: [] }),
      card({ id: "a2", name: "Hammerheim", targets: [] })]);
    stub.deck = async () => ({ id: 99 });
    stub.decks = async () => ({ decks: [] });
    await openDeck(2);
    renderConsidering([card({ id: "b1", name: "Bayou", targets: [] })]);
    const one = { names: texts($("list-uncut"), ".card-name"), count: $("count-uncut").textContent };
    await openDeck(3);
    renderConsidering([]);
    return { one, none: { names: texts($("list-uncut"), ".card-name"), hidden: $("uncut").hidden } };
    """)
    assert shown["one"] == {"names": ["Bayou"], "count": "(1)"}
    assert shown["none"] == {"names": [], "hidden": True}


def test_a_pooled_idea_with_no_combined_share_still_renders_its_tile() -> None:
    """A commander without cached lists has no share; an alike idea still carries its pool."""
    tile = run("""
    const idea = card({ origin: "adjacent decks", share: null, pool: { share: 0.5, count: 2, total: 4 } });
    const tile = buildTile(idea);
    const pop = tile.querySelector(".share-pop");
    return { pill: tile.querySelector(".share-pill").textContent, total: texts(pop, ".share-total"),
      rows: texts(pop, ".share-row .share-numbers") };
    """)
    assert tile == {"pill": "50%", "total": [], "rows": ["50% · 2 of 4 decks"]}


def test_a_deck_view_renders_when_a_pooled_idea_has_no_combined_share() -> None:
    shown = run("""
    renderConsidering([card({ origin: "tagged decks", originTag: "Storm", share: null,
      pool: { share: 0.25, count: 1, total: 4 } })]);
    return { tiles: $("list-considering").children.length + $("list-uncut").children.length,
      empty: $("empty-considering").hidden };
    """)
    assert shown == {"tiles": 1, "empty": True}


def test_the_side_panels_start_folded_on_every_launch_and_opening_one_stores_nothing() -> None:
    seen = run("""
    for (const id of ["h-considering", "h-uncut", "h-meta"]) storage[`commander.panelOpen.${id}`] = "1";
    setupPanels();
    const panels = () => ["considering-body", "uncut-body", "meta-panel"].map((id) => ({
      expanded: document.querySelector(`[aria-controls="${id}"]`).getAttribute("aria-expanded"),
      bodyHidden: $(id).hidden }));
    const start = panels();
    $("h-considering").click();
    const opened = panels();
    $("h-considering").click();
    return { start, opened, refolded: panels(), stored: Object.keys(storage).sort() };
    """)
    folded = {"expanded": "false", "bodyHidden": True}
    assert seen["start"] == [folded, folded, folded]
    assert seen["opened"] == [{"expanded": "true", "bodyHidden": False}, folded, folded]
    assert seen["refolded"] == [folded, folded, folded]
    # Only the stale keys set above: the page neither reads nor writes a panel's fold any more.
    assert seen["stored"] == [
        "commander.panelOpen.h-considering",
        "commander.panelOpen.h-meta",
        "commander.panelOpen.h-uncut",
    ]


def test_proposing_a_card_with_no_cut_opens_its_folded_panel() -> None:
    seen = run("""
    setupPanels();
    const folded = { expanded: $("h-uncut").getAttribute("aria-expanded"),
      bodyHidden: $("uncut-body").hidden };
    const waiting = card({ id: "n", name: "New", targets: [] });
    state.view = { cards: [waiting] };
    renderConsidering(state.view.cards);
    stub.propose = async () => ({ card: waiting });
    stub.decks = async () => ({ decks: [] });
    await propose("New");
    return { folded, expanded: $("h-uncut").getAttribute("aria-expanded"), bodyHidden: $("uncut-body").hidden,
      selected: state.selected, status: $("status-text").textContent };
    """)
    assert seen["folded"] == {"expanded": "false", "bodyHidden": True}
    assert seen["expanded"] == "true"
    assert seen["bodyHidden"] is False
    assert seen["selected"] == "n"
    assert seen["status"] == "New: pick the deck card it replaces, or add it without one"


GRACE = """{ cards: [{ name: "Basalt Monolith", factor: 3 }, { name: "Rings of Brighthearth", factor: 2 }],
  holding: 12, lists: 30 }"""


GRACE_LINE = (
    "Weighted toward lists with Basalt Monolith \u00d73, Rings of Brighthearth \u00d72: 12 of 30 hold one"
)


def test_the_breakdown_says_which_grace_cards_weigh_the_lists_and_how_many_hold_one() -> None:
    seen = run(
        """
    const idea = card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) });
    const none = buildTile(idea);
    // An empty [grace] table comes as no cards, and says nothing either.
    state.view = { grace: { cards: [], holding: 0, lists: 30 } };
    const empty = buildTile(idea);
    state.view = { grace: """
        + GRACE
        + """ };
    const graced = buildTile(idea);
    state.view = { grace: { cards: [{ name: "Basalt Monolith", factor: 2.5 }], holding: 1, lists: 30 } };
    const one = buildTile(idea);
    const unshared = buildTile(card({ origin: null, share: null, pool: part(0.5, 2, 4) }));
    return { none: texts(none, ".share-grace"), empty: texts(empty, ".share-grace"),
      graced: texts(graced, ".share-grace"),
      one: texts(one, ".share-grace"), unshared: texts(unshared, ".share-grace"),
      total: texts(graced, ".share-total") };
    """
    )
    assert seen["none"] == []
    assert seen["empty"] == []
    assert seen["graced"] == [GRACE_LINE]
    assert seen["one"] == ["Weighted toward lists with Basalt Monolith \u00d72.5: 1 of 30 holds one"]
    # No combined share, nothing weighted to explain.
    assert seen["unshared"] == []
    assert seen["total"] == ["Combined 60% \u00b7 3 of 5"]


def test_the_grid_tooltip_names_two_grace_cards_and_counts_the_rest() -> None:
    seen = run("""
    const gridCard = { name: "Sol Ring", quantity: 1, image: null, category: "Artifact", label: "",
      labelColour: null, cents: 100, share: combined(0.5, 5, 10, { tip: "meta 50% (5/10 lists)" }) };
    state.view = { grace: { cards: [{ name: "A", factor: 3 }, { name: "B", factor: 2 },
      { name: "C", factor: 4 }, { name: "D", factor: 5 }], holding: 1, lists: 30 } };
    const title = buildGridCard(gridCard).querySelector(".share").title;
    const tile = buildTile(card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) }));
    const popover = texts(tile, ".share-grace");
    return { title, popover };
    """)
    assert seen["title"] == (
        "meta 50% (5/10 lists)\nWeighted toward lists with A \u00d73, B \u00d72 +2 more: 1 of 30 holds one"
    )
    # The breakdown has the room to name them all.
    assert seen["popover"] == [
        "Weighted toward lists with A \u00d73, B \u00d72, C \u00d74, D \u00d75: 1 of 30 holds one"
    ]


def test_a_tile_is_rebuilt_when_the_decks_grace_changes_under_an_unchanged_card() -> None:
    seen = run(
        """
    const idea = card({ share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) });
    state.view = { grace: """
        + GRACE
        + """ };
    const first = tileFor(idea);
    $("list-considering").append(first);
    const same = tileFor(idea) === first;
    state.view = { grace: { cards: [{ name: "Basalt Monolith", factor: 4 }], holding: 12, lists: 30 } };
    const second = tileFor(idea);
    return { same, rebuilt: second !== first, text: texts(second, ".share-grace") };
    """
    )
    assert seen["same"] is True
    assert seen["rebuilt"] is True
    assert seen["text"] == ["Weighted toward lists with Basalt Monolith \u00d74: 12 of 30 hold one"]


def test_a_deck_grid_cards_share_title_adds_the_grace_line() -> None:
    seen = run(
        """
    const gridCard = { name: "Sol Ring", quantity: 1, image: null, category: "Artifact", label: "",
      labelColour: null, cents: 100, share: combined(0.5, 5, 10, { tip: "meta 50% (5/10 lists)" }) };
    state.view = { grace: """
        + GRACE
        + """ };
    const graced = buildGridCard(gridCard).querySelector(".share").title;
    state.view = { grace: { cards: [], holding: 0, lists: 30 } };
    const plain = buildGridCard(gridCard).querySelector(".share").title;
    return { graced, plain };
    """
    )
    assert seen["plain"] == "meta 50% (5/10 lists)"
    assert seen["graced"] == f"meta 50% (5/10 lists)\n{GRACE_LINE}"


def test_a_page_error_is_sent_to_the_apps_log_once_with_its_place_and_stack() -> None:
    sent = page("""
    stub.log_page_error = async () => ({});
    const failure = { message: "x is not a function", filename: "deck.js", lineno: 42,
      error: { stack: "at a" } };
    await fire(window, "error", failure);
    await fire(window, "error", failure);
    await settle();
    return calls.filter((c) => c[0] === "log_page_error");
    """)
    assert sent == [["log_page_error", "x is not a function", "deck.js", 42, "at a"]]


def test_a_rejection_nobody_handled_is_sent_but_a_refusal_the_page_already_showed_is_not() -> None:
    sent = page("""
    stub.log_page_error = async () => ({});
    stub.propose = async () => ({ error: "Wait for the sync to finish" });
    let refusal;
    try { await api("propose", 1, "x"); } catch (error) { refusal = error; }
    await fire(window, "unhandledrejection", { reason: refusal });
    await fire(window, "unhandledrejection", { reason: new Error("boom") });
    await settle();
    return calls.filter((c) => c[0] === "log_page_error").map((c) => [c[1], c[2]]);
    """)
    assert sent == [["boom", "unhandledrejection"]]


def test_the_page_sends_no_more_than_a_few_distinct_errors_a_session() -> None:
    sent = page("""
    stub.log_page_error = async () => ({});
    for (let n = 0; n < 60; n++) {
      await fire(window, "error", { message: `e${n}`, filename: "a.js", lineno: n });
    }
    await settle();
    return calls.filter((c) => c[0] === "log_page_error").length;
    """)
    assert sent == 20


def test_a_bridge_that_fails_while_reporting_an_error_starts_no_loop() -> None:
    sent = page("""
    stub.log_page_error = async () => { throw new Error("bridge down"); };
    await fire(window, "error", { message: "first", filename: "a.js", lineno: 1 });
    await settle();
    await settle();
    return calls.filter((c) => c[0] === "log_page_error").length;
    """)
    assert sent == 1
