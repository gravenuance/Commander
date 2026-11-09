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
const deck = (id, short, cards, extra = {}) => ({
  id, short, name: `${short} deck`, known: true, included: true,
  copies: cards.reduce((n, c) => n + c.quantity, 0), cards, ...extra });
const PANTLAZA = deck(2, "Pantlaza", [{ name: "Arcane Signet", quantity: 2 }, { name: "Mind Stone",
  quantity: 1 }]);
const ELLIVERE = deck(1, "Ellivere", [{ name: "Lotus Petal", quantity: 1 }]);
const idle = (extra = {}) => ({ phase: "idle", running: false, cancelling: false, progress: "", message: "",
  detail: "", results: null, ...extra });
const FILTERS = { language: "english", condition: "NM", foil: "any", countries: [], delivery: "any" };
const view = (extra = {}) => ({ decks: [ELLIVERE, PANTLAZA], copies: 4,
  filters: FILTERS, baseSellers: 15, status: idle(), ...extra });
const withFilters = (change, extra = {}) => view({ filters: { ...FILTERS, ...change }, ...extra });
const COUNTRIES = [{ id: 1, name: "Austria" }, { id: 7, name: "Germany" }, { id: 23, name: "Netherlands" },
  { id: 28, name: "Sweden" }];
const COUNTRY_ERROR = { error: "Cardmarket's countries could not be loaded; try again." };
stub.buy_countries = async () => ({ countries: COUNTRIES });
const row = (sellers, total, cards, shipping, saving, cheapest) => ({ sellers, totalCents: total,
  cardsCents: total - shipping,
  shippingCents: shipping, cards, savingCents: saving, cheapest, optimal: true });
const RESULTS = { rows: [row(1, 760, 2, 200, null, false), row(2, 490, 2, 400, 270, true)], pages: 5,
  destination: "Sweden", notFound: [], noOffers: [], unmatched: [], quantityMismatch: [] };
const done = (results = RESULTS, extra = {}) => idle({ phase: "results", results, ...extra });
const PACKAGE = { sellers: 2, totalCents: 490, cardsCents: 90, shippingCents: 400, optimal: true,
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
            "text": "No Don't Have cards to buy.",
            "shown": True,
            "cards": True,
            "scanOff": True,
            "tip": "Tick a deck to scan",
            "summary": "",
        }

    def test_with_every_deck_unticked_the_decks_stay_to_be_ticked_again(self) -> None:
        seen = run("""
        const off = (d) => ({ ...d, included: false });
        await open(view({ decks: [off(ELLIVERE), off(PANTLAZA)], copies: 0 }));
        return { text: $("buy-empty-text").textContent, cards: $("buy-cards-block").hidden,
          scanOff: $("buy-scan").disabled };
        """)
        assert seen == {"text": "Tick a deck to scan.", "cards": False, "scanOff": True}


class TestChoices:
    def test_unticking_a_deck_sends_it_and_draws_the_answer(self) -> None:
        seen = run("""
        await open();
        stub.buy_include = async () => view({ decks: [ELLIVERE, { ...PANTLAZA, included: false }],
          copies: 1 });
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
        assert seen["call"] == ["buy_filters", "english", "NM", "non-foil", [], "any"]
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
            ["buy_filters", "english", "any", "any", [], "any"],
            ["buy_filters", "any", "any", "any", [], "any"],
        ]

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


class TestEndings:
    def test_a_scan_waiting_for_the_sign_in_says_where(self) -> None:
        seen = run("""
        await open(view({ status: idle({ phase: "signin", running: true,
          progress: "Sign in to Cardmarket in the window that opened" }) }));
        return [$("buy-progress").textContent, !$("buy-cancel").hidden];
        """)
        assert seen == ["Sign in to Cardmarket in the window that opened", True]

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
          results: RESULTS }) }));
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

    def test_a_row_that_is_only_an_estimate_says_so(self) -> None:
        seen = run("""
        const rough = { ...RESULTS, rows: [{ ...row(1, 760, 2, 200, null, true), optimal: false }] };
        await open(view({ status: done(rough) }));
        return leaves($("buy-curve").children[0]);
        """)
        assert seen[-2:] == ["Cheapest", "Estimate"]

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
            "language": ["BUTTON", "BUTTON"],
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


def test_the_screens_styles_use_the_design_tokens_and_no_colours_of_their_own() -> None:
    css = (WEB / "app.css").read_text(encoding="utf-8")
    buy = css.split("/* Buy cards", 1)[1]

    assert re.findall(r"#[0-9a-fA-F]{3,8}\b|rgb\(|rgba\(|hsl\(", buy) == []
    assert "var(--primary)" in buy
    assert "var(--surface)" in buy


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
            "popup": "true",
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
            ["english", "NM", "any", [23], "any"],
            ["english", "NM", "any", [7, 23], "any"],
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
        assert seen == [["english", "NM", "any", [], "any"], "Any country"]

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
            "sent": ["english", "NM", "any", [], "any"],
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
            "call": ["buy_filters", "english", "NM", "foil", [7], "7"],
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
