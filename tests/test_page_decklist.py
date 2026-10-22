"""The decklist on the app page: queued count changes and adds, + and -, the groups outside the deck, the
stats above it, adding a card without a cut, and the rail that belongs to an open deck."""

from pagerun import Answer, needs_node, page
from test_page import PRELUDE as BOARD_PRELUDE

pytestmark = needs_node

PRELUDE = (
    BOARD_PRELUDE
    + """
const deckCard = (over = {}) => ({
  name: "Mountain", quantity: 6, image: "", category: "Land", label: "", labelColour: "", cents: 72,
  share: null, pending: null, more: true, less: true, ...over });
const group = (name, cards, over = {}) => ({
  key: name, name, inDeck: true, count: cards.reduce((n, c) => n + c.quantity, 0), cents: 0, cards,
  ...over });
const labels = (node, selector) => node.querySelectorAll(selector).map((n) => n.getAttribute("aria-label"));
"""
)


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_basic_shows_plus_and_minus_and_a_singleton_only_minus() -> None:
    seen = run("""
    const ring = deckCard({ name: "Sol Ring", quantity: 1, more: false });
    renderGrid([group("Land", [deckCard()]), group("Artifact", [ring])]);
    return {
      mountain: labels(state.gridCards.get("Mountain"), ".count-step"),
      ring: labels(state.gridCards.get("Sol Ring"), ".count-step"),
    };
    """)
    assert seen == {"mountain": ["One fewer Mountain", "One more Mountain"], "ring": ["Take Sol Ring out"]}


def test_plus_queues_one_more_through_the_bridge_and_says_so() -> None:
    seen = run("""
    state.current = 7;
    renderGrid([group("Land", [deckCard()])]);
    stub.count = async () => ({ change: { name: "Mountain", was: 6, count: 7, by: "you", at: "t" } });
    stub.decks = async () => ({ decks: [] });
    stub.deck = async () => ({ id: 99 });
    await fire(state.gridCards.get("Mountain").querySelector('[data-step="1"]'), "click");
    return { calls: calls.filter((c) => c[0] === "count"), status: $("status-text").textContent };
    """)
    assert seen == {"calls": [["count", 7, "Mountain", 1]], "status": "Mountain: 6 → 7, queued"}


def test_a_queued_count_change_shows_on_the_copies_badge_and_a_removal_greys_the_card() -> None:
    seen = run("""
    renderGrid([group("Land", [
      deckCard({ pending: { kind: "count", was: 6, count: 7 } }),
      deckCard({ name: "Sol Ring", quantity: 1, more: true, less: false,
        pending: { kind: "count", was: 1, count: 0 } }),
    ])]);
    const ring = state.gridCards.get("Sol Ring");
    return {
      mountain: texts(state.gridCards.get("Mountain"), ".qty"),
      ring: texts(ring, ".qty"), note: texts(ring, ".pending-note"),
      leaving: ring.classList.contains("leaving"),
      back: labels(ring, ".count-step"),
    };
    """)
    assert seen == {
        "mountain": ["\u00d76 → 7"],
        "ring": ["Out"],
        "note": ["Coming out"],
        "leaving": True,
        "back": ["Put back one Sol Ring"],
    }


def test_a_card_queued_to_come_in_shows_its_category_and_minus_takes_it_off_the_queue() -> None:
    seen = run("""
    state.current = 7;
    renderGrid([group("Artifact", [deckCard({ name: "Arcane Signet", quantity: 1, category: "Ramp",
      label: "Don't Have", more: false, less: false, pending: { kind: "add", id: "b1" } })])]);
    const node = $("grid").querySelector(".gcard");
    stub.decide = async () => ({ card: {} });
    stub.decks = async () => ({ decks: [] });
    stub.deck = async () => ({ id: 99 });
    const minus = node.querySelector(".count-step");
    const label = minus.getAttribute("aria-label");
    await fire(minus, "click");
    return { mapped: state.gridCards.has("Arcane Signet"), flags: node.querySelectorAll(".flag").length,
      note: texts(node, ".pending-note"), label, calls: calls.filter((c) => c[0] === "decide") };
    """)
    # It is not a deck card yet: never a cut, no label to set.
    assert seen["mapped"] is False
    assert seen["flags"] == 0
    assert seen["note"] == ["New · Ramp"]
    assert seen["label"] == "Don't add Arcane Signet"
    assert seen["calls"] == [["decide", 7, "b1", "reopen", None, ""]]


def test_a_group_outside_the_deck_is_marked_and_its_cards_have_no_controls() -> None:
    seen = run("""
    renderGrid([
      group("Land", [deckCard()]),
      group("Maybeboard", [deckCard({ name: "Hoverbike", quantity: 1, more: false, less: false,
        share: { label: "40%", tip: "t" } })], { key: "outside:Maybeboard", inDeck: false }),
    ]);
    const outside = $("grid").querySelectorAll(".group")[1];
    return { outside: outside.classList.contains("outside"), badge: texts(outside, ".outside-badge"),
      pill: texts(outside, ".share"), steps: outside.querySelectorAll(".count-step").length,
      flags: outside.querySelectorAll(".flag").length, mapped: [...state.gridCards.keys()] };
    """)
    assert seen == {
        "outside": True,
        "badge": ["Not in deck"],
        "pill": ["40%"],
        "steps": 0,
        "flags": 0,
        "mapped": ["Mountain"],
    }


