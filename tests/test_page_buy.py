"""The app page's Buy screen: its real scripts run under Node (`pagerun`) over made-up bridge answers."""

import re

import pytest

from pagerun import WEB, Answer, needs_node, page

pytestmark = needs_node

# The countries SETUP's fake bridge lists, by id.
NAMES = {1: "Austria", 7: "Germany", 23: "Netherlands", 28: "Sweden"}

SETUP = """
setupBuy();
accountState.active = true;
accountState.epoch = 1;
const card = (name, quantity, included = true) => ({ name, quantity, included });
const copies = (cards) => cards.reduce((n, c) => n + c.quantity, 0);
const deck = (id, short, cards, extra = {}) => {
  const ticked = cards.filter((c) => c.included);
  const tick = ticked.length === 0 ? "none" : ticked.length === cards.length ? "all" : "some";
  return { id, short, name: `${short} deck`, known: true, included: ticked.length > 0, tick,
    copies: copies(cards), tickedCopies: copies(ticked), cards, ...extra };
};
const unticked = (d) => deck(d.id, d.short, d.cards.map((c) => ({ ...c, included: false })));
const PANTLAZA = deck(2, "Pantlaza", [card("Arcane Signet", 2), card("Mind Stone", 1)]);
const countOf = (index) => $("buy-decks").children[index].querySelector(".count").textContent;
const ELLIVERE = deck(1, "Ellivere", [card("Lotus Petal", 1)]);
const idle = (extra = {}) => ({ phase: "idle", running: false, cancelling: false, progress: "", message: "",
  detail: "", results: null, ...extra });
const FILTERS = { language: "english", condition: "NM", foil: "any", countries: [], delivery: "any",
  maxPriceCents: null };
const view = (extra = {}) => ({ decks: [ELLIVERE, PANTLAZA], copies: 4,
  filters: FILTERS, baseSellers: 15, status: idle(), ...extra });
const withFilters = (change, extra = {}) => view({ filters: { ...FILTERS, ...change }, ...extra });
const COUNTRIES = [{ id: 1, name: "Austria" }, { id: 7, name: "Germany" }, { id: 23, name: "Netherlands" },
  { id: 28, name: "Sweden" }];
const COUNTRY_ERROR = { error: "Cardmarket's countries could not be loaded; try again." };
stub.buy_countries = async () => ({ countries: COUNTRIES });
const panes = [];
stub.buy_pane = async (...area) => { panes.push(area); return {}; };
const placePane = (rect) => { $("buy-pane").getBoundingClientRect = () => ({ ...rect }); };
const row = (sellers, total, cards, shipping, saving, cheapest) => ({ sellers, totalCents: total,
  cardsCents: total - shipping,
  shippingCents: shipping, cards, savingCents: saving, cheapest, optimal: true });
const RESULTS = { rows: [row(1, 760, 2, 200, null, false), row(2, 490, 2, 400, 270, true)], timedOut: [],
  partial: false,
  pages: 5, destination: "Sweden", notFound: [], noOffers: [], overPrice: [], unmatched: [],
  quantityMismatch: [] };
const done = (results = RESULTS, extra = {}) => idle({ phase: "results", results, ...extra });
const PACKAGE = { sellers: 2, totalCents: 490, cardsCents: 90, shippingCents: 400, optimal: true,
  check: { totalCents: 490, picks: [[101, 2], [102, 1]] },
  slowestDays: 12, withoutEstimate: 0, leftOutNoEstimate: 0,
  shipments: [
    { seller: "SellerB", country: "France", cards: 1, valueCents: 30, method: "Lettre Internationale",
      shippingCents: 255, tracked: false, deliveryDays: 6, deliveryBasis: "own",
      picks: [{ card: "Arcane Signet", copies: 2,
        expansion: "Commander Legends",
        condition: "NM", language: "English", foil: false, priceCents: 30, trendCents: 25 }] },
    { seller: "SellerA", country: "Germany", cards: 1, valueCents: 60, method: "Tracked Parcel",
      shippingCents: 345, tracked: true, deliveryDays: 12, deliveryBasis: "country",
      picks: [{ card: "Mind Stone", copies: 1, expansion: "Kaladesh",
        condition: "EX",
        language: "English", foil: true, priceCents: 60, trendCents: null }] },
  ], missing: ["Lotus Petal"], short: [{ card: "Mind Stone", wanted: 3, available: 1 }] };
// The same two sellers after a new scan, 30 cents dearer.
const DEARER = { ...PACKAGE, totalCents: 520, check: { totalCents: 520, picks: [[103, 2], [102, 1]] } };
const DEARER_RESULTS = { ...RESULTS, rows: [RESULTS.rows[0], row(2, 520, 2, 400, 240, true)] };
const leaves = (node) => walk(node)
  .filter((n) => n.childNodes.length === 1 && n.childNodes[0].data !== undefined)
  .map((n) => n.textContent);
const open = async (answer = view()) => { stub.buy = async () => answer; await fire($("buy-open"),
  "click"); };
"""


def run(program: str) -> Answer:
    return page(SETUP + program)


class TestOpening:
    def test_opening_lists_the_cards_by_deck_with_a_tick_each_and_the_choices_made(self) -> None:
        seen = run("""
        await open();
        const ticks = [...$("buy-decks").children].map((item) => {
          const box = walk(item).find((n) => n.tagName === "INPUT");
          return [item.dataset.id, box.checked, box.getAttribute("aria-label"), box.disabled];
        });
        const pressed = (id) => [...$(id).children].filter((b) => b.getAttribute("aria-pressed") === "true")
          .map((b) => b.textContent);
        return {
          buy: !$("buy").hidden, deck: $("deck").hidden, current: $("buy-open").getAttribute("aria-current"),
          summary: $("buy-summary").textContent, ticks, language: pressed("buy-language"),
            foil: pressed("buy-foil"),
          condition: $("buy-condition").value, base: $("buy-base-sellers").value,
          counts: leaves($("buy-decks")), scan: $("buy-scan-label").textContent,
            scanOff: $("buy-scan").disabled,
          calls: calls.map((c) => c[0]),
        };
        """)
        assert seen["buy"] is True
        assert seen["deck"] is True
        assert seen["current"] == "page"
        assert seen["summary"] == "4 cards from 2 decks"
        assert seen["ticks"] == [
            ["1", True, "Include Ellivere", False],
            ["2", True, "Include Pantlaza", False],
        ]
        assert (seen["language"], seen["foil"], seen["condition"], seen["base"]) == (
            ["English"],
            ["Any"],
            "NM",
            "15",
        )
        assert seen["counts"] == [
            "Ellivere", "1 card", "Lotus Petal", "Pantlaza", "3 cards", "2 × Arcane Signet", "Mind Stone",
        ]  # fmt: skip
        assert (seen["scan"], seen["scanOff"]) == ("Scan Cardmarket", False)
        assert seen["calls"] == ["buy"]

    def test_opening_it_closes_the_open_deck_and_a_deck_closes_it_again(self) -> None:
        seen = run("""
        stub.open_deck = async () => ({});
        stub.deck = async (id) => ({ error: "not needed" });
        state.decks = [{ id: 1, short: "Ellivere", name: "x", queued: 0 }];
        state.current = 1;
        $("deck").hidden = false;
        await open();
        const during = { buy: $("buy").hidden, deck: $("deck").hidden, current: state.current };
        try { await openDeck(1); } catch (error) { /* the stub's refusal is shown, not needed here */ }
        return { during, after: { buy: $("buy").hidden, current: state.current,
          aria: $("buy-open").getAttribute("aria-current") } };
        """)
        assert seen["during"] == {"buy": False, "deck": True, "current": None}
        assert seen["after"] == {"buy": True, "current": 1, "aria": None}

    def test_coming_back_keeps_the_elements_and_the_results_built_before(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const first = { deck: $("buy-decks").children[0], row: $("buy-curve").children[0] };
        closeBuy();
        await open(view({ status: done() }));
        return {
          sameDeck: $("buy-decks").children[0] === first.deck,
          sameRow: $("buy-curve").children[0] === first.row,
          rows: $("buy-curve").children.length,
        };
        """)
        assert seen == {"sameDeck": True, "sameRow": True, "rows": 2}

    def test_a_deck_that_leaves_the_list_leaves_the_screen_and_the_others_stay(self) -> None:
        seen = run("""
        await open();
        const kept = $("buy-decks").children[1];
        stub.buy = async () => view({ decks: [PANTLAZA], copies: 3 });
        await refreshBuy();
        return { count: $("buy-decks").children.length, kept: $("buy-decks").children[0] === kept };
        """)
        assert seen == {"count": 1, "kept": True}

    def test_a_deck_folds_out_its_cards_from_a_button_that_says_so(self) -> None:
        seen = run("""
        await open();
        const item = $("buy-decks").children[1];
        const fold = walk(item).find((n) => n.tagName === "BUTTON");
        const list = walk(item).find((n) => n.tagName === "UL");
        const before = [fold.getAttribute("aria-expanded"), list.hidden];
        await fire(fold, "click");
        return { before, after: [fold.getAttribute("aria-expanded"), list.hidden],
          controls: fold.getAttribute("aria-controls") === list.id, label: fold.getAttribute("aria-label") };
        """)
        assert seen["before"] == ["false", True]
        assert seen["after"] == ["true", False]
        assert seen["controls"] is True
        assert seen["label"] == "Pantlaza cards to buy"

    def test_a_deck_whose_labels_are_unknown_cannot_be_ticked(self) -> None:
        seen = run("""
        const old = deck(4, "Old", [], { known: false, included: false });
        await open(view({ decks: [old], copies: 0 }));
        const box = walk($("buy-decks")).find((n) => n.tagName === "INPUT");
        return { disabled: box.disabled, text: leaves($("buy-decks")), empty: !$("buy-empty").hidden,
          scanOff: $("buy-scan").disabled };
        """)
        assert seen["disabled"] is True
        assert seen["text"] == ["Old", "Labels unknown; sync"]
        assert seen["empty"] is True
        assert seen["scanOff"] is True


class TestNothingToBuy:
    def test_without_any_deck_there_is_an_empty_state_and_no_scan(self) -> None:
        seen = run("""
        await open(view({ decks: [], copies: 0 }));
        return { text: $("buy-empty-text").textContent, shown: !$("buy-empty").hidden,
          cards: $("buy-cards-block").hidden,
          scanOff: $("buy-scan").disabled, tip: $("buy-scan").title, summary: $("buy-summary").textContent };
        """)
        assert seen == {
            "text": "No “Don't Have” cards to buy.",
            "shown": True,
            "cards": True,
            "scanOff": True,
            "tip": "Tick a deck to scan",
            "summary": "",
        }

    def test_with_every_deck_unticked_the_decks_stay_to_be_ticked_again(self) -> None:
        seen = run("""
        const off = (d) => ({ ...d, included: false, tick: "none", tickedCopies: 0 });
        await open(view({ decks: [off(ELLIVERE), off(PANTLAZA)], copies: 0 }));
        return { text: $("buy-empty-text").textContent, cards: $("buy-cards-block").hidden,
          scanOff: $("buy-scan").disabled };
        """)
        assert seen == {"text": "Tick a deck to scan.", "cards": False, "scanOff": True}


class TestChoices:
    def test_unticking_a_deck_sends_it_and_draws_the_answer(self) -> None:
        seen = run("""
        await open();
        stub.buy_include = async () => view({ decks: [ELLIVERE, unticked(PANTLAZA)], copies: 1 });
        const box = walk($("buy-decks").children[1]).find((n) => n.tagName === "INPUT");
        box.checked = false;
        await fire(box, "change");
        return { call: calls.find((c) => c[0] === "buy_include"), checked: box.checked,
          summary: $("buy-summary").textContent };
        """)
        assert seen == {"call": ["buy_include", 2, False], "checked": False, "summary": "1 card from 1 deck"}

    def test_a_refused_tick_puts_the_box_back_and_says_why(self) -> None:
        seen = run("""
        await open();
        stub.buy_include = async () => ({ error: "Wait for the scan, or cancel it." });
        const box = walk($("buy-decks").children[1]).find((n) => n.tagName === "INPUT");
        box.checked = false;
        await fire(box, "change");
        return { checked: box.checked, toast: $("status-text").textContent,
          alert: $("status").classList.contains("error") };
        """)
        assert seen == {"checked": True, "toast": "Wait for the scan, or cancel it.", "alert": True}

    def test_a_filter_choice_is_sent_with_the_others_and_resets_what_was_shown(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_filters = async () => withFilters({ foil: "non-foil" });
        const button = [...$("buy-foil").children].find((b) => b.textContent === "Non-foil");
        await fire(button, "click");
        const pressed = [...$("buy-foil").children].filter((b) => b.getAttribute("aria-pressed") === "true")
          .map((b) => b.textContent);
        return { call: calls.find((c) => c[0] === "buy_filters"), pressed, results: $("buy-results").hidden,
          curve: $("buy-curve").children.length };
        """)
        assert seen["call"] == ["buy_filters", "english", "NM", "non-foil", [], "any", ""]
        assert seen["pressed"] == ["Non-foil"]
        assert seen["results"] is True
        assert seen["curve"] == 0

    def test_the_condition_and_language_choices_are_sent_too(self) -> None:
        seen = run("""
        await open();
        stub.buy_filters = async (language, condition, foil) =>
          withFilters({ language, condition, foil });
        $("buy-condition").value = "any";
        await fire($("buy-condition"), "change");
        const any = [...$("buy-language").children].find((b) => b.textContent === "Any");
        await fire(any, "click");
        return calls.filter((c) => c[0] === "buy_filters");
        """)
        assert seen == [
            ["buy_filters", "english", "any", "any", [], "any", ""],
            ["buy_filters", "any", "any", "any", [], "any", ""],
        ]

    def test_the_language_control_reads_english_japanese_both_any_with_english_pressed(self) -> None:
        seen = run("""
        await open();
        return [...$("buy-language").children].map((b) => [b.textContent, b.dataset.value,
          b.getAttribute("aria-pressed"), b.getAttribute("type")]);
        """)
        assert seen == [
            ["English", "english", "true", "button"],
            ["Japanese", "japanese", "false", "button"],
            ["Both", "both", "false", "button"],
            ["Any", "any", "false", "button"],
        ]

    def test_each_language_button_sends_its_choice_and_is_the_only_one_pressed_afterwards(self) -> None:
        seen = run("""
        await open();
        stub.buy_filters = async (language, condition, foil) =>
          withFilters({ language, condition, foil });
        const trail = [];
        for (const label of ["Japanese", "Both", "Any", "English"]) {
          const button = [...$("buy-language").children].find((b) => b.textContent === label);
          await fire(button, "click");
          trail.push([calls[calls.length - 1][1], [...$("buy-language").children]
            .filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.textContent)]);
        }
        return trail;
        """)
        assert seen == [
            ["japanese", ["Japanese"]],
            ["both", ["Both"]],
            ["any", ["Any"]],
            ["english", ["English"]],
        ]

    def test_the_language_buttons_take_focus_in_reading_order_and_are_not_taken_out_of_it(self) -> None:
        seen = run("""
        await open();
        const buttons = [...$("buy-language").children];
        const focused = [];
        for (const button of buttons) {
          button.focus();
          focused.push(document.activeElement.textContent);
        }
        return { focused, tabindex: buttons.map((b) => b.getAttribute("tabindex")),
          disabled: buttons.map((b) => b.disabled) };
        """)
        assert seen == {
            "focused": ["English", "Japanese", "Both", "Any"],
            "tabindex": [None, None, None, None],
            "disabled": [False, False, False, False],
        }

    def test_a_saved_both_shows_pressed_on_opening(self) -> None:
        seen = run("""
        await open(view({ filters: { ...FILTERS, language: "both" } }));
        return [...$("buy-language").children]
          .filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.textContent);
        """)
        assert seen == ["Both"]

    def test_every_condition_is_offered_with_any_first(self) -> None:
        seen = run("""
        return [...$("buy-condition").children].map((o) => [o.getAttribute("value"), o.textContent]);
        """)
        assert seen == [
            ["any", "Any"],
            ["MT", "Mint"],
            ["NM", "Near Mint"],
            ["EX", "Excellent"],
            ["GD", "Good"],
            ["LP", "Light Played"],
            ["PL", "Played"],
            ["PO", "Poor"],
        ]

    def test_base_sellers_sit_in_a_fold_that_starts_closed_and_save_on_change(self) -> None:
        seen = run("""
        setupPanels();
        await open();
        const toggle = $("h-buy-settings");
        const before = [toggle.getAttribute("aria-expanded"), $("buy-settings-body").hidden];
        await fire(toggle, "click");
        stub.buy_base_sellers = async (count) => view({ baseSellers: count });
        $("buy-base-sellers").value = "20";
        await fire($("buy-base-sellers"), "change");
        return { before, open: $("buy-settings-body").hidden,
          call: calls.find((c) => c[0] === "buy_base_sellers"),
          shown: $("buy-base-sellers").value };
        """)
        assert seen["before"] == ["false", True]
        assert seen["open"] is False
        assert seen["call"] == ["buy_base_sellers", 20]
        assert seen["shown"] == "20"

    def test_a_refused_base_sellers_value_goes_back_to_the_saved_one(self) -> None:
        seen = run("""
        await open();
        stub.buy_base_sellers = async () => ({ error: "Base sellers must be 1 to 40." });
        $("buy-base-sellers").value = "99";
        await fire($("buy-base-sellers"), "change");
        return [$("buy-base-sellers").value, $("status-text").textContent];
        """)
        assert seen == ["15", "Base sellers must be 1 to 40."]


