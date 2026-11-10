"""Putting a package in the Cardmarket cart: the answers' reader, the page scripts and the run, against a fake
cart page. The answers are the trimmed real ones in `fixtures/`; the cart pages are built to the markup the
real reader takes."""

import base64
import json
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from commander.cmcart import (
    ADD_ACTION,
    ANSWER_POLL_S,
    CART_URL,
    CartOutcome,
    CartProgress,
    CartRun,
    CartStatus,
    ItemResult,
    ShippingResult,
    add_script,
    add_to_cart,
    answer_script,
    not_sent,
    read_add_answer,
)
from commander.cmpages import PageError
from commander.cmscan import MAX_GAP_S, MIN_GAP_S
from commander.cmsession import (
    LOAD_TIMEOUT_S,
    BrowserError,
    Cancelled,
    FormBrowser,
    LoadTimeout,
    SessionBrowser,
)
from commander.cmshipping import ShippingQuote
from commander.cmwants import WANTS_URL
from commander.credentials import SessionCookie
from commander.sellersolve import Package, Pick, Shipment
from conftest import FIXTURES
from test_cmsession import FakeStore
from test_sellersolve import offer

if TYPE_CHECKING:
    from collections.abc import Callable

ADDED_XML = (FIXTURES / "cm_cart_add_ok.xml").read_text(encoding="utf-8")
GONE_XML = (FIXTURES / "cm_cart_add_gone.xml").read_text(encoding="utf-8")
GONE_MESSAGE = "An error has occurred. The item is not available."
NODE = shutil.which("node")
CART_RUNNER = Path(__file__).with_name("cartrun.js")
ALERT = '<div class="alert"><div class="alert-body"><p>1 => {}</p></div></div>'
LETTER = ("Brief S Priority", 175)
TOKEN = "0" * 64


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def answer_xml(result: str, message_html: str | None = None) -> str:
    message = "" if message_html is None else f"<systemMessage>{b64(message_html)}</systemMessage>"
    kind = f"<resultType>{b64(result)}</resultType>"
    return f"<?xml version='1.0'?><ajaxResponse>{message}{kind}</ajaxResponse>"


class TestTheAnswer:
    def test_a_success_answer_is_ok_with_its_heading(self) -> None:
        assert read_add_answer(ADDED_XML).ok is True
        assert read_add_answer(ADDED_XML).message == "Your request was executed successfully"

    def test_an_error_answer_is_not_ok_and_gives_the_reason_without_its_list_key(self) -> None:
        answer = read_add_answer(GONE_XML)

        assert answer.ok is False
        assert answer.message == GONE_MESSAGE

    def test_a_message_without_a_body_is_its_heading_and_without_a_key_is_left_whole(self) -> None:
        html = '<div class="alert"><h4 class="alert-heading">  Two   words\n here </h4></div>'

        assert read_add_answer(answer_xml("success", html)).message == "Two words here"

    def test_an_error_with_no_message_says_cardmarket_gave_no_reason(self) -> None:
        assert read_add_answer(answer_xml("error")).message == "Cardmarket gave no reason."

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "<html><body><h1>Not here</h1></body></html>",
            "<ajaxResponse><updatedRows>[]</updatedRows></ajaxResponse>",
            "<ajaxResponse><resultType>not base64!</resultType></ajaxResponse>",
            f"<ajaxResponse><resultType>{b64('maybe')}</resultType></ajaxResponse>",
            f"<ajaxResponse><resultType>{b64('error')}</resultType><systemMessage>#</systemMessage></ajaxResponse>",
            "<ajaxResponse><resultType>/w==</resultType></ajaxResponse>",
        ],
        ids=["empty", "a page", "no result type", "bad base64", "unknown type", "bad message", "not utf-8"],
    )
    def test_anything_else_is_a_page_error(self, text: str) -> None:
        with pytest.raises(PageError):
            read_add_answer(text)


