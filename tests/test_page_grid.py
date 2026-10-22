"""Redrawing the deck grid after a change: only the cards and group headers that changed are rebuilt. Its real
scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const gcard = (name, over = {}) => ({
  name, quantity: 1, image: "", category: "Creature", label: "", labelColour: "", cents: 0, share: null,
  pending: null, more: true, less: true, ...over });
const names = ["Ayula", "Birds", "Cultivate", "Dryad"];
const group = (cards, over = {}) => ({
  key: "Creature", name: "Creature", inDeck: true, count: cards.length, cents: 0, cards, ...over });
const rows = (key = "Creature") => [...state.groups.get(key).node.querySelectorAll(".gcard")];
const order = (key = "Creature") => rows(key).map((node) => node.dataset.name);
state.current = 1;
state.view = { id: 1, grace: null, categories: [], cards: [], labels: [] };
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_label_change_rebuilds_only_that_card_and_keeps_the_group() -> None:
    seen = run("""
    renderGrid([group(names.map((name) => gcard(name)))]);
    const section = state.groups.get("Creature").node;
    const before = Object.fromEntries(rows().map((node) => [node.dataset.name, node]));
    renderGrid([group(names.map((name) => gcard(name, name === "Cultivate" ? { label: "Proxy" } : {})))]);
    const after = Object.fromEntries(rows().map((node) => [node.dataset.name, node]));
    return {
      sameSection: state.groups.get("Creature").node === section,
      kept: names.filter((name) => before[name] === after[name]),
      order: order(),
      mapped: names.every((name) => state.gridCards.get(name) === after[name]),
      flag: after.Cultivate.querySelector(".flag").getAttribute("aria-label"),
    };
    """)
    assert seen == {
        "sameSection": True,
        "kept": ["Ayula", "Birds", "Dryad"],
        "order": ["Ayula", "Birds", "Cultivate", "Dryad"],
        "mapped": True,
        "flag": "Label for Cultivate: Proxy",
    }


def test_cards_added_removed_and_reordered_in_a_group_land_in_the_new_order() -> None:
    seen = run("""
    renderGrid([group(names.map((name) => gcard(name)))]);
    const birds = state.gridCards.get("Birds");
    const next = ["Dryad", "Birds", "Elk", "Ayula"];
    renderGrid([group(next.map((name) => gcard(name)))]);
    return {
      order: order(), birdsKept: state.gridCards.get("Birds") === birds,
      mapped: [...state.gridCards.keys()].sort(), cultivateGone: !state.gridCards.has("Cultivate"),
      count: state.groups.get("Creature").node.querySelector(".count").textContent,
    };
    """)
    assert seen == {
        "order": ["Dryad", "Birds", "Elk", "Ayula"],
        "birdsKept": True,
        "mapped": ["Ayula", "Birds", "Dryad", "Elk"],
        "cultivateGone": True,
        "count": "(4)",
    }


def test_a_card_leaving_above_a_focused_card_does_not_take_its_focus_with_it() -> None:
    seen = run("""
    renderGrid([group(names.map((name) => gcard(name)))]);
    const step = state.gridCards.get("Cultivate").querySelector(".count-step");
    step.focus();
    renderGrid([group(names.filter((name) => name !== "Ayula").map((name) => gcard(name)))]);
    return { order: order(), kept: document.activeElement === step };
    """)
    assert seen == {"order": ["Birds", "Cultivate", "Dryad"], "kept": True}


def test_a_group_leaving_above_a_focused_group_does_not_take_its_focus_with_it() -> None:
    seen = run("""
    const creatures = group([gcard("Ayula")]);
    const ramp = group([gcard("Cultivate", { category: "Ramp" })], { key: "Ramp", name: "Ramp" });
    renderGrid([creatures, ramp]);
    const toggle = state.groups.get("Ramp").node.querySelector(".group-toggle");
    toggle.focus();
    renderGrid([ramp]);
    return { groups: [...state.groups.keys()], kept: document.activeElement === toggle };
    """)
    assert seen == {"groups": ["Ramp"], "kept": True}


def test_a_changed_group_header_shows_the_new_count_and_price_and_keeps_its_cards() -> None:
    seen = run("""
    renderGrid([group([gcard("Ayula"), gcard("Birds")])]);
    const ayula = state.gridCards.get("Ayula");
    const title = () => state.groups.get("Creature").node.querySelector(".group-title");
    renderGrid([group([gcard("Ayula"), gcard("Birds")], { count: 3, cents: 250 })]);
    const text = title().textContent;
    await fire(title().querySelector(".group-toggle"), "click");
    const collapsed = state.groups.get("Creature").node.querySelector(".stack").hidden;
    return { text, kept: state.gridCards.get("Ayula") === ayula, collapsed };
    """)
    assert seen["kept"] is True
    assert seen["collapsed"] is True
    assert "(3)" in seen["text"]
    assert "€2.50" in seen["text"]


def test_a_grace_change_rebuilds_only_the_cards_that_show_it() -> None:
    seen = run("""
    const share = { label: "40%", tip: "In 40% of decks" };
    const cards = [gcard("Ayula", { share }), gcard("Birds")];
    renderGrid([group(cards)]);
    const before = { ayula: state.gridCards.get("Ayula"), birds: state.gridCards.get("Birds") };
    const grace = { cards: [{ name: "Sol Ring", factor: 2 }], holding: 1, lists: 5 };
    state.view = { ...state.view, grace };
    renderGrid([group(cards)]);
    return {
      ayula: state.gridCards.get("Ayula") !== before.ayula,
      birds: state.gridCards.get("Birds") !== before.birds,
    };
    """)
    assert seen == {"ayula": True, "birds": False}


def test_only_the_decks_own_cards_that_are_not_incoming_can_be_cut_or_lit() -> None:
    seen = run("""
    renderGrid([
      group([gcard("Ayula"), gcard("Incoming", { pending: { kind: "add", id: "x" } })]),
      group([gcard("Elsewhere")], { key: "outside:Ramp 1", name: "Ramp 1", inDeck: false }),
      group([gcard("Cmdr Card")], { key: "Commander", name: "Commander" }),
    ]);
    return { cards: [...state.gridCards.keys()],
      commander: state.gridCards.get("Cmdr Card").dataset.commander,
      plain: state.gridCards.get("Ayula").dataset.commander ?? null };
    """)
    assert seen == {"cards": ["Ayula", "Cmdr Card"], "commander": "1", "plain": None}


def test_a_category_outside_the_deck_keeps_its_own_cards_apart_from_a_deck_group_of_the_same_name() -> None:
    seen = run("""
    renderGrid([
      group([gcard("Ayula")], { key: "Ramp", name: "Ramp" }),
      group([gcard("Birds")], { key: "outside:Ramp", name: "Ramp", inDeck: false }),
    ]);
    return { deck: order("Ramp"), outside: order("outside:Ramp"), mapped: [...state.gridCards.keys()] };
    """)
    assert seen == {"deck": ["Ayula"], "outside": ["Birds"], "mapped": ["Ayula"]}


def test_categories_whose_names_differ_only_in_digits_or_punctuation_get_their_own_stack_ids() -> None:
    seen = run("""
    const outside = (name) => group([gcard(name)], { key: `outside:${name}`, name, inDeck: false });
    renderGrid([outside("Ramp 1"), outside("Ramp 2"), outside("Ramp-1")]);
    const sections = ["Ramp 1", "Ramp 2", "Ramp-1"].map((name) => state.groups.get(`outside:${name}`).node);
    const ids = sections.map((node) => node.querySelector(".stack").id);
    const controls = sections.map((node) =>
      node.querySelector(".group-toggle").getAttribute("aria-controls"));
    return { distinct: new Set(ids).size, controlsOwn: controls.every((id, i) => id === ids[i]) };
    """)
    assert seen == {"distinct": 3, "controlsOwn": True}
