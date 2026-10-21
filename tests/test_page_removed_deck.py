"""The app page when a sync deletes the open deck's folder: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const decks = [
  { id: 1, short: "Magda", name: "Honestly, though..", queued: 0 },
  { id: 2, short: "Ellivere", name: "Second", queued: 0 },
];
accountState.active = true;
state.decks = decks;
state.current = 2;
state.view = { id: 2, name: "Second" };
$("deck").hidden = false;
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_open_deck_that_leaves_the_deck_list_is_closed_with_a_notice() -> None:
    seen = run("""
    showRailFor(true);
    const asked = [];
    stub.decks = async () => ({ decks: decks.slice(0, 1) });
    stub.deck = async (id) => { asked.push(id); return { error: "deck is not in the workspace" }; };
    await loadDecks();
    await loadDeck();
    return { current: state.current, view: state.view, deckHidden: $("deck").hidden,
      railHidden: $("activity-pane").hidden, asked, rows: $("deck-list").children.length,
      status: $("status-text").textContent, error: $("status").classList.contains("error") };
    """)
    assert seen == {
        "current": None,
        "view": None,
        "deckHidden": True,
        "railHidden": True,
        "asked": [],
        "rows": 1,
        "status": "Second is no longer among your decks (deleted on Archidekt?), so it is closed.",
        "error": False,
    }


def test_the_open_deck_stays_open_while_it_is_in_the_deck_list() -> None:
    seen = run("""
    stub.decks = async () => ({ decks });
    await loadDecks();
    return { current: state.current, deckHidden: $("deck").hidden, statusHidden: $("status").hidden };
    """)
    assert seen == {"current": 2, "deckHidden": False, "statusHidden": True}
