"""The app page's Meta panel during a sync: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

# Deck 1 is open with a bracket 5 view and its Meta panel unfolded; the cache holds a report for it.
PRELUDE = """
const REPORT = {
  report: { sample: { known: true, withLists: 5, thin: false }, since: "2026-08-16",
    sources: [{ fetchedAt: null }] },
  keys: [], stoppers: [], untouched: { combos: [], keys: [] }, comboGroups: [], racing: false,
};
const NOTHING = { report: null };
const syncing = (decks) => ({ user: "TESTER", signedIn: true,
  sync: { running: true, progress: "Fetching", summary: "", failed: false, decks } });
accountState.active = true;
state.current = 1;
state.view = { id: 1, bracket: 5 };
state.decks = [
  { id: 1, short: "Magda", name: "A", queued: 0 },
  { id: 2, short: "Ellivere", name: "B", queued: 0 },
];
renderDecks();
showPanel($("h-meta"), true);
const metaCalls = () => calls.filter((c) => c[0] === "meta").length;
const sample = () => $("meta-sample").textContent;
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_deck_opened_mid_sync_shows_the_cached_meta_at_once() -> None:
    seen = run("""
    stub.meta = async () => REPORT;
    renderAccount(syncing({ "1": "queued", "2": "fetching" }));
    await metaPanel.load();
    return { sample: sample(), empty: $("meta-empty").hidden, calls: metaCalls() };
    """)
    assert seen == {"sample": "5 lists since 2026-08-16", "empty": True, "calls": 1}


def test_the_updating_line_shows_only_while_the_open_decks_own_fetch_is_pending() -> None:
    lines = run("""
    stub.meta = async () => REPORT;
    const line = (decks) => { renderAccount(syncing(decks)); return !$("meta-busy").hidden; };
    return [
      line({ "1": "queued", "2": "fetching" }),
      line({ "1": "fetching", "2": "queued" }),
      line({ "1": "done", "2": "fetching" }),
      line({ "1": "incomplete", "2": "queued" }),
    ];
    """)
    assert lines == [True, True, False, False]


def test_the_updating_line_goes_when_the_sync_ends_and_follows_the_deck_opened() -> None:
    lines = run("""
    stub.meta = async () => REPORT;
    renderAccount(syncing({ "1": "queued", "2": "done" }));
    const mid = !$("meta-busy").hidden;
    state.current = 2;
    syncMetaUpdating();
    const other = !$("meta-busy").hidden;
    state.current = 1;
    syncMetaUpdating();
    renderAccount({ user: "TESTER", signedIn: true,
      sync: { running: false, progress: "", summary: "", failed: false, decks: {} } });
    return [mid, other, !$("meta-busy").hidden];
    """)
    assert lines == [True, False, False]


def test_the_panel_rereads_the_cache_when_the_open_decks_fetch_finishes_mid_sync() -> None:
    seen = run("""
    stub.meta = async () => NOTHING;
    await metaPanel.load();
    renderAccount(syncing({ "1": "fetching", "2": "queued" }));
    const before = { sample: sample(), empty: $("meta-empty").hidden, calls: metaCalls() };
    stub.meta = async () => REPORT;
    renderAccount(syncing({ "1": "done", "2": "fetching" }));
    await settle();
    return { before, after: { sample: sample(), empty: $("meta-empty").hidden, calls: metaCalls() } };
    """)
    assert seen["before"] == {"sample": "", "empty": False, "calls": 1}
    assert seen["after"] == {"sample": "5 lists since 2026-08-16", "empty": True, "calls": 2}


def test_a_poll_that_leaves_the_fetch_state_alone_does_not_reread_the_meta() -> None:
    calls = run("""
    stub.meta = async () => REPORT;
    renderAccount(syncing({ "1": "fetching", "2": "queued" }));
    renderAccount(syncing({ "1": "fetching", "2": "queued" }));
    renderAccount(syncing({ "1": "done", "2": "queued" }));
    renderAccount(syncing({ "1": "done", "2": "fetching" }));
    await settle();
    return metaCalls();
    """)
    assert calls == 1


def test_a_rising_combo_carries_a_rising_badge_and_the_others_do_not() -> None:
    seen = run("""
    const combo = (pieces, rising) => ({ pieces, others: pieces, results: [], url: "https://example.test/c",
      exposure: 0.1, rising, decks: [], answers: [] });
    const combos = [combo(["Sol Ring", "<b>x</b>"], true), combo(["Mana Vault"], false)];
    stub.meta = async () => ({ ...REPORT, comboGroups: combos.map((c) => ({ shared: [], combos: [c] })) });
    await metaPanel.load();
    const rows = $("meta-body").querySelectorAll(".meta-card");
    const badges = (row) => row.querySelectorAll(".badge.rising").map((b) => b.textContent);
    return rows.map((row) => ({ text: row.textContent, badges: badges(row) }));
    """)
    assert seen == [
        {"text": "Sol Ring + <b>x</b>Rising", "badges": ["Rising"]},
        {"text": "Mana Vault", "badges": []},
    ]