class TestScanning:
    def test_scanning_shows_the_progress_a_cancel_button_and_locks_the_choices(self) -> None:
        seen = run("""
        await open();
        stub.buy_scan = async () => idle({ phase: "wants", running: true, progress: "Starting the scan" });
        await fire($("buy-scan"), "click");
        const lock = () => ({
          scan: $("buy-scan").disabled, busy: $("buy-scan").getAttribute("aria-busy"),
          spinner: !$("buy-scan").querySelector(".spinner").hidden, cancel: !$("buy-cancel").hidden,
          progress: $("buy-progress").textContent, live: $("buy-progress").getAttribute("role"),
          ticks: [...$("buy-decks").children].map((i) => walk(i).find((n) => n.tagName === "INPUT").disabled),
          foil: [...$("buy-foil").children].map((b) => b.disabled), condition: $("buy-condition").disabled,
          delivery: [...$("buy-delivery").children].map((b) => b.disabled),
          countries: $("buy-countries").disabled, base: $("buy-base-sellers").disabled,
        });
        const started = lock();
        stub.buy_status = async () => idle({ phase: "scanning", running: true,
          progress: "Reading Sol Ring · 12 of 40" });
        await pollBuy();
        return { started, polled: lock().progress, timers: timers.length, calls: calls.map((c) => c[0]) };
        """)
        assert seen["started"] == {
            "scan": True,
            "busy": "true",
            "spinner": True,
            "cancel": True,
            "progress": "Starting the scan",
            "live": "status",
            "ticks": [True, True],
            "foil": [True, True, True],
            "condition": True,
            "delivery": [True] * 5,
            "countries": True,
            "base": True,
        }
        assert seen["polled"] == "Reading Sol Ring · 12 of 40"
        # One timer after the click, one after each poll that found the scan still running.
        assert seen["timers"] == 2
        assert seen["calls"] == ["buy", "buy_scan", "buy_status"]

    def test_the_focus_follows_the_run_from_scan_to_cancel_and_back_to_scan_at_its_end(self) -> None:
        seen = run("""
        await open();
        const focused = () => document.activeElement.id;
        $("buy-scan").focus();
        stub.buy_scan = async () => idle({ phase: "wants", running: true, progress: "Starting the scan" });
        await fire($("buy-scan"), "click");
        const started = focused();
        stub.buy_cancel = async () => idle({ phase: "scanning", running: true, cancelling: true });
        await fire($("buy-cancel"), "click");
        const cancelling = focused();
        $("buy-cancel").hidden = false;
        $("buy-cancel").focus();
        stub.buy_status = async () => done();
        await pollBuy();
        return { started, cancelling, ended: focused() };
        """)
        assert seen == {"started": "buy-cancel", "cancelling": "buy-title", "ended": "buy-scan"}

    def test_the_focus_is_handed_on_even_when_the_browser_blurs_a_control_as_it_is_disabled_or_hidden(
        self,
    ) -> None:
        seen = run("""
        // Chromium drops the focus of a control the moment it is disabled or hidden.
        for (const id of ["buy-scan", "buy-cart", "buy-cancel"]) {
          const node = $(id);
          for (const prop of ["disabled", "hidden"]) {
            const own = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), prop);
            Object.defineProperty(node, prop, { get() { return own.get.call(this); },
              set(on) { own.set.call(this, on); if (on && document.activeElement === this) this.blur(); } });
          }
        }
        await open();
        const focused = () => document.activeElement.id;
        $("buy-scan").focus();
        stub.buy_scan = async () => idle({ phase: "wants", running: true, progress: "Starting the scan" });
        await fire($("buy-scan"), "click");
        const started = focused();
        stub.buy_cancel = async () => idle({ phase: "scanning", running: true, cancelling: true });
        await fire($("buy-cancel"), "click");
        const cancelling = focused();
        paintBuyStatus(idle({ phase: "scanning", running: true }));
        $("buy-cancel").focus();
        paintBuyStatus(done());
        return { started, cancelling, ended: focused() };
        """)
        assert seen == {"started": "buy-cancel", "cancelling": "buy-title", "ended": "buy-scan"}

    def test_a_run_that_ends_with_a_cart_report_moves_the_focus_to_its_title(self) -> None:
        seen = run_cart("""
        await open(view({ status: idle({ phase: "cart", running: true, results: RESULTS }) }));
        $("buy-cancel").focus();
        paintBuyStatus(carted());
        return [document.activeElement.id, $("buy-cart-title").getAttribute("tabindex")];
        """)
        assert seen == ["buy-cart-title", "-1"]

    def test_a_paint_leaves_the_focus_alone_when_it_is_somewhere_else(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "scanning", running: true }) }));
        const box = walk($("buy-decks")).find((n) => n.tagName === "INPUT");
        box.focus();
        paintBuyStatus(done());
        const after = document.activeElement === box;
        $("buy-scan").focus();
        paintBuyStatus(done());
        return [after, document.activeElement.id];
        """)
        assert seen == [True, "buy-scan"]

    def test_a_second_click_while_the_first_is_sent_is_ignored(self) -> None:
        seen = run("""
        await open();
        let release;
        stub.buy_scan = () => new Promise((resolve) => { release = () => resolve(idle({ phase: "wants",
          running: true })); });
        const first = fire($("buy-scan"), "click");
        const second = fire($("buy-scan"), "click");
        release();
        await Promise.all([first, second]);
        return calls.filter((c) => c[0] === "buy_scan").length;
        """)
        assert seen == 1

    def test_the_poll_ends_when_the_scan_does_and_the_packages_appear(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "scanning", running: true,
          progress: "Reading Sol Ring · 1 of 2" }) }));
        const timersBefore = timers.length;
        stub.buy_status = async () => done();
        await pollBuy();
        return { timersBefore, timersAfter: timers.length, results: !$("buy-results").hidden,
          rows: $("buy-curve").children.length, line: $("buy-progress").textContent,
          scan: $("buy-scan").disabled, label: $("buy-scan-label").textContent,
            cancel: $("buy-cancel").hidden };
        """)
        assert seen == {
            "timersBefore": 1,
            "timersAfter": 1,
            "results": True,
            "rows": 2,
            "line": "Found 2 packages.",
            "scan": False,
            "label": "Scan Cardmarket again",
            "cancel": True,
        }

    def test_cancelling_asks_the_bridge_and_says_it_stops_after_the_page(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "scanning", running: true,
          progress: "Reading Sol Ring · 1 of 2" }) }));
        stub.buy_cancel = async () => idle({ phase: "scanning", running: true, cancelling: true });
        await fire($("buy-cancel"), "click");
        return { call: calls.at(-1), line: $("buy-progress").textContent, text: $("buy-cancel").textContent,
          off: $("buy-cancel").disabled };
        """)
        assert seen == {
            "call": ["buy_cancel"],
            "line": "Stopping after this page…",
            "text": "Cancelling…",
            "off": True,
        }

    def test_a_refused_scan_is_shown_and_the_button_comes_back(self) -> None:
        seen = run("""
        await open();
        stub.buy_scan = async () => ({ error: "No cards to buy; tick a deck first." });
        await fire($("buy-scan"), "click");
        return { toast: $("status-text").textContent, off: $("buy-scan").disabled };
        """)
        assert seen == {"toast": "No cards to buy; tick a deck first.", "off": False}

    def test_a_poll_that_fails_is_not_repeated_by_the_page(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "scanning", running: true, progress: "x" }) }));
        const before = timers.length;
        stub.buy_status = async () => { throw new Error("gone"); };
        await pollBuy();
        return [before, timers.length];
        """)
        assert seen == [1, 1]

    def test_the_scan_going_on_while_a_deck_is_open_is_picked_up_on_return(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "scanning", running: true,
          progress: "Reading Sol Ring · 1 of 2" }) }));
        closeBuy();
        stub.buy_status = async () => { throw new Error("must not be asked while another screen shows"); };
        await pollBuy();
        await open(view({ status: done() }));
        return { calls: calls.map((c) => c[0]), rows: $("buy-curve").children.length };
        """)
        assert seen == {"calls": ["buy", "buy"], "rows": 2}


class TestTheCardmarketPane:
    def test_the_pane_is_hidden_and_reports_nothing_while_idle_scanning_or_done(self) -> None:
        seen = run("""
        const states = [idle(), idle({ phase: "wants", running: true, progress: "Preparing wants list" }),
          idle({ phase: "scanning", running: true, progress: "Reading Sol Ring" }),
          idle({ phase: "solving", running: true, progress: "Finding" }), done(),
          idle({ phase: "cancelled", message: "Scan cancelled." }),
          idle({ phase: "stopped", message: "Scan stopped." })];
        const shown = [];
        for (const status of states) {
          await open(view({ status }));
          shown.push([status.phase, $("buy-pane").hidden, $("buy-body").classList.contains("covered")]);
        }
        return { shown, panes };
        """)
        assert seen["shown"] == [
            [phase, True, False]
            for phase in ("idle", "wants", "scanning", "solving", "results", "cancelled", "stopped")
        ]
        assert seen["panes"] == []

    @pytest.mark.parametrize(
        ("phase", "line"),
        [("signin", "Sign in to Cardmarket below"), ("check", "Finish Cardmarket's check below")],
    )
    def test_the_owners_turn_shows_the_pane_under_the_scan_row_covers_the_rest_and_keeps_cancel(
        self, phase: str, line: str
    ) -> None:
        seen = run(f"""
        placePane({{ left: 24, top: 120, width: 800, height: 420 }});
        window.devicePixelRatio = 1.25;
        await open(view({{ status: idle({{ phase: "{phase}", running: true, progress: "{line}" }}) }}));
        const body = $("buy-body").childNodes.filter((n) => n.tagName);
        const pane = $("buy-pane");
        return {{
          pane: !pane.hidden, covered: $("buy-body").classList.contains("covered"),
          ariaHidden: pane.getAttribute("aria-hidden"), text: pane.textContent,
          line: $("buy-progress").textContent, cancel: [!$("buy-cancel").hidden, $("buy-cancel").disabled],
          scanOff: $("buy-scan").disabled, language: [...$("buy-language").children].every((b) => b.disabled),
          afterScanRow: body.indexOf(pane) > body.indexOf($("buy-scan").parentNode),
          panes,
        }};
        """)
        assert seen["pane"] is True
        assert seen["covered"] is True
        assert (seen["ariaHidden"], seen["text"]) == ("true", "")
        assert seen["line"] == line
        assert seen["cancel"] == [True, False]
        assert seen["scanOff"] is True
        assert seen["language"] is True
        assert seen["afterScanRow"] is True
        assert seen["panes"] == [[24, 120, 800, 420, 1.25]]

    def test_the_pane_without_a_device_pixel_ratio_reports_a_scale_of_one(self) -> None:
        seen = run("""
        placePane({ left: 0, top: 80, width: 640, height: 400 });
        await open(view({ status: idle({ phase: "signin", running: true, progress: "x" }) }));
        return panes;
        """)
        assert seen == [[0, 80, 640, 400, 1]]

    def test_the_same_area_is_reported_once_and_a_new_one_when_the_window_resizes(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        const status = idle({ phase: "signin", running: true, progress: "x" });
        await open(view({ status }));
        paintBuyStatus(status);
        paintBuyStatus(status);
        await fire(window, "resize");
        const once = panes.length;
        placePane({ left: 24, top: 120, width: 700, height: 380 });
        await fire(window, "resize");
        return { once, panes };
        """)
        assert seen["once"] == 1
        assert seen["panes"] == [[24, 120, 800, 420, 1], [24, 120, 700, 380, 1]]

    def test_the_pane_goes_when_the_phase_leaves_and_no_area_is_reported_once(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        await open(view({ status: idle({ phase: "signin", running: true, progress: "x" }) }));
        stub.buy_status = async () => idle({ phase: "scanning", running: true, progress: "Reading it" });
        await pollBuy();
        const after = { pane: $("buy-pane").hidden, covered: $("buy-body").classList.contains("covered"),
          line: $("buy-progress").textContent };
        await pollBuy();
        return { after, panes };
        """)
        assert seen["after"] == {"pane": True, "covered": False, "line": "Reading it"}
        assert seen["panes"] == [[24, 120, 800, 420, 1], [0, 0, 0, 0, 1]]

    def test_the_pane_is_released_when_the_screen_closes_and_found_again_when_it_opens(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        const status = idle({ phase: "check", running: true, progress: "x" });
        await open(view({ status }));
        closeBuy();
        await settle();
        const closed = { pane: $("buy-pane").hidden, panes: panes.length };
        await open(view({ status }));
        return { closed, pane: $("buy-pane").hidden, panes };
        """)
        assert seen["closed"] == {"pane": True, "panes": 2}
        assert seen["pane"] is False
        assert seen["panes"] == [[24, 120, 800, 420, 1], [0, 0, 0, 0, 1], [24, 120, 800, 420, 1]]

    def test_an_account_that_goes_releases_the_pane(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        await open(view({ status: idle({ phase: "signin", running: true, progress: "x" }) }));
        buyForget();
        await settle();
        return { pane: $("buy-pane").hidden, panes };
        """)
        assert seen == {"pane": True, "panes": [[24, 120, 800, 420, 1], [0, 0, 0, 0, 1]]}

    def test_an_area_that_changes_while_a_report_is_out_is_reported_after_it_in_order(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        const status = idle({ phase: "signin", running: true, progress: "x" });
        let release;
        stub.buy_pane = (...area) => new Promise((resolve) => {
          panes.push(area);
          if (panes.length === 1) release = () => resolve({}); else resolve({});
        });
        const opening = open(view({ status }));
        await settle();
        const during = panes.length;
        placePane({ left: 24, top: 120, width: 700, height: 380 });
        await fire(window, "resize");
        const stillOne = panes.length;
        release();
        await opening;
        await settle();
        return { during, stillOne, panes, sending: buyState.paneSending };
        """)
        assert seen == {
            "during": 1,
            "stillOne": 1,
            "panes": [[24, 120, 800, 420, 1], [24, 120, 700, 380, 1]],
            "sending": False,
        }

    def test_a_report_the_bridge_refused_is_sent_again_with_the_next_paint(self) -> None:
        seen = run("""
        placePane({ left: 24, top: 120, width: 800, height: 420 });
        const status = idle({ phase: "signin", running: true, progress: "x" });
        stub.buy_pane = async () => ({ error: "The sign-in area was not valid." });
        await open(view({ status }));
        stub.buy_pane = async (...area) => { panes.push(area); return {}; };
        paintBuyStatus(status);
        await settle();
        return panes;
        """)
        assert seen == [[24, 120, 800, 420, 1]]


