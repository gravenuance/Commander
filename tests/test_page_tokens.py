"""The Tokens group at the end of the deck grid: images with the makers named, and nothing to count or label
(ADR 0061). Its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const gcard = (name, over = {}) => ({
  name, quantity: 1, image: "", category: "Creature", label: "", labelColour: "", cents: 0, share: null,
  pending: null, more: true, less: true, ...over });
const deck = (cards) => ({
  key: "Creature", name: "Creature", inDeck: true, count: cards.length, cents: 0, cards });
const token = (name, makers, over = {}) => ({
  name, quantity: 1, image: `https://cards.example/${name.toLowerCase()}.jpg`, category: "Tokens", label: "",
  labelColour: "", cents: 0, share: null, pending: null, more: false, less: false, tokenKind: "token",
  makers, ...over });
const made = (cards) => ({
  key: "tokens-made", kind: "tokens", name: "Tokens", inDeck: false, count: cards.length, cents: 0, cards });
const rows = () => [...state.groups.get("tokens-made").node.querySelectorAll(".gcard")];
state.current = 1;
state.view = { id: 1, grace: null, categories: [], cards: [], labels: [] };
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_each_token_shows_its_image_and_names_the_cards_that_make_it() -> None:
    seen = run("""
    renderGrid([deck([gcard("Magda")]), made([
      token("Vampire", ["Charismatic Conqueror", "Preacher of the Schism"]), token("Treasure", ["Magda"])])]);
    return rows().map((node) => ({
      title: node.getAttribute("title"), image: node.querySelector("img").getAttribute("src"),
      spoken: node.querySelector(".sr-only").textContent, name: node.querySelector(".art-name").textContent,
    }));
    """)
    assert seen == [
        {
            "title": "Vampire\nMade by Charismatic Conqueror, Preacher of the Schism",
            "image": "https://cards.example/vampire.jpg",
            "spoken": "Vampire. Made by Charismatic Conqueror, Preacher of the Schism",
            "name": "Vampire",
        },
        {
            "title": "Treasure\nMade by Magda",
            "image": "https://cards.example/treasure.jpg",
            "spoken": "Treasure. Made by Magda",
            "name": "Treasure",
        },
    ]


def test_a_token_has_no_count_tools_label_or_copies_badge_and_is_no_cut_target() -> None:
    seen = run("""
    renderGrid([deck([gcard("Magda")]), made([token("Treasure", ["Magda"])])]);
    const [node] = rows();
    return {
      tools: node.querySelector(".count-tools"), buttons: node.querySelectorAll("button").length,
      flag: node.querySelector(".flag"), swap: node.querySelector(".swap"),
      badge: node.querySelector(".copies"), targets: [...state.gridCards.keys()],
    };
    """)
    assert seen == {
        "tools": None,
        "buttons": 0,
        "flag": None,
        "swap": None,
        "badge": None,
        "targets": ["Magda"],
    }


def test_the_tokens_group_is_outside_the_deck_with_a_count_and_no_price() -> None:
    seen = run("""
    renderGrid([deck([gcard("Magda")]), made([token("Treasure", ["Magda"]), token("Soldier", ["Elspeth"])])]);
    const group = state.groups.get("tokens-made").node;
    const toggle = group.querySelector(".group-toggle");
    return {
      keys: [...state.groups.keys()], outside: group.classList.contains("outside"),
      title: toggle.querySelector(".group-name").textContent,
      count: toggle.querySelector(".count").textContent,
      badge: toggle.querySelector(".outside-badge").textContent,
      price: toggle.querySelector(".group-price"),
      stack: group.querySelector(".stack").getAttribute("aria-label"),
    };
    """)
    assert seen == {
        "keys": ["Creature", "tokens-made"],
        "outside": True,
        "title": "Tokens",
        "count": "2",
        "badge": "Not in deck",
        "price": None,
        "stack": "Tokens, not in the deck",
    }


def test_a_token_named_like_a_deck_card_leaves_the_deck_card_alone() -> None:
    seen = run("""
    renderGrid([deck([gcard("Soldier")]), made([token("Soldier", ["Elspeth"])])]);
    const inDeck = state.gridCards.get("Soldier");
    return {
      deckCardTools: inDeck.querySelector(".count-tools") !== null, targets: state.gridCards.size,
      tokenTools: rows()[0].querySelector(".count-tools"),
    };
    """)
    assert seen == {"deckCardTools": True, "targets": 1, "tokenTools": None}


def test_a_new_maker_rebuilds_only_that_token() -> None:
    seen = run("""
    renderGrid([made([token("Treasure", ["Magda"]), token("Soldier", ["Elspeth"])])]);
    const [treasure, soldier] = rows();
    renderGrid([made([token("Treasure", ["Magda", "Pitiless Plunderer"]), token("Soldier", ["Elspeth"])])]);
    const [after, soldierAfter] = rows();
    return {
      treasureRebuilt: after !== treasure, soldierKept: soldierAfter === soldier,
      title: after.getAttribute("title"),
    };
    """)
    assert seen == {
        "treasureRebuilt": True,
        "soldierKept": True,
        "title": "Treasure\nMade by Magda, Pitiless Plunderer",
    }


def test_the_group_goes_when_the_deck_makes_no_token() -> None:
    seen = run("""
    renderGrid([deck([gcard("Magda")]), made([token("Treasure", ["Magda"])])]);
    renderGrid([deck([gcard("Magda")])]);
    return [...state.groups.keys()];
    """)
    assert seen == ["Creature"]


def test_a_token_with_an_image_can_be_focused_and_one_without_shows_its_name() -> None:
    seen = run("""
    renderGrid([made([token("Treasure", ["Magda"]), token("Copy", ["Stormsplitter"], { image: "" })])]);
    const [treasure, copy] = rows();
    return {
      tabbable: [treasure.hasAttribute("tabindex") || treasure.tabIndex === 0,
                 copy.hasAttribute("tabindex") || copy.tabIndex === 0],
      missing: copy.querySelector(".face").className, img: copy.querySelector("img"),
    };
    """)
    assert seen == {"tabbable": [True, False], "missing": "face missing", "img": None}
