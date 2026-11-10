"""Readers for the Cardmarket pages the seller optimiser scans, from the page's HTML to typed data (ADR 0067).

The markup is Cardmarket's own and undocumented, so each reader checks the page is the kind it expects and
raises `PageError` rather than guess: a Cloudflare challenge, a sign-in form, a page without the structure,
or a row without a field the solver needs. The scan stops on that error; it is never retried or worked around.
These functions read text only. They fetch nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import Enum
from urllib.parse import urlsplit

from commander.htmltree import Node, Predicate, parse_html, tagged, with_class


class PageError(ValueError):
    """A page was not the Cardmarket page the reader expected, or a row on it could not be read."""


class PageNotFound(PageError):  # noqa: N818 - a kind of PageError, named for the page Cardmarket shows
    """Cardmarket's 404 page: the address names no card."""


class PageState(Enum):
    """What kind of page a browser holds, before any reader looks at it."""

    # Cloudflare's "Just a moment..." check, which passes by itself in a few seconds.
    CHALLENGE = "challenge"
    # A sign-in form and no logout link: the session has ended.
    SIGNED_OUT = "signed out"
    SIGNED_IN = "signed in"
    # Neither sign-in form nor logout link, as on a trimmed page.
    UNKNOWN = "unknown"


class Condition(Enum):
    """Card conditions, best first, as Cardmarket's condition badge writes them."""

    MT = "MT"
    NM = "NM"
    EX = "EX"
    GD = "GD"
    LP = "LP"
    PL = "PL"
    PO = "PO"

    def at_least(self, minimum: Condition) -> bool:
        order = list(Condition)
        return order.index(self) <= order.index(minimum)


@dataclass(slots=True, frozen=True)
class Offer:
    """One seller's article. Sales and items for sale come on card pages only; delivery days on both."""

    article_id: int
    card: str
    seller: str
    seller_country: str
    expansion: str
    condition: Condition
    language: str
    foil: bool
    # False when Cardmarket greys out the cart button, as when the seller does not ship to the buyer.
    buyable: bool
    price_cents: int
    quantity: int
    # The seller must ship this buyer tracked ("Only tracked shipping to Sweden"), whatever the order's value.
    tracked_only: bool = False
    seller_sales: int | None = None
    seller_items: int | None = None
    # The seller's own estimate to this buyer; None when Cardmarket shows "-".
    delivery_days: int | None = None
    # Cardmarket's average from the seller's country to the buyer's, from a card-page row's calendar tooltip.
    route_days: int | None = None


@dataclass(slots=True, frozen=True)
class CardOffers:
    offers: tuple[Offer, ...]
    # True when "Show more results" is offered: 50 offers a page, 300 in all.
    has_more: bool
    metacard_id: int | None
    # The buyer's country, from a row's delivery tooltip.
    destination: str | None


@dataclass(slots=True, frozen=True)
class SellerOffers:
    seller: str
    seller_country: str | None
    offers: tuple[Offer, ...]
    pages: int


@dataclass(slots=True, frozen=True)
class CartItem:
    card: str
    expansion: str
    condition: Condition
    # The row's price column; unverified for a quantity above 1, as the cart fixture has none.
    price_cents: int
    quantity: int
    article_id: int | None


@dataclass(slots=True, frozen=True)
class CartShipment:
    seller: str
    seller_id: int
    seller_country: str | None
    items: tuple[CartItem, ...]
    article_value_cents: int
    shipping_cents: int
    # The selected method without its price: "Brief S Priority".
    shipping_method: str
    total_cents: int


@dataclass(slots=True, frozen=True)
class Want:
    want_id: str
    card: str
    quantity: int
    # Empty means any language.
    languages: tuple[str, ...]
    min_condition: Condition


@dataclass(slots=True, frozen=True)
class WantsList:
    list_id: int
    name: str
    wants: tuple[Want, ...]


@dataclass(slots=True, frozen=True)
class WantsListEntry:
    """One of the buyer's wants lists, as the lists page names it."""

    list_id: int
    name: str


_CARD_PAGE = "card page"
_WANTS_LISTS_PAGE = "wants lists page"
_SELLER_PAGE = "seller offers page"
_CART_PAGE = "shopping cart"
_WANTS_PAGE = "wants list page"

