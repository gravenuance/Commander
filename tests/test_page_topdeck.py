"""The app page's TopDeck panel and credit: its real scripts run under Node (`pagerun`)."""

from pagerun import Answer, needs_node, page

pytestmark = needs_node

SETUP = """
setupTopdeck();
const shownError = () => ($("topdeck-error").hidden ? "" : $("topdeck-error").textContent);
const state_ = () => ({ text: $("topdeck-state").textContent, error: shownError(),
  remove: !$("topdeck-remove").hidden, key: $("topdeck-key").value, open: $("topdeck-dialog").open,
  tip: $("topdeck-open").title });
"""


def run(program: str) -> Answer:
    return page(SETUP + program)


def test_opening_the_panel_says_whether_a_key_is_saved() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false, developers: "https://topdeck.gg/developers" });
    await fire($("topdeck-open"), "click");
    const without = state_();
    stub.topdeck = async () => ({ hasKey: true, developers: "https://topdeck.gg/developers" });
    await fire($("topdeck-open"), "click");
    return [without, state_()];
    """)
    assert seen == [
        {"text": "No TopDeck key saved", "error": "", "remove": False, "key": "", "open": True,
         "tip": "No TopDeck key saved"},
        {"text": "TopDeck key saved", "error": "", "remove": True, "key": "", "open": True,
         "tip": "TopDeck key saved"},
    ]  # fmt: skip


def test_testing_a_typed_key_keeps_it_in_the_field_and_saves_nothing() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false });
    stub.test_topdeck_key = async () => ({ ok: true });
    await fire($("topdeck-open"), "click");
    $("topdeck-key").value = "typed-key";
    await fire($("topdeck-test"), "click");
    return { now: state_(), called: calls.map((c) => c.join(":")) };
    """)
    assert seen["now"]["text"] == "TopDeck accepts the key."
    assert seen["now"]["key"] == "typed-key"
    assert seen["called"] == ["topdeck", "test_topdeck_key:typed-key"]


def test_a_refusal_is_shown_in_the_panel_and_the_key_stays_for_mending() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false });
    stub.save_topdeck_key = async () => ({
      error: "TopDeck refused the key (HTTP 401); check it in the TopDeck panel" });
    await fire($("topdeck-open"), "click");
    $("topdeck-key").value = "typed-key";
    await fire($("topdeck-form"), "submit");
    return { now: state_(), role: $("topdeck-error").getAttribute("role"), busy: $("topdeck-save").disabled };
    """)
    assert seen["now"]["error"] == "TopDeck refused the key (HTTP 401); check it in the TopDeck panel"
    assert seen["now"]["key"] == "typed-key"
    assert seen["role"] == "alert"
    assert seen["busy"] is False


def test_a_saved_key_leaves_the_field_and_offers_removal() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false });
    stub.save_topdeck_key = async () => ({ hasKey: true });
    await fire($("topdeck-open"), "click");
    $("topdeck-key").value = "typed-key";
    await fire($("topdeck-form"), "submit");
    return state_();
    """)
    assert seen == {
        "text": "Key saved. Sync fetches the tournaments.",
        "error": "",
        "remove": True,
        "key": "",
        "open": True,
        "tip": "TopDeck key saved",
    }


def test_removing_the_key_hides_the_remove_button() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: true });
    stub.remove_topdeck_key = async () => ({ hasKey: false });
    await fire($("topdeck-open"), "click");
    await fire($("topdeck-remove"), "click");
    return state_();
    """)
    assert (seen["text"], seen["remove"], seen["tip"]) == ("Key removed.", False, "No TopDeck key saved")


def test_closing_the_panel_clears_the_field() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false });
    await fire($("topdeck-open"), "click");
    $("topdeck-key").value = "typed-key";
    await fire($("topdeck-close"), "click");
    return state_();
    """)
    assert (seen["key"], seen["open"]) == ("", False)


def test_a_bridge_that_fails_is_told_in_the_panel() -> None:
    seen = run("""
    stub.topdeck = async () => ({ hasKey: false });
    stub.test_topdeck_key = async () => { throw new Error("gone"); };
    await fire($("topdeck-open"), "click");
    await fire($("topdeck-test"), "click");
    return [state_()["error"], $("topdeck-test").disabled];
    """)
    assert seen == ["TopDeck could not be reached; the details are in the log.", False]


def test_the_panel_links_to_the_page_that_issues_keys() -> None:
    links = page("""
    return walk(document.body).filter((n) => n.tagName === "A" && n.getAttribute("href").startsWith("https://topdeck.gg"))
      .map((a) => [a.getAttribute("href"), a.getAttribute("rel"), a.getAttribute("target")]);
    """)
    assert ["https://topdeck.gg/developers", "noopener", "_blank"] in links


def test_every_place_tournament_data_shows_carries_the_credit_with_its_link() -> None:
    shown = page("""
    const found = walk(document.body).filter((n) => n.className === "credit muted small");
    const link = (p) => p.childNodes.find((c) => c.tagName === "A");
    return found.map((p) => [p.textContent, link(p).getAttribute("href")]);
    """)
    assert shown == [
        ["Tournament lists from TopDeck.gg", "https://topdeck.gg"],
        ["Tournament data from TopDeck.gg", "https://topdeck.gg"],
        ["Tournament data from TopDeck.gg", "https://topdeck.gg"],
    ]
