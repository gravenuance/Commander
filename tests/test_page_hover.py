"""Lighting the deck grid for the Considering card under the pointer or in focus, or selected: its real
scripts run under Node (`pagerun`). The write counts pin that a hover touches only the cards it lights."""

from pagerun import Answer, needs_node, page
from test_page import PRELUDE as BOARD_PRELUDE

pytestmark = needs_node

PRELUDE = (
    BOARD_PRELUDE
    + """
const deckCard = (name, over = {}) => ({
  name, quantity: 1, image: "", category: "Creature", label: "", labelColour: "", cents: 0, share: null,
  pending: null, more: false, less: true, ...over });
const idea = (id, targets, over = {}) => card({
  id, name: `Idea ${id}`, ...over,
  targets: targets.map((n) => ({ name: `Creature ${n}`, reason: `why ${n}`, source: "cmdr" })) });
const ideas = [idea("a", [1, 2, 3]), idea("b", [4, 5, 6]), idea("c", [7, 8, 9])];
const creatures = Array.from({ length: 20 }, (_, i) => deckCard(`Creature ${i}`));
setupHover();
state.current = 1;
state.view = { id: 1, grace: null, categories: ["Ramp"], cards: ideas, labels: [] };
renderGrid([{ key: "Creature", name: "Creature", inDeck: true, count: 20, cents: 0, cards: creatures }]);
renderConsidering(ideas);
paint();

// Every attribute write under the grid, by the grid card it landed on ("#grid" for the grid itself).
const log = [];
for (const node of [$("grid"), ...walk($("grid"))]) {
  const owner = () => (node === $("grid") ? "#grid" : node.closest(".gcard").dataset.name);
  for (const method of ["set", "delete"]) {
    const original = node.attrs[method].bind(node.attrs);
    node.attrs[method] = (...args) => { log.push(owner()); return original(...args); };
  }
}
const writes = () => log.splice(0);
const touched = () => [...new Set(writes())].sort();
const list = $("list-considering");
const tileOf = (id) => state.tiles.get(id).node;
// The events a browser fires when the pointer moves between two tiles, or out of the list.
const enter = async (id) => {
  await fire(tileOf(id).querySelector(".card-name"), "mouseover");
  await fire(tileOf(id), "mouseenter");
};
const exit = async (id) => {
  await fire(tileOf(id), "mouseout");
  await fire(tileOf(id), "mouseleave");
};
const leave = async (id) => {
  await exit(id);
  await fire(list, "mouseleave");
};
const lit = () => state.gridCards.size && [...state.gridCards]
  .filter(([, node]) => node.classList.contains("target")).map(([name]) => name).sort();
"""
)


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_hovering_a_card_lights_its_targets_with_a_swap_and_leaving_clears_them() -> None:
    seen = run("""
    await enter("a");
    const swap = state.gridCards.get("Creature 2").querySelector(".swap");
    const on = { lit: lit(), choosing: $("grid").classList.contains("choosing"), tile: tileOf("a").className,
      swapShown: !swap.hidden, swapLabel: swap.getAttribute("aria-label"), swapTitle: swap.title,
      other: state.gridCards.get("Creature 9").querySelector(".swap").hidden };
    await leave("a");
    return { on, off: { lit: lit(), choosing: $("grid").classList.contains("choosing"),
      tile: tileOf("a").className, swapShown: !swap.hidden } };
    """)
    assert seen["on"] == {
        "lit": ["Creature 1", "Creature 2", "Creature 3"],
        "choosing": True,
        "tile": "card lit",
        "swapShown": True,
        "swapLabel": "Replace Creature 2 with Idea a",
        "swapTitle": "why 2",
        "other": True,
    }
    assert seen["off"] == {"lit": [], "choosing": False, "tile": "card", "swapShown": False}


def test_keyboard_focus_on_a_card_lights_its_targets_like_hovering_it() -> None:
    seen = run("""
    tileOf("b").querySelector(".pick").focus();
    const on = lit();
    await fire(tileOf("b").querySelector(".pick"), "blur");
    return { on, off: lit() };
    """)
    assert seen == {"on": ["Creature 4", "Creature 5", "Creature 6"], "off": []}