_PRICE = re.compile(r"(?P<whole>\d{1,3}(?:\.\d{3})+|\d+),(?P<cents>\d{2})\s*€")
_SALES_ITEMS = re.compile(r"(?P<sales>\d[\d.,]*)\s*Sales\s*\|\s*(?P<items>\d[\d.,]*)\s*Available items")
_DELIVERY = re.compile(r"delivery time from [^:<]+? to (?P<destination>[^:<]+?)\s*:")
_ROUTE_DAYS = re.compile(r"Avg\. delivery time from [^:<]+? to [^:<]+?\s*:\s*(?P<days>\d+)\s*days?")
_PAGE_OF = re.compile(r"Page\s+\d+\s+of\s+(?P<pages>\d+)")
_SHIPPING_PRICE_SUFFIX = re.compile(r"\s*\(\d[\d.,]*\s*€\)\s*$")
_USER_PATH = re.compile(r"/en/Magic/Users/[^/?#]+")
_WANTS_ADD_CARDS = re.compile(r"/en/Magic/Wants/(?P<id>\d+)/AddCards")
_WANTS_LIST_PATH = re.compile(r"/en/Magic/Wants/(?P<id>\d+)/?")
_CREATE_WANTS_LIST = "WantsList_CreateWantsList"
_LOCATION_PREFIX = "Item location: "
_PRODUCT_PATH = "/en/Magic/Products/"
_FOIL = "Foil"
_TRACKED_ONLY = "tracked shipping"


def parse_price_cents(text: str, where: str = "price") -> int:
    """Cents from Cardmarket's "4,50 €" or "1.234,50 €"."""
    match = _PRICE.fullmatch(text.strip())
    if match is None:
        raise PageError(f"{where}: {text.strip()!r} is not a price like '4,50 €'")
    return int(match["whole"].replace(".", "")) * 100 + int(match["cents"])


def _page_state(root: Node) -> PageState:
    title = root.find(tagged("title"))
    if title is not None and "just a moment" in title.text().lower():
        return PageState.CHALLENGE
    form = root.find(
        lambda n: n.tag == "input" and (n.get("type") == "password" or n.get("name") == "username")
    )
    logout = root.find(lambda n: n.tag == "a" and "Logout" in (n.get("href") or ""))
    if logout is not None:
        return PageState.SIGNED_IN
    return PageState.SIGNED_OUT if form is not None else PageState.UNKNOWN


def page_state(html: str) -> PageState:
    return _page_state(parse_html(html))


def _open_page(html: str, page: str) -> Node:
    """Parse the page, refusing a challenge or a sign-in form."""
    root = parse_html(html)
    state = _page_state(root)
    if state is PageState.CHALLENGE:
        raise PageError(f"{page}: Cloudflare showed its check instead of the page")
    if state is PageState.SIGNED_OUT:
        raise PageError(f"{page}: Cardmarket asks to sign in; the session has ended")
    return root


def _need[T](value: T | None, where: str, what: str) -> T:
    if value is None:
        raise PageError(f"{where}: no {what}")
    return value


def _number(text: str, where: str, what: str) -> int:
    digits = re.sub(r"\D", "", text)
    if not digits:
        raise PageError(f"{where}: {what} {text.strip()!r} is not a number")
    return int(digits)


def _id_after(prefix: str, node_id: str | None, page: str) -> int:
    digits = (node_id or "").removeprefix(prefix)
    if not digits.isdigit():
        raise PageError(f"{page}: id {node_id!r} does not end in a number")
    return int(digits)


def _condition(scope: Node, where: str) -> Condition:
    badge = scope.find(tagged("a", cls="article-condition"))
    label = badge.text().strip() if badge is not None else ""
    try:
        return Condition(label)
    except ValueError:
        known = ", ".join(c.value for c in Condition)
        raise PageError(f"{where}: condition {label!r} is not one of {known}") from None


def _text_of(scope: Node, predicate: Predicate) -> str | None:
    node = scope.find(predicate)
    return None if node is None else node.text().strip()


def _text_of_own(scope: Node, predicate: Predicate) -> str | None:
    node = scope.find(predicate)
    return None if node is None else node.own_text().strip()


def _attr_of(scope: Node, predicate: Predicate, attribute: str) -> str | None:
    node = scope.find(predicate)
    return None if node is None else node.get(attribute)


