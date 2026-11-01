"""The Previews panel (ADR 0055): shown for a deck with fitting previews, folded, loaded when opened, and a
card in it can be moved to Considering. Its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const view = (id, previews) => ({
  id, short: `D${id}`, name: `Deck ${id}`, url: `https://archidekt.com/decks/${id}`, bracket: null,
  effectiveBracket: null, archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: [],
  activity: [], stamp: "s", previews,
});
const tutelage = {
  name: "Captain's Tutelage", manaCost: "{1}{R}", typeLine: "Enchantment — Aura",
  text: "Flash\\nEnchant creature", image: "https://cards.scryfall.io/normal/front/0/c/0c34.jpg",
  set: "Star Trek", out: "2026-11-13", fits: ["Flash", "Treasure"], against: [],
};
accountState.active = true;
state.decks = [
  { id: 1, short: "D1", name: "Deck 1", queued: 0 }, { id: 2, short: "D2", name: "Deck 2", queued: 0 }];
stub.open_deck = async () => ({});
stub.previews = async () => ({ cards: [tutelage] });
setupPanels();
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_deck_without_fitting_previews_has_no_panel_and_asks_for_none() -> None:
    seen = run("""
    stub.deck = async (id) => view(id, 0);
    await openDeck(1);
    return { hidden: $("previews").hidden, asked: calls.filter((c) => c[0] === "previews").length };
    """)
    assert seen == {"hidden": True, "asked": 0}


def test_the_panel_shows_folded_with_its_count_and_loads_only_once_opened() -> None:
    seen = run("""
    stub.deck = async (id) => view(id, 3);
    await openDeck(1);
    const folded = {
      hidden: $("previews").hidden, count: $("count-previews").textContent,
      expanded: $("h-previews").getAttribute("aria-expanded"), bodyHidden: $("previews-body").hidden,
      asked: calls.filter((c) => c[0] === "previews").length,
    };
    await fire($("h-previews"), "click");
    await settle();
    return { folded, bodyHidden: $("previews-body").hidden, asked: calls.filter((c) => c[0] === "previews"),
      tiles: $("list-previews").children.length };
    """)
    assert seen["folded"] == {
        "hidden": False,
        "count": "3",
        "expanded": "false",
        "bodyHidden": True,
        "asked": 0,
    }
    assert seen["bodyHidden"] is False
    assert seen["asked"] == [["previews", 1]]
    assert seen["tiles"] == 1


def test_a_tile_shows_the_card_and_its_fit_with_two_actions_zooming_its_art_and_moving_it() -> None:
    seen = run("""
    previewsPanel.render(1, { cards: [tutelage] });
    const tile = $("list-previews").children[0];
    const buttons = walk(tile).filter((n) => n.tagName === "BUTTON");
    return {
      text: walk(tile).filter((n) => n.childNodes.length && n.childNodes.every((c) => !c.tagName))
        .map((n) => n.textContent),
      buttons: buttons.map((b) => b.getAttribute("aria-label")),
      moveTitle: buttons[1].title,
      folds: walk(tile).filter((n) => n.tagName === "SUMMARY").map((n) => n.textContent),
    };
    """)
    assert seen["buttons"] == ["Zoom Captain's Tutelage", "Move Captain's Tutelage to Considering"]
    assert seen["moveTitle"] == "Queue it for the deck like any suggestion"
    assert seen["folds"] == ["Rules text"]
    for shown in [
        "Captain's Tutelage",
        "Enchantment — Aura",
        "Star Trek",
        "Out 13 Nov 2026",
        "Fits: #Flash #Treasure",
    ]:
        assert shown in seen["text"]
    assert not [t for t in seen["text"] if "€" in t]
    assert not [t for t in seen["text"] if t.startswith("Works against")]


def test_a_card_working_against_the_commander_carries_a_warning() -> None:
    seen = run("""
    previewsPanel.render(1, { cards: [{ ...tutelage, against: ["Arabella"] }] });
    const tile = $("list-previews").children[0];
    return walk(tile).filter((n) => n.className === "badge status against").map((n) => n.textContent);
    """)
    assert seen == ["Works against Arabella"]


def test_opening_another_deck_drops_the_last_decks_previews() -> None:
    seen = run("""
    stub.deck = async (id) => view(id, id === 1 ? 1 : 0);
    await openDeck(1);
    await fire($("h-previews"), "click");
    await settle();
    const before = $("list-previews").children.length;
    await openDeck(2);
    return { before, after: $("list-previews").children.length, hidden: $("previews").hidden };
    """)
    assert seen == {"before": 1, "after": 0, "hidden": True}


def test_moving_a_tile_asks_the_bridge_then_rereads_the_deck_and_the_panel_and_keeps_focus_in_the_list() -> (
    None
):
    seen = run("""
    const kirk = { ...tutelage, name: "Kirk" };
    let moved = false;
    stub.deck = async (id) => view(id, moved ? 1 : 2);
    stub.decks = async () => ({ decks: state.decks });
    stub.previews = async () => ({ cards: moved ? [kirk] : [tutelage, kirk] });
    stub.move_preview = async () => { moved = true; return { card: {} }; };
    await openDeck(1);
    await fire($("h-previews"), "click");
    await settle();
    const before = [...$("list-previews").children].map((t) => t.getAttribute("aria-label"));
    const move = $("list-previews").children[0].querySelector(".move");
    await fire(move, "click");
    await settle();
    return { before, after: [...$("list-previews").children].map((t) => t.getAttribute("aria-label")),
      count: $("count-previews").textContent, status: $("status-text").textContent,
      sent: calls.filter((c) => c[0] === "move_preview"),
      focus: document.activeElement.getAttribute("aria-label") };
    """)
    assert seen["before"] == ["Captain's Tutelage", "Kirk"]
    assert seen["after"] == ["Kirk"]
    assert seen["count"] == "1"
    assert seen["status"] == "Captain's Tutelage moved to Considering"
    assert seen["sent"] == [["move_preview", 1, "Captain's Tutelage"]]
    assert seen["focus"] == "Move Kirk to Considering"


def test_a_double_click_on_move_sends_one_request() -> None:
    seen = run("""
    stub.move_preview = async () => ({ card: {} });
    stub.deck = async (id) => view(id, 1);
    stub.decks = async () => ({ decks: state.decks });
    state.current = 1;
    previewsPanel.render(1, { cards: [tutelage] });
    const move = $("list-previews").children[0].querySelector(".move");
    await Promise.all([fire(move, "click"), fire(move, "click")]);
    return calls.filter((c) => c[0] === "move_preview").length;
    """)
    assert seen == 1


def test_a_refused_move_is_shown_and_leaves_the_panel_as_it_was() -> None:
    seen = run("""
    stub.move_preview = async () => ({ error: "Wait for the sync to finish, then move the card." });
    state.current = 1;
    previewsPanel.render(1, { cards: [tutelage] });
    await fire($("list-previews").children[0].querySelector(".move"), "click");
    await settle();
    return { status: $("status-text").textContent, tiles: $("list-previews").children.length,
      reloaded: calls.filter((c) => c[0] === "deck").length };
    """)
    assert seen == {
        "status": "Wait for the sync to finish, then move the card.",
        "tiles": 1,
        "reloaded": 0,
    }