def test_selecting_a_card_offers_every_deck_card_as_a_cut_and_a_click_again_clears_it() -> None:
    seen = run("""
    select("a");
    const pick = tileOf("a").querySelector(".pick");
    const on = { tile: tileOf("a").className, pressed: pick.getAttribute("aria-pressed"),
      targets: !tileOf("a").querySelector(".targets").hidden,
      plain: !tileOf("a").querySelector(".add-plain").hidden,
      swaps: [...state.gridCards.values()].filter((n) => !n.querySelector(".swap").hidden).length,
      any: state.gridCards.get("Creature 15").querySelector(".swap").title };
    select("a");
    return { on, off: { tile: tileOf("a").className, pressed: pick.getAttribute("aria-pressed"),
      swaps: [...state.gridCards.values()].filter((n) => !n.querySelector(".swap").hidden).length } };
    """)
    assert seen["on"] == {
        "tile": "card selected lit",
        "pressed": "true",
        "targets": True,
        "plain": True,
        "swaps": 20,
        "any": "Replace Creature 15 (your pick, not suggested)",
    }
    assert seen["off"] == {"tile": "card", "pressed": "false", "swaps": 0}


def test_a_card_queued_to_replace_another_marks_that_card_going_out() -> None:
    seen = run("""
    const queued = idea("q", [1], { status: "add", choice: "Creature 7", targets: [] });
    renderConsidering([...ideas, queued]);
    paint();
    const out = state.gridCards.get("Creature 7");
    return { cls: out.className, note: texts(out, ".out-note"),
      other: state.gridCards.get("Creature 8").className };
    """)
    assert seen == {"cls": "gcard out", "note": ["→ Idea q"], "other": "gcard"}


def test_the_pointer_moving_between_two_cards_writes_only_to_their_target_cards() -> None:
    seen = run("""
    await enter("a");
    const first = touched();
    await exit("a");
    await enter("b");
    const second = touched();
    await leave("b");
    return { first, second, last: touched() };
    """)
    assert seen == {
        "first": ["#grid", "Creature 1", "Creature 2", "Creature 3"],
        "second": ["Creature 1", "Creature 2", "Creature 3", "Creature 4", "Creature 5", "Creature 6"],
        "last": ["#grid", "Creature 4", "Creature 5", "Creature 6"],
    }


def test_a_sweep_across_the_cards_switches_the_grids_dimming_on_once_and_off_once() -> None:
    seen = run("""
    await enter("a");
    await exit("a");
    await enter("b");
    await exit("b");
    await enter("c");
    await leave("c");
    return writes().filter((owner) => owner === "#grid").length;
    """)
    assert seen == 2


def test_the_pointer_moving_inside_one_card_or_into_its_gap_changes_nothing_until_it_leaves() -> None:
    seen = run("""
    await enter("a");
    writes();
    await fire(tileOf("a").querySelector(".pick"), "mouseover");
    await fire(tileOf("a").querySelector(".card-meta"), "mouseover");
    const inside = writes().length;
    await exit("a");
    await fire(list, "mouseover");
    return { inside, gap: lit() };
    """)
    assert seen == {"inside": 0, "gap": []}


def test_tabbing_from_one_card_to_the_next_keeps_the_grid_dimmed() -> None:
    seen = run("""
    const first = tileOf("a").querySelector(".pick");
    const second = tileOf("b").querySelector(".pick");
    first.focus();
    writes();
    await fire(first, "blur", { relatedTarget: second });
    second.focus();
    return { grid: writes().filter((owner) => owner === "#grid").length, lit: lit() };
    """)
    assert seen == {"grid": 0, "lit": ["Creature 4", "Creature 5", "Creature 6"]}


def test_hovering_other_cards_while_one_is_selected_changes_nothing() -> None:
    seen = run("""
    select("a");
    writes();
    const before = lit();
    await enter("b");
    await leave("b");
    await enter("c");
    await leave("c");
    return { writes: writes().length, before, after: lit(),
      tiles: ["a", "b", "c"].map((id) => tileOf(id).className) };
    """)
    assert seen == {
        "writes": 0,
        "before": ["Creature 1", "Creature 2", "Creature 3"],
        "after": ["Creature 1", "Creature 2", "Creature 3"],
        "tiles": ["card selected lit", "card", "card"],
    }