def _is_row(prefix: str) -> Predicate:
    return lambda n: n.tag == "div" and n.has_class("article-row") and (n.get("id") or "").startswith(prefix)


def _is_link(path: Predicate) -> Predicate:
    return lambda n: n.tag == "a" and path(n)


def _location(scope: Node) -> str | None:
    label = _attr_of(scope, lambda n: (n.get("aria-label") or "").startswith(_LOCATION_PREFIX), "aria-label")
    return None if label is None else label.removeprefix(_LOCATION_PREFIX).strip()


def _heading(root: Node, page: str) -> Node:
    return _need(root.find(tagged("h1")), page, "heading")


def _require_offer_table(root: Node, rows: list[Node], page: str) -> None:
    if not rows and root.find(with_class("noResults")) is None:
        raise PageError(f"{page}: no offer rows and no notice that there are none")


def _adds_to_cart(node: Node) -> bool:
    return node.tag == "form" and (node.get("data-ajax-action") or "").startswith("ShoppingCart_Add")


def _read_offer(
    row: Node, article_id: int, page: str, *, card: str, seller: str, country: str | None
) -> Offer:
    where = f"{page}: article {article_id}"
    attributes = _need(row.find(with_class("product-attributes")), where, "product attributes")
    icons = [
        label
        for child in attributes.children
        if isinstance(child, Node) and child.tag == "span" and (label := child.get("aria-label"))
    ]
    price_box = _need(row.find(with_class("price-container")), where, "price")
    # The price is the bold figure; an icon before it can carry the same colour class.
    price = _text_of(price_box, with_class("fw-bold"))
    tracked_only = price_box.find(lambda n: _TRACKED_ONLY in (n.get("aria-label") or "")) is not None
    return Offer(
        article_id=article_id,
        card=card,
        seller=seller,
        seller_country=_need(country, where, "seller country"),
        expansion=_need(_attr_of(row, tagged("a", cls="expansion-symbol"), "aria-label"), where, "expansion"),
        condition=_condition(row, where),
        language=_need(icons[0] if icons else None, where, "language icon"),
        foil=_FOIL in icons,
        buyable=row.find(_adds_to_cart) is not None,
        price_cents=parse_price_cents(_need(price, where, "price"), f"{where} price"),
        tracked_only=tracked_only,
        quantity=_number(
            _need(_text_of(row, with_class("item-count")), where, "quantity"), where, "quantity"
        ),
    )


def _card_page_has_more(root: Node) -> bool:
    button = root.find(tagged("button", id="loadMoreButton"))
    if button is None or any(n.has_class("d-none") for n in (button, *button.ancestors())):
        return False
    notice = root.find(tagged("div", id="MaxResultsReachedNotice"))
    return notice is None or notice.has_class("d-none")


def _card_row_offer(row: Node, card: str) -> tuple[Offer, str | None]:
    """The offer of one card-page row, and the buyer's country from its delivery tooltip."""
    article_id = _id_after("articleRow", row.get("id"), _CARD_PAGE)
    where = f"{_CARD_PAGE}: article {article_id}"
    seller = _need(
        _text_of(row, _is_link(lambda n: _USER_PATH.fullmatch(n.get("href") or "") is not None)),
        where,
        "seller",
    )
    offer = _read_offer(row, article_id, _CARD_PAGE, card=card, seller=seller, country=_location(row))
    sales = _SALES_ITEMS.search(_attr_of(row, with_class("sell-count"), "data-bs-original-title") or "")
    days = _text_of(row, with_class("shippingTime-info"))
    calendar = _attr_of(row, with_class("fonticon-calendar"), "data-bs-original-title") or ""
    tooltip = _DELIVERY.search(calendar)
    route = _ROUTE_DAYS.search(calendar)
    full = replace(
        offer,
        seller_sales=None if sales is None else _number(sales["sales"], where, "seller sales"),
        seller_items=None if sales is None else _number(sales["items"], where, "seller items"),
        # A seller without enough deliveries for an estimate shows "-".
        delivery_days=None if days is None or days.strip() == "-" else _number(days, where, "delivery days"),
        route_days=None if route is None else int(route["days"]),
    )
    return full, None if tooltip is None else tooltip["destination"].strip()


