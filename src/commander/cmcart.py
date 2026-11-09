"""Puts a confirmed package in the Cardmarket cart and reads the cart back (ADR 0067).

`add_to_cart` loads the cart page, then sends one add request per offer, the request the offer's own cart
button sends: an ajax post with the session's token from the wants lists page, which the page script makes
with `fetch`, so it carries the session and the headers Cardmarket's own script sends. Requests are paced
like the scan's. It only ever adds what it was given: an article already in the cart is left as it is, and
nothing is removed, cleared or checked out. When a request gets no answer (or its start is not confirmed) the
state of the cart is unknown, so adding stops and the cart page is read to say what is there. Only "The item
is not available." is an expected refusal; any other error answer, or three refusals in a row, stops adding
too. The cart is read once more at the end and compared with the package: each offer's copies, and each of
the package's sellers' shipping against the estimate.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import threading
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, cast

from commander.cmpages import CartShipment, PageError, read_cart
from commander.cmscan import MAX_GAP_S, MIN_GAP_S
from commander.cmsession import LOAD_TIMEOUT_S, BrowserError, Cancelled, LoadTimeout
from commander.cmwants import WANTS_URL
from commander.htmltree import parse_html, with_class
from commander.sellersolve import Package, Pick, card_key

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from commander.cmsession import FormBrowser

log = logging.getLogger(__name__)

CART_URL = "https://www.cardmarket.com/en/Magic/ShoppingCart"
# The address the card page's cart button posts to, relative to the page the request is sent from.
ADD_ACTION = "/en/Magic/AjaxAction/ShoppingCart_Add_AddArticlesFromMetacardPage"
# How often the stored answer is looked at while the request is out.
ANSWER_POLL_S = 0.5
HTTP_OK = 200
# Refused requests in a row after which adding stops: Cardmarket warns that cart abuse can suspend an account.
MAX_REFUSALS_IN_A_ROW = 3

_STARTED = "started"
# What an error answer says of an article that is no longer for sale; any other error answer is not that.
_GONE_REASON = "The item is not available."
_ERROR_PREFIX = "An error has occurred."
_NOT_ANSWERED = "Cardmarket did not answer; adding stopped."
_NOT_READ_BACK = "The cart was not read back; check it on Cardmarket."
_TOKEN_FIELD = "__cmtkn"  # noqa: S105 - the name of the page's token field, not a secret
_TOKEN_SELECTOR = f'input[name="{_TOKEN_FIELD}"]'
_ANSWER_KEY = "__cmdrCartAnswer"
_NO_REASON = "Cardmarket gave no reason."
_ELEMENT = re.compile(r"<(?P<name>resultType|systemMessage)>(?P<body>[^<]*)</(?P=name)>")
# An error answer's list of reasons is keyed ("1 => The item is not available.").
_REASON_KEY = re.compile(r"^\d+\s*=>\s*")


class CartOutcome(Enum):
    """What came of one offer of the package."""

    # In the cart, as many copies as asked.
    ADDED = "added"
    # It was in the cart before, so no request was sent for it.
    ALREADY = "already"
    # Cardmarket refused it because the offer is gone; the message is its reason.
    GONE = "gone"
    # Cardmarket refused it for another reason, given in the message; adding stopped there.
    REFUSED = "refused"
    # In the cart, but fewer copies than asked.
    SHORT = "short"
    # Cardmarket accepted the request but the cart read back holds none of its copies.
    MISSING = "missing"
    # The request got no answer (or its start was not confirmed), and the cart read back does not show every
    # copy of the offer or was not read back.
    NO_ANSWER = "no-answer"
    # Adding ended before its turn.
    NOT_SENT = "not-sent"


class CartStatus(Enum):
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    STOPPED = "stopped"


@dataclass(slots=True, frozen=True)
class AddAnswer:
    ok: bool
    # Cardmarket's own words, as plain text.
    message: str


@dataclass(slots=True, frozen=True)
class CartProgress:
    """The offer about to be sent: its card, how many were sent before it and how many will be."""

    card: str
    done: int
    planned: int


@dataclass(slots=True, frozen=True)
class ItemResult:
    pick: Pick
    outcome: CartOutcome
    # The copies the cart read back holds of the offer; None when the cart was not read back.
    in_cart: int | None
    message: str = ""


@dataclass(slots=True, frozen=True)
class ShippingResult:
    """A package seller's shipping in the cart against the package's estimate."""

    seller: str
    estimate_method: str
    estimate_cents: int
    cart_method: str
    cart_cents: int

    @property
    def differs(self) -> bool:
        return self.estimate_cents != self.cart_cents


@dataclass(slots=True, frozen=True)
class CartRun:
    status: CartStatus
    # Why adding stopped, or why the cart was not read back after a cancel, in words for the owner; empty for
    # a complete run and for a cancelled one whose cart was read back.
    reason: str
    items: tuple[ItemResult, ...]
    # Only the package's sellers that have a shipment in the cart.
    shipping: tuple[ShippingResult, ...]

    def copies(self, outcome: CartOutcome) -> int:
        return sum(item.pick.copies for item in self.items if item.outcome is outcome)


def not_sent(package: Package, status: CartStatus, reason: str) -> CartRun:
    """The run of a package of which nothing was sent."""
    items = tuple(ItemResult(pick, CartOutcome.NOT_SENT, None) for pick in _in_order(package.picks))
    return CartRun(status, reason, items, ())


def read_add_answer(text: str) -> AddAnswer:
    """The answer of an add request: `<ajaxResponse>` XML whose elements hold base64 text. The message is the
    alert's body (its heading when it has none) as plain text. Raises `PageError` for anything else."""
    found = {match["name"]: match["body"] for match in reversed(list(_ELEMENT.finditer(text)))}
    if "resultType" not in found:
        raise PageError("the cart's answer has no result type; it is not an ajax answer")
    result = _decode(found["resultType"], "result type").strip()
    if result not in ("success", "error"):
        raise PageError(f"the cart's answer has an unknown result type {result!r}")
    message = _message(_decode(found["systemMessage"], "message")) if "systemMessage" in found else ""
    ok = result == "success"
    return AddAnswer(ok, message or ("" if ok else _NO_REASON))