class TestEndings:
    def test_a_scan_waiting_for_the_owner_says_what_to_do_and_can_still_be_cancelled(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "signin", running: true,
          progress: "Sign in to Cardmarket below" }) }));
        return [$("buy-progress").textContent, !$("buy-cancel").hidden];
        """)
        assert seen == ["Sign in to Cardmarket below", True]

    def test_a_cancelled_scan_says_so_and_offers_to_scan_again(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "cancelled", message: "Scan cancelled." }) }));
        return { line: $("buy-progress").textContent, error: $("buy-error").hidden,
          label: $("buy-scan-label").textContent,
          off: $("buy-scan").disabled, results: $("buy-results").hidden };
        """)
        assert seen == {
            "line": "Scan cancelled.",
            "error": True,
            "label": "Scan Cardmarket again",
            "off": False,
            "results": True,
        }

    def test_a_stopped_scan_is_an_alert_with_its_reason_a_tooltip_and_a_way_to_scan_again(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "stopped",
          message: "Scan stopped after 14 pages; results are partial.",
          detail: "Sylvan Safekeeper: Cloudflare's check was still up after 30 s" }) }));
        const error = $("buy-error");
        return { text: error.textContent, shown: !error.hidden, role: error.getAttribute("role"),
          tip: error.title,
          label: $("buy-scan-label").textContent, off: $("buy-scan").disabled };
        """)
        assert seen == {
            "text": "Scan stopped after 14 pages; results are partial.",
            "shown": True,
            "role": "alert",
            "tip": "Sylvan Safekeeper: Cloudflare's check was still up after 30 s",
            "label": "Scan Cardmarket again",
            "off": False,
        }

    def test_partial_results_are_marked_and_still_listed(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "stopped",
          message: "Scan stopped after 14 pages; results are partial.",
          results: { ...RESULTS, partial: true } }) }));
        return { partial: !$("buy-partial").hidden, tip: $("buy-partial").title,
          rows: $("buy-curve").children.length };
        """)
        assert seen == {
            "partial": True,
            "tip": "Scan stopped after 14 pages; results are partial.",
            "rows": 2,
        }

    def test_a_scan_that_found_no_offer_says_there_is_nothing_to_buy_and_why(self) -> None:
        seen = run("""
        const none = { ...RESULTS, rows: [], noOffers: ["Sol Ring", "Mind Stone"], notFound: ["Fake Card"] };
        await open(view({ status: done(none) }));
        return { line: $("buy-progress").textContent, curve: leaves($("buy-curve")),
          notes: leaves($("buy-notes")),
          package: $("buy-package").hidden };
        """)
        assert seen["line"] == "Nothing to buy: no offers found."
        assert seen["curve"] == ["No package covers these cards."]
        assert seen["notes"] == ["Not found on Cardmarket: ", "No offers for these filters: "]
        assert seen["package"] is True