def read_card_offers(html: str) -> CardOffers:
    root = _open_page(html, _CARD_PAGE)
    not_found = root.find(tagged("h1", cls="display-1"))
    if not_found is not None and "404" in not_found.text():
        raise PageNotFound(f"{_CARD_PAGE}: Cardmarket has no card at this address")
    rows = root.find_all(_is_row("articleRow"))
    _require_offer_table(root, rows, _CARD_PAGE)
    card = _heading(root, _CARD_PAGE).own_text().strip()
    if not card:
        raise PageError(f"{_CARD_PAGE}: the heading has no card name")
    read = [_card_row_offer(row, card) for row in rows]
    metacard = _attr_of(root, lambda n: n.tag == "input" and n.get("name") == "idMetacard", "value") or ""
    return CardOffers(
        offers=tuple(offer for offer, _ in read),
        has_more=_card_page_has_more(root),
        metacard_id=int(metacard) if metacard.isdigit() else None,
        destination=next((where for _, where in read if where is not None), None),
    )


def _seller_delivery_days(root: Node) -> int | None:
    lead = root.find(lambda n: n.has_class("lead") and "days to you" in n.text())
    days = None if lead is None or lead.parent is None else _text_of(lead.parent, with_class("h3"))
    return None if days is None else _number(days, _SELLER_PAGE, "days to you")


def _seller_pages(root: Node) -> int:
    for node in root.find_all(tagged("span")):
        match = _PAGE_OF.fullmatch(node.text().strip())
        if match is not None:
            return int(match["pages"])
    return 1


def read_seller_offers(html: str) -> SellerOffers:
    root = _open_page(html, _SELLER_PAGE)
    rows = root.find_all(_is_row("stockRow"))
    _require_offer_table(root, rows, _SELLER_PAGE)
    heading = _heading(root, _SELLER_PAGE)
    seller = heading.own_text().strip()
    if not seller:
        raise PageError(f"{_SELLER_PAGE}: the heading has no seller name")
    country = _attr_of(heading, lambda n: bool(n.get("aria-label")), "aria-label")
    days = _seller_delivery_days(root)
    offers: list[Offer] = []
    for row in rows:
        article_id = _id_after("stockRow", row.get("id"), _SELLER_PAGE)
        card = _need(
            _text_of(row, _is_link(lambda n: (n.get("href") or "").startswith(_PRODUCT_PATH))),
            f"{_SELLER_PAGE}: article {article_id}",
            "product link",
        )
        offer = _read_offer(row, article_id, _SELLER_PAGE, card=card, seller=seller, country=country)
        offers.append(replace(offer, delivery_days=days))
    return SellerOffers(
        seller=seller, seller_country=country, offers=tuple(offers), pages=_seller_pages(root)
    )


def _cart_item(line: Node, seller_id: int) -> CartItem:
    article = line.get("data-article-id") or ""
    where = f"{_CART_PAGE}: seller {seller_id}, article {article}"
    price = _text_of(line, tagged("td", cls="price"))
    return CartItem(
        card=_need(line.get("data-name"), where, "card name"),
        expansion=_need(line.get("data-expansion-name"), where, "expansion"),
        condition=_condition(line, where),
        price_cents=parse_price_cents(_need(price, where, "price"), f"{where} price"),
        quantity=_number(_need(line.get("data-amount"), where, "quantity"), where, "quantity"),
        article_id=int(article) if article.isdigit() else None,
    )


def _cart_items(block: Node, seller_id: int) -> tuple[CartItem, ...]:
    """The block's items once each: it lists them again for the narrow layout."""
    items: dict[str, CartItem] = {}
    for line in block.find_all(lambda n: n.tag == "tr" and n.get("data-article-id") is not None):
        items.setdefault(line.get("data-article-id") or "", _cart_item(line, seller_id))
    return tuple(items.values())


def _cart_shipment(block: Node) -> CartShipment:
    seller_id = _id_after("seller", block.get("id"), _CART_PAGE)
    where = f"{_CART_PAGE}: seller {seller_id}"
    summary = _need(block.find(tagged("div", cls="summary")), where, "summary")
    method = _need(_text_of_own(block, with_class("shipmentDisplay")), where, "shipping method")

    def amount(predicate: Predicate, what: str) -> int:
        return parse_price_cents(_need(_text_of(summary, predicate), where, what), f"{where} {what}")

    return CartShipment(
        seller=_need(
            _text_of(block, _is_link(lambda n: _USER_PATH.fullmatch(n.get("href") or "") is not None)),
            where,
            "seller name",
        ),
        seller_id=seller_id,
        seller_country=_location(block),
        items=_cart_items(block, seller_id),
        article_value_cents=amount(with_class("item-value"), "article value"),
        shipping_cents=amount(with_class("shipping-price"), "shipping cost"),
        shipping_method=_SHIPPING_PRICE_SUFFIX.sub("", method).strip(),
        total_cents=amount(lambda n: n.has_class("strong") and n.has_class("total"), "total"),
    )