def _decode(body: str, what: str) -> str:
    try:
        return base64.b64decode(body, validate=True).decode("utf-8")
    except ValueError as error:
        raise PageError(f"the cart's answer has a {what} that is not base64 text") from error


def _message(html: str) -> str:
    root = parse_html(html)
    holder = root.find(with_class("alert-body")) or root.find(with_class("alert-heading")) or root
    return _REASON_KEY.sub("", " ".join(holder.text().split()))


def add_script(article_id: int, copies: int) -> str:
    """Start the add request for one article from the current page, as the cart button's own script does, and
    keep its answer on the page for `answer_script`. Answers `started`, or what it did not find."""
    _check_count(article_id, "article id")
    _check_count(copies, "copies")
    return f"""(() => {{
  const token = document.querySelector({json.dumps(_TOKEN_SELECTOR)});
  if (!token) return 'no element matches ' + {json.dumps(_TOKEN_SELECTOR)};
  const id = {json.dumps(article_id)};
  const form = new FormData();
  form.append({json.dumps(_TOKEN_FIELD)}, token.value);
  form.append('idArticle[' + id + ']', String(id));
  form.append('amount[' + id + ']', String({json.dumps(copies)}));
  window[{json.dumps(_ANSWER_KEY)}] = null;
  fetch({json.dumps(ADD_ACTION)}, {{
    method: 'POST', body: form, credentials: 'same-origin', headers: {{'X-Requested-With': 'XMLHttpRequest'}},
  }}).then((response) => response.text().then((text) => {{
    window[{json.dumps(_ANSWER_KEY)}] = {{status: response.status, text: text}};
  }})).catch((error) => {{
    window[{json.dumps(_ANSWER_KEY)}] = {{status: 0, text: String(error)}};
  }});
  return {json.dumps(_STARTED)};
}})()"""


