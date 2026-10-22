"""The page acts on a deck's effective bracket, set or estimated (ADR 0051), and shows only the set one: its
real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const view = (bracket, effectiveBracket) => ({
  id: 1, short: "Magda", name: "A", url: "https://archidekt.com/decks/1", bracket, effectiveBracket,
  archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: [], activity: [],
  stamp: "s",
});
stub.bracket = async () => ({ estimate: null, notes: [], causes: [], unavailable: "" });
stub.mana = async () => ({});
accountState.active = true;
state.current = 1;
state.decks = [{ id: 1, short: "Magda", name: "A", queued: 0 }];
renderDecks();
const shown = async (bracket, effectiveBracket) => {
  state.view = view(bracket, effectiveBracket);
  renderDeck();
  await settle();
  return { meta: !$("meta").hidden, pill: $("deck-bracket").textContent };
};
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_meta_panel_shows_for_bracket_4_and_5_and_the_header_names_only_a_set_one() -> None:
    seen = run("""
    return {
      set5: await shown(5, 5),
      set4: await shown(4, 4),
      estimated4: await shown(null, 4),
      set3: await shown(3, 3),
      estimated3: await shown(null, 3),
      setBelowEstimate: await shown(2, 2),
      unset: await shown(null, null),
    };
    """)
    assert seen == {
        "set5": {"meta": True, "pill": "Bracket 5"},
        "set4": {"meta": True, "pill": "Bracket 4"},
        "estimated4": {"meta": True, "pill": ""},
        "set3": {"meta": False, "pill": "Bracket 3"},
        "estimated3": {"meta": False, "pill": ""},
        "setBelowEstimate": {"meta": False, "pill": "Bracket 2"},
        "unset": {"meta": False, "pill": ""},
    }


def test_the_meta_is_read_for_an_open_deck_at_bracket_4_or_5_not_below() -> None:
    seen = run("""
    showPanel($("h-meta"), true);
    const wanted = (bracket, effective) => {
      state.view = view(bracket, effective);
      return Boolean(metaWanted());
    };
    return [wanted(null, 4), wanted(null, 5), wanted(5, 5), wanted(4, 4), wanted(null, 3), wanted(3, 3)];
    """)
    assert seen == [True, True, True, True, False, False]


def test_an_estimate_that_gives_an_unset_deck_its_bracket_reloads_the_deck_once() -> None:
    seen = run("""
    let loads = 0;
    stub.deck = async () => { loads += 1; return view(null, 4); };
    state.view = view(null, null);
    const cause = { tag: "Ruthless", brackets: "4+", reason: "a combo", cards: ["Mana Crypt"] };
    const estimate = { estimate: { tag: "Ruthless", brackets: "4+" }, notes: [], causes: [cause],
      effectiveBracket: 4 };
    renderBracket(1, estimate, false);
    await settle();
    const first = loads;
    renderBracket(1, estimate, false);
    await settle();
    return { first, second: loads, bracket: state.view.effectiveBracket };
    """)
    assert seen == {"first": 1, "second": 1, "bracket": 4}


def test_only_a_bracket_5_deck_gets_the_cedh_note_in_the_mana_tab() -> None:
    seen = run("""
    const lands = { lands: 36, suggested: 36, averageManaValue: 2, cheapDraw: 0, cheapRamp: 0,
      landSpells: 0 };
    const mana = { lands, comparison: null, compareWith: "", colours: [], turnOne: [], tappedLands: 0,
      skipped: [], plan: null };
    const note = (bracket, effective) => {
      state.view = view(bracket, effective);
      renderMana(1, mana);
      return walk($("mana")).some((node) => node.textContent.includes("Made for casual games"));
    };
    return [note(5, 5), note(4, 4), note(null, 4), note(null, null)];
    """)
    assert seen == [True, False, False, False]


def test_an_estimate_with_notes_but_no_causes_shows_only_the_notes() -> None:
    # replaceChildren(null) writes the text "null" into the detail in a browser; the fake DOM throws on it.
    seen = run("""
    renderBracket(1, { estimate: { tag: "Core", brackets: "2+" }, notes: ["Set to 2 on Archidekt"],
      causes: [], unavailable: "", effectiveBracket: 2 }, true);
    const detail = $("bracket-detail");
    return { children: [...detail.children].map((c) => c.tagName), text: detail.textContent };
    """)
    assert seen == {"children": ["P"], "text": "Set to 2 on Archidekt"}
