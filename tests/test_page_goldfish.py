"""The Goldfish rail tab: it plays the open deck only while it is open and draws the report, a refusal with a
retry, and the ways to win when the report has them (ADR 0054); its real scripts run under Node
(`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const view = (id) => ({
  id, short: `D${id}`, name: `Deck ${id}`, url: `https://archidekt.com/decks/${id}`, bracket: 4,
  effectiveBracket: 4, archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: [],
  activity: [], stamp: "s",
});
const combo = (id, label, landedBy) => ({
  id, label, pieces: label.split(" + "), requirements: ["Opposition"], results: ["Infinite mana"],
  landedBy, fetchedShare: 0.25 });
const report = (ways) => ({
  games: 10000, seed: 7, policyVersion: 3, bracket: 4, keepBracket: 4, bracketTurn: 6,
  keepRule: "bracket 4 rule: keeps seven cards with a land", turns: [4, 6, 8],
  opening: { seven: 0.7, six: 0.2, fiveOrFewer: 0.1 },
  commanders: [{ name: "Magda", castBy: [0.5, 0.8, 0.9], medianTurn: 4 }],
  mana: { median: [4, 6, 8], worstTenth: [3, 4.5, 6] },
  colourScrew: { share: 0.05, turn: 4 }, waysToWin: ways, notModelled: [] });
const ways = (faster) => ({
  combos: [
    combo("a", "Mountain + Sol Ring", [0.1, 0.4, 0.6]), combo("b", "Kiki + Pestermite", [0, 0.1, 0.2])],
  finishers: [{ name: "Craterhoof Behemoth", castableBy: [0, 0.3, 0.5] }],
  fasterThanBracket: faster });
const headings = () => walk($("goldfish")).filter((n) => n.tagName === "H3").map((n) => n.textContent);
const texts = (selector) => walk($("goldfish")).filter((n) => n.matches(selector)).map((n) => n.textContent);
stub.bracket = async () => ({ estimate: null, notes: [], causes: [], unavailable: "" });
stub.mana = async () => ({});
accountState.active = true;
state.current = 1;
state.decks = [
  { id: 1, short: "D1", name: "Deck 1", queued: 0 }, { id: 2, short: "D2", name: "Deck 2", queued: 0 }];
renderDecks();
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_deck_is_played_only_while_the_goldfish_tab_is_open() -> None:
    seen = run("""
    stub.goldfish = async () => report(null);
    state.view = view(1);
    renderDeck();
    await settle();
    const closed = calls.filter((c) => c[0] === "goldfish").length;
    showRail("mana");
    await settle();
    const elsewhere = calls.filter((c) => c[0] === "goldfish").length;
    showRail("goldfish");
    await settle();
    const open = calls.filter((c) => c[0] === "goldfish");
    renderDeck();
    await settle();
    const asked = calls.filter((c) => c[0] === "goldfish").length;
    return { closed, elsewhere, open, hidden: $("goldfish").hidden, again: asked,
      expanded: $("goldfish-toggle").getAttribute("aria-expanded") };
    """)
    assert seen == {
        "closed": 0,
        "elsewhere": 0,
        "open": [["goldfish", 1]],
        "hidden": False,
        "again": 2,
        "expanded": "true",
    }


def test_the_tab_shows_a_spinner_while_the_deck_plays_and_then_the_figures() -> None:
    seen = run("""
    let release;
    stub.goldfish = () => new Promise((resolve) => { release = resolve; });
    state.view = view(1);
    showRail("goldfish");
    await settle();
    const status = texts(".state");
    const hasSpinner = texts(".spinner").length;
    release(report(null));
    await settle();
    return { status, hasSpinner, after: texts(".state"), headings: headings(),
      rows: texts("tr").map((t) => t),
      source: texts(".goldfish-source"), keep: texts(".muted.small")[0] };
    """)
    assert seen["status"] == ["Playing the deck…"]
    assert seen["hasSpinner"] == 1
    assert seen["after"] == []
    assert seen["headings"] == ["Opening hand", "Commander cast by turn", "Mana by turn", "Colour screw"]
    assert seen["rows"] == [
        "Turn468",
        "Magda50%80%90%",
        "Turn468",
        "Median468",
        "Worst tenth34.56",
    ]
    assert seen["source"] == ["10,000 games against an empty table"]


def test_a_refusal_shows_where_the_figures_go_with_a_button_to_play_the_deck_again() -> None:
    seen = run("""
    let answers = [{ error: "The deck has no commander." }, report(null)];
    stub.goldfish = async () => answers.shift();
    state.view = view(1);
    showRail("goldfish");
    await settle();
    const refused = { alert: texts(".field-error"), headings: headings(), button: texts(".button") };
    await fire(walk($("goldfish")).find((n) => n.matches(".button")), "click");
    await settle();
    return { refused, headings: headings(), asked: calls.filter((c) => c[0] === "goldfish").length,
      alerts: texts(".field-error") };
    """)
    assert seen == {
        "refused": {
            "alert": ["The deck has no commander."],
            "headings": [],
            "button": ["Play the deck again"],
        },
        "headings": ["Opening hand", "Commander cast by turn", "Mana by turn", "Colour screw"],
        "asked": 2,
        "alerts": [],
    }


def test_a_bridge_failure_is_the_tabs_retry_not_a_stuck_spinner() -> None:
    seen = run("""
    stub.goldfish = async () => { throw new Error("bridge down"); };
    state.view = view(1);
    showRail("goldfish");
    await settle();
    return { alert: texts(".field-error"), spinner: texts(".spinner").length };
    """)
    assert seen == {"alert": ["The deck could not be played; try again."], "spinner": 0}


def test_a_report_without_ways_to_win_has_no_such_section() -> None:
    seen = run("""
    stub.goldfish = async () => report(null);
    state.view = view(1);
    showRail("goldfish");
    await settle();
    const without = headings();
    resetGoldfish();
    stub.goldfish = async () => report(ways([]));
    await loadGoldfish();
    return { without, withWays: headings(), tables: texts("table").length };
    """)
    assert "Ways to win" not in seen["without"]
    assert seen["withWays"][-1] == "Ways to win"


def test_a_combo_faster_than_the_bracket_is_warned_about_and_marked_in_its_row() -> None:
    seen = run("""
    const faster = [{ id: "a", label: "Mountain + Sol Ring", turn: 6, share: 0.4 }];
    stub.goldfish = async () => report(ways(faster));
    state.view = view(1);
    showRail("goldfish");
    await settle();
    const rows = walk($("goldfish")).filter((n) => n.matches("tr") && n.matches(".faster"));
    const none = async () => {
      resetGoldfish();
      stub.goldfish = async () => report(ways([]));
      await loadGoldfish();
      const rows = walk($("goldfish")).filter((n) => n.matches(".faster")).length;
      return texts(".goldfish-warning").length + rows;
    };
    return { warning: texts(".goldfish-warning"), marked: rows.map((r) => r.textContent),
      title: texts(".goldfish-warning-title"), none: await none() };
    """)
    assert seen["title"] == ["⚠ Faster than bracket 4"]
    assert seen["warning"] == ["⚠ Faster than bracket 4Mountain + Sol Ring: by turn 6 in 40% of games"]
    assert len(seen["marked"]) == 1
    assert seen["marked"][0].startswith("Faster than its bracket: Mountain + Sol Ring")
    assert seen["none"] == 0


def test_opening_another_deck_clears_the_tab_and_plays_that_deck() -> None:
    seen = run("""
    stub.goldfish = async (id) => ({ ...report(null), games: id === 1 ? 10000 : 500 });
    stub.open_deck = async () => ({});
    stub.deck = async (id) => view(id);
    state.view = view(1);
    showRail("goldfish");
    await settle();
    const first = texts(".goldfish-source");
    stub.goldfish = () => new Promise(() => {});
    await openDeck(2);
    await settle();
    return { first, during: texts(".state") };
    """)
    assert seen == {"first": ["10,000 games against an empty table"], "during": ["Playing the deck…"]}