def answer_script() -> str:
    """The stored answer as JSON with `status` and `text`, or `null` while the request is out; read once."""
    return f"""(() => {{
  const answer = window[{json.dumps(_ANSWER_KEY)}];
  if (answer) delete window[{json.dumps(_ANSWER_KEY)}];
  return JSON.stringify(answer || null);
}})()"""


def _check_count(value: object, what: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"the {what} must be a positive whole number, got {value!r}")


def _in_order(picks: Sequence[Pick]) -> list[Pick]:
    """By seller, then card: the same package is always added in the same order."""
    return sorted(picks, key=_order_key)


def _order_key(pick: Pick) -> tuple[str, str, int]:
    offer = pick.offer
    return offer.seller.casefold(), card_key(offer.card), offer.article_id


@dataclass(slots=True, frozen=True)
class _Sent:
    outcome: CartOutcome
    message: str = ""


@dataclass(slots=True, frozen=True)
class _Stop:
    """Why one offer's request gave no answer to go on; `unanswered` when the request may have gone out."""

    status: CartStatus
    reason: str
    unanswered: bool = False


class _Adder:
    def __init__(
        self,
        browser: FormBrowser,
        package: Package,
        *,
        cancel: threading.Event,
        on_progress: Callable[[CartProgress], None],
        sleep: Callable[[float], object],
        uniform: Callable[[float, float], float],
    ) -> None:
        self._browser = browser
        self._package = package
        self._cancel = cancel
        self._on_progress = on_progress
        self._sleep = sleep
        self._uniform = uniform
        # Requests begun (page loads and adds): all but the first wait out the gap.
        self._requests = 0

    def run(self) -> CartRun:
        picks = _in_order(self._package.picks)
        before = self._read_cart()
        in_cart_before = _copies_by_article(before)
        todo = [pick for pick in picks if pick.offer.article_id not in in_cart_before]
        if todo:
            self._open_sending_page()
        sent, status, reason = self._send(todo)
        try:
            after = self._read_cart()
        except Cancelled:
            log.info("cart: cancelled while the cart was read back")
            unchecked = _items(picks, sent, in_cart_before, None)
            return CartRun(CartStatus.CANCELLED, _NOT_READ_BACK, unchecked, ())
        except (BrowserError, PageError) as error:
            log.warning("cart: the cart could not be read back: %s", error)
            unread = "Cardmarket's cart could not be read back; check it on Cardmarket."
            return CartRun(CartStatus.STOPPED, unread, _items(picks, sent, in_cart_before, None), ())
        items = _items(picks, sent, in_cart_before, _copies_by_article(after))
        return CartRun(status, reason, items, self._shipping(after))

    # --- pacing ---------------------------------------------------------------------------------------

    def _pace(self) -> None:
        """Before every request: all but the first wait out the gap since the one before."""
        if self._requests > 0:
            self._sleep(self._uniform(MIN_GAP_S, MAX_GAP_S))
        self._requests += 1

    def _read_cart(self) -> tuple[CartShipment, ...]:
        self._pace()
        log.debug("loading %s", CART_URL)
        return read_cart(self._browser.load(CART_URL))

    def _open_sending_page(self) -> None:
        """The requests go out from the wants lists page: its create-list form always carries the session's
        token, while the cart page has one only in its items' forms, so an empty cart has none."""
        self._pace()
        log.debug("loading %s", WANTS_URL)
        self._browser.load(WANTS_URL)

    # --- the requests ---------------------------------------------------------------------------------

    def _send(self, todo: Sequence[Pick]) -> tuple[dict[int, _Sent], CartStatus, str]:
        """Send each offer's add request in turn; the outcome of each sent, and why sending ended early."""
        sent: dict[int, _Sent] = {}
        refusals = 0
        for index, pick in enumerate(todo):
            if self._cancel.is_set():
                return sent, CartStatus.CANCELLED, ""
            self._pace()
            if self._cancel.is_set():
                return sent, CartStatus.CANCELLED, ""
            offer = pick.offer
            self._on_progress(CartProgress(offer.card, index, len(todo)))
            answer = self._request(pick)
            if isinstance(answer, _Stop):
                if answer.unanswered:
                    sent[offer.article_id] = _Sent(CartOutcome.NO_ANSWER)
                return sent, answer.status, answer.reason
            outcome = _outcome(answer)
            log.info("cart: article %d %s: %s", offer.article_id, outcome.value, answer.message)
            sent[offer.article_id] = _Sent(outcome, answer.message)
            refusals = 0 if answer.ok else refusals + 1
            enough = _enough_after(outcome, answer.message, refusals, more=index + 1 < len(todo))
            if enough is not None:
                return sent, CartStatus.STOPPED, enough
        return sent, CartStatus.COMPLETE, ""

    def _request(self, pick: Pick) -> AddAnswer | _Stop:
        """One offer's add request and its answer, or why there is no answer to use."""
        article = pick.offer.article_id
        try:
            self._start(pick)
        except PageError as error:
            log.warning("cart: article %d was not sent: %s", article, error)
            return _Stop(CartStatus.STOPPED, "Cardmarket's page was not as expected; adding stopped.")
        except BrowserError as error:
            # The script may have run before the window failed, so the request may be out.
            return self._unanswered(article, error)
        try:
            return self._await_answer(pick)
        except (BrowserError, PageError) as error:
            return self._unanswered(article, error)

    @staticmethod
    def _unanswered(article: int, error: BrowserError | PageError) -> _Stop:
        log.warning("cart: no usable answer for article %d: %s", article, error)
        if isinstance(error, Cancelled):
            return _Stop(CartStatus.CANCELLED, "", unanswered=True)
        return _Stop(CartStatus.STOPPED, _NOT_ANSWERED, unanswered=True)

    def _start(self, pick: Pick) -> None:
        answer = self._browser.act(add_script(pick.offer.article_id, pick.copies))
        if answer != _STARTED:
            raise PageError(f"adding article {pick.offer.article_id}: {answer}")

    def _await_answer(self, pick: Pick) -> AddAnswer:
        """The request's answer, looked for every `ANSWER_POLL_S` until `LOAD_TIMEOUT_S` has passed."""
        polls = int(LOAD_TIMEOUT_S / ANSWER_POLL_S)
        for attempt in range(polls):
            if attempt > 0:
                self._sleep(ANSWER_POLL_S)
                if self._cancel.is_set():
                    raise Cancelled(f"article {pick.offer.article_id}: cancelled while awaiting its answer")
            stored = _stored(self._browser.act(answer_script()), pick.offer.article_id)
            if stored is not None:
                return stored
        raise LoadTimeout(f"article {pick.offer.article_id}: no answer within {LOAD_TIMEOUT_S:.0f} s")

    # --- the result -----------------------------------------------------------------------------------

    def _shipping(self, cart: Sequence[CartShipment]) -> tuple[ShippingResult, ...]:
        in_cart = {shipment.seller.casefold(): shipment for shipment in cart}
        results: list[ShippingResult] = []
        for planned in self._package.shipments:
            found = in_cart.get(planned.seller.casefold())
            if found is not None:
                results.append(
                    ShippingResult(
                        planned.seller,
                        planned.quote.method_name,
                        planned.quote.price_cents,
                        found.shipping_method,
                        found.shipping_cents,
                    )
                )
        return tuple(results)


