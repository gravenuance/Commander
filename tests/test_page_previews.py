"""The Previews panel (ADR 0055): shown for a deck with fitting previews, folded, loaded when opened, and only
to look at. Its real scripts run under Node (`pagerun`)."""

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
  set: "Star Trek", out: "2026-11-13", fits: ["Flash", "Treasure"],
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
        "count": "(3)",
        "expanded": "false",
        "bodyHidden": True,
        "asked": 0,
    }
    assert seen["bodyHidden"] is False
    assert seen["asked"] == [["previews", 1]]
    assert seen["tiles"] == 1


def test_a_tile_shows_the_card_and_its_fit_with_no_action_but_zooming_its_art() -> None:
    seen = run("""
    previewsPanel.render(1, { cards: [tutelage] });
    const tile = $("list-previews").children[0];
    const buttons = walk(tile).filter((n) => n.tagName === "BUTTON");
    return {
      text: walk(tile).filter((n) => n.childNodes.length && n.childNodes.every((c) => !c.tagName))
        .map((n) => n.textContent),
      buttons: buttons.map((b) => b.getAttribute("aria-label")),
      folds: walk(tile).filter((n) => n.tagName === "SUMMARY").map((n) => n.textContent),
    };
    """)
    assert seen["buttons"] == ["Zoom Captain's Tutelage"]
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