class TestTheScripts:
    @pytest.mark.parametrize(("article", "copies"), [(0, 1), (-5, 1), (7, 0), (True, 1), ("7; alert(1)", 1)])
    def test_an_id_or_count_that_is_not_a_positive_whole_number_is_refused(
        self, article: object, copies: int
    ) -> None:
        with pytest.raises(ValueError, match="positive whole number"):
            add_script(article, copies)  # pyright: ignore[reportArgumentType]

    def test_the_script_only_posts_the_request_the_cart_button_sends(self) -> None:
        script = add_script(2168057329, 3)

        assert json.dumps(ADD_ACTION) in script
        assert "const id = 2168057329;" in script
        assert "String(3)" in script
        assert "X-Requested-With" in script
        assert not {"submit", "click", "cloneNode", "createElement"} & set(re.findall(r"\w+", script))

    @pytest.mark.skipif(NODE is None, reason="node is not on PATH")
    def test_the_script_posts_the_forms_fields_from_the_page_and_keeps_the_answer_once(self) -> None:
        ran = run_scripts(add_script(2168057329, 3), token=TOKEN, reply=ADDED_XML)

        assert ran["started"] == "started"
        assert ran["selectors"] == ['input[name="__cmtkn"]']
        assert ran["calls"] == [
            {
                "url": ADD_ACTION,
                "method": "POST",
                "credentials": "same-origin",
                "headers": {"X-Requested-With": "XMLHttpRequest"},
                "fields": [
                    ["__cmtkn", TOKEN],
                    ["idArticle[2168057329]", "2168057329"],
                    ["amount[2168057329]", "3"],
                ],
            }
        ]
        assert ran["early"] == "null"
        assert json.loads(ran["answer"]) == {"status": 200, "text": ADDED_XML}
        assert ran["again"] == "null"

    @pytest.mark.skipif(NODE is None, reason="node is not on PATH")
    def test_a_page_without_the_token_sends_nothing_and_says_what_is_missing(self) -> None:
        ran = run_scripts(add_script(5, 1), token=None, reply=ADDED_XML)

        assert ran["started"] == 'no element matches input[name="__cmtkn"]'
        assert ran["calls"] == []


def number_after(prefix: str, script: str) -> int:
    found = re.search(prefix + r"(\d+)", script)
    assert found is not None, script
    return int(found[1])


def run_scripts(start: str, *, token: str | None, reply: str) -> dict[str, Any]:
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 - node and a fixed script, no shell
        [NODE, str(CART_RUNNER)],
        input=json.dumps({"start": start, "answer": answer_script(), "token": token, "reply": reply}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout)


# --- the fake cart ------------------------------------------------------------------------------------------


def euro(cents: int) -> str:
    return f"{cents // 100},{cents % 100:02d} €"


@dataclass(slots=True)
class CartRow:
    article_id: int
    card: str
    copies: int


@dataclass(slots=True)
class CartSeller:
    name: str
    seller_id: int
    rows: list[CartRow] = field(default_factory=list[CartRow])
    method: str = LETTER[0]
    shipping_cents: int = LETTER[1]


def cart_page(sellers: list[CartSeller]) -> str:
    blocks: list[str] = []
    for seller in sellers:
        value = sum(row.copies * 100 for row in seller.rows)
        lines = "".join(
            f'<tr data-article-id="{row.article_id}" data-name="{row.card}" data-expansion-name="Any" '
            f'data-amount="{row.copies}"><td><a class="article-condition"><span>NM</span></a></td>'
            f'<td class="price">{euro(100)}</td></tr>'
            for row in seller.rows
        )
        blocks.append(
            f'<section id="seller{seller.seller_id}" class="shipment-block card">'
            f'<a href="/en/Magic/Users/{seller.name}">{seller.name}</a>'
            f'<div class="summary"><span class="item-value">{euro(value)}</span>'
            f'<span class="shipping-price">{euro(seller.shipping_cents)}</span>'
            f'<span class="strong total">{euro(value + seller.shipping_cents)}</span></div>'
            f'<p class="shipmentDisplay">{seller.method} ({euro(seller.shipping_cents)}) <span>20g</span></p>'
            f"<table><tbody>{lines}</tbody></table></section>"
        )
    return f'<main><div id="shipments-col">{"".join(blocks)}</div></main>'


