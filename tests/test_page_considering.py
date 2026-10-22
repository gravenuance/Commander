"""The Considering lists and the decisions made on their cards: its real scripts run under Node
(`pagerun`)."""

from pagerun import Answer, needs_node, page
from test_page import PRELUDE as BOARD_PRELUDE

pytestmark = needs_node

PRELUDE = (
    BOARD_PRELUDE
    + """
let board = [];
const deckView = () => ({
  id: 1, short: "A", name: "A", url: "https://archidekt.com/decks/1", bracket: null, effectiveBracket: null,
  archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: board, activity: [],
  stamp: "s", categories: ["Ramp"], labels: [], grace: null });
const names = (ids) => ids.map((id) => card({ id, name: `Card ${id}` }));
const showBoard = (cards) => { board = cards; state.view = deckView(); renderConsidering(board); };
const turnDown = async (id) => {
  const tile = state.tiles.get(id).node;
  await fire(tile.querySelector(".reject"), "click");
  await fire(tile.querySelector(".reject-form"), "submit");
  await settle();
};
const focused = () => document.activeElement.getAttribute("aria-label");
accountState.active = true;
state.current = 1;
state.decks = [{ id: 1, short: "A", name: "A", queued: 0 }];
stub.decks = async () => ({ decks: state.decks });
stub.deck = async () => deckView();
stub.bracket = async () => ({ estimate: null, notes: [], causes: [], unavailable: "" });
stub.decide = async (deck, id, decision) => {
  board = decision === "reopen" ? board.map((c) => (c.id === id ? { ...c, status: "open" } : c))
    : board.map((c) => (c.id === id ? { ...c, status: "rejected", note: "" } : c));
  return { card: {} };
};
"""
)


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_double_click_on_reconsider_sends_one_decision() -> None:
    seen = run("""
    showBoard([card({ id: "r", name: "Rejected One", status: "rejected",
      updatedAt: "2026-05-01T00:00:00Z" })]);
    const button = $("list-rejected").querySelector("button");
    await Promise.all([fire(button, "click"), fire(button, "click")]);
    return calls.filter((c) => c[0] === "decide").length;
    """)
    assert seen == 1


def test_turning_a_card_down_moves_focus_to_the_next_card_not_the_top_of_the_page() -> None:
    seen = run("""
    showBoard(names(["a", "b", "c"]));
    await turnDown("a");
    return { focus: focused(), rows: $("list-considering").children.length };
    """)
    assert seen == {"focus": "Card b: show what it could replace", "rows": 2}


def test_turning_down_the_last_card_moves_focus_to_the_one_before_it() -> None:
    seen = run("""
    showBoard(names(["a", "b"]));
    await turnDown("b");
    return focused();
    """)
    assert seen == "Card a: show what it could replace"


def test_turning_down_the_only_card_moves_focus_to_the_panels_toggle() -> None:
    seen = run("""
    showBoard(names(["a"]));
    await turnDown("a");
    return document.activeElement.id;
    """)
    assert seen == "h-considering"


def test_reconsidering_a_card_moves_focus_to_the_next_turned_down_card() -> None:
    seen = run("""
    const rejected = (id) => card({
      id, name: `Card ${id}`, status: "rejected", updatedAt: `2026-05-0${id}T00:00:00Z` });
    showBoard([rejected("1"), rejected("2")]);
    // Latest first: Card 2 is the first row.
    await fire($("list-rejected").querySelector("button"), "click");
    return focused();
    """)
    assert seen == "Reconsider Card 1"


def test_reconsidering_the_only_turned_down_card_moves_focus_to_the_considering_toggle() -> None:
    seen = run("""
    showBoard([card({ id: "1", name: "Card 1", status: "rejected", updatedAt: "2026-05-01T00:00:00Z" })]);
    stub.decide = async (deck, id) => { board = []; return { card: {} }; };
    await fire($("list-rejected").querySelector("button"), "click");
    return document.activeElement.id;
    """)
    assert seen == "h-considering"


def test_undoing_a_queued_card_keeps_focus_on_its_tile() -> None:
    seen = run("""
    showBoard([card({ id: "q", name: "Queued", status: "add", choice: "Sol Ring" })]);
    const tile = state.tiles.get("q").node;
    await fire(tile.querySelector(".replacing button"), "click");
    return focused();
    """)
    assert seen == "Queued: show what it could replace"