def _outcome(answer: AddAnswer) -> CartOutcome:
    """Only the item being gone is expected of Cardmarket's refusals; any other error answer is not."""
    if answer.ok:
        return CartOutcome.ADDED
    if answer.message.removeprefix(_ERROR_PREFIX).strip() == _GONE_REASON:
        return CartOutcome.GONE
    return CartOutcome.REFUSED


def _enough_after(outcome: CartOutcome, message: str, refusals: int, *, more: bool) -> str | None:
    """The reason to stop sending after this answer, or None to carry on."""
    if outcome is CartOutcome.REFUSED:
        return f"Cardmarket refused a request ({message}); adding stopped."
    if refusals >= MAX_REFUSALS_IN_A_ROW and more:
        return f"Cardmarket refused {refusals} requests in a row; adding stopped."
    return None


def _copies_by_article(cart: Sequence[CartShipment]) -> dict[int, int]:
    copies: dict[int, int] = {}
    for shipment in cart:
        for item in shipment.items:
            if item.article_id is not None:
                copies[item.article_id] = copies.get(item.article_id, 0) + item.quantity
    return copies


def _stored(answer: str, article_id: int) -> AddAnswer | None:
    """The add answer in what the page stored (its HTTP status and the response's text); None while the
    request is still out."""
    try:
        stored: object = json.loads(answer)
    except json.JSONDecodeError as error:
        raise PageError(f"article {article_id}: the page's stored answer is not JSON: {answer!r}") from error
    if stored is None:
        return None
    if not isinstance(stored, dict):
        raise PageError(f"article {article_id}: the page's stored answer is not an object: {answer!r}")
    fields = cast("dict[str, object]", stored)
    status, text = fields.get("status"), fields.get("text")
    if status != HTTP_OK or not isinstance(text, str):
        raise PageError(f"article {article_id}: Cardmarket answered with status {status!r}")
    return read_add_answer(text)