def test_minus_on_a_maybeboard_card_queues_it_to_come_off_its_category() -> None:
    seen = run("""
    state.current = 7;
    renderGrid([group("Maybeboard", [deckCard({ name: "Hoverbike", quantity: 1, more: false, less: true })],
      { key: "outside:Maybeboard", inDeck: false })]);
    const minus = $("grid").querySelector(".count-step");
    const label = minus.getAttribute("aria-label");
    stub.count = async () => ({ change: { name: "Hoverbike", was: 1, count: 0, by: "you", at: "t",
      category: "Maybeboard" } });
    stub.decks = async () => ({ decks: [] });
    stub.deck = async () => ({ id: 99 });
    await fire(minus, "click");
    return { label, steps: $("grid").querySelectorAll(".count-step").length,
      calls: calls.filter((c) => c[0] === "count"), status: $("status-text").textContent };
    """)
    assert seen == {
        "label": "Remove Hoverbike from the Maybeboard",
        "steps": 1,
        "calls": [["count", 7, "Hoverbike", -1, "Maybeboard"]],
        "status": "Hoverbike: queued to come off the Maybeboard",
    }


def test_a_card_queued_off_a_category_of_the_users_shows_out_and_plus_keeps_it_there() -> None:
    seen = run("""
    state.current = 7;
    renderGrid([group("TO ADD", [deckCard({ name: "Chaos Warp", quantity: 1, more: true, less: false,
      pending: { kind: "count", was: 1, count: 0 } })], { key: "outside:TO ADD", inDeck: false })]);
    const node = $("grid").querySelector(".gcard");
    const plus = node.querySelector(".count-step");
    const label = plus.getAttribute("aria-label");
    stub.count = async () => ({ change: null });
    stub.decks = async () => ({ decks: [] });
    stub.deck = async () => ({ id: 99 });
    await fire(plus, "click");
    return { qty: texts(node, ".qty"), note: texts(node, ".pending-note"),
      leaving: node.classList.contains("leaving"), label,
      calls: calls.filter((c) => c[0] === "count"), status: $("status-text").textContent };
    """)
    assert seen == {
        "qty": ["Out"],
        "note": ["Coming out"],
        "leaving": True,
        "label": "Keep Chaos Warp on TO ADD",
        "calls": [["count", 7, "Chaos Warp", 1, "TO ADD"]],
        "status": "Chaos Warp: stays on TO ADD",
    }


def test_the_stats_show_the_saved_deck_and_what_review_would_change() -> None:
    seen = run("""
    const saved = { cards: 100, dontHave: 2, dontHaveCents: 450, dontHaveUnpriced: 0, averageManaValue: 2.5 };
    renderStats({ saved, after: { ...saved, cards: 101, dontHave: 3, dontHaveCents: 480 } });
    const stats = $("deck-stats").querySelectorAll(".stat");
    return stats.map((s) => [s.querySelector("dt").textContent, s.querySelector("dd").textContent,
      s.querySelector("dd").classList.contains("changed")]);
    """)
    assert seen == [
        ["Cards", "100, after Review  → 101", True],
        ["Don't Have", "2, after Review  → 3", True],
        ["Don't Have cost", "€4.50, after Review  → €4.80", True],
        ["Avg. mana value", "2.50", False],
    ]


def test_adding_without_a_cut_offers_the_decks_categories_preset_to_the_cards_type() -> None:
    seen = run("""
    state.current = 7;
    state.view = { categories: ["Land", "Ramp"], cards: [] };
    const idea = card({ typeCategory: "Artifact", category: null, targets: [] });
    const tile = buildTile(idea);
    state.tiles.set(idea.id, { key: "", node: tile, card: idea });
    select(idea.id);
    const category = tile.querySelector(".add-plain select");
    const options = texts(category, "option");
    const preset = category.value;
    category.value = "Ramp";
    stub.decide = async () => ({ card: {} });
    stub.decks = async () => ({ decks: [] });
    stub.deck = async () => ({ id: 99 });
    await fire(tile.querySelector(".add-plain button"), "click");
    return { options, preset, calls: calls.filter((c) => c[0] === "decide"),
      status: $("status-text").textContent };
    """)
    assert seen["options"] == ["Artifact", "Land", "Ramp"]
    assert seen["preset"] == "Artifact"
    assert seen["calls"] == [["decide", 7, "c1", "add", None, "", "Ramp"]]
    assert seen["status"] == "Arcane Signet queued to add to Ramp"


def test_the_rail_is_hidden_until_a_deck_opens_and_folds_when_the_selection_clears() -> None:
    seen = run("""
    storage["commander.activityOpen"] = "mana";
    stub.mana = async () => ({});
    setupActivity();
    const before = { hidden: $("activity-pane").hidden, open: $("activity-pane").classList.contains("open") };
    showRailFor(true);
    const opened = { hidden: $("activity-pane").hidden, mana: $("mana").hidden };
    showRailFor(false);
    return { before, opened, after: { hidden: $("activity-pane").hidden, mana: $("mana").hidden,
      expanded: $("mana-toggle").getAttribute("aria-expanded") }, stored: storage["commander.activityOpen"] };
    """)
    assert seen["before"] == {"hidden": True, "open": False}
    assert seen["opened"] == {"hidden": False, "mana": False}
    assert seen["after"] == {"hidden": True, "mana": True, "expanded": "false"}
    # Folded because no deck is open, not by the viewer: the next deck opens the rail as they left it.
    assert seen["stored"] == "mana"
