"""The Mana tab's lands section: the calibrated land floor and the fetch figures (ADRs 0062, 0063, 0064). Its
real script runs under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const mana = {
  lands: { lands: 36, suggested: 37.7, averageManaValue: 2, cheapDraw: 0, cheapRamp: 0, landSpells: 0 },
  comparison: null, compareWith: "", colours: [], turnOne: [], tappedLands: 0, skipped: [], plan: null };
const fetches = { fetchers: 9, targets: 4, perFetch: 4 / 9, stranded: 0, bar: 1.5, cap: 12 };
const linesOf = (m) => {
  state.view = { effectiveBracket: 5 };
  renderMana(1, { ...mana, ...m });
  return [...$("mana").querySelectorAll(".mana-line")].map((n) => n.textContent);
};
"""


def lines(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_calibrated_land_floor_is_shown_beside_karstens_count_and_only_when_there_is_one() -> None:
    seen = lines("""
    return { with: linesOf({ landFloor: 33.14 }), without: linesOf({ landFloor: null }), old: linesOf({}) };
    """)
    assert [line for line in seen["with"] if line.startswith("Land floor")] == [
        "Land floor: 33.1Karsten's formula moved by the top-cut lists' gap"
    ]
    assert not any(line.startswith("Land floor") for line in seen["without"] + seen["old"])


def test_a_fetching_deck_shows_its_fetchers_the_lands_they_find_and_the_field_it_is_measured_on() -> None:
    seen = lines("return linesOf({ fetches });")
    fetch = [line for line in seen if line.startswith("Fetchers")]
    assert fetch == [
        (
            "Fetchers: 9, finding 4 lands0.44 lands per fetcherLists run a median 1.50 lands per fetcher, "
            "up to 12 fetchers"
        )
    ]


def test_the_fetch_line_agrees_in_number_and_names_a_stranded_fetcher() -> None:
    seen = lines("""
    const one = { fetchers: 1, targets: 1, perFetch: 1, stranded: 0, bar: null, cap: null };
    const stuck = { fetchers: 2, targets: 0, perFetch: 0, stranded: 2, bar: null, cap: null };
    const stuckOne = { ...stuck, fetchers: 1, stranded: 1 };
    return [one, stuck, stuckOne].map((f) => linesOf({ fetches: f }).filter((l) => l.startsWith("Fetchers")));
    """)
    assert seen == [
        ["Fetchers: 1, finding 1 land1.00 lands per fetcher"],
        ["Fetchers: 2, finding 0 lands0.00 lands per fetcher2 fetchers find nothing"],
        ["Fetchers: 1, finding 0 lands0.00 lands per fetcher1 fetcher finds nothing"],
    ]


def test_a_field_with_only_a_bar_or_only_a_cap_names_the_one_it_has() -> None:
    seen = lines("""
    const only = (extra) =>
      linesOf({ fetches: { ...fetches, ...extra } }).find((l) => l.startsWith("Fetchers"));
    return [only({ cap: null }), only({ bar: null }), only({ bar: null, cap: null })];
    """)
    assert seen == [
        "Fetchers: 9, finding 4 lands0.44 lands per fetcherLists run a median 1.50 lands per fetcher",
        "Fetchers: 9, finding 4 lands0.44 lands per fetcherLists run up to 12 fetchers",
        "Fetchers: 9, finding 4 lands0.44 lands per fetcher",
    ]


def test_a_deck_without_fetchers_shows_no_fetch_line_and_so_no_bar() -> None:
    seen = lines("""
    return { none: linesOf({ fetches: null }), old: linesOf({}) };
    """)
    for shown in seen.values():
        assert not any("fetcher" in line.lower() for line in shown)