class FakeCartPage:
    """Cardmarket's cart page and add endpoint: the cart a person has, what the add requests do to it and
    what they answer. Every call it takes is recorded in `events`, with the gaps `Gaps` records."""

    def __init__(self, picks: list[Pick]) -> None:
        self.offers = {pick.offer.article_id: pick.offer for pick in picks}
        self.cart: list[CartSeller] = []
        self.events: list[str] = []
        self.on_event: Callable[[str], None] = lambda _: None
        # Article ids whose request is refused as gone, never answered (not added), or never answered though
        # the article lands in the cart.
        self.refused: set[int] = set()
        # Article ids whose request is refused with another error, by the reason the answer gives.
        self.errors: dict[int, str] = {}
        # Article ids whose request goes out (and lands as the other settings say) but whose script call
        # raises a `LoadTimeout`, as when the window stops answering.
        self.start_timeout: set[int] = set()
        self.silent: set[int] = set()
        self.late: set[int] = set()
        # Article ids whose answer says success but the cart keeps this many copies (0 for none).
        self.kept: dict[int, int] = {}
        # The shipping a seller's new cart block shows, by seller name.
        self.shipping: dict[str, tuple[str, int]] = {}
        self.status = 200
        self.no_token = False
        # Loads that fail, by their position (1 is the first load).
        self.load_failures: dict[int, Exception] = {}
        self.closed = False
        self.page: str | None = None
        self._loads = 0
        self._reply: str | None = None

    def record(self, event: str) -> None:
        self.events.append(event)
        self.on_event(event)

    def close(self) -> None:
        self.closed = True

    def load(self, url: str, *, cancellable: bool = True) -> str:  # noqa: ARG002 - the protocol's parameter
        assert url in (CART_URL, WANTS_URL)
        self._loads += 1
        self.page = url
        self.record("load" if url == CART_URL else "load wants")
        if self._loads in self.load_failures:
            raise self.load_failures[self._loads]
        return cart_page(self.cart) if url == CART_URL else "<main>wants lists</main>"

    def act(self, script: str) -> str:
        if "fetch(" in script:
            started = self._send(script)
            if started == "started" and number_after(r"const id = ", script) in self.start_timeout:
                raise LoadTimeout("the window stopped answering")
            return started
        assert "JSON.stringify(answer" in script, script
        reply, self._reply = self._reply, None
        return "null" if reply is None else json.dumps({"status": self.status, "text": reply})

    def submit(self, script: str) -> str:
        raise AssertionError(f"the cart never submits a form: {script}")

    def _send(self, script: str) -> str:
        # As live: only the cart's own item forms carry the token there, so an empty cart page has none; the
        # wants lists page always has one (its create-list form).
        token = self.page == WANTS_URL or (self.page == CART_URL and bool(self.cart))
        if self.no_token or not token:
            return 'no element matches input[name="__cmtkn"]'
        article, copies = (
            number_after(r"const id = ", script),
            number_after(r"form.append\('amount.*String\(", script),
        )
        self.record(f"send {article} x{copies}")
        if article in self.refused:
            self._reply = GONE_XML
        elif article in self.errors:
            self._reply = answer_xml("error", ALERT.format(self.errors[article]))
        elif article in self.silent:
            self._reply = None
        else:
            self._reply = ADDED_XML
            self._land(article, self.kept.get(article, copies))
            return "started"
        if article in self.late:
            self._land(article, copies)
        return "started"

    def _land(self, article: int, copies: int) -> None:
        if copies < 1:
            return
        offer = self.offers[article]
        seller = next((s for s in self.cart if s.name == offer.seller), None)
        if seller is None:
            method, cents = self.shipping.get(offer.seller, LETTER)
            seller = CartSeller(offer.seller, 100 + len(self.cart), method=method, shipping_cents=cents)
            self.cart.append(seller)
        seller.rows.append(CartRow(article, offer.card, copies))


