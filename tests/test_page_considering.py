"""The Considering lists and the decisions made on their cards: its real scripts run under Node
(`pagerun`)."""

import pytest

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
    const folds = () => [...state.tiles.get("a").node.querySelectorAll("details")];
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


def test_a_failure_in_the_refresh_after_a_decision_reaches_the_app_log_not_only_the_page() -> None:
    seen = run("""
    stub.log_page_error = async () => ({});
    showBoard(names(["a", "b"]));
    stub.deck = async () => ({ ...deckView(), cards: null });
    await turnDown("a");
    return calls.filter((c) => c[0] === "log_page_error").map((c) => [c[1].slice(0, 30), c[2]]);
    """)
    assert seen == [["Cannot read properties of null", "caught"]]


def test_a_refusal_from_the_bridge_on_a_decision_is_shown_but_not_sent_to_the_app_log() -> None:
    seen = run("""
    stub.log_page_error = async () => ({});
    showBoard(names(["a", "b"]));
    stub.decide = async () => ({ error: "Wait for the sync to finish" });
    await turnDown("a");
    return { sent: calls.filter((c) => c[0] === "log_page_error").length,
      status: $("status-text").textContent };
    """)
    assert seen == {"sent": 0, "status": "Wait for the sync to finish"}


CLEAR_PRELUDE = """
const gone = (id) => card({ id, name: `Gone ${id}`, status: "rejected", updatedAt: "2026-05-01T00:00:00Z" });
const syncing = { user: "T", signedIn: true,
  sync: { running: true, progress: "Starting", summary: "", failed: false, decks: {} } };
// What the bridge says of the sync it started for the cleared deck: "started", "running" or "none".
let syncAnswer = "started";
stub.clear_rejected = async () => {
  const cleared = board.filter((c) => c.status === "rejected").length;
  board = board.filter((c) => c.status !== "rejected");
  return { cleared, sync: syncAnswer };
};
stub.sync = async () => ({});
stub.account = async () => syncing;
setupClearRejected();
"""


def test_clear_asks_first_then_clears_reloads_the_deck_and_reads_the_sync_the_bridge_started() -> None:
    seen = run(
        CLEAR_PRELUDE
        + """
    showBoard([gone("1"), gone("2"), card({ id: "o", name: "Open one" })]);
    const button = $("clear-rejected");
    const shown = () => ({ label: button.textContent, cancelHidden: $("clear-cancel").hidden });
    const before = shown();
    await fire(button, "click");
    const asking = { ...shown(), sent: calls.length };
    await fire($("clear-cancel"), "click");
    const cancelled = { ...shown(), sent: calls.length };
    await fire(button, "click");
    await fire(button, "click");
    await settle();
    return { before, asking, cancelled,
      order: calls.map((c) => c[0]).filter((n) => ["clear_rejected", "deck", "account", "sync"].includes(n)),
      clear: calls.find((c) => c[0] === "clear_rejected"), hidden: $("rejected").hidden,
      status: $("status-text").textContent, syncOff: $("sync-decks").disabled,
      focus: document.activeElement.id };
    """
    )
    assert seen["before"] == {"label": "Clear", "cancelHidden": True}
    assert seen["asking"] == {"label": "Clear 2 cards?", "cancelHidden": False, "sent": 0}
    assert seen["cancelled"] == {"label": "Clear", "cancelHidden": True, "sent": 0}
    # The page asks for no sync itself: the bridge started one, and the account read shows it running.
    assert seen["order"] == ["clear_rejected", "deck", "account"]
    assert seen["clear"] == ["clear_rejected", 1]
    assert seen["hidden"] is True
    assert seen["status"] == "Cleared 2 turned down cards; compiling its ideas again"
    assert (seen["syncOff"], seen["focus"]) == (True, "h-considering")


@pytest.mark.parametrize("answer", ["running", "none"])
def test_clear_names_no_new_compile_when_the_bridge_started_no_sync(answer: str) -> None:
    seen = run(
        CLEAR_PRELUDE
        + f"""
    syncAnswer = "{answer}";
    showBoard([gone("1")]);
    await fire($("clear-rejected"), "click");
    await fire($("clear-rejected"), "click");
    await settle();
    return {{ cleared: calls.filter((c) => c[0] === "clear_rejected").length,
      synced: calls.filter((c) => c[0] === "sync").length,
      accounts: calls.filter((c) => c[0] === "account").length, status: $("status-text").textContent }};
    """
    )
    assert seen == {
        "cleared": 1,
        "synced": 0,
        "accounts": 1 if answer == "running" else 0,
        "status": "Cleared 1 turned down card",
    }


