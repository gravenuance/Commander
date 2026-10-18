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