class TestThePackages:
    def test_one_row_per_seller_count_with_total_cards_shipping_and_the_saving(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        return [...$("buy-curve").children].map((r) => leaves(r));
        """)
        assert seen == [
            ["1 seller", "€7.60", "2", "€2.00"],
            ["2 sellers", "€4.90", "2", "€4.00", "Saves €2.70", "Cheapest"],
        ]

    def test_the_rows_are_buttons_and_the_group_is_named(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        return { tags: [...$("buy-curve").children].map((r) => [r.tagName, r.getAttribute("type"),
          r.getAttribute("aria-pressed")]),
          group: [$("buy-curve").getAttribute("role"), $("buy-curve").getAttribute("aria-label")] };
        """)
        assert seen["tags"] == [["BUTTON", "button", "false"]] * 2
        assert seen["group"] == ["group", "Packages by number of sellers"]

    def test_each_row_names_its_figures_for_a_screen_reader(self) -> None:
        seen = run("""
        const rough = { ...RESULTS, rows: [RESULTS.rows[0], { ...RESULTS.rows[1], optimal: false }] };
        await open(view({ status: done() }));
        const labels = () => [...$("buy-curve").children].map((r) => r.getAttribute("aria-label"));
        const proven = labels();
        paintBuyStatus(done(rough));
        return [proven, labels()];
        """)
        assert seen == [
            [
                "1 seller, total €7.60, 2 cards, shipping €2.00",
                "2 sellers, total €4.90, 2 cards, shipping €4.00, saves €2.70, cheapest",
            ],
            [
                "1 seller, total €7.60, 2 cards, shipping €2.00",
                "2 sellers, total €4.90, 2 cards, shipping €4.00, saves €2.70, may not be the cheapest",
            ],
        ]

    def test_a_row_the_solver_did_not_prove_cheapest_says_so_instead_of_cheapest(self) -> None:
        seen = run("""
        const rough = { ...RESULTS, rows: [{ ...row(1, 760, 2, 200, null, true), optimal: false }] };
        await open(view({ status: done(rough) }));
        const badge = walk($("buy-curve")).find((n) => n.className === "badge status gc");
        return [leaves($("buy-curve").children[0]), badge.title];
        """)
        assert seen == [
            ["1 seller", "€7.60", "2", "€2.00", "May not be cheapest"],
            "The solver ran out of time; a cheaper package may exist.",
        ]

    def test_a_package_the_solver_did_not_prove_cheapest_says_so_in_its_notes(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => ({ ...PACKAGE, optimal: false, missing: [], short: [] });
        await fire($("buy-curve").children[1], "click");
        return [...$("buy-package-notes").children].map((n) => n.textContent);
        """)
        assert seen == ["May not be the cheapest: the solver ran out of time."]

    def test_the_seller_counts_with_no_package_found_in_time_are_rows_that_say_so_in_order(self) -> None:
        seen = run("""
        const late = { ...RESULTS, timedOut: [4, 3] };
        await open(view({ status: done(late) }));
        const rows = [...$("buy-curve").children];
        return { texts: rows.map((r) => leaves(r)), tags: rows.map((r) => r.tagName),
          sellers: rows.map((r) => r.dataset.sellers) };
        """)
        assert seen["texts"] == [
            ["1 seller", "€7.60", "2", "€2.00"],
            ["2 sellers", "€4.90", "2", "€4.00", "Saves €2.70", "Cheapest"],
            ["3 sellers", "Not found in time"],
            ["4 sellers", "Not found in time"],
        ]
        assert seen["tags"] == ["BUTTON", "BUTTON", "DIV", "DIV"]
        assert seen["sellers"] == ["1", "2", None, None]

    def test_a_count_between_the_rows_is_listed_between_them_and_cannot_be_picked(self) -> None:
        seen = run("""
        const gap = { ...RESULTS, rows: [RESULTS.rows[0], row(3, 400, 2, 300, 360, true)], timedOut: [2] };
        await open(view({ status: done(gap) }));
        stub.buy_package = async (sellers) => ({ ...PACKAGE, sellers });
        const rows = [...$("buy-curve").children];
        await fire(rows[1], "click");
        await fire($("buy-curve"), "keydown", { key: "ArrowDown" });
        return { order: rows.map((r) => leaves(r)[0]), asked: calls.filter((c) => c[0] === "buy_package") };
        """)
        assert seen == {"order": ["1 seller", "2 sellers", "3 sellers"], "asked": []}

    def test_with_no_package_and_no_count_timed_out_the_curve_says_none_covers_the_cards(self) -> None:
        seen = run("""
        await open(view({ status: done({ ...RESULTS, rows: [] }) }));
        const none = $("buy-curve").children[0].textContent;
        paintBuyStatus(done({ ...RESULTS, rows: [], timedOut: [1] }));
        return [none, [...$("buy-curve").children].map((r) => r.textContent)];
        """)
        assert seen == ["No package covers these cards.", ["1 sellerNot found in time"]]

    def test_picking_a_row_shows_its_sellers_with_shipping_cards_prices_and_trend(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        return {
          title: $("buy-package-title").textContent, sub: $("buy-package-sub").textContent,
          pressed: [...$("buy-curve").children].map((r) => r.getAttribute("aria-pressed")),
          shown: !$("buy-package").hidden, call: calls.find((c) => c[0] === "buy_package"),
          sellers: [...$("buy-sellers").children].map((s) => leaves(s)),
          notes: [...$("buy-package-notes").children].map((n) => n.textContent),
        };
        """)
        assert seen["title"] == "2 sellers · €4.90"
        assert seen["sub"] == "Cards €0.90 · shipping €4.00"
        assert seen["pressed"] == ["false", "true"]
        assert seen["shown"] is True
        assert seen["call"] == ["buy_package", 2]
        assert seen["sellers"] == [
            [
                "SellerB", "France · Lettre Internationale €2.55, not tracked · 6 days",
                "Card", "Set", "Condition", "Language", "Foil", "Price", "Trend",
                "2 × Arcane Signet", "Commander Legends", "NM", "English", "€0.30", "€0.25",
            ],
            [
                "SellerA", "Germany · Tracked Parcel €3.45, tracked · about 12 days (Germany median)",
                "Card", "Set", "Condition", "Language", "Foil", "Price", "Trend",
                "Mind Stone", "Kaladesh", "EX", "English", "Foil", "€0.60", "–",
            ],
        ]  # fmt: skip
        assert seen["notes"] == [
            "No usable offer: Lotus Petal",
            "Fewer copies offered than wanted: Mind Stone (3 wanted, 1 offered)",
        ]

    def test_the_trend_column_says_what_it_is_in_a_tooltip(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        const trend = walk($("buy-sellers")).find((n) => n.tagName === "TH" && n.textContent === "Trend");
        return trend.title;
        """)
        assert seen == "Cardmarket's trend price of the card's cheapest printing"

    def test_a_row_picked_before_is_shown_again_without_asking_the_bridge(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async (sellers) => ({ ...PACKAGE, sellers });
        await fire($("buy-curve").children[1], "click");
        await fire($("buy-curve").children[0], "click");
        await fire($("buy-curve").children[1], "click");
        return { asked: calls.filter((c) => c[0] === "buy_package").map((c) => c[1]),
          title: $("buy-package-title").textContent };
        """)
        assert seen["asked"] == [2, 1]
        assert seen["title"].startswith("2 sellers")

    def test_an_answer_that_arrives_after_another_row_was_picked_is_dropped(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const waiting = [];
        stub.buy_package = (sellers) => new Promise((resolve) => waiting.push(() => resolve({ ...PACKAGE,
          sellers })));
        const first = fire($("buy-curve").children[0], "click");
        const second = fire($("buy-curve").children[1], "click");
        waiting[1]();
        await second;
        waiting[0]();
        await first;
        return $("buy-package-title").textContent;
        """)
        assert seen.startswith("2 sellers")

    def test_a_poll_that_changes_nothing_keeps_the_package_on_show(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        const second = $("buy-curve").children[1];
        paintBuyStatus(done());
        return { shown: !$("buy-package").hidden, same: $("buy-curve").children[1] === second };
        """)
        assert seen == {"shown": True, "same": True}

    def test_new_results_without_the_picked_row_close_the_package(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        paintBuyStatus(done({ ...RESULTS, rows: [row(1, 700, 2, 200, null, true)] }));
        return { shown: !$("buy-package").hidden, picked: buyState.picked };
        """)
        assert seen == {"shown": False, "picked": None}

    def test_new_results_that_keep_the_picked_count_draw_its_package_again(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        stub.buy_package = async () => DEARER;
        paintBuyStatus(done(DEARER_RESULTS));
        await settle();
        return { title: $("buy-package-title").textContent, shown: !$("buy-package").hidden,
          pressed: $("buy-curve").children[1].getAttribute("aria-pressed"),
          detail: buyState.detail.check, asked: calls.filter((c) => c[0] === "buy_package").length };
        """)
        assert seen == {
            "title": "2 sellers · €5.20",
            "shown": True,
            "pressed": "true",
            "detail": {"totalCents": 520, "picks": [[103, 2], [102, 1]]},
            "asked": 2,
        }

    def test_what_the_scan_could_not_place_is_listed_in_plain_words(self) -> None:
        seen = run("""
        const odd = { ...RESULTS, notFound: ["Fake Card"], noOffers: ["Sol Ring"], unmatched: ["Mind Stone"],
          quantityMismatch: [{ card: "Arcane Signet", wanted: 3, got: 2 }] };
        await open(view({ status: done(odd) }));
        return [...$("buy-notes").children].map((n) => n.textContent);
        """)
        assert seen == [
            "Not found on Cardmarket: Fake Card",
            "No offers for these filters: Sol Ring",
            "Left out of Cardmarket's wants list: Mind Stone",
            "Another quantity on Cardmarket's wants list: Arcane Signet (3 wanted, 2 listed)",
        ]