def test_combos_sharing_pieces_sit_under_a_shared_line_with_only_their_other_pieces() -> None:
    seen = run("""
    const combo = (others, rising) => ({ pieces: ["Kinnan", "Oracle", ...others], others, results: [],
      url: "https://example.test/c", exposure: 0.1, rising, decks: [], answers: [] });
    const single = { pieces: ["Sol Ring", "Mana Vault"], others: ["Sol Ring", "Mana Vault"], results: [],
      url: "https://example.test/s", exposure: 0.05, rising: false, decks: [], answers: [] };
    const comboGroups = [
      { shared: ["Kinnan", "Oracle"],
        combos: [combo(["<b>Ballista</b>"], true), combo(["Monolith"], false)] },
      { shared: [], combos: [single] },
    ];
    stub.meta = async () => ({ ...REPORT, comboGroups });
    await metaPanel.load();
    const rows = $("meta-body").querySelector(".meta-rows").children;
    return rows.map((node) => ({
      shared: node.querySelectorAll(".combo-shared").map((p) => p.textContent),
      combos: node.matches(".combo-group")
        ? node.querySelectorAll(".meta-card").map((p) => p.textContent)
        : [node.querySelector(".meta-card").textContent],
      title: node.querySelector(".meta-card a").title,
    }));
    """)
    assert seen == [
        {
            "shared": ["Shared: Kinnan, Oracle"],
            "combos": ["<b>Ballista</b>Rising", "Monolith"],
            "title": "Open on Commander Spellbook: Kinnan + Oracle + <b>Ballista</b>",
        },
        {
            "shared": [],
            "combos": ["Sol Ring + Mana Vault"],
            "title": "Open on Commander Spellbook: Sol Ring + Mana Vault",
        },
    ]


# Meta cards that stop the deck's key cards, filed by how: two removal, one stax, one counter.
STOPPERS = """
const stopper = (card, role) =>
  ({ card, role, exposure: 0.1, decks: [], combos: [], keys: ["Sol Ring"], kinds: [] });
const STOPPING = { ...REPORT, stoppers: [stopper("Swords", "removal"), stopper("Fierce Guardian", "removal"),
  stopper("Null Rod", "stax"), stopper("Force of Will", "counter")] };
const groups = () => $("meta-body").querySelectorAll(".role-group");
const states = () => groups().map((g) => [g.tagName, g.querySelector("summary").textContent, g.open]);
const sections = () => $("meta-body").querySelectorAll(".meta-section");
"""

ALL_CLOSED = [
    ["DETAILS", "Stax (1)", False],
    ["DETAILS", "Removal (2)", False],
    ["DETAILS", "Counters (1)", False],
]


def test_every_meta_section_and_category_starts_closed_even_with_an_old_stored_choice() -> None:
    seen = run(
        STOPPERS
        + """
    storage["commander.panelOpen.meta-stops-removal"] = "1";
    stub.meta = async () => STOPPING;
    await metaPanel.load();
    return { categories: states(), sections: sections().length,
      open: sections().filter((s) => s.open).length };
    """
    )
    assert seen["categories"] == ALL_CLOSED
    assert seen["sections"] > 3
    assert seen["open"] == 0


def test_a_category_the_viewer_opens_stays_open_on_a_reload_and_for_other_decks_unstored() -> None:
    seen = run(
        STOPPERS
        + """
    stub.meta = async () => STOPPING;
    await metaPanel.load();
    const removal = groups()[1];
    removal.open = true;
    await fire(removal, "toggle");
    renderMeta(1, STOPPING);
    const reloaded = states();
    renderMeta(2, STOPPING);
    const otherDeck = states();
    groups()[1].open = false;
    await fire(groups()[1], "toggle");
    renderMeta(1, STOPPING);
    return { reloaded, otherDeck, closedAgain: states(), stored: Object.keys(storage) };
    """
    )
    opened = [
        ["DETAILS", "Stax (1)", False],
        ["DETAILS", "Removal (2)", True],
        ["DETAILS", "Counters (1)", False],
    ]
    assert seen == {"reloaded": opened, "otherDeck": opened, "closedAgain": ALL_CLOSED, "stored": []}


def test_a_grouped_combo_made_of_the_shared_pieces_alone_says_so() -> None:
    seen = run("""
    const combo = (others) => ({ pieces: ["Kinnan", "Oracle", ...others], others, results: [],
      url: "https://example.test/c", exposure: 0.1, rising: false, decks: [], answers: [] });
    const comboGroups = [{ shared: ["Kinnan", "Oracle"], combos: [combo(["Ballista"]), combo([])] }];
    stub.meta = async () => ({ ...REPORT, comboGroups });
    await metaPanel.load();
    return $("meta-body").querySelectorAll(".meta-card a").map((a) => [a.textContent, a.title]);
    """)
    assert seen == [
        ["Ballista", "Open on Commander Spellbook: Kinnan + Oracle + Ballista"],
        ["Only the shared pieces", "Open on Commander Spellbook: Kinnan + Oracle"],
    ]


def test_the_reason_meta_data_is_missing_shows_beneath_the_panel_and_goes_when_there_is_none() -> None:
    seen = run("""
    const why = () => ({ text: $("meta-why").textContent, hidden: $("meta-why").hidden });
    stub.meta = async () => ({ report: null, unavailable: "EDHTop16 refused the query: Only persisted" });
    await metaPanel.load();
    const empty = why();
    stub.meta = async () => ({ ...REPORT, unavailable: "Archidekt answered HTTP 429" });
    await metaPanel.load();
    const withReport = why();
    stub.meta = async () => NOTHING;
    await metaPanel.load();
    return { empty, withReport, none: why() };
    """)
    assert seen == {
        "empty": {"text": "EDHTop16 refused the query: Only persisted", "hidden": False},
        "withReport": {"text": "Archidekt answered HTTP 429", "hidden": False},
        "none": {"text": "", "hidden": True},
    }
