"""The rail and header panels that load an answer for the open deck (bracket chip, mana, meta): their real
scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const view = (id) => ({
  id, short: `D${id}`, name: `Deck ${id}`, url: `https://archidekt.com/decks/${id}`, bracket: null,
  effectiveBracket: null, archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: [],
  activity: [], stamp: "s",
});
const cause = { tag: "Ruthless", brackets: "4+", reason: "a combo", cards: ["Mana Crypt"] };
const estimate = { estimate: { tag: "Ruthless", brackets: "4+" }, notes: ["Set to 2 on Archidekt"],
  causes: [cause], unavailable: "" };
accountState.active = true;
const mana = {
  lands: { lands: 36, suggested: 36, averageManaValue: 2, cheapDraw: 0, cheapRamp: 0, landSpells: 0 },
  comparison: null, compareWith: "", colours: [], turnOne: [], tappedLands: 0, skipped: [], plan: null };
state.decks = [
  { id: 1, short: "D1", name: "Deck 1", queued: 0 }, { id: 2, short: "D2", name: "Deck 2", queued: 0 }];
state.current = 1;
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_two_loads_of_one_panel_for_the_same_deck_draw_the_newer_answer_last() -> None:
    drawn = run("""
    let release;
    const slow = new Promise((resolve) => { release = resolve; });
    let sent = 0;
    const drawn = [];
    const p = panel(async () => (++sent === 1 ? slow : { v: "new" }), (deck, answer) => drawn.push(answer.v));
    const first = p.load();
    await p.load();
    release({ v: "old" });
    await first;
    return drawn;
    """)
    assert drawn == ["new"]


def test_a_slow_older_bracket_estimate_does_not_replace_the_newer_one() -> None:
    seen = run("""
    let release;
    const slow = new Promise((resolve) => { release = resolve; });
    const answers = [slow, { ...estimate, estimate: { tag: "Core", brackets: "2+" } }];
    stub.bracket = async () => answers.shift();
    const first = bracketPanel.load();
    await bracketPanel.load();
    release(estimate);
    await first;
    return $("bracket-check").textContent;
    """)
    assert seen == "Estimate 2+"


def test_opening_another_deck_clears_the_previous_decks_bracket_chip_and_mana_panel() -> None:
    seen = run("""
    stub.bracket = async () => estimate;
    stub.mana = async () => mana;
    stub.open_deck = async () => ({});
    stub.deck = async (id) => view(id);
    await bracketPanel.load();
    $("bracket-check").click();
    manaPanel.render(1, mana);
    const drawn = $("mana").children.length > 0;
    stub.bracket = () => new Promise(() => {});
    await openDeck(2);
    const chip = $("bracket-check");
    return {
      drawn, chipHidden: chip.hidden, chipExpanded: chip.getAttribute("aria-expanded"),
      detailHidden: $("bracket-detail").hidden, detailText: $("bracket-detail").textContent,
      mana: $("mana").children.length,
    };
    """)
    assert seen == {
        "drawn": True,
        "chipHidden": True,
        "chipExpanded": None,
        "detailHidden": True,
        "detailText": "",
        "mana": 0,
    }


def test_returning_to_a_deck_draws_its_bracket_estimate_again() -> None:
    seen = run("""
    stub.bracket = async () => estimate;
    stub.mana = async () => ({});
    stub.open_deck = async () => ({});
    stub.deck = async (id) => view(id);
    await bracketPanel.load();
    stub.bracket = async () => ({ estimate: null, notes: [], causes: [], unavailable: "" });
    await openDeck(2);
    await settle();
    stub.bracket = async () => estimate;
    await openDeck(1);
    await settle();
    return { hidden: $("bracket-check").hidden, text: $("bracket-check").textContent };
    """)
    assert seen == {"hidden": False, "text": "Estimate 4+"}


def test_putting_the_mana_swaps_on_the_board_moves_focus_to_the_plans_heading() -> None:
    seen = run("""
    const swap = {
      add: "Sol Ring", cut: "Wayfarer", copies: 1, reason: "r", cents: null, withinBudget: true };
    const plan = (board) => ({
      unchanged: false, landChange: 0, lands: 36, cutCandidates: [], basics: [], colours: [], stillShort: [],
      budgetCents: null, swaps: [{ ...swap, board }] });
    let boarded = false;
    railTab = "mana";
    stub.mana = async () => ({ ...mana, plan: plan(boarded ? "added" : null) });
    stub.mana_to_board = async () => { boarded = true; return { added: ["Sol Ring"] }; };
    stub.deck = async () => view(1);
    await manaPanel.load();
    const button = $("mana").querySelector(".button.primary");
    button.focus();
    await fire(button, "click");
    const active = document.activeElement;
    return { id: active.id, buttonGone: $("mana").querySelector(".button.primary") === null,
      focusable: active.getAttribute("tabindex") };
    """)
    assert seen == {"id": "mana-plan-title", "buttonGone": True, "focusable": "-1"}