class TestKeyboard:
    def test_the_controls_are_native_buttons_inputs_and_selects(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const tags = (id) => [...$(id).children].map((n) => n.tagName);
        return { scan: $("buy-scan").tagName, cancel: $("buy-cancel").tagName, language: tags("buy-language"),
          foil: tags("buy-foil"), condition: $("buy-condition").tagName, base: $("buy-base-sellers").tagName,
          fold: walk($("buy-decks")).filter((n) => n.tagName === "BUTTON").length };
        """)
        assert seen == {
            "scan": "BUTTON",
            "cancel": "BUTTON",
            "language": ["BUTTON", "BUTTON", "BUTTON", "BUTTON"],
            "foil": ["BUTTON", "BUTTON", "BUTTON"],
            "condition": "SELECT",
            "base": "INPUT",
            "fold": 2,
        }

    def test_arrow_keys_move_between_the_rows_and_stop_at_the_ends(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const rows = [...$("buy-curve").children];
        const at = () => rows.indexOf(document.activeElement);
        rows[0].focus();
        const trail = [at()];
        for (const key of ["ArrowDown", "ArrowDown", "ArrowUp", "End", "Home"]) {
          const event = await fire(rows[at()], "keydown", { key });
          trail.push(at(), event.defaultPrevented);
        }
        return trail;
        """)
        assert seen == [0, 1, True, 1, True, 0, True, 1, True, 0, True]

    def test_other_keys_are_left_alone(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const rows = [...$("buy-curve").children];
        rows[0].focus();
        const event = await fire(rows[0], "keydown", { key: "Tab" });
        return [event.defaultPrevented, rows.indexOf(document.activeElement)];
        """)
        assert seen == [False, 0]

    def test_opening_the_screen_moves_focus_to_its_heading(self) -> None:
        seen = run("""
        await open();
        return [document.activeElement.id, $("buy-title").getAttribute("tabindex")];
        """)
        assert seen == ["buy-title", "-1"]

    def test_the_heading_and_the_groups_have_names(self) -> None:
        seen = run("""
        return {
          section: $("buy").getAttribute("aria-labelledby"),
          language: $("buy-language").getAttribute("aria-labelledby"),
            foil: $("buy-foil").getAttribute("aria-labelledby"),
          condition: walk(document.body)
            .find((n) => n.tagName === "LABEL" && n.getAttribute("for") === "buy-condition").textContent,
          results: $("buy-results").getAttribute("aria-labelledby"),
        };
        """)
        assert seen == {
            "section": "buy-title",
            "language": "buy-language-label",
            "foil": "buy-foil-label",
            "condition": "Minimum condition",
            "results": "buy-results-title",
        }


class TestTheCardsOfADeck:
    def test_each_card_has_its_own_tick_in_the_fold_and_starts_ticked(self) -> None:
        seen = run("""
        await open();
        const list = $("buy-cards-2");
        const boxes = walk(list).filter((n) => n.tagName === "INPUT");
        return {
          types: boxes.map((b) => b.getAttribute("type")),
          checked: boxes.map((b) => b.checked),
          names: boxes.map((b) => b.getAttribute("aria-label")),
          labelled: boxes.every((b) => b.closest("label") !== null),
          text: leaves(list),
          ids: new Set(walk($("buy-decks")).filter((n) => n.tagName === "INPUT").map((b) => b.id)).size,
        };
        """)
        assert seen["types"] == ["checkbox", "checkbox"]
        assert seen["checked"] == [True, True]
        assert seen["names"] == ["Include Arcane Signet from Pantlaza", "Include Mind Stone from Pantlaza"]
        assert seen["labelled"] is True
        assert seen["text"] == ["2 × Arcane Signet", "Mind Stone"]
        assert seen["ids"] == 5

    def test_unticking_a_card_sends_the_deck_and_the_card_and_draws_the_answer(self) -> None:
        seen = run("""
        await open();
        const pantlaza = deck(2, "Pantlaza", [card("Arcane Signet", 2), card("Mind Stone", 1, false)]);
        stub.buy_include_card = async () => view({ decks: [ELLIVERE, pantlaza], copies: 3 });
        const [signet, stone] = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT");
        stone.checked = false;
        await fire(stone, "change");
        const head = walk($("buy-decks").children[1]).find((n) => n.tagName === "INPUT");
        return { call: calls.find((c) => c[0] === "buy_include_card"), stone: stone.checked,
          signet: signet.checked, summary: $("buy-summary").textContent, counts: countOf(1),
          head: [head.checked, head.indeterminate, head.getAttribute("aria-checked")] };
        """)
        assert seen["call"] == ["buy_include_card", 2, "Mind Stone", False]
        assert (seen["stone"], seen["signet"]) == (False, True)
        assert seen["summary"] == "3 cards from 2 decks"
        assert seen["counts"] == "2 of 3 cards"
        assert seen["head"] == [False, True, "mixed"]

    def test_a_tick_keeps_the_boxes_and_the_focus_it_was_made_with(self) -> None:
        seen = run("""
        await open();
        const [signet, stone] = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT");
        stone.focus();
        const pantlaza = deck(2, "Pantlaza", [card("Arcane Signet", 2), card("Mind Stone", 1, false)]);
        stub.buy_include_card = async () => view({ decks: [ELLIVERE, pantlaza], copies: 3 });
        stone.checked = false;
        await fire(stone, "change");
        const again = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT");
        return { same: again[0] === signet && again[1] === stone, focus: document.activeElement === stone };
        """)
        assert seen == {"same": True, "focus": True}

    def test_a_refused_card_tick_puts_the_box_back_and_says_why(self) -> None:
        seen = run("""
        await open();
        stub.buy_include_card = async () => ({ error: "Wait for the scan, or cancel it." });
        const stone = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT")[1];
        stone.checked = false;
        await fire(stone, "change");
        return { checked: stone.checked, toast: $("status-text").textContent };
        """)
        assert seen == {"checked": True, "toast": "Wait for the scan, or cancel it."}

    def test_the_deck_box_is_ticked_for_all_unticked_for_none_and_mixed_for_some(self) -> None:
        seen = run("""
        const states = {
          all: PANTLAZA,
          none: deck(2, "Pantlaza", PANTLAZA.cards.map((c) => ({ ...c, included: false }))),
          some: deck(2, "Pantlaza", [card("Arcane Signet", 2), card("Mind Stone", 1, false)]),
        };
        const out = {};
        for (const [name, pantlaza] of Object.entries(states)) {
          await open(view({ decks: [ELLIVERE, pantlaza], copies: 4 }));
          const box = walk($("buy-decks").children[1]).find((n) => n.tagName === "INPUT");
          out[name] = [box.checked, box.indeterminate, box.getAttribute("aria-checked"),
            countOf(1)];
        }
        return out;
        """)
        assert seen == {
            "all": [True, False, None, "3 cards"],
            "none": [False, False, None, "3 cards"],
            "some": [False, True, "mixed", "2 of 3 cards"],
        }

    def test_clicking_the_mixed_deck_box_ticks_every_card_and_clicking_it_again_unticks_them(self) -> None:
        seen = run("""
        const some = deck(2, "Pantlaza", [card("Arcane Signet", 2), card("Mind Stone", 1, false)]);
        await open(view({ decks: [ELLIVERE, some], copies: 3 }));
        const box = walk($("buy-decks").children[1]).find((n) => n.tagName === "INPUT");
        stub.buy_include = async (id, included) => view({ decks: [ELLIVERE,
          included ? PANTLAZA : unticked(PANTLAZA)] });
        // A click on a mixed box leaves it ticked, as in the browser.
        box.indeterminate = false;
        box.checked = true;
        await fire(box, "change");
        const ticked = { checked: box.checked, mixed: box.indeterminate,
          cards: walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT").map((b) => b.checked) };
        box.checked = false;
        await fire(box, "change");
        const cleared = { checked: box.checked, cards: walk($("buy-cards-2"))
          .filter((n) => n.tagName === "INPUT").map((b) => b.checked) };
        return { ticked, unticked: cleared, calls: calls.filter((c) => c[0] === "buy_include") };
        """)
        assert seen["ticked"] == {"checked": True, "mixed": False, "cards": [True, True]}
        assert seen["unticked"] == {"checked": False, "cards": [False, False]}
        assert seen["calls"] == [["buy_include", 2, True], ["buy_include", 2, False]]

    def test_the_scan_stays_on_while_some_cards_are_ticked_and_goes_off_with_none(self) -> None:
        seen = run("""
        const some = deck(2, "Pantlaza", [card("Arcane Signet", 2, false), card("Mind Stone", 1)]);
        await open(view({ decks: [some], copies: 1 }));
        const on = { summary: $("buy-summary").textContent, off: $("buy-scan").disabled };
        const none = deck(2, "Pantlaza", PANTLAZA.cards.map((c) => ({ ...c, included: false })));
        stub.buy_include_card = async () => view({ decks: [none], copies: 0 });
        const stone = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT")[1];
        stone.checked = false;
        await fire(stone, "change");
        return { on, off: { summary: $("buy-summary").textContent, off: $("buy-scan").disabled,
          text: $("buy-empty-text").textContent } };
        """)
        assert seen["on"] == {"summary": "1 card from 1 deck", "off": False}
        assert seen["off"] == {"summary": "", "off": True, "text": "Tick a deck to scan."}

    def test_the_card_boxes_are_locked_while_a_scan_runs(self) -> None:
        seen = run("""
        await open();
        stub.buy_scan = async () => idle({ phase: "wants", running: true, progress: "Starting" });
        await fire($("buy-scan"), "click");
        return walk($("buy-decks")).filter((n) => n.tagName === "INPUT").map((b) => b.disabled);
        """)
        assert seen == [True, True, True, True, True]

    def test_the_card_boxes_are_rebuilt_when_the_cards_change(self) -> None:
        seen = run("""
        await open();
        const before = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT");
        const fewer = deck(2, "Pantlaza", [card("Mind Stone", 1)]);
        stub.buy = async () => view({ decks: [ELLIVERE, fewer], copies: 2 });
        await refreshBuy();
        const after = walk($("buy-cards-2")).filter((n) => n.tagName === "INPUT");
        return { before: before.length, after: after.length, text: leaves($("buy-cards-2")) };
        """)
        assert seen == {"before": 2, "after": 1, "text": ["Mind Stone"]}


class TestTheMaxPrice:
    def test_the_field_starts_blank_for_any_price_and_has_a_name_and_a_euro_sign(self) -> None:
        seen = run("""
        await open();
        const input = $("buy-max-price");
        const label = walk($("buy").querySelector(".buy-filters")).find((n) => n.tagName === "LABEL"
          && n.getAttribute("for") === "buy-max-price");
        return { value: input.value, placeholder: input.getAttribute("placeholder"),
          mode: input.getAttribute("inputmode"), label: label?.textContent,
          name: input.getAttribute("aria-label"), off: input.disabled,
          unit: $("buy").querySelector(".buy-money-unit").textContent };
        """)
        assert seen == {
            "value": "",
            "placeholder": "Any",
            "mode": "decimal",
            "label": "Max price per card",
            "name": "Max price per card in euros",
            "off": False,
            "unit": "€",
        }

    @pytest.mark.parametrize(
        ("cents", "shown"), [(500, "5"), (250, "2.50"), (5, "0.05"), (1_000_000, "10000")]
    )
    def test_the_saved_cap_shows_in_whole_euros_or_with_cents(self, cents: int, shown: str) -> None:
        seen = run(f"""
        await open(withFilters({{ maxPriceCents: {cents} }}));
        return $("buy-max-price").value;
        """)
        assert seen == shown

    def test_a_typed_price_is_sent_as_typed_with_the_other_filters_and_resets_what_was_shown(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_filters = async () => withFilters({ maxPriceCents: 250 });
        $("buy-max-price").value = " 2,50 ";
        await fire($("buy-max-price"), "change");
        return { call: calls.find((c) => c[0] === "buy_filters"), shown: $("buy-max-price").value,
          results: $("buy-results").hidden };
        """)
        assert seen["call"] == ["buy_filters", "english", "NM", "any", [], "any", "2,50"]
        assert seen["shown"] == "2.50"
        assert seen["results"] is True

    def test_enter_sends_the_price_and_the_change_that_follows_does_not_send_it_again(self) -> None:
        seen = run("""
        await open();
        stub.buy_filters = async () => withFilters({ maxPriceCents: 500 });
        $("buy-max-price").value = "5";
        const enter = await fire($("buy-max-price"), "keydown", { key: "Enter" });
        await fire($("buy-max-price"), "change");
        return { sent: calls.filter((c) => c[0] === "buy_filters").map((c) => c.at(-1)),
          shown: $("buy-max-price").value };
        """)
        assert seen == {"sent": ["5"], "shown": "5"}

    def test_a_price_left_as_it_was_sends_nothing(self) -> None:
        seen = run("""
        await open(withFilters({ maxPriceCents: 250 }));
        $("buy-max-price").value = "2.50";
        await fire($("buy-max-price"), "change");
        await fire($("buy-max-price"), "keydown", { key: "Enter" });
        return calls.filter((c) => c[0] === "buy_filters").length;
        """)
        assert seen == 0

    def test_clearing_the_field_asks_for_any_price(self) -> None:
        seen = run("""
        await open(withFilters({ maxPriceCents: 250 }));
        stub.buy_filters = async () => view();
        $("buy-max-price").value = "";
        await fire($("buy-max-price"), "change");
        return [calls.find((c) => c[0] === "buy_filters").at(-1), $("buy-max-price").value];
        """)
        assert seen == ["", ""]

    def test_escape_puts_the_saved_price_back_without_sending(self) -> None:
        seen = run("""
        await open(withFilters({ maxPriceCents: 250 }));
        $("buy-max-price").value = "9";
        await fire($("buy-max-price"), "keydown", { key: "Escape" });
        return [$("buy-max-price").value, calls.filter((c) => c[0] === "buy_filters").length];
        """)
        assert seen == ["2.50", 0]

    def test_a_refused_price_goes_back_to_the_saved_one_and_says_why(self) -> None:
        seen = run("""
        await open(withFilters({ maxPriceCents: 250 }));
        stub.buy_filters = async () => ({ error: "Max price: 0.01 to 10000 euros, at most two decimals." });
        $("buy-max-price").value = "free";
        await fire($("buy-max-price"), "change");
        return [$("buy-max-price").value, $("status-text").textContent];
        """)
        assert seen == ["2.50", "Max price: 0.01 to 10000 euros, at most two decimals."]

    def test_another_filter_sends_the_saved_price_with_it(self) -> None:
        seen = run("""
        await open(withFilters({ maxPriceCents: 250 }));
        stub.buy_filters = async () => withFilters({ maxPriceCents: 250, foil: "foil" });
        const foil = [...$("buy-foil").children].find((b) => b.textContent === "Foil");
        await fire(foil, "click");
        return calls.find((c) => c[0] === "buy_filters");
        """)
        assert seen == ["buy_filters", "english", "NM", "foil", [], "any", "2.50"]

    def test_the_field_is_locked_while_a_scan_runs(self) -> None:
        seen = run("""
        await open();
        stub.buy_scan = async () => idle({ phase: "wants", running: true, progress: "Starting" });
        await fire($("buy-scan"), "click");
        return $("buy-max-price").disabled;
        """)
        assert seen is True

    def test_a_new_cap_that_is_solved_again_shows_the_work_and_then_the_packages(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_filters = async () => withFilters({ maxPriceCents: 400 }, { status: idle({
          phase: "solving", running: true, progress: "Finding the cheapest packages" }) });
        $("buy-max-price").value = "4";
        await fire($("buy-max-price"), "change");
        const during = { line: $("buy-progress").textContent, results: $("buy-results").hidden,
          timers: timers.length, input: $("buy-max-price").disabled };
        stub.buy_status = async () => done();
        await pollBuy();
        return { during, after: { line: $("buy-progress").textContent, rows: $("buy-curve").children.length,
          input: $("buy-max-price").disabled }, scans: calls.filter((c) => c[0] === "buy_scan").length };
        """)
        assert seen["during"] == {
            "line": "Finding the cheapest packages",
            "results": True,
            "timers": 1,
            "input": True,
        }
        assert seen["after"] == {"line": "Found 2 packages.", "rows": 2, "input": False}
        assert seen["scans"] == 0

    def test_cards_over_the_cap_are_listed_apart_with_their_cheapest_price(self) -> None:
        seen = run("""
        const results = { ...RESULTS, noOffers: ["Sol Ring"],
          overPrice: [{ card: "Arcane Signet", cheapestCents: 650 },
            { card: "Mind Stone", cheapestCents: 1200 }] };
        await open(view({ status: done(results) }));
        return [...$("buy-notes").children].map((li) => [li.children[0].textContent, li.textContent]);
        """)
        assert seen == [
            ["No offers for these filters: ", "No offers for these filters: Sol Ring"],
            [
                "Over your max price: ",
                "Over your max price: Arcane Signet (cheapest €6.50), Mind Stone (cheapest €12.00)",
            ],
        ]

    def test_without_cards_over_the_cap_there_is_no_such_line(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        return $("buy-notes").children.length;
        """)
        assert seen == 0


class TestTheFilterRow:
    """The rows' labels share one line and so do their controls; the page tests cannot measure a layout, so
    these check the rules that make it so."""

    @staticmethod
    def rule(css: str, selector: str) -> str:
        found = re.search(rf"(?:^|\n){re.escape(selector)}\s*\{{([^}}]*)\}}", css)
        assert found is not None, selector
        return found.group(1)

    def test_the_row_starts_every_item_at_the_top_so_the_labels_share_one_line(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")

        assert "align-items: flex-start" in self.rule(css, ".buy-filters")
        assert "align-items: flex-end" not in self.rule(css, ".buy-filters")

    def test_the_pills_the_dropdowns_the_field_and_the_location_button_are_one_height(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")

        assert re.search(r"--control-h:\s*\d+px", css)
        assert "min-height: var(--control-h)" in self.rule(css, ".buy-filter .segmented")
        assert "min-height: var(--control-h)" in self.rule(css, ".buy-select, .buy-input")
        assert "box-sizing: border-box" in self.rule(css, ".buy-filter .segmented")
        assert "box-sizing: border-box" in self.rule(css, ".buy-select, .buy-input")
        picker = re.search(r'id="buy-countries" class="([^"]*)"', (WEB / "index.html").read_text("utf-8"))
        assert picker is not None
        assert "buy-select" in picker.group(1).split()

    def test_the_pill_labels_keep_their_inset_and_the_select_keeps_its_chevron(self) -> None:
        css = (WEB / "app.css").read_text(encoding="utf-8")

        label = self.rule(css, ".buy-filter > span:first-child")
        assert "padding-left: calc(var(--segmented-inset) + var(--space-2))" in label
        assert "padding: var(--segmented-inset)" in self.rule(css, ".segmented")
        select = self.rule(css, ".buy-select")
        assert "linear-gradient(45deg" in select
        assert "calc(100% - var(--space-3)" in select
        assert "appearance: none" in select

    def test_every_filter_in_the_row_has_a_label_over_its_control(self) -> None:
        page = (WEB / "index.html").read_text(encoding="utf-8")
        row = page.split('<div class="buy-filters">', 1)[1].split("</section>", 1)[0]

        filters = row.split('<div class="buy-filter">')[1:]
        assert len(filters) == 6
        for one in filters:
            first = one.strip().split("\n", 1)[0].strip()
            assert first.startswith(("<span id=", "<label for=")), first


def test_the_screens_styles_use_the_design_tokens_and_no_colours_of_their_own() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    buy = css.split("/* Buy cards", 1)[1]

    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|hsl\(", buy) == []
    assert "var(--primary)" in buy
    assert "var(--surface)" in buy


def test_the_screens_sizes_are_tokens_apart_from_hairline_borders() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    buy = css.split("/* Buy cards", 1)[1]
    without_tokens = re.sub(r"--[\w-]+:[^;]+;", "", buy)

    sizes = re.findall(r"(?<![\w.-])(?:\d+\.\d+|\d+)(?:px|rem|em|vh|vw|ch)\b", without_tokens)
    assert [size for size in sizes if size != "1px"] == []
    for token in ("--content-w", "--control-h", "--list-row-h", "--check-size", "--menu-w", "--menu-h"):
        assert re.search(rf"\n  {token}:\s*[\d.]+(?:px|rem|vh)", css), token


class TestAccount:
    def test_the_sidebar_button_shows_with_the_decks_beside_the_copy_button(self) -> None:
        seen = run("""
        stub.decks = async () => ({ decks: [{ id: 1, short: "A", name: "A", queued: 0, open: 0 }] });
        await loadDecks();
        const shown = [!$("buy-open").hidden, !$("copy-to-buy").hidden];
        stub.decks = async () => ({ decks: [] });
        await loadDecks();
        return { shown, text: $("buy-open").textContent, after: [$("buy-open").hidden,
          $("copy-to-buy").hidden],
          sameRow: $("buy-open").parentNode === $("copy-to-buy").parentNode };
        """)
        assert seen["shown"] == [True, True]
        assert seen["text"] == "Buy cards"
        assert seen["after"] == [True, True]
        assert seen["sameRow"] is True

    def test_signing_out_forgets_what_was_built_for_the_account(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        showSignedOut();
        return { hidden: $("buy").hidden, decks: $("buy-decks").children.length,
          rows: $("buy-curve").children.length,
          results: $("buy-results").hidden, open: buyState.open, view: buyState.view };
        """)
        assert seen == {"hidden": True, "decks": 0, "rows": 0, "results": True, "open": False, "view": None}

    def test_an_answer_for_a_page_that_moved_on_is_dropped(self) -> None:
        seen = run("""
        let release;
        stub.buy = () => new Promise((resolve) => { release = () => resolve(view()); });
        const opening = fire($("buy-open"), "click");
        showSignedOut();
        release();
        await opening;
        return { decks: $("buy-decks").children.length, view: buyState.view };
        """)
        assert seen == {"decks": 0, "view": None}

    @pytest.mark.parametrize(
        ("name", "call", "answer"),
        [
            ("buy_status", "pollBuy()", 'idle({ phase: "scanning", running: true })'),
            ("buy_include", "setBuyDeck(1, false)", "view()"),
            ("buy_include_card", 'setBuyCard(2, "Mind Stone", false)', "view()"),
            ("buy_filters", 'changeBuyFilters({ foil: "foil" })', "view()"),
            ("buy_base_sellers", "changeBuyBaseSellers()", "view()"),
            ("buy_scan", "startBuyScan()", 'idle({ phase: "wants", running: true })'),
            ("buy_cancel", "cancelBuyScan()", 'idle({ phase: "scanning", running: true })'),
            ("buy_package", "pickBuyRow(1)", "PACKAGE"),
            (
                "buy_add_to_cart",
                "(openBuyCart(), confirmBuyCart())",
                'idle({ phase: "cart", running: true })',
            ),
        ],
    )
    def test_an_answer_that_arrives_after_signing_out_is_dropped(
        self, name: str, call: str, answer: str
    ) -> None:
        seen = run_cart(f"""
        await open(view({{ status: done() }}));
        await pickPackage();
        const errors = [];
        stub.log_page_error = async (message) => {{ errors.push(message); }};
        let release;
        stub.{name} = () => new Promise((resolve) => {{ release = () => resolve({answer}); }});
        const held = {call};
        showSignedOut();
        release();
        await held;
        await settle();
        return {{ view: buyState.view, decks: $("buy-decks").children.length,
          rows: $("buy-curve").children.length, timers: timers.length, adding: buyState.adding,
          requested: buyState.requested, picked: buyState.picked, errors,
          toast: $("status-text").textContent }};
        """)
        assert seen == {
            "view": None,
            "decks": 0,
            "rows": 0,
            "timers": 0,
            "adding": False,
            "requested": False,
            "picked": None,
            "errors": [],
            "toast": "",
        }

    def test_a_refresh_of_the_screen_that_fails_does_not_cut_the_rest_of_the_poll_short(self) -> None:
        seen = run("""
        await open();
        const errors = [];
        stub.log_page_error = async (message) => { errors.push(message); };
        stub.decks = async () => ({ decks: [{ id: 1, short: "A", name: "A", queued: 0 }] });
        stub.buy = async () => { throw new Error("the bridge fell over"); };
        state.stamps = { "1": "old" };
        state.metaStamp = "m";
        state.previewsStamp = "p1";
        stub.stamps = async () => ({ stamps: { "1": "new" }, meta: "m", previews: "p2" });
        await poll();
        return { previews: state.previewsStamp, stamps: state.stamps, errors, timers: timers.length };
        """)
        assert seen == {
            "previews": "p2",
            "stamps": {"1": "new"},
            "errors": ["the bridge fell over"],
            "timers": 1,
        }

    def test_a_change_the_poll_finds_in_the_decks_reaches_the_open_screen(self) -> None:
        seen = run("""
        await open();
        stub.buy = async () => view({ decks: [PANTLAZA], copies: 3 });
        await refreshBuy();
        return $("buy-summary").textContent;
        """)
        assert seen == "3 cards from 1 deck"


class TestSellerLocation:
    def test_the_button_says_any_country_until_some_are_chosen(self) -> None:
        seen = run("""
        await open();
        const trigger = $("buy-countries");
        return { text: $("buy-countries-text").textContent, tag: trigger.tagName,
          type: trigger.getAttribute("type"), expanded: trigger.getAttribute("aria-expanded"),
          menu: $("buy-countries-menu").hidden, calls: calls.map((c) => c[0]), tip: trigger.title };
        """)
        assert seen == {
            "text": "Any country",
            "tag": "BUTTON",
            "type": "button",
            "expanded": "false",
            "menu": True,
            "calls": ["buy"],
            "tip": "",
        }

    @pytest.mark.parametrize(
        ("ids", "text"),
        [
            ([7], "Germany"),
            ([23, 7], "Germany, Netherlands"),
            ([28, 7, 1], "3 countries"),
            ([1, 7, 23, 28], "4 countries"),
        ],
    )
    def test_the_button_names_one_or_two_countries_and_counts_more(self, ids: list[int], text: str) -> None:
        seen = run(f"""
        await open(withFilters({{ countries: {ids} }}));
        return [$("buy-countries-text").textContent, $("buy-countries").title];
        """)
        assert seen[0] == text
        assert seen[1] == ", ".join(sorted(n for i, n in NAMES.items() if i in ids))

    def test_a_chosen_country_the_list_does_not_have_yet_is_counted(self) -> None:
        seen = run("""
        stub.buy_countries = async () => COUNTRY_ERROR;
        await open(withFilters({ countries: [7, 23] }));
        return [$("buy-countries-text").textContent, $("buy-countries").title, $("status-text").textContent];
        """)
        assert seen == ["2 countries", "", "Cardmarket's countries could not be loaded; try again."]

    def test_names_are_fetched_on_opening_only_when_countries_are_chosen(self) -> None:
        seen = run("""
        await open();
        const without = calls.map((c) => c[0]);
        closeBuy();
        await open(withFilters({ countries: [7] }));
        return [without, calls.map((c) => c[0])];
        """)
        assert seen == [["buy"], ["buy", "buy", "buy_countries"]]

    def test_pressing_the_button_fetches_once_and_shows_a_checkbox_per_country_after_a_clear_button(
        self,
    ) -> None:
        seen = run("""
        await open();
        const trigger = $("buy-countries");
        await fire(trigger, "click");
        const shown = { menu: $("buy-countries-menu").hidden, expanded: trigger.getAttribute("aria-expanded"),
          items: [...$("buy-countries-menu").children].map((n) => [n.tagName, n.textContent]),
          boxes: walk($("buy-countries-menu")).filter((n) => n.tagName === "INPUT")
            .map((n) => [n.getAttribute("type"), n.id, n.checked]),
          focus: document.activeElement.id };
        await fire(trigger, "click");
        await fire(trigger, "click");
        return { shown, fetched: calls.filter((c) => c[0] === "buy_countries").length };
        """)
        assert seen["shown"] == {
            "menu": False,
            "expanded": "true",
            "items": [
                ["BUTTON", "Clear countries"],
                ["LABEL", "Austria"],
                ["LABEL", "Germany"],
                ["LABEL", "Netherlands"],
                ["LABEL", "Sweden"],
            ],
            "boxes": [
                ["checkbox", "buy-country-1", False],
                ["checkbox", "buy-country-7", False],
                ["checkbox", "buy-country-23", False],
                ["checkbox", "buy-country-28", False],
            ],
            "focus": "buy-country-1",
        }
        assert seen["fetched"] == 1

    def test_the_button_and_the_list_have_names_and_the_button_says_what_it_opens(self) -> None:
        seen = run("""
        await open();
        const trigger = $("buy-countries");
        return { labelledby: trigger.getAttribute("aria-labelledby"),
          controls: trigger.getAttribute("aria-controls"),
          menu: [$("buy-countries-menu").id, $("buy-countries-menu").getAttribute("role"),
            $("buy-countries-menu").getAttribute("aria-label")],
          label: $("buy-countries-label").textContent, popup: trigger.getAttribute("aria-haspopup") };
        """)
        assert seen == {
            "labelledby": "buy-countries-label buy-countries-text",
            "controls": "buy-countries-menu",
            "menu": ["buy-countries-menu", "group", "Seller countries"],
            "label": "Seller location",
            "popup": None,
        }

    def test_the_list_opens_on_the_first_ticked_country(self) -> None:
        seen = run("""
        await open(withFilters({ countries: [23] }));
        await fire($("buy-countries"), "click");
        return document.activeElement.id;
        """)
        assert seen == "buy-country-23"

    def test_ticking_a_country_sends_the_ticked_ones_and_draws_the_answer(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        await fire($("buy-countries"), "click");
        stub.buy_filters = async (language, condition, foil, countries, delivery) =>
          withFilters({ countries, delivery });
        const box = (id) => $(`buy-country-${id}`);
        box(23).checked = true;
        await fire(box(23), "change");
        box(7).checked = true;
        await fire(box(7), "change");
        return { sent: calls.filter((c) => c[0] === "buy_filters").map((c) => c.slice(1)),
          checked: [1, 7, 23, 28].map((id) => box(id).checked), text: $("buy-countries-text").textContent,
          open: !$("buy-countries-menu").hidden, results: $("buy-results").hidden,
          focus: document.activeElement.id };
        """)
        assert seen["sent"] == [
            ["english", "NM", "any", [23], "any", ""],
            ["english", "NM", "any", [7, 23], "any", ""],
        ]
        assert seen["checked"] == [False, True, True, False]
        assert seen["text"] == "Germany, Netherlands"
        assert seen["open"] is True
        assert seen["results"] is True
        assert seen["focus"] == "buy-country-1"

    def test_unticking_the_last_country_goes_back_to_any_country(self) -> None:
        seen = run("""
        await open(withFilters({ countries: [7] }));
        await fire($("buy-countries"), "click");
        stub.buy_filters = async (language, condition, foil, countries) => withFilters({ countries });
        const box = $("buy-country-7");
        box.checked = false;
        await fire(box, "change");
        return [calls.at(-1).slice(1), $("buy-countries-text").textContent];
        """)
        assert seen == [["english", "NM", "any", [], "any", ""], "Any country"]

    def test_clear_countries_unticks_all_and_is_off_when_none_are_ticked(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        const before = $("buy-countries-clear").disabled;
        stub.buy = async () => withFilters({ countries: [1, 28] });
        await refreshBuy();
        const during = $("buy-countries-clear").disabled;
        stub.buy_filters = async (language, condition, foil, countries) => withFilters({ countries });
        await fire($("buy-countries-clear"), "click");
        return { before, during, sent: calls.at(-1).slice(1), text: $("buy-countries-text").textContent,
          after: $("buy-countries-clear").disabled };
        """)
        assert seen == {
            "before": True,
            "during": False,
            "sent": ["english", "NM", "any", [], "any", ""],
            "text": "Any country",
            "after": True,
        }

    def test_a_refused_tick_puts_the_box_back_and_says_why(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        stub.buy_filters = async () => ({ error: "Wait for the scan, or cancel it." });
        const box = $("buy-country-7");
        box.checked = true;
        await fire(box, "change");
        return [box.checked, $("status-text").textContent, $("buy-countries-text").textContent];
        """)
        assert seen == [False, "Wait for the scan, or cancel it.", "Any country"]

    def test_a_list_that_cannot_be_fetched_stays_shut_and_the_next_press_tries_again(self) -> None:
        seen = run("""
        await open();
        stub.buy_countries = async () => COUNTRY_ERROR;
        await fire($("buy-countries"), "click");
        const first = { menu: $("buy-countries-menu").hidden, toast: $("status-text").textContent,
          expanded: $("buy-countries").getAttribute("aria-expanded") };
        stub.buy_countries = async () => ({ countries: COUNTRIES });
        await fire($("buy-countries"), "click");
        return { first, menu: $("buy-countries-menu").hidden,
          fetched: calls.filter((c) => c[0] === "buy_countries").length };
        """)
        assert seen["first"] == {
            "menu": True,
            "toast": "Cardmarket's countries could not be loaded; try again.",
            "expanded": "false",
        }
        assert seen["menu"] is False
        assert seen["fetched"] == 2

    def test_a_second_press_while_the_list_loads_asks_once(self) -> None:
        seen = run("""
        await open();
        let release;
        stub.buy_countries = () => new Promise((resolve) => {
          release = () => resolve({ countries: COUNTRIES });
        });
        const first = fire($("buy-countries"), "click");
        const second = fire($("buy-countries"), "click");
        release();
        await Promise.all([first, second]);
        return { fetched: calls.filter((c) => c[0] === "buy_countries").length,
          boxes: walk($("buy-countries-menu")).filter((n) => n.tagName === "INPUT").length };
        """)
        assert seen == {"fetched": 1, "boxes": 4}

    def test_a_scan_running_shuts_the_list_and_locks_the_choice(self) -> None:
        seen = run("""
        await open(withFilters({ countries: [7] }));
        await fire($("buy-countries"), "click");
        const listed = !$("buy-countries-menu").hidden;
        paintBuyStatus(idle({ phase: "scanning", running: true, progress: "Reading Sol Ring · 1 of 2" }));
        return { listed, menu: $("buy-countries-menu").hidden, trigger: $("buy-countries").disabled,
          expanded: $("buy-countries").getAttribute("aria-expanded"),
          boxes: walk($("buy-countries-menu")).filter((n) => n.tagName === "INPUT").map((n) => n.disabled) };
        """)
        assert seen == {
            "listed": True,
            "menu": True,
            "trigger": True,
            "expanded": "false",
            "boxes": [True, True, True, True],
        }

    def test_the_list_built_once_is_kept_when_coming_back_to_the_screen(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        const box = $("buy-country-7");
        closeBuy();
        const closed = $("buy-countries-menu").hidden;
        await open(withFilters({ countries: [7] }));
        return { closed, same: $("buy-country-7") === box, checked: $("buy-country-7").checked,
          fetched: calls.filter((c) => c[0] === "buy_countries").length };
        """)
        assert seen == {"closed": True, "same": True, "checked": True, "fetched": 1}

    def test_signing_out_forgets_nothing_it_should_not_and_closes_the_list(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        showSignedOut();
        return { menu: $("buy-countries-menu").hidden,
          expanded: $("buy-countries").getAttribute("aria-expanded") };
        """)
        assert seen == {"menu": True, "expanded": "false"}


class TestSellerLocationKeyboard:
    def test_arrows_home_and_end_move_between_the_ticks_and_wrap(self) -> None:
        seen = run("""
        await open(withFilters({ countries: [7] }));
        await fire($("buy-countries"), "click");
        const at = () => document.activeElement.id;
        const trail = [at()];
        for (const key of ["ArrowDown", "ArrowDown", "ArrowDown", "ArrowDown", "ArrowUp", "End", "Home"]) {
          const event = await fire(document.activeElement, "keydown", { key });
          trail.push(at(), event.defaultPrevented);
        }
        return trail;
        """)
        assert seen == [
            "buy-country-7",
            "buy-country-23", True,
            "buy-country-28", True,
            "buy-countries-clear", True,
            "buy-country-1", True,
            "buy-countries-clear", True,
            "buy-country-28", True,
            "buy-countries-clear", True,
        ]  # fmt: skip

    def test_a_clear_button_that_is_off_is_skipped(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        await fire(document.activeElement, "keydown", { key: "Home" });
        const first = document.activeElement.id;
        await fire(document.activeElement, "keydown", { key: "ArrowUp" });
        return [first, document.activeElement.id];
        """)
        assert seen == ["buy-country-1", "buy-country-28"]

    def test_escape_shuts_the_list_returns_to_the_button_and_goes_no_further(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        const event = await fire(document.activeElement, "keydown", { key: "Escape" });
        return { menu: $("buy-countries-menu").hidden, focus: document.activeElement.id,
          expanded: $("buy-countries").getAttribute("aria-expanded"), prevented: event.defaultPrevented,
          stopped: event.stopped };
        """)
        assert seen == {
            "menu": True,
            "focus": "buy-countries",
            "expanded": "false",
            "prevented": True,
            "stopped": True,
        }

    def test_escape_does_nothing_while_the_list_is_shut(self) -> None:
        seen = run("""
        await open();
        const event = await fire($("buy-countries"), "keydown", { key: "Escape" });
        return [event.defaultPrevented, event.stopped];
        """)
        assert seen == [False, False]

    def test_tab_leaves_the_list_and_shuts_it_without_taking_the_key(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        const event = await fire(document.activeElement, "keydown", { key: "Tab" });
        return [$("buy-countries-menu").hidden, event.defaultPrevented,
          $("buy-countries").getAttribute("aria-expanded")];
        """)
        assert seen == [True, False, "false"]

    def test_pressing_elsewhere_shuts_the_list_and_pressing_inside_does_not(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        await fire($("buy-country-7"), "mousedown");
        await fire($("buy-countries-text"), "mousedown");
        const inside = !$("buy-countries-menu").hidden;
        await fire($("buy-title"), "mousedown");
        return { inside, after: $("buy-countries-menu").hidden,
          expanded: $("buy-countries").getAttribute("aria-expanded") };
        """)
        assert seen == {"inside": True, "after": True, "expanded": "false"}

    def test_the_checks_are_native_checkboxes_inside_labels_for_the_screen_reader(self) -> None:
        seen = run("""
        await open();
        await fire($("buy-countries"), "click");
        const label = $("buy-countries-menu").children[2];
        const box = walk(label).find((n) => n.tagName === "INPUT");
        return { labelFor: label.getAttribute("for"), box: box.id, text: label.textContent };
        """)
        assert seen == {"labelFor": "buy-country-7", "box": "buy-country-7", "text": "Germany"}


class TestDeliveryTime:
    def test_five_choices_with_any_first_and_the_chosen_one_pressed(self) -> None:
        seen = run("""
        await open(withFilters({ delivery: "10" }));
        return { choices: [...$("buy-delivery").children].map((b) => [b.dataset.value, b.textContent,
          b.getAttribute("aria-pressed"), b.tagName]),
          group: [$("buy-delivery").getAttribute("role"), $("buy-delivery").getAttribute("aria-labelledby"),
            $("buy-delivery-label").textContent] };
        """)
        assert seen["choices"] == [
            ["any", "Any", "false", "BUTTON"],
            ["5", "5 days", "false", "BUTTON"],
            ["7", "1 week", "false", "BUTTON"],
            ["10", "10 days", "true", "BUTTON"],
            ["14", "2 weeks", "false", "BUTTON"],
        ]
        assert seen["group"] == ["group", "buy-delivery-label", "Delivery time"]

    def test_a_choice_is_sent_with_the_other_filters_and_resets_what_was_shown(self) -> None:
        seen = run("""
        await open(withFilters({ countries: [7], foil: "foil" }, { status: done() }));
        stub.buy_filters = async (language, condition, foil, countries, delivery) =>
          withFilters({ foil, countries, delivery });
        const week = [...$("buy-delivery").children].find((b) => b.textContent === "1 week");
        await fire(week, "click");
        const pressed = [...$("buy-delivery").children]
          .filter((b) => b.getAttribute("aria-pressed") === "true").map((b) => b.textContent);
        return { call: calls.find((c) => c[0] === "buy_filters"), pressed, results: $("buy-results").hidden,
          curve: $("buy-curve").children.length };
        """)
        assert seen == {
            "call": ["buy_filters", "english", "NM", "foil", [7], "7", ""],
            "pressed": ["1 week"],
            "results": True,
            "curve": 0,
        }

    def test_a_refused_choice_goes_back_to_the_saved_one(self) -> None:
        seen = run("""
        await open();
        stub.buy_filters = async () => ({ error: "Wait for the scan, or cancel it." });
        const two = [...$("buy-delivery").children].find((b) => b.textContent === "2 weeks");
        await fire(two, "click");
        return [...$("buy-delivery").children].filter((b) => b.getAttribute("aria-pressed") === "true")
          .map((b) => b.textContent);
        """)
        assert seen == ["Any"]


class TestArrival:
    def test_each_seller_shows_its_days_and_where_an_estimate_comes_from(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        const unknown = { ...PACKAGE.shipments[0], seller: "SellerC", deliveryDays: null,
          deliveryBasis: null };
        const route = { ...PACKAGE.shipments[0], seller: "SellerD", country: "Italy", deliveryDays: 14,
          deliveryBasis: "route" };
        stub.buy_package = async () => ({ ...PACKAGE, shipments: [...PACKAGE.shipments, unknown, route] });
        await fire($("buy-curve").children[1], "click");
        return [...$("buy-sellers").children].map((s) => s.children[1].textContent);
        """)
        assert seen == [
            "France · Lettre Internationale €2.55, not tracked · 6 days",
            "Germany · Tracked Parcel €3.45, tracked · about 12 days (Germany median)",
            "France · Lettre Internationale €2.55, not tracked · no delivery estimate",
            "Italy · Lettre Internationale €2.55, not tracked · about 14 days (Cardmarket's average)",
        ]

    def test_the_package_says_when_its_slowest_seller_arrives(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        return $("buy-package-arrival").textContent;
        """)
        assert seen == "Arrives in about 12 days"

    def test_a_package_whose_sellers_have_no_estimate_says_nothing_of_arrival(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => ({ ...PACKAGE, slowestDays: null, withoutEstimate: 2 });
        await fire($("buy-curve").children[1], "click");
        return $("buy-package-arrival").textContent;
        """)
        assert seen == ""

    def test_sellers_without_an_estimate_are_counted_beside_the_arrival(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => ({ ...PACKAGE, slowestDays: 1, withoutEstimate: 1 });
        await fire($("buy-curve").children[1], "click");
        return $("buy-package-arrival").textContent;
        """)
        assert seen == "Arrives in about 1 day · 1 seller without an estimate"

    def test_offers_left_out_for_want_of_an_estimate_are_counted_in_the_notes(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => ({ ...PACKAGE, leftOutNoEstimate: 3 });
        await fire($("buy-curve").children[1], "click");
        return [...$("buy-package-notes").children].map((n) => n.textContent);
        """)
        assert seen == [
            "Left out, no delivery estimate: 3 offers",
            "No usable offer: Lotus Petal",
            "Fewer copies offered than wanted: Mind Stone (3 wanted, 1 offered)",
        ]

    def test_closing_the_package_clears_the_arrival(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_package = async () => PACKAGE;
        await fire($("buy-curve").children[1], "click");
        paintBuyStatus(done({ ...RESULTS, rows: [row(1, 700, 2, 200, null, true)] }));
        return [$("buy-package").hidden, $("buy-package-arrival").textContent];
        """)
        assert seen == [True, ""]


CART_SETUP = """
const GONE = { seller: "SellerB", card: "Arcane Signet", copies: 2, outcome: "gone", inCart: 0,
  message: "An error has occurred. The item is not available." };
const SHORT = { seller: "SellerA", card: "Mind Stone", copies: 3, outcome: "short", inCart: 1, message: "" };
const ALREADY = { seller: "SellerA", card: "Sol Ring", copies: 1, outcome: "already", inCart: 1,
  message: "" };
const SHIPPING = [
  { seller: "SellerB", estimateMethod: "Letter", estimateCents: 200, cartMethod: "Brief S Priority",
    cartCents: 255, differs: true },
  { seller: "SellerA", estimateMethod: "Tracked Parcel", estimateCents: 345, cartMethod: "Tracked Parcel",
    cartCents: 345, differs: false }];
const CART = { status: "complete", reason: "", added: 1, already: 1, gone: 2, refused: 0, short: 3,
  missing: 0, noAnswer: 0, notSent: 0, items: [GONE, SHORT, ALREADY], shipping: SHIPPING };
const carted = (cart = CART) => done(RESULTS, { phase: "carted", cart });
const pickPackage = async () => {
  stub.buy_package = async () => PACKAGE;
  await fire($("buy-curve").children[1], "click");
};
"""


def run_cart(program: str) -> Answer:
    return run(CART_SETUP + program)


class TestTheCartButtonAndDialog:
    def test_the_package_has_a_primary_add_to_cart_button_that_waits_while_a_run_is_going(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        const before = $("buy-cart").disabled;
        await pickPackage();
        const shown = [$("buy-cart").textContent, $("buy-cart").className, $("buy-cart").disabled];
        paintBuyStatus(idle({ phase: "cart", running: true, progress: "Adding Mind Stone · 1 of 3",
          results: RESULTS }));
        const running = [$("buy-cart").disabled, $("buy-cart").getAttribute("aria-busy")];
        paintBuyStatus(carted());
        return { before, shown, running, after: $("buy-cart").disabled };
        """)
        assert seen == {
            "before": False,
            "shown": ["Add to cart", "button primary", False],
            "running": [True, "true"],
            "after": False,
        }

    def test_the_dialog_names_the_cards_the_sellers_their_prices_and_the_total(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        const dialog = $("buy-cart-dialog");
        return { open: dialog.open, title: $("buy-cart-dialog-title").textContent,
          confirm: $("buy-cart-confirm").textContent, keep: $("buy-cart-cancel").textContent,
          sellers: leaves($("buy-cart-dialog-sellers")), total: $("buy-cart-dialog-total").textContent,
          focus: document.activeElement.id, asked: calls.filter((c) => c[0] === "buy_add_to_cart") };
        """)
        assert seen == {
            "open": True,
            "title": "Add 3 cards to your Cardmarket cart?",
            "confirm": "Add 3 cards to cart",
            "keep": "Keep looking",
            "sellers": [
                "SellerB",
                "2 × Arcane Signet",
                "€0.30",
                "Shipping €2.55",
                "SellerA",
                "Mind Stone",
                "€0.60",
                "Shipping €3.45",
            ],
            "total": "Total €4.90 · cards €0.90 · shipping €4.00",
            "focus": "buy-cart-cancel",
            "asked": [],
        }

    def test_one_card_is_not_pluralised(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        stub.buy_package = async () => ({ ...PACKAGE, shipments: [PACKAGE.shipments[1]] });
        await fire($("buy-curve").children[1], "click");
        await fire($("buy-cart"), "click");
        return [$("buy-cart-dialog-title").textContent, $("buy-cart-confirm").textContent];
        """)
        assert seen == ["Add 1 card to your Cardmarket cart?", "Add 1 card to cart"]

    def test_confirming_sends_the_seller_count_and_shows_the_run_with_a_cancel_that_says_adding(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        stub.buy_add_to_cart = async () => idle({ phase: "cart", running: true, results: RESULTS,
          progress: "Adding Arcane Signet · 1 of 3" });
        await fire($("buy-cart-confirm"), "click");
        return { call: calls.find((c) => c[0] === "buy_add_to_cart"), open: $("buy-cart-dialog").open,
          line: $("buy-progress").textContent, cancel: [$("buy-cancel").hidden, $("buy-cancel").textContent],
          off: $("buy-cart").disabled, focus: document.activeElement.id, timers: timers.length };
        """)
        assert seen == {
            "call": ["buy_add_to_cart", 2, {"totalCents": 490, "picks": [[101, 2], [102, 1]]}],
            "open": False,
            "line": "Adding Arcane Signet · 1 of 3",
            "cancel": [False, "Cancel adding"],
            "off": True,
            "focus": "buy-cancel",
            "timers": 1,
        }

    def test_new_results_close_the_dialog_and_the_next_confirmation_is_of_the_new_package(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        stub.buy_package = async () => DEARER;
        paintBuyStatus(done(DEARER_RESULTS));
        await settle();
        const closed = $("buy-cart-dialog").open;
        await fire($("buy-cart"), "click");
        const total = $("buy-cart-dialog-total").textContent;
        stub.buy_add_to_cart = async () => idle({ phase: "cart", running: true, results: RESULTS });
        await fire($("buy-cart-confirm"), "click");
        return { closed, total, call: calls.find((c) => c[0] === "buy_add_to_cart") };
        """)
        assert seen == {
            "closed": False,
            "total": "Total €5.20 · cards €0.90 · shipping €4.00",
            "call": ["buy_add_to_cart", 2, {"totalCents": 520, "picks": [[103, 2], [102, 1]]}],
        }

    def test_an_answer_arriving_while_the_dialog_is_open_closes_it_and_sends_nothing(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        const shown = $("buy-cart-dialog-title").textContent;
        let release = () => {};
        stub.buy_package = (sellers) => new Promise((resolve) => { release = () => resolve({ ...PACKAGE,
          sellers, totalCents: 700, check: { totalCents: 700, picks: [[101, 2], [102, 1], [104, 1]] } }); });
        const pick = fire($("buy-curve").children[0], "click");
        release();
        await pick;
        const stillOpen = $("buy-cart-dialog").open;
        await fire($("buy-cart-confirm"), "click");
        return { shown, stillOpen, asked: calls.filter((c) => c[0] === "buy_add_to_cart") };
        """)
        assert seen == {"shown": "Add 3 cards to your Cardmarket cart?", "stillOpen": False, "asked": []}

    def test_add_to_cart_waits_while_a_package_is_being_asked_for(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        let release = () => {};
        stub.buy_package = (sellers) => new Promise((resolve) => { release = () => resolve({ ...PACKAGE,
          sellers }); });
        const pick = fire($("buy-curve").children[0], "click");
        const waiting = $("buy-cart").disabled;
        await fire($("buy-cart"), "click");
        const opened = $("buy-cart-dialog").open;
        release();
        await pick;
        return { waiting, opened, after: $("buy-cart").disabled };
        """)
        assert seen == {"waiting": True, "opened": False, "after": False}

    def test_the_confirmation_sends_the_package_the_dialog_showed(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        buyState.detail = { ...PACKAGE, sellers: 3, check: { totalCents: 700, picks: [[104, 1]] } };
        stub.buy_add_to_cart = async () => idle({ phase: "cart", running: true, results: RESULTS });
        await fire($("buy-cart-confirm"), "click");
        return calls.find((c) => c[0] === "buy_add_to_cart");
        """)
        assert seen == ["buy_add_to_cart", 2, {"totalCents": 490, "picks": [[101, 2], [102, 1]]}]

    def test_keep_looking_calls_nothing_and_closing_the_dialog_any_way_returns_to_the_button(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        const focus = [];
        await fire($("buy-cart"), "click");
        await fire($("buy-cart-cancel"), "click");
        focus.push(document.activeElement.id);
        await fire($("buy-cart"), "click");
        $("buy-cart-dialog").close();
        focus.push(document.activeElement.id);
        return { focus, open: $("buy-cart-dialog").open,
          asked: calls.filter((c) => c[0] === "buy_add_to_cart") };
        """)
        assert seen == {"focus": ["buy-cart", "buy-cart"], "open": False, "asked": []}

    def test_a_refusal_is_shown_and_the_button_comes_back(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        stub.buy_add_to_cart = async () => ({ error: "That package is gone; scan again." });
        await fire($("buy-cart-confirm"), "click");
        return { toast: $("status-text").textContent, off: $("buy-cart").disabled,
          cancel: $("buy-cancel").hidden };
        """)
        assert seen == {"toast": "That package is gone; scan again.", "off": False, "cancel": True}

    def test_cancelling_the_run_says_it_stops_after_the_card_even_while_the_window_is_the_owners(
        self,
    ) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        stub.buy_add_to_cart = async () => idle({ phase: "signin", running: true, results: RESULTS,
          progress: "Sign in to Cardmarket below" });
        await fire($("buy-cart-confirm"), "click");
        const label = $("buy-cancel").textContent;
        stub.buy_cancel = async () => idle({ phase: "signin", running: true, cancelling: true,
          results: RESULTS });
        await fire($("buy-cancel"), "click");
        return { label, line: $("buy-progress").textContent, pane: !$("buy-pane").hidden };
        """)
        assert seen == {"label": "Cancel adding", "line": "Stopping after this card…", "pane": True}


class TestTheCartReport:
    def test_no_report_shows_no_block(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        return $("buy-cart-report").hidden;
        """)
        assert seen is True

    def test_the_report_counts_lists_what_did_not_simply_go_in_and_the_shipping_that_differs(self) -> None:
        seen = run_cart("""
        await open(view({ status: carted() }));
        const items = [...$("buy-cart-items").children].map((li) => li.textContent);
        return { shown: !$("buy-cart-report").hidden, title: $("buy-cart-title").textContent,
          counts: $("buy-cart-counts").textContent, items,
          shipping: [...$("buy-cart-shipping").children].map((li) => li.textContent),
          reason: $("buy-cart-reason").hidden };
        """)
        assert seen == {
            "shown": True,
            "title": "Added 1 card to your cart",
            "counts": "1 already in your cart · 2 gone · 3 short of the copies asked",
            "items": [
                "2 × Arcane Signet (SellerB): An error has occurred. The item is not available.",
                "3 × Mind Stone (SellerA): 1 of 3 in the cart",
                "Sol Ring (SellerA): Already in your cart",
            ],
            "shipping": ["SellerB Shipping: estimate €2.00 Letter → cart €2.55 Brief S Priority"],
            "reason": True,
        }

    def test_the_link_opens_the_cart_page_on_cardmarket_in_the_default_browser(self) -> None:
        seen = run_cart("""
        await open(view({ status: carted() }));
        const link = $("buy-cart-link");
        return [link.tagName, link.getAttribute("href"), link.getAttribute("target"),
          link.getAttribute("rel"), link.textContent];
        """)
        assert seen == [
            "A",
            "https://www.cardmarket.com/en/Magic/ShoppingCart",
            "_blank",
            "noopener",
            "Open your cart on Cardmarket",
        ]

    @pytest.mark.parametrize(
        ("cart", "title"),
        [
            ("{ ...CART, status: 'cancelled', added: 2 }", "Cancelled: 2 cards added to your cart"),
            ("{ ...CART, status: 'stopped', added: 0, reason: 'x' }", "Stopped: 0 cards added to your cart"),
            ("{ ...CART, added: 0 }", "Nothing was added to your cart"),
            ("{ ...CART, added: 5 }", "Added 5 cards to your cart"),
        ],
    )
    def test_the_title_says_how_many_cards_went_in_and_whether_the_run_ended_early(
        self, cart: str, title: str
    ) -> None:
        seen = run_cart(f"""
        await open(view({{ status: carted({cart}) }}));
        return $("buy-cart-title").textContent;
        """)
        assert seen == title

    def test_a_stopped_run_gives_its_reason_as_an_alert_and_counts_what_was_not_added(self) -> None:
        seen = run_cart("""
        const stopped = { ...CART, status: "stopped", reason: "Cardmarket did not answer; adding stopped.",
          added: 1, already: 0, gone: 0, short: 0, noAnswer: 1, notSent: 2, shipping: [],
          items: [{ seller: "SellerA", card: "Mind Stone", copies: 1, outcome: "no-answer", inCart: 0,
            message: "" }, { seller: "SellerB", card: "Sol Ring", copies: 2, outcome: "not-sent",
            inCart: null, message: "" }] };
        await open(view({ status: carted(stopped) }));
        return { reason: [$("buy-cart-reason").hidden, $("buy-cart-reason").textContent,
            $("buy-cart-reason").getAttribute("role")],
          counts: $("buy-cart-counts").textContent,
          items: [...$("buy-cart-items").children].map((li) => li.textContent),
          shipping: $("buy-cart-shipping").children.length };
        """)
        assert seen == {
            "reason": [False, "Cardmarket did not answer; adding stopped.", "alert"],
            "counts": "1 without an answer · 2 not added",
            "items": [
                "Mind Stone (SellerA): No answer from Cardmarket; check your cart",
                "2 × Sol Ring (SellerB): Not added",
            ],
            "shipping": 0,
        }

    def test_an_offer_cardmarket_refused_or_did_not_keep_is_counted_and_told_in_words(self) -> None:
        seen = run_cart("""
        const odd = { ...CART, added: 0, already: 0, gone: 0, short: 0, refused: 2, missing: 1,
          items: [{ seller: "SellerA", card: "Mind Stone", copies: 2, outcome: "refused", inCart: 0,
            message: "Your cart is full." },
          { seller: "SellerA", card: "Sol Ring", copies: 1, outcome: "refused", inCart: 0, message: "" },
          { seller: "SellerB", card: "Arcane Signet", copies: 1, outcome: "missing", inCart: 0,
            message: "" }], shipping: [] };
        await open(view({ status: carted(odd) }));
        return { counts: $("buy-cart-counts").textContent,
          items: [...$("buy-cart-items").children].map((li) => li.textContent) };
        """)
        assert seen == {
            "counts": "2 refused by Cardmarket · 1 missing from your cart",
            "items": [
                "2 × Mind Stone (SellerA): Your cart is full.",
                "Sol Ring (SellerA): Cardmarket refused it.",
                (
                    "Arcane Signet (SellerB): Cardmarket accepted it but your cart does not show it;"
                    " check your cart"
                ),
            ],
        }

    def test_the_scan_results_and_the_package_shown_stay_while_the_cart_runs_and_after(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        const curve = $("buy-curve").children[1];
        paintBuyStatus(idle({ phase: "cart", running: true, results: RESULTS,
          progress: "Adding Mind Stone" }));
        const during = [$("buy-curve").children[1] === curve, $("buy-package").hidden];
        paintBuyStatus(carted());
        return { during, after: [$("buy-curve").children[1] === curve, $("buy-package").hidden,
          $("buy-package-title").textContent, $("buy-partial").hidden,
          $("buy-curve").children[1].getAttribute("aria-pressed")],
          line: $("buy-progress").textContent };
        """)
        assert seen == {
            "during": [True, False],
            "after": [True, False, "2 sellers · €4.90", True, "true"],
            "line": "Found 2 packages.",
        }

    def test_a_partial_scan_stays_marked_through_the_cart(self) -> None:
        seen = run_cart("""
        const partial = { ...RESULTS, partial: true };
        await open(view({ status: done(partial, { phase: "carted", cart: CART,
          message: "Scan cancelled after 14 pages; results are partial." }) }));
        return [$("buy-partial").hidden, $("buy-partial").title];
        """)
        assert seen == [False, "Scan cancelled after 14 pages; results are partial."]

    def test_a_status_without_a_report_takes_the_block_away_again(self) -> None:
        seen = run_cart("""
        await open(view({ status: carted() }));
        paintBuyStatus(done());
        return [$("buy-cart-report").hidden, $("buy-cart-items").children.length,
          $("buy-cart-shipping").children.length];
        """)
        assert seen == [True, 0, 0]

    def test_an_unchanged_report_is_not_drawn_again(self) -> None:
        seen = run_cart("""
        await open(view({ status: carted() }));
        const items = $("buy-cart-items").children[0];
        paintBuyStatus(carted({ ...CART }));
        return $("buy-cart-items").children[0] === items;
        """)
        assert seen is True

    def test_forgetting_the_account_closes_an_open_confirmation(self) -> None:
        seen = run_cart("""
        await open(view({ status: done() }));
        await pickPackage();
        await fire($("buy-cart"), "click");
        const before = $("buy-cart-dialog").open;
        buyForget();
        return [before, $("buy-cart-dialog").open, $("buy-cart-dialog-sellers").children.length];
        """)
        assert seen == [True, False, 2]

    def test_forgetting_the_account_clears_the_report_and_the_package(self) -> None:
        seen = run_cart("""
        await open(view({ status: carted() }));
        await pickPackage();
        buyForget();
        return [$("buy-cart-report").hidden, $("buy-cart-items").children.length, buyState.detail];
        """)
        assert seen == [True, 0, None]
