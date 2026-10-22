"""The label menu on a grid card's flag: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const labelled = (name) => ({
  name, quantity: 1, image: "", category: "Land", label: "", labelColour: "", cents: 0, share: null,
  pending: null, more: true, less: true });
state.current = 1;
state.view = { id: 1, grace: null, categories: [], cards: [],
  labels: [{ name: "", colour: "" }, { name: "Proxy", colour: "#ff0000" }] };
setupLabelMenu();
renderGrid([{ key: "Land", name: "Land", inDeck: true, count: 1, cents: 0, cards: [labelled("Mountain")] }]);
const flag = state.gridCards.get("Mountain").querySelector(".flag");
const menu = $("label-menu");
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_flag_starts_collapsed_and_is_expanded_while_its_menu_is_open() -> None:
    seen = run("""
    const fresh = flag.getAttribute("aria-expanded");
    await fire(flag, "click");
    const open = flag.getAttribute("aria-expanded");
    await fire(flag, "click");
    return { fresh, open, closed: flag.getAttribute("aria-expanded") };
    """)
    assert seen == {"fresh": "false", "open": "true", "closed": "false"}


def test_scrolling_the_pane_that_holds_the_grid_closes_the_menu() -> None:
    seen = run("""
    const closedBy = async (scroller) => {
      await fire(flag, "click");
      const opened = !menu.hidden;
      await fire(scroller, "scroll");
      return { opened, closed: menu.hidden };
    };
    return {
      gridPane: await closedBy(document.querySelector(".grid-pane")),
      main: await closedBy($("main")),
    };
    """)
    assert seen == {
        "gridPane": {"opened": True, "closed": True},
        "main": {"opened": True, "closed": True},
    }


def test_scrolling_the_menu_itself_keeps_it_open() -> None:
    seen = run("""
    await fire(flag, "click");
    await fire(menu, "scroll");
    return menu.hidden;
    """)
    assert seen is False
