"""The app page's deck list, fetch icons and sign-in gate: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

# Shared by the programs: two decks as the bridge lists them and an account answer.
PRELUDE = """
const decks = [
  { id: 1, short: "Magda", name: "Sample, deck..", queued: 0 },
  { id: 2, short: "Ellivere", name: "Second", queued: 0 },
];
const account = (over = {}) => ({ user: "TESTER", signedIn: true,
  sync: { running: false, progress: "", summary: "", failed: false, decks: {} }, ...over });
const icons = () => $("deck-list").querySelectorAll(".fetch-icon");
const rowLabels = () => $("deck-list").children.map((li) => li.firstChild.getAttribute("aria-label"));
"""


def run(program: str) -> Answer:
    return page(PRELUDE + program)


def test_the_deck_list_paints_a_labelled_icon_per_fetch_state() -> None:
    painted = run("""
    state.decks = decks;
    renderDecks();
    const read = () => icons().map((i) => ({ text: i.textContent, role: i.getAttribute("role"),
      label: i.getAttribute("aria-label"), title: i.title, spinner: i.classList.contains("spinner") }));
    const none = read();
    paintFetchStates({ "1": "queued", "2": "fetching" });
    const running = read();
    paintFetchStates({ "1": "done", "2": "incomplete" });
    const ended = read();
    paintFetchStates({});
    return { none, running, ended, cleared: read(), labels: rowLabels() };
    """)
    blank = {"text": "", "role": None, "label": None, "title": "", "spinner": False}
    assert painted["none"] == [blank, blank]
    assert painted["running"] == [
        {
            "text": "…",
            "role": "img",
            "label": "Waiting to fetch",
            "title": "Waiting to fetch",
            "spinner": False,
        },
        {
            "text": "",
            "role": "img",
            "label": "Fetching meta and ideas",
            "title": "Fetching meta and ideas",
            "spinner": True,
        },
    ]
    assert [(i["text"], i["label"]) for i in painted["ended"]] == [
        ("✓", "Fetched"),
        ("⚠", "Fetch incomplete; see the log"),
    ]
    assert painted["cleared"] == [blank, blank]
    assert painted["labels"] == ["Magda, Sample, deck..", "Ellivere, Second"]


def test_a_fetch_state_change_touches_only_its_own_deck_and_a_redraw_keeps_the_icon() -> None:
    seen = run("""
    state.decks = decks;
    renderDecks();
    paintFetchStates({ "1": "queued", "2": "queued" });
    const buttons = $("deck-list").children.map((li) => li.firstChild);
    buttons[1].setAttribute("aria-label", "untouched");
    paintFetchStates({ "1": "done", "2": "queued" });
    const labels = rowLabels();
    state.decks = decks.map((d) => ({ ...d, queued: d.id }));
    renderDecks();
    return { labels, redrawn: icons().map((i) => i.getAttribute("aria-label")),
      sameRows: $("deck-list").children.every((li, i) => li.firstChild === buttons[i]) };
    """)
    assert seen["labels"] == ["Magda, Sample, deck.., Fetched", "untouched"]
    assert seen["redrawn"] == ["Fetched", "Waiting to fetch"]
    assert seen["sameRows"] is True


def test_the_accounts_poll_paints_a_running_syncs_fetch_states_and_clears_them_when_it_ends() -> None:
    seen = run("""
    accountState.active = true;
    state.decks = decks;
    renderDecks();
    renderAccount(account({ sync: { running: true, progress: "Fetching", summary: "", failed: false,
      decks: { "1": "fetching", "2": "queued" } } }));
    const running = icons().map((i) => i.getAttribute("aria-label"));
    renderAccount(account({ sync: { running: false, progress: "", summary: "", failed: false,
      decks: { "1": "done", "2": "done" } } }));
    return { running, ended: icons().map((i) => i.getAttribute("aria-label")) };
    """)
    assert seen == {"running": ["Fetching meta and ideas", "Waiting to fetch"], "ended": [None, None]}


def test_leaving_the_sign_in_dialog_without_signing_in_empties_the_page() -> None:
    seen = run("""
    setupAccount();
    accountState.active = true;
    accountState.signedIn = false;
    state.decks = decks;
    renderDecks();
    openSignIn();
    $("signin-dialog").close();
    return { rows: $("deck-list").children.length, gate: $("signed-out").hidden, decks: state.decks.length,
      focus: document.activeElement.id, note: $("sync-status").textContent, syncOff: $("sync-decks").disabled,
      active: accountState.active };
    """)
    assert seen == {
        "rows": 0,
        "gate": False,
        "decks": 0,
        "focus": "signed-out-sign-in",
        "note": "Sign in to sync",
        "syncOff": True,
        "active": False,
    }


def test_a_sign_in_closes_the_dialog_and_lists_the_accounts_decks() -> None:
    seen = run("""
    setupAccount();
    stub.sign_in = async () => account();
    stub.decks = async () => ({ decks });
    $("signin-user").value = "TESTER";
    $("signin-password").value = "secret";
    openSignIn();
    $("signin-user").value = "TESTER";
    $("signin-password").value = "secret";
    await fire($("signin-form"), "submit");
    await settle();
    return { rows: $("deck-list").children.length, gate: $("signed-out").hidden, active: accountState.active,
      password: $("signin-password").value, open: $("signin-dialog").open,
      called: calls.map((c) => c[0]) };
    """)
    assert seen == {
        "rows": 2,
        "gate": True,
        "active": True,
        "password": "",
        "open": False,
        "called": ["sign_in", "decks"],
    }


def test_after_a_sign_in_focus_lands_on_the_first_deck() -> None:
    focus = run("""
    setupAccount();
    stub.sign_in = async () => account();
    stub.decks = async () => ({ decks });
    openSignIn();
    await fire($("signin-form"), "submit");
    await settle();
    const on = document.activeElement;
    return [on.className, on.parentNode.getAttribute("data-id")];
    """)
    assert focus == ["deck-button", "1"]


def test_after_a_sign_in_to_an_account_without_decks_focus_lands_on_sync() -> None:
    focus = run("""
    setupAccount();
    stub.sign_in = async () => account();
    stub.decks = async () => ({ decks: [] });
    openSignIn();
    await fire($("signin-form"), "submit");
    await settle();
    return document.activeElement.id;
    """)
    assert focus == "sync-decks"


def test_signing_out_empties_the_page_before_the_sign_in_dialog_opens() -> None:
    seen = run("""
    setupAccount();
    accountState.active = true;
    state.decks = decks;
    renderDecks();
    stub.sign_out = async () => account({ user: "", signedIn: false });
    let atOpen = null;
    $("signin-dialog").showModal = () => {
      atOpen = { rows: $("deck-list").children.length, gate: $("signed-out").hidden };
    };
    await fire($("sign-out"), "click");
    return atOpen;
    """)
    assert seen == {"rows": 0, "gate": False}


def test_a_deck_answer_that_outlives_its_account_is_dropped() -> None:
    seen = run("""
    accountState.active = true;
    let answer;
    stub.decks = () => new Promise((resolve) => { answer = resolve; });
    const loading = loadDecks();
    showSignedOut();
    answer({ decks });
    await loading;
    return { held: state.decks.length, rows: $("deck-list").children.length };
    """)
    assert seen == {"held": 0, "rows": 0}


def start_page(account_answer: str) -> Answer:
    """Fire the page's start-up with the bridge answering `account` as given."""
    return run(f"""
    stub.account = async () => {account_answer};
    stub.decks = async () => ({{ decks }});
    stub.stamps = async () => ({{ stamps: {{ "1": "a" }}, meta: "m" }});
    const opened = [];
    $("signin-dialog").showModal = () => opened.push("dialog");
    await fire(window, "pywebviewready");
    return {{ dialog: opened.length, gate: $("signed-out").hidden, rows: $("deck-list").children.length,
      deck: $("deck").hidden, note: $("sync-status").textContent, syncOff: $("sync-decks").disabled,
      called: calls.map((c) => c[0]) }};
    """)


def test_starting_signed_out_shows_the_gate_and_the_dialog_and_loads_no_decks() -> None:
    started = start_page("account({ user: 'TESTER', signedIn: false })")
    assert started["dialog"] == 1
    assert (started["gate"], started["rows"], started["syncOff"]) == (False, 0, True)
    assert "decks" not in started["called"]


def test_starting_signed_in_lists_the_decks_and_leaves_the_main_area_blank() -> None:
    started = start_page("account()")
    assert (started["dialog"], started["gate"], started["rows"], started["deck"]) == (0, True, 2, True)
    assert "open_deck" not in started["called"]


def test_starting_with_an_unreadable_account_empties_the_page_and_says_why() -> None:
    started = start_page("({ error: 'broken' })")
    assert (started["dialog"], started["gate"], started["rows"]) == (0, False, 0)
    assert (started["note"], started["syncOff"]) == ("Could not read the account; see the log", True)
