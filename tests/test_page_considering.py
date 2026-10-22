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


def test_a_refresh_that_changes_a_card_keeps_the_reason_being_typed_and_its_focus() -> None:
    seen = run("""
    showBoard([card({ id: "a", name: "Card a", reason: "old" })]);
    const tile = state.tiles.get("a").node;
    await fire(tile.querySelector(".reject"), "click");
    const input = tile.querySelector(".reject-form input");
    input.value = "too slow";
    input.focus();
    showBoard([card({ id: "a", name: "Card a", reason: "new" })]);
    const again = state.tiles.get("a").node;
    const form = again.querySelector(".reject-form");
    const typed = form.querySelector("input");
    return { replaced: again !== tile, formShown: !form.hidden, typed: typed.value,
      focusKept: document.activeElement === typed && typed.isConnected };
    """)
    assert seen == {"replaced": True, "formShown": True, "typed": "too slow", "focusKept": True}


def test_a_refresh_that_changes_a_card_keeps_its_share_breakdown_open() -> None:
    seen = run("""
    const idea = (reason) => card({ id: "a", reason, share: combined(0.6, 3, 5, { meta: part(0.6, 3, 5) }) });
    const parts = () => {
      const tile = state.tiles.get("a").node;
      return { pill: tile.querySelector(".share-pill"), pop: tile.querySelector(".share-pop") };
    };
    showBoard([idea("old")]);
    await fire(parts().pill, "click");
    showBoard([idea("new")]);
    const open = { expanded: parts().pill.getAttribute("aria-expanded"), hidden: parts().pop.hidden };
    await fire(parts().pill, "click");
    const closed = { expanded: parts().pill.getAttribute("aria-expanded"), hidden: parts().pop.hidden };
    return { open, closed };
    """)
    assert seen == {
        "open": {"expanded": "true", "hidden": False},
        "closed": {"expanded": "false", "hidden": True},
    }


def test_a_refresh_that_changes_a_card_keeps_the_folds_the_viewer_opened() -> None:
    seen = run("""
    const idea = (reason) => card({ id: "a", text: "Add {C}.", reason });
    const folds = () => state.tiles.get("a").node.querySelectorAll("details");
    showBoard([idea("old")]);
    folds()[1].open = true;
    showBoard([idea("new")]);
    return folds().map((fold) => fold.open);
    """)
    assert seen == [False, True]


def test_a_category_added_by_a_sync_is_offered_by_the_selected_card_without_a_cut() -> None:
    seen = run("""
    const options = () => texts(state.tiles.get("a").node.querySelector(".add-plain select"), "option");
    showBoard([card({ id: "a", targets: [] })]);
    select("a");
    const before = options();
    state.view = { ...deckView(), categories: ["Ramp", "Draw"] };
    renderConsidering(board);
    paint();
    return { before, after: options(), selected: state.selected };
    """)
    assert seen == {"before": ["Ramp"], "after": ["Draw", "Ramp"], "selected": "a"}


def test_a_tile_builds_its_cuts_category_list_and_reason_form_only_when_they_are_needed() -> None:
    seen = run("""
    showBoard([card({ id: "a", targets: [] }), card({ id: "b" })]);
    const has = (id) => ["targets", "add-plain", "reject-form"]
      .map((cls) => state.tiles.get(id).node.querySelector(`.${cls}`) !== null);
    const built = { a: has("a"), b: has("b") };
    select("a");
    const afterSelect = { a: has("a"), b: has("b") };
    await fire(state.tiles.get("b").node.querySelector(".reject"), "click");
    return { built, afterSelect, afterReject: has("b") };
    """)
    assert seen == {
        "built": {"a": [False, False, False], "b": [False, False, False]},
        "afterSelect": {"a": [True, True, False], "b": [False, False, False]},
        "afterReject": [False, False, True],
    }


def test_a_category_added_by_a_sync_is_offered_to_a_card_selected_afterwards() -> None:
    seen = run("""
    showBoard([card({ id: "a", targets: [] })]);
    state.view = { ...deckView(), categories: ["Ramp", "Draw"] };
    renderConsidering(board);
    select("a");
    return texts(state.tiles.get("a").node.querySelector(".add-plain select"), "option");
    """)
    assert seen == ["Draw", "Ramp"]
