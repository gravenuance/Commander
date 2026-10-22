"""The page's poll for changes from the CLI and Claude, and the reload after a sync: its real scripts run
under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

PRELUDE = """
const view = (stamp) => ({
  id: 7, short: "A", name: "A", url: "https://archidekt.com/decks/7", bracket: null, effectiveBracket: null,
  archetype: "", tags: [], leanings: [], pending: 0, stats: null, grid: [], cards: [], activity: [], stamp,
});
accountState.active = true;
state.current = 7;
state.decks = [{ id: 7, short: "A", name: "A", queued: 0 }];
state.stamps = { "7": "old" };
state.metaStamp = null;
stub.stamps = async () => ({ stamps: { "7": "new" }, meta: null });
stub.decks = async () => ({ decks: [{ id: 7, short: "A", name: "A", queued: 0 }] });
stub.deck = async () => view("new");
stub.bracket = async () => ({ estimate: null, notes: [], causes: [], unavailable: "" });
stub.account = async () => ({ user: "u", signedIn: true, sync: { running: false, decks: {} } });
stub.meta = async () => ({});
const asked = (name) => calls.filter((c) => c[0] === name).length;
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_a_poll_whose_deck_list_reload_failed_still_shows_the_open_deck_and_retries_the_list() -> None:
    seen = run("""
    let failing = true;
    stub.decks = async () => (failing ? { error: "decks folder busy" } : { decks: state.decks });
    await poll();
    failing = false;
    await poll();
    return { shown: state.view?.stamp, decks: asked("decks"), stamps: state.stamps };
    """)
    assert seen == {"shown": "new", "decks": 2, "stamps": {"7": "new"}}


def test_a_poll_whose_open_deck_reload_failed_reloads_it_on_the_next_poll() -> None:
    seen = run("""
    let failing = true;
    stub.deck = async () => (failing ? { error: "busy" } : view("new"));
    await poll();
    const afterFailure = asked("deck");
    failing = false;
    await poll();
    return { afterFailure, afterRetry: asked("deck") };
    """)
    assert seen == {"afterFailure": 1, "afterRetry": 2}


def test_a_poll_that_loaded_everything_does_not_load_it_again() -> None:
    seen = run("""
    await poll();
    await poll();
    return { decks: asked("decks"), deck: asked("deck"), stamps: state.stamps };
    """)
    assert seen == {"decks": 1, "deck": 1, "stamps": {"7": "new"}}


def test_a_poll_during_a_save_keeps_the_open_deck_stamp_so_the_change_is_loaded_when_it_ends() -> None:
    seen = run("""
    state.busy = true;
    await poll();
    const during = { deck: asked("deck"), stamp: state.stamps["7"] };
    state.busy = false;
    await poll();
    return { during, after: asked("deck") };
    """)
    assert seen == {"during": {"deck": 0, "stamp": "old"}, "after": 1}


def test_a_poll_asks_for_the_deck_list_and_the_open_deck_together() -> None:
    seen = run("""
    let open;
    const gate = new Promise((resolve) => { open = resolve; });
    stub.decks = async () => { await gate; return { decks: state.decks }; };
    stub.deck = async () => { await gate; return view("new"); };
    const polling = poll();
    await settle();
    const bothAsked = { decks: asked("decks"), deck: asked("deck") };
    open();
    await polling;
    return bothAsked;
    """)
    assert seen == {"decks": 1, "deck": 1}


def test_the_reload_after_a_sync_asks_for_the_meta_once() -> None:
    seen = run("""
    state.view = view("old");
    $("h-meta").setAttribute("aria-expanded", "true");
    stub.deck = async () => ({ ...view("new"), bracket: 5, effectiveBracket: 5 });
    await reloadAfterSync();
    await settle();
    return asked("meta");
    """)
    assert seen == 1