def _items(
    picks: Sequence[Pick], sent: dict[int, _Sent], before: dict[int, int], after: dict[int, int] | None
) -> tuple[ItemResult, ...]:
    """Each offer's result: what its request got, judged against the cart read back (None if it was not)."""
    results: list[ItemResult] = []
    for pick in picks:
        article = pick.offer.article_id
        request = sent.get(article)
        held = None if after is None else after.get(article, 0)
        if article in before:
            outcome = CartOutcome.ALREADY if before[article] >= pick.copies else CartOutcome.SHORT
            results.append(ItemResult(pick, outcome, before[article]))
        elif request is None:
            results.append(ItemResult(pick, CartOutcome.NOT_SENT, held))
        elif request.outcome in (CartOutcome.GONE, CartOutcome.REFUSED):
            results.append(ItemResult(pick, request.outcome, held, request.message))
        else:
            results.append(ItemResult(pick, _judged(request.outcome, pick.copies, held), held))
    return tuple(results)


def _judged(answered: CartOutcome, copies: int, held: int | None) -> CartOutcome:
    """The outcome of an offer whose request was not refused, from the copies the cart holds of it."""
    if held is None:
        return answered
    if held >= copies:
        return CartOutcome.ADDED
    if held > 0:
        return CartOutcome.SHORT
    return CartOutcome.MISSING if answered is CartOutcome.ADDED else answered


def add_to_cart(
    browser: FormBrowser,
    package: Package,
    *,
    cancel: threading.Event | None = None,
    on_progress: Callable[[CartProgress], None] | None = None,
    sleep: Callable[[float], object] | None = None,
    uniform: Callable[[float, float], float] | None = None,
) -> CartRun:
    """Put the package's offers in the cart, each at its picked copies, and read the cart back.

    Raises `PageError` or the browser's own errors, `Cancelled` among them, when the cart page cannot be read
    before anything is sent. Once sending has begun every failure is reported in the run instead. `cancel`
    ends the adding between two requests; the cart is still read back. `sleep` waits out the gap between
    requests and polls for the answers; the default waits on `cancel`, so a cancel ends the wait too.
    """
    stop = cancel if cancel is not None else threading.Event()
    return _Adder(
        browser,
        package,
        cancel=stop,
        on_progress=on_progress if on_progress is not None else _ignore,
        sleep=sleep if sleep is not None else stop.wait,
        uniform=uniform if uniform is not None else random.uniform,
    ).run()


def _ignore(_: CartProgress) -> None:
    return
