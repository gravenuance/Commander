"""The app page's Meta panel during a sync: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

# Deck 1 is open with a bracket 5 view and its Meta panel unfolded; the cache holds a report for it.
PRELUDE = """
const REPORT = {
  report: { sample: { known: true, withLists: 5, thin: false }, since: "2026-08-16",
    sources: [{ fetchedAt: null }] },
  keys: [], stoppers: [], untouched: { combos: [], keys: [] }, combos: [], racing: false,
};
const NOTHING = { report: null };
const syncing = (decks) => ({ user: "MAKKIS", signedIn: true,
  sync: { running: true, progress: "Fetching", summary: "", failed: false, decks } });
accountState.active = true;
state.current = 1;
state.view = { id: 1, bracket: 5 };
state.decks = [
  { id: 1, short: "Magda", name: "A", queued: 0 },
  { id: 2, short: "Ellivere", name: "B", queued: 0 },
];
renderDecks();
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
    renderAccount({ user: "MAKKIS", signedIn: true,
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