class SlowCartWindow:
    """A window showing `FakeCartPage`, whose load event comes only on the second wait of a load, so a page
    takes more than one load slice, as a real one can."""

    def __init__(self, page: FakeCartPage) -> None:
        self.page = page
        self.html = ""
        self.waits = 0

    def navigate(self, url: str) -> bool:
        self.html = self.page.load(url)
        self.waits = 0
        return True

    def wait_loaded(self, timeout_s: float) -> bool:  # noqa: ARG002 - the protocol's name
        self.waits += 1
        return self.waits >= 2

    def read_html(self, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol's name
        return self.html

    def run_script(self, script: str, timeout_s: float) -> str | None:  # noqa: ARG002 - the protocol's name
        return self.page.act(script)

    def begin_navigation(self) -> None:
        self.waits = 0

    def read_session_cookie(self) -> SessionCookie | None:
        return None

    def add_session_cookie(self, cookie: SessionCookie) -> None:
        raise AssertionError(f"no session is restored in this test: {cookie}")

    def close(self) -> None:
        self.page.close()


class Gaps:
    """The pacing seam: every wait goes in the page's event list, and `uniform` answers 4.5."""

    def __init__(self, page: FakeCartPage) -> None:
        self.page = page
        self.waits: list[float] = []
        self.bounds: list[tuple[float, float]] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.page.record(f"sleep {seconds}")

    def uniform(self, low: float, high: float) -> float:
        self.bounds.append((low, high))
        return 4.5


def pick(card: str, seller: str, copies: int = 1) -> Pick:
    return Pick(offer(card, seller, 100, quantity=copies), copies)


def package(*picks: Pick) -> Package:
    """A package whose every seller was quoted the letter."""
    sellers = dict.fromkeys(p.offer.seller for p in picks)
    shipments = tuple(
        Shipment(
            seller,
            "France",
            sum(p.copies for p in picks if p.offer.seller == seller),
            100,
            ShippingQuote(*LETTER, tracked=False),
        )
        for seller in sellers
    )
    return Package(tuple(picks), shipments, (), (), 100, 175)


@dataclass(slots=True)
class Run:
    result: CartRun
    page: FakeCartPage
    gaps: Gaps
    progress: list[CartProgress]


def run_cart(page: FakeCartPage, wanted: Package, cancel: threading.Event | None = None) -> Run:
    gaps = Gaps(page)
    progress: list[CartProgress] = []
    result = add_to_cart(
        cast("FormBrowser", page),
        wanted,
        cancel=cancel,
        on_progress=progress.append,
        sleep=gaps.sleep,
        uniform=gaps.uniform,
    )
    return Run(result, page, gaps, progress)


def outcomes(run: Run) -> list[tuple[str, CartOutcome]]:
    return [(item.pick.offer.card, item.outcome) for item in run.result.items]


def sent_to(run: Run) -> list[str]:
    return [event for event in run.page.events if event.startswith("send")]


class TestAdding:
    def test_every_offer_is_sent_in_seller_then_card_order_with_its_copies_and_the_cart_read_around_them(
        self,
    ) -> None:
        picks = [pick("Mind Stone", "B"), pick("Lotus Petal", "A"), pick("Arcane Signet", "A", 2)]
        page = FakeCartPage(picks)
        a_signet, a_petal, b_stone = (p.offer.article_id for p in (picks[2], picks[1], picks[0]))

        run = run_cart(page, package(*picks))

        assert page.events == [
            "load",
            "sleep 4.5",
            "load wants",
            "sleep 4.5",
            f"send {a_signet} x2",
            "sleep 4.5",
            f"send {a_petal} x1",
            "sleep 4.5",
            f"send {b_stone} x1",
            "sleep 4.5",
            "load",
        ]
        assert run.result.status is CartStatus.COMPLETE
        assert outcomes(run) == [
            ("Arcane Signet", CartOutcome.ADDED),
            ("Lotus Petal", CartOutcome.ADDED),
            ("Mind Stone", CartOutcome.ADDED),
        ]
        assert [item.in_cart for item in run.result.items] == [2, 1, 1]
        assert run.result.copies(CartOutcome.ADDED) == 4

    def test_the_gaps_are_the_scans_and_none_comes_before_the_first_request(self) -> None:
        page = FakeCartPage([p := pick("Mind Stone", "B")])

        run = run_cart(page, package(p))

        assert run.gaps.bounds == [(MIN_GAP_S, MAX_GAP_S)] * 3
        assert page.events[0] == "load"

    def test_the_requests_go_out_from_the_wants_lists_page_as_an_empty_cart_page_has_no_token(self) -> None:
        page = FakeCartPage([p := pick("Mind Stone", "B")])

        run = run_cart(page, package(p))

        assert page.events[:3] == ["load", "sleep 4.5", "load wants"]
        assert outcomes(run) == [("Mind Stone", CartOutcome.ADDED)]

    def test_nothing_to_send_loads_no_wants_page(self) -> None:
        page = FakeCartPage([p := pick("Mind Stone", "B")])
        page.cart = [CartSeller("B", 5, [CartRow(p.offer.article_id, "Mind Stone", 1)])]

        run_cart(page, package(p))

        assert page.events == ["load", "sleep 4.5", "load"]

    def test_the_progress_names_each_card_sent_and_counts_only_those_sent(self) -> None:
        picks = [pick("Mind Stone", "B"), pick("Lotus Petal", "A"), pick("Arcane Signet", "A")]
        page = FakeCartPage(picks)
        page.cart = [CartSeller("A", 5, [CartRow(picks[1].offer.article_id, "Lotus Petal", 1)])]

        run = run_cart(page, package(*picks))

        assert run.progress == [CartProgress("Arcane Signet", 0, 2), CartProgress("Mind Stone", 1, 2)]

    def test_an_article_already_in_the_cart_is_not_sent_and_is_reported_as_already(self) -> None:
        picks = [pick("Lotus Petal", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.cart = [CartSeller("A", 5, [CartRow(picks[0].offer.article_id, "Lotus Petal", 1)])]

        run = run_cart(page, package(*picks))

        assert sent_to(run) == [f"send {picks[1].offer.article_id} x1"]
        assert outcomes(run) == [
            ("Lotus Petal", CartOutcome.ALREADY),
            ("Mind Stone", CartOutcome.ADDED),
        ]
        assert run.result.items[0].in_cart == 1
        # The row that was there stays as it was.
        assert page.cart[0].rows == [CartRow(picks[0].offer.article_id, "Lotus Petal", 1)]

    def test_an_offer_cardmarket_refuses_is_gone_with_its_reason_and_the_rest_are_still_sent(self) -> None:
        picks = [pick("Lotus Petal", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.refused = {picks[0].offer.article_id}

        run = run_cart(page, package(*picks))

        assert outcomes(run) == [("Lotus Petal", CartOutcome.GONE), ("Mind Stone", CartOutcome.ADDED)]
        assert run.result.items[0] == ItemResult(picks[0], CartOutcome.GONE, 0, GONE_MESSAGE)
        assert run.result.status is CartStatus.COMPLETE
        assert run.result.copies(CartOutcome.GONE) == 1

    def test_fewer_copies_in_the_cart_than_asked_are_short_with_the_number_held(self) -> None:
        picks = [pick("Arcane Signet", "A", 3)]
        page = FakeCartPage(picks)
        page.kept = {picks[0].offer.article_id: 1}

        run = run_cart(page, package(*picks))

        assert [(item.outcome, item.in_cart) for item in run.result.items] == [(CartOutcome.SHORT, 1)]

    def test_an_offer_cardmarket_accepted_but_the_cart_does_not_show_is_missing_not_short(self) -> None:
        picks = [pick("Mind Stone", "B", 2)]
        page = FakeCartPage(picks)
        page.kept = {picks[0].offer.article_id: 0}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.COMPLETE
        assert [(item.outcome, item.in_cart) for item in run.result.items] == [(CartOutcome.MISSING, 0)]
        assert run.result.copies(CartOutcome.MISSING) == 2

    def test_an_article_in_the_cart_with_fewer_copies_than_picked_is_short_and_nothing_is_sent_for_it(
        self,
    ) -> None:
        picks = [pick("Lotus Petal", "A", 3), pick("Mind Stone", "B", 2)]
        page = FakeCartPage(picks)
        page.cart = [
            CartSeller("A", 5, [CartRow(picks[0].offer.article_id, "Lotus Petal", 2)]),
            CartSeller("B", 6, [CartRow(picks[1].offer.article_id, "Mind Stone", 2)]),
        ]

        run = run_cart(page, package(*picks))

        assert sent_to(run) == []
        assert [(item.outcome, item.in_cart) for item in run.result.items] == [
            (CartOutcome.SHORT, 2),
            (CartOutcome.ALREADY, 2),
        ]
        assert page.cart[0].rows == [CartRow(picks[0].offer.article_id, "Lotus Petal", 2)]

    def test_the_sellers_of_other_items_in_the_cart_are_left_alone_and_not_reported(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.cart = [CartSeller("Other", 9, [CartRow(777, "Sol Ring", 1)])]

        run = run_cart(page, package(*picks))

        assert [seller.name for seller in page.cart] == ["Other", "B"]
        assert page.cart[0].rows == [CartRow(777, "Sol Ring", 1)]
        assert [item.pick.offer.seller for item in run.result.items] == ["B"]
        assert [s.seller for s in run.result.shipping] == ["B"]

    def test_the_run_never_submits_a_form(self) -> None:
        # `FakeCartPage.submit` fails the test; a run that completes did not call it.
        picks = [pick("Mind Stone", "B")]

        assert run_cart(FakeCartPage(picks), package(*picks)).result.status is CartStatus.COMPLETE


class WaitRecorder(threading.Event):
    """A cancel event whose waits are recorded instead of slept."""

    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        assert timeout is not None
        self.waits.append(timeout)
        return self.is_set()


class TestRefusals:
    def test_an_error_that_is_not_the_item_being_gone_stops_the_run_with_cardmarkets_reason(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Lotus Petal", "B"), pick("Mind Stone", "C")]
        page = FakeCartPage(picks)
        page.errors = {picks[1].offer.article_id: "Your cart is full."}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket refused a request (Your cart is full.); adding stopped."
        assert run.result.items[1] == ItemResult(picks[1], CartOutcome.REFUSED, 0, "Your cart is full.")
        assert outcomes(run) == [
            ("Arcane Signet", CartOutcome.ADDED),
            ("Lotus Petal", CartOutcome.REFUSED),
            ("Mind Stone", CartOutcome.NOT_SENT),
        ]
        assert len(sent_to(run)) == 2
        assert page.events[-1] == "load"

    def test_an_error_without_a_reason_also_stops_the_run(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.errors = {picks[0].offer.article_id: ""}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert outcomes(run) == [("Arcane Signet", CartOutcome.REFUSED), ("Mind Stone", CartOutcome.NOT_SENT)]
        assert len(sent_to(run)) == 1

    def test_three_refusals_in_a_row_stop_the_run_and_the_rest_are_not_sent(self) -> None:
        picks = [pick(f"Card {n}", f"S{n}") for n in range(1, 6)]
        page = FakeCartPage(picks)
        page.refused = {p.offer.article_id for p in picks[:3]}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket refused 3 requests in a row; adding stopped."
        assert [i.outcome for i in run.result.items] == [CartOutcome.GONE] * 3 + [CartOutcome.NOT_SENT] * 2
        assert len(sent_to(run)) == 3

    def test_refusals_with_an_add_between_them_do_not_add_up(self) -> None:
        picks = [pick(f"Card {n}", f"S{n}") for n in range(1, 6)]
        page = FakeCartPage(picks)
        page.refused = {picks[i].offer.article_id for i in (0, 1, 3, 4)}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.COMPLETE
        assert len(sent_to(run)) == 5

    def test_a_run_whose_last_offers_are_refused_but_none_is_left_unsent_is_complete(self) -> None:
        picks = [pick(f"Card {n}", f"S{n}") for n in range(1, 4)]
        page = FakeCartPage(picks)
        page.refused = {p.offer.article_id for p in picks}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.COMPLETE
        assert [i.outcome for i in run.result.items] == [CartOutcome.GONE] * 3


class TestShipping:
    def test_a_sellers_cart_shipping_is_set_beside_the_estimate_and_differs_by_price(self) -> None:
        picks = [pick("Lotus Petal", "A"), pick("Mind Stone", "B"), pick("Sol Ring", "C")]
        page = FakeCartPage(picks)
        page.shipping = {"A": ("Brief S Priority", 255), "B": ("Lettre Internationale", 175)}
        page.refused = {picks[2].offer.article_id}

        run = run_cart(page, package(*picks))

        assert run.result.shipping == (
            ShippingResult("A", "Brief S Priority", 175, "Brief S Priority", 255),
            ShippingResult("B", "Brief S Priority", 175, "Lettre Internationale", 175),
        )
        assert [s.differs for s in run.result.shipping] == [True, False]

    def test_a_seller_with_nothing_in_the_cart_has_no_shipping_to_compare(self) -> None:
        picks = [pick("Sol Ring", "C")]
        page = FakeCartPage(picks)
        page.refused = {picks[0].offer.article_id}

        assert run_cart(page, package(*picks)).result.shipping == ()


class TestStopping:
    def test_cancel_between_requests_leaves_the_rest_not_sent_and_still_reads_the_cart(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Lotus Petal", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        cancel = threading.Event()
        page.on_event = lambda event: cancel.set() if event.startswith("send") else None

        run = run_cart(page, package(*picks), cancel)

        assert run.result.status is CartStatus.CANCELLED
        assert outcomes(run) == [
            ("Arcane Signet", CartOutcome.ADDED),
            ("Lotus Petal", CartOutcome.NOT_SENT),
            ("Mind Stone", CartOutcome.NOT_SENT),
        ]
        first = picks[0].offer.article_id
        assert page.events == [
            "load",
            "sleep 4.5",
            "load wants",
            "sleep 4.5",
            f"send {first} x1",
            "sleep 4.5",
            "load",
        ]
        assert run.result.copies(CartOutcome.ADDED) == 1
        assert run.result.copies(CartOutcome.NOT_SENT) == 2

    def test_cancel_before_the_first_request_sends_nothing(self) -> None:
        picks = [pick("Mind Stone", "B")]
        cancel = threading.Event()
        cancel.set()

        run = run_cart(FakeCartPage(picks), package(*picks), cancel)

        assert outcomes(run) == [("Mind Stone", CartOutcome.NOT_SENT)]
        assert sent_to(run) == []

    def test_cancel_during_the_gap_before_a_request_sends_nothing_more(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        cancel = threading.Event()
        page.on_event = lambda event: cancel.set() if event == "sleep 4.5" else None

        run = run_cart(page, package(*picks), cancel)

        assert run.result.status is CartStatus.CANCELLED
        assert sent_to(run) == []

    def test_a_request_with_no_answer_stops_the_adding_and_the_cart_is_still_read(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Lotus Petal", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.silent = {picks[1].offer.article_id}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket did not answer; adding stopped."
        assert outcomes(run) == [
            ("Arcane Signet", CartOutcome.ADDED),
            ("Lotus Petal", CartOutcome.NO_ANSWER),
            ("Mind Stone", CartOutcome.NOT_SENT),
        ]
        assert page.events[-1] == "load"
        waits = [w for w in run.gaps.waits if w == ANSWER_POLL_S]
        assert len(waits) == int(LOAD_TIMEOUT_S / ANSWER_POLL_S) - 1

    def test_a_request_with_no_answer_that_the_cart_shows_was_added(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.late = page.silent = {picks[0].offer.article_id}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert [(i.outcome, i.in_cart) for i in run.result.items] == [(CartOutcome.ADDED, 1)]

    def test_an_answer_that_is_not_http_ok_is_no_answer(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.status = 503
        page.kept = {picks[0].offer.article_id: 0}

        run = run_cart(page, package(*picks))

        assert [(i.outcome, i.in_cart) for i in run.result.items] == [(CartOutcome.NO_ANSWER, 0)]

    @pytest.mark.parametrize(
        ("silent", "outcome", "in_cart"),
        [(False, CartOutcome.ADDED, 1), (True, CartOutcome.NO_ANSWER, 0)],
        ids=["the cart holds it", "the cart does not"],
    )
    def test_a_timeout_starting_a_request_is_no_answer_and_the_cart_decides(
        self, *, silent: bool, outcome: CartOutcome, in_cart: int
    ) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        first = picks[0].offer.article_id
        page.start_timeout = {first}
        if silent:
            page.silent = {first}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket did not answer; adding stopped."
        assert [(i.outcome, i.in_cart) for i in run.result.items] == [
            (outcome, in_cart),
            (CartOutcome.NOT_SENT, 0),
        ]
        assert len(sent_to(run)) == 1

    def test_cancelling_while_an_answer_is_awaited_stops_polling_at_once(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.silent = {picks[0].offer.article_id}
        cancel = threading.Event()
        page.on_event = lambda event: cancel.set() if event == f"sleep {ANSWER_POLL_S}" else None

        run = run_cart(page, package(*picks), cancel)

        assert run.result.status is CartStatus.CANCELLED
        assert [i.outcome for i in run.result.items] == [CartOutcome.NO_ANSWER, CartOutcome.NOT_SENT]
        assert run.gaps.waits.count(ANSWER_POLL_S) == 1
        assert len(sent_to(run)) == 1

    def test_the_default_pacing_waits_on_the_cancel_event_so_a_cancel_ends_every_wait(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        cancel = WaitRecorder()

        browser = cast("FormBrowser", page)

        result = add_to_cart(browser, package(*picks), cancel=cancel, uniform=Gaps(page).uniform)

        assert result.status is CartStatus.COMPLETE
        assert cancel.waits == [4.5, 4.5, 4.5]

    def test_after_a_cancel_the_cart_is_read_back_through_a_slow_load_and_after_its_gap(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        cancel = WaitRecorder()
        page.on_event = lambda event: cancel.set() if event.startswith("send") else None
        after_cancel: list[float] = []
        browser = SessionBrowser(SlowCartWindow(page), FakeStore(), cancel=cancel.is_set)

        result = add_to_cart(
            browser,
            package(*picks),
            cancel=cancel,
            uniform=Gaps(page).uniform,
            after_cancel_sleep=after_cancel.append,
        )

        assert result.status is CartStatus.CANCELLED
        assert result.reason == ""
        assert [(item.outcome, item.in_cart) for item in result.items] == [
            (CartOutcome.ADDED, 1),
            (CartOutcome.NOT_SENT, 0),
        ]
        assert after_cancel == [4.5]
        assert page.events[-1] == "load"

    def test_a_page_without_the_token_stops_before_sending_and_still_reads_the_cart(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.no_token = True

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket's page was not as expected; adding stopped."
        assert outcomes(run) == [("Mind Stone", CartOutcome.NOT_SENT)]
        assert page.events == ["load", "sleep 4.5", "load wants", "sleep 4.5", "sleep 4.5", "load"]

    def test_a_cart_that_cannot_be_read_back_leaves_the_answers_unchecked(self) -> None:
        picks = [pick("Arcane Signet", "A"), pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.refused = {picks[1].offer.article_id}
        # Loads: the cart, the wants lists page, then the cart read back.
        page.load_failures = {3: BrowserError("the window failed")}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.STOPPED
        assert run.result.reason == "Cardmarket's cart could not be read back; check it on Cardmarket."
        assert [(i.outcome, i.in_cart) for i in run.result.items] == [
            (CartOutcome.ADDED, None),
            (CartOutcome.GONE, None),
        ]
        assert run.result.shipping == ()

    def test_cancelling_while_the_cart_is_read_back_is_a_cancelled_run(self) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.load_failures = {3: Cancelled("cancelled while the cart waited for the owner")}

        run = run_cart(page, package(*picks))

        assert run.result.status is CartStatus.CANCELLED
        assert run.result.reason == "The cart was not read back; check it on Cardmarket."
        assert [(i.outcome, i.in_cart) for i in run.result.items] == [(CartOutcome.ADDED, None)]

    @pytest.mark.parametrize(
        "failure", [Cancelled("cancelled"), BrowserError("the window failed"), PageError("not the cart")]
    )
    def test_a_cart_that_cannot_be_read_first_sends_nothing_and_raises(self, failure: Exception) -> None:
        picks = [pick("Mind Stone", "B")]
        page = FakeCartPage(picks)
        page.load_failures = {1: failure}

        with pytest.raises(type(failure)):
            run_cart(page, package(*picks))

        assert page.events == ["load"]


class TestNotSent:
    def test_a_package_that_was_never_sent_lists_every_offer_not_sent_in_order(self) -> None:
        picks = [pick("Mind Stone", "B"), pick("Lotus Petal", "A")]

        run = not_sent(package(*picks), CartStatus.STOPPED, "Something went wrong.")

        assert run == CartRun(
            CartStatus.STOPPED,
            "Something went wrong.",
            (
                ItemResult(picks[1], CartOutcome.NOT_SENT, None),
                ItemResult(picks[0], CartOutcome.NOT_SENT, None),
            ),
            (),
        )
