"""The app page's settings dialog, grace cards included: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

# Shared by the programs: the bridge's settings answer and helpers that read and drive the grace rows.
PRELUDE = """
const answer = (over = {}) => ({
  adjacent: [], ignore: [], tagSearch: false, themes: [], leanings: [], maxAdjacent: 8, maxGrace: 10,
  grace: [{ name: "Basalt Monolith", factor: 3 }, { name: "Rings of Brighthearth", factor: 2.5 }], ...over,
});
let refreshed = 0;
refresh = async () => { refreshed++; };
state.current = 1;
$("deck-short").textContent = "Magda";
setupSettings();
const open = async (over) => {
  stub.settings = async () => answer(over);
  await openSettings();
};
const rows = () => $("settings-grace").children.map((li) => ({
  name: li.querySelector(".grace-name").textContent,
  factor: li.querySelector("input").value,
  factorLabel: li.querySelector("input").getAttribute("aria-label"),
  removeLabel: li.querySelector("button").getAttribute("aria-label"),
}));
const saves = () => calls.filter((c) => c[0] === "save_settings");
const type = async (node, value) => { node.value = value; await fire(node, "input"); };
const add = async (name, factor) => {
  await type($("settings-grace-name"), name);
  await type($("settings-grace-factor"), factor);
  await fire($("settings-grace-add"), "click");
};
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_dialog_lists_each_grace_card_with_its_factor() -> None:
    seen = run("""
    await open();
    return { rows: rows(), open: $("settings-dialog").open, name: $("settings-grace-name").value,
      factor: $("settings-grace-factor").value };
    """)
    assert seen["open"] is True
    assert seen["rows"] == [
        {
            "name": "Basalt Monolith",
            "factor": "3",
            "factorLabel": "Factor for Basalt Monolith",
            "removeLabel": "Remove Basalt Monolith",
        },
        {
            "name": "Rings of Brighthearth",
            "factor": "2.5",
            "factorLabel": "Factor for Rings of Brighthearth",
            "removeLabel": "Remove Rings of Brighthearth",
        },
    ]
    assert (seen["name"], seen["factor"]) == ("", "3")


def test_a_deck_without_grace_cards_lists_none() -> None:
    assert run("await open({ grace: [] }); return rows();") == []


def test_adding_changing_and_removing_cards_save_as_one_payload_on_the_save_button() -> None:
    seen = run("""
    stub.save_settings = async () => answer();
    await open();
    await add("  Sol Ring ", "4");
    const added = rows();
    await type($("settings-grace").children[1].querySelector("input"), "6");
    await fire($("settings-grace").children[0].querySelector("button"), "click");
    const beforeSave = saves().length;
    await fire($("settings-save"), "click");
    return { added, beforeSave, saves: saves(), after: { name: $("settings-grace-name").value,
      factor: $("settings-grace-factor").value }, closed: !$("settings-dialog").open, refreshed };
    """)
    assert [r["name"] for r in seen["added"]] == ["Basalt Monolith", "Rings of Brighthearth", "Sol Ring"]
    assert seen["added"][2]["factor"] == "4"
    assert seen["beforeSave"] == 0
    assert seen["saves"] == [
        [
            "save_settings",
            1,
            [],
            [],
            False,
            [{"name": "Rings of Brighthearth", "factor": 6}, {"name": "Sol Ring", "factor": 4}],
        ]
    ]
    assert seen["after"] == {"name": "", "factor": "3"}
    assert seen["closed"] is True
    assert seen["refreshed"] == 1


def test_adding_a_card_already_listed_changes_its_factor_instead_of_adding_a_row() -> None:
    seen = run("""
    await open();
    await add("basalt monolith", "7.5");
    return rows().map((r) => [r.name, r.factor]);
    """)
    assert seen == [["Basalt Monolith", "7.5"], ["Rings of Brighthearth", "2.5"]]


def test_the_name_field_takes_enter_and_a_blank_name_adds_nothing() -> None:
    seen = run("""
    await open({ grace: [] });
    await fire($("settings-grace-add"), "click");
    const blank = rows().length;
    $("settings-grace-name").value = "Sol Ring";
    await fire($("settings-grace-name"), "keydown", { key: "Enter" });
    return { blank, rows: rows().map((r) => [r.name, r.factor]) };
    """)
    assert seen == {"blank": 0, "rows": [["Sol Ring", "3"]]}


def test_a_factor_outside_the_range_is_refused_in_the_dialog_and_adds_nothing() -> None:
    seen = run("""
    await open({ grace: [] });
    const refusals = [];
    for (const factor of ["1", "0.5", "11", "", "abc"]) {
      await add("Sol Ring", factor);
      refusals.push([$("settings-error").hidden, $("settings-error").textContent, rows().length]);
    }
    await add("Sol Ring", "2");
    return { refusals, rows: rows().length, hidden: $("settings-error").hidden };
    """)
    assert seen["refusals"] == [[False, "A factor over 1 and at most 10", 0]] * 5
    assert (seen["rows"], seen["hidden"]) == (1, True)


def test_a_refusal_from_the_save_shows_in_the_dialog_and_keeps_it_open_with_the_edits() -> None:
    seen = run("""
    stub.save_settings = async () => ({ error: "No card named 'Nonesuch Relic' (check the spelling)" });
    await open();
    await add("Nonesuch Relic", "3");
    await fire($("settings-save"), "click");
    return { text: $("settings-error").textContent, hidden: $("settings-error").hidden,
      open: $("settings-dialog").open, rows: rows().length, refreshed };
    """)
    assert seen == {
        "text": "No card named 'Nonesuch Relic' (check the spelling)",
        "hidden": False,
        "open": True,
        "rows": 3,
        "refreshed": 0,
    }


def test_a_cleared_factor_is_sent_as_null_for_the_save_to_refuse() -> None:
    seen = run("""
    stub.save_settings = async () => answer();
    await open();
    await type($("settings-grace").children[0].querySelector("input"), "");
    await fire($("settings-save"), "click");
    return saves()[0][5];
    """)
    assert seen == [
        {"name": "Basalt Monolith", "factor": None},
        {"name": "Rings of Brighthearth", "factor": 2.5},
    ]


def test_a_card_name_is_shown_as_text_never_as_markup() -> None:
    seen = run("""
    await open({ grace: [{ name: "<img src=x onerror=boom()> & <b>Sol</b>", factor: 2 }] });
    const row = $("settings-grace").children[0];
    return { tags: row.children.map((c) => c.tagName), name: row.querySelector(".grace-name").textContent,
      nested: row.querySelector(".grace-name").children.length };
    """)
    assert seen["tags"] == ["SPAN", "INPUT", "BUTTON"]
    assert seen["name"] == "<img src=x onerror=boom()> & <b>Sol</b>"
    assert seen["nested"] == 0


def test_reopening_the_dialog_shows_the_saved_cards_not_the_discarded_edits() -> None:
    seen = run("""
    await open();
    await add("Sol Ring", "4");
    await fire($("settings-cancel"), "click");
    await open();
    return rows().map((r) => r.name);
    """)
    assert seen == ["Basalt Monolith", "Rings of Brighthearth"]