def read_cart(html: str) -> tuple[CartShipment, ...]:
    root = _open_page(html, _CART_PAGE)
    blocks = root.find_all(lambda n: n.tag == "section" and n.has_class("shipment-block"))
    if not blocks and root.find(tagged("div", id="shipments-col")) is None:
        raise PageError(f"{_CART_PAGE}: no shipment blocks and no shipments column")
    shipments: dict[int, CartShipment] = {}
    for block in blocks:
        shipment = _cart_shipment(block)
        shipments.setdefault(shipment.seller_id, shipment)
    return tuple(shipments.values())


def _wants_list_id(root: Node) -> int | None:
    for link in root.find_all(tagged("a")):
        match = _WANTS_ADD_CARDS.match(link.get("href") or "")
        if match is not None:
            return int(match["id"])
    hidden = _attr_of(root, lambda n: n.tag == "input" and n.get("name") == "idWantsList", "value") or ""
    return int(hidden) if hidden.isdigit() else None


def _want(checkbox: Node, row: Node) -> Want:
    want_id = _need(checkbox.get("data-id-want") or None, f"{_WANTS_PAGE}: a want", "want id")
    where = f"{_WANTS_PAGE}: want {want_id}"
    amount = _need(_text_of(row, tagged("td", cls="amount")), where, "quantity")
    languages = _need(row.find(tagged("td", cls="languages")), where, "languages cell")
    return Want(
        want_id=want_id,
        card=_need(_text_of(row, tagged("td", cls="name")), where, "card name"),
        quantity=_number(amount, where, "quantity"),
        languages=tuple(
            label for n in languages.find_all(lambda n: n.has_class("icon")) if (label := n.get("aria-label"))
        ),
        min_condition=_condition(
            _need(row.find(tagged("td", cls="condition")), where, "condition cell"), where
        ),
    )


def read_wants_list(html: str) -> WantsList:
    root = _open_page(html, _WANTS_PAGE)
    list_id = _need(_wants_list_id(root), _WANTS_PAGE, "list id")
    name = _heading(root, _WANTS_PAGE).own_text().strip()
    if not name:
        raise PageError(f"{_WANTS_PAGE}: the heading has no list name")
    wants: list[Want] = []
    for row in root.find_all(tagged("tr")):
        checkbox = row.find(lambda n: n.tag == "input" and n.get("name") == "checkWantsRow[]")
        if checkbox is not None:
            wants.append(_want(checkbox, row))
    return WantsList(list_id=list_id, name=name, wants=tuple(wants))


def read_wants_lists(html: str) -> tuple[WantsListEntry, ...]:
    """The buyer's wants lists from the lists page, in page order.

    A list's name is the last text before its first link to `/en/Magic/Wants/<id>` (found so on the live
    page, not confirmed against a saved one). The page must hold the form that creates a list, which also
    makes an empty page a page with no lists rather than a page of another kind.
    """
    root = _open_page(html, _WANTS_LISTS_PAGE)
    if root.find(lambda n: n.tag == "form" and (n.get("action") or "").endswith(_CREATE_WANTS_LIST)) is None:
        raise PageError(f"{_WANTS_LISTS_PAGE}: no form to create a list")
    lists: dict[int, WantsListEntry] = {}
    last_text = ""
    for item in root.walk():
        if isinstance(item, str):
            last_text = item.strip() or last_text
            continue
        match = _WANTS_LIST_PATH.fullmatch(urlsplit(item.get("href") or "").path) if item.tag == "a" else None
        if match is None or int(match["id"]) in lists:
            continue
        list_id = int(match["id"])
        if not last_text:
            raise PageError(f"{_WANTS_LISTS_PAGE}: list {list_id} has no name before its link")
        lists[list_id] = WantsListEntry(list_id, last_text)
    return tuple(lists.values())