def test_a_refused_clear_is_shown_and_starts_no_sync() -> None:
    seen = run(
        CLEAR_PRELUDE
        + """
    showBoard([gone("1")]);
    stub.clear_rejected = async () => ({ error: "Wait for the sync to finish, then clear the list." });
    await fire($("clear-rejected"), "click");
    await fire($("clear-rejected"), "click");
    await settle();
    return { synced: calls.filter((c) => c[0] === "sync").length, status: $("status-text").textContent,
      accounts: calls.filter((c) => c[0] === "account").length, label: $("clear-rejected").textContent };
    """
    )
    assert seen == {
        "synced": 0,
        "accounts": 0,
        "status": "Wait for the sync to finish, then clear the list.",
        "label": "Clear",
    }


def test_a_confirmation_to_clear_gives_up_when_the_list_changes_or_escape_is_pressed() -> None:
    seen = run(
        CLEAR_PRELUDE
        + """
    showBoard([gone("1"), gone("2")]);
    const button = $("clear-rejected");
    await fire(button, "click");
    showBoard([gone("1")]);
    const changed = button.textContent;
    await fire(button, "click");
    const again = button.textContent;
    const escape = await fire(button, "keydown", { key: "Escape" });
    return { changed, again, afterEscape: button.textContent, stopped: escape.stopped,
      cancelHidden: $("clear-cancel").hidden, calls: calls.filter((c) => c[0] === "clear_rejected").length };
    """
    )
    assert seen == {
        "changed": "Clear",
        "again": "Clear 1 card?",
        "afterEscape": "Clear",
        "stopped": True,
        "cancelHidden": True,
        "calls": 0,
    }


UPCOMING = """
const soon = (over = {}) => card({ id: "u", name: "Quark", source: "you", origin: null, targets: [],
  upcoming: true, out: "2026-11-13", typeLine: "Legendary Creature", ...over });
"""


def test_an_upcoming_card_with_no_cut_is_listed_like_any_with_its_release_day_for_a_share() -> None:
    seen = run(
        UPCOMING
        + """
    showBoard([soon(), card({ id: "n", name: "Plain", targets: [] })]);
    const labels = (list) => [...$(list).children].map((n) => n.getAttribute("aria-label"));
    const tile = state.tiles.get("u").node;
    select("u");
    const add = tile.querySelector(".add-plain button");
    const badge = tile.querySelector(".badge");
    return { considering: labels("list-considering"), uncut: labels("list-uncut"),
      badges: texts(tile, ".badge"), badgeTitle: badge.title,
      pills: tile.querySelectorAll(".share-pill").length,
      addDisabled: add.disabled, categoryDisabled: tile.querySelector(".add-plain select").disabled,
      plainShown: !tile.querySelector(".add-plain").hidden };
    """
    )
    assert seen == {
        "considering": [],
        "uncut": ["Quark", "Plain"],
        "badges": ["Out 13 Nov 2026"],
        "badgeTitle": "Not out yet; no share to show",
        "pills": 0,
        "addDisabled": False,
        "categoryDisabled": False,
        "plainShown": True,
    }


def test_an_upcoming_card_with_cuts_sits_in_considering_and_offers_them() -> None:
    seen = run(
        UPCOMING
        + """
    showBoard([soon({ targets: [{ name: "Creature 7", reason: "fewer plays", source: "picked" }] })]);
    const tile = state.tiles.get("u").node;
    select("u");
    return { considering: [...$("list-considering").children].length,
      uncut: [...$("list-uncut").children].length,
      cuts: !tile.querySelector(".targets").hidden, badges: texts(tile, ".badge") };
    """
    )
    assert seen == {"considering": 1, "uncut": 0, "cuts": True, "badges": ["Out 13 Nov 2026"]}


def test_once_out_a_card_is_an_idea_like_any_other_with_its_add_on_offer() -> None:
    seen = run(
        UPCOMING
        + """
    showBoard([soon({ upcoming: false })]);
    select("u");
    const tile = state.tiles.get("u").node;
    return { uncut: [...$("list-uncut").children].length, badges: texts(tile, ".badge"),
      addDisabled: tile.querySelector(".add-plain button").disabled };
    """
    )
    assert seen == {"uncut": 1, "badges": ["you"], "addDisabled": False}
