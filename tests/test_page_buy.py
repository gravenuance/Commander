"""The app page's Buy screen: its real scripts run under Node (`pagerun`) over made-up bridge answers."""

import re

from pagerun import WEB, Answer, needs_node, page

pytestmark = needs_node

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
const view = (extra = {}) => ({ decks: [ELLIVERE, PANTLAZA], copies: 4,
  filters: { language: "english", condition: "NM", foil: "any" }, baseSellers: 15, status: idle(),
    ...extra });
const row = (sellers, total, cards, shipping, saving, cheapest) => ({ sellers, totalCents: total,
  cardsCents: total - shipping,
  shippingCents: shipping, cards, savingCents: saving, cheapest, optimal: true });
const RESULTS = { rows: [row(1, 760, 2, 200, null, false), row(2, 490, 2, 400, 270, true)], pages: 5,
  destination: "Sweden", notFound: [], noOffers: [], unmatched: [], quantityMismatch: [] };
const done = (results = RESULTS, extra = {}) => idle({ phase: "results", results, ...extra });
const PACKAGE = { sellers: 2, totalCents: 490, cardsCents: 90, shippingCents: 400, optimal: true,
  shipments: [
    { seller: "SellerB", country: "France", cards: 1, valueCents: 30, method: "Lettre Internationale",
      shippingCents: 255, tracked: false, picks: [{ card: "Arcane Signet", copies: 2,
        expansion: "Commander Legends",
        condition: "NM", language: "English", foil: false, priceCents: 30, trendCents: 25 }] },
    { seller: "SellerA", country: "Germany", cards: 1, valueCents: 60, method: "Tracked Parcel",
      shippingCents: 345, tracked: true, picks: [{ card: "Mind Stone", copies: 1, expansion: "Kaladesh",
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

    def test_a_filter_choice_is_sent_with_the_other_two_and_resets_what_was_shown(self) -> None:
        seen = run("""
        await open(view({ status: done() }));
        stub.buy_filters = async () => view({ filters: { language: "english", condition: "NM",
          foil: "non-foil" } });
        const button = [...$("buy-foil").children].find((b) => b.textContent === "Non-foil");
        await fire(button, "click");
        const pressed = [...$("buy-foil").children].filter((b) => b.getAttribute("aria-pressed") === "true")
          .map((b) => b.textContent);
        return { call: calls.find((c) => c[0] === "buy_filters"), pressed, results: $("buy-results").hidden,
          curve: $("buy-curve").children.length };
        """)
        assert seen["call"] == ["buy_filters", "english", "NM", "non-foil"]
        assert seen["pressed"] == ["Non-foil"]
        assert seen["results"] is True
        assert seen["curve"] == 0

    def test_the_condition_and_language_choices_are_sent_too(self) -> None:
        seen = run("""
        await open();
        stub.buy_filters = async (language, condition, foil) =>
          view({ filters: { language, condition, foil } });
        $("buy-condition").value = "any";
        await fire($("buy-condition"), "change");
        const any = [...$("buy-language").children].find((b) => b.textContent === "Any");
        await fire(any, "click");
        return calls.filter((c) => c[0] === "buy_filters");
        """)
        assert seen == [
            ["buy_filters", "english", "any", "any"],
            ["buy_filters", "any", "any", "any"],
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
          base: $("buy-base-sellers").disabled,
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
                "SellerB", "France · Lettre Internationale €2.55, not tracked",
                "Card", "Set", "Condition", "Language", "Foil", "Price", "Trend",
                "2 × Arcane Signet", "Commander Legends", "NM", "English", "€0.30", "€0.25",
            ],
            [
                "SellerA", "Germany · Tracked Parcel €3.45, tracked",
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
