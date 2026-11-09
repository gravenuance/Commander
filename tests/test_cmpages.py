import re

import pytest

from commander.cmpages import (
    CardOffers,
    CartItem,
    CartShipment,
    Condition,
    Offer,
    PageError,
    PageNotFound,
    PageState,
    SellerOffers,
    Want,
    WantsList,
    WantsListEntry,
    page_state,
    parse_price_cents,
    read_card_offers,
    read_cart,
    read_seller_offers,
    read_wants_list,
    read_wants_lists,
)
from conftest import FIXTURES

# The fixtures are trimmed to <main>, so the header's sign-in state is not in them (ADR 0067).
CHALLENGE = (
    "<html><head><title>Just a moment...</title></head><body><div id='challenge-error-text'></div></body>"
)
LOGIN = (
    "<html><head><title>Cardmarket</title></head><body><form action='/en/Magic/PostGetAction/User_Login'>"
    "<input name='username'><input type='password' name='userPassword'></form></body></html>"
)
UNRELATED = "<html><head><title>Cardmarket</title></head><body><main><h1>Help</h1><p>Hello</p></main></body>"
SIGNED_IN_UNRELATED = (
    "<html><body><a href='/en/Magic/PostGetAction/User_Logout'>Log out</a>"
    "<input name='username'><main><h1>Help</h1></main></body></html>"
)

CARD_PAGE = "cm_card_offers.html"
SELLER_PAGE = "cm_seller_wants.html"
EMPTY_PAGE = "cm_seller_empty.html"
CART_PAGE = "cm_cart.html"
WANTS_PAGE = "cm_wants_list.html"

READERS = {
    CARD_PAGE: read_card_offers,
    SELLER_PAGE: read_seller_offers,
    CART_PAGE: read_cart,
    WANTS_PAGE: read_wants_list,
}


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def changed(html: str, old: str, new: str) -> str:
    """The page with `old` replaced, failing the test if the fixture no longer holds it."""
    assert old in html
    return html.replace(old, new)


def summary(offer: Offer) -> tuple[int, str, str, str, int, int | None, int | None, int | None]:
    return (
        offer.article_id,
        offer.seller,
        offer.seller_country,
        offer.language,
        offer.price_cents,
        offer.seller_sales,
        offer.seller_items,
        offer.delivery_days,
    )


class TestCardOffers:
    def test_first_row_in_full(self) -> None:
        first = read_card_offers(page(CARD_PAGE)).offers[0]

        assert first == Offer(
            article_id=2051305106,
            card="Sylvan Safekeeper",
            seller="Seller01",
            seller_country="Italy",
            expansion="Modern Horizons 3",
            condition=Condition.NM,
            language="Japanese",
            foil=False,
            buyable=True,
            price_cents=450,
            quantity=1,
            seller_sales=60,
            seller_items=640,
            delivery_days=16,
            route_days=14,
        )

    def test_the_route_average_of_every_row_comes_from_its_calendar_tooltip(self) -> None:
        offers = read_card_offers(page(CARD_PAGE)).offers

        # "Avg. delivery time from <country> to Sweden : N days" in the fixture, row by row.
        assert [(o.seller_country, o.route_days) for o in offers] == [
            ("Italy", 14),
            ("Hungary", 7),
            ("Croatia", 8),
            ("Sweden", 4),
            ("Belgium", 7),
            ("Switzerland", 7),
            ("Spain", 7),
        ]

    def test_a_row_without_the_route_average_has_none(self) -> None:
        bare = changed(
            page(CARD_PAGE),
            "(Avg. delivery time from Italy to Sweden :  14 days)",
            "(no average yet)",
        )

        first = read_card_offers(bare).offers[0]

        assert (first.route_days, first.delivery_days) == (None, 16)

    def test_a_seller_stock_page_has_no_route_average(self) -> None:
        offers = read_seller_offers(page(SELLER_PAGE)).offers

        assert {o.route_days for o in offers} == {None}

    def test_all_seven_rows(self) -> None:
        offers = read_card_offers(page(CARD_PAGE)).offers

        assert [summary(o) for o in offers] == [
            (2051305106, "Seller01", "Italy", "Japanese", 450, 60, 640, 16),
            (2021641662, "Seller02", "Hungary", "Japanese", 489, 6280, 4973, 11),
            (2167577992, "Seller03", "Croatia", "English", 500, 563, 36, 16),
            (2078427428, "Seller04", "Sweden", "Japanese", 500, 237, 352, 6),
            (2194092882, "Seller05", "Belgium", "English", 500, 154, 33, 12),
            (2144958945, "Seller06", "Switzerland", "German", 500, 142, 328, 7),
            (2147649428, "Seller07", "Spain", "English", 600, 93, 271, 9),
        ]
        assert {o.card for o in offers} == {"Sylvan Safekeeper"}
        assert {o.expansion for o in offers} == {"Modern Horizons 3"}
        assert {o.condition for o in offers} == {Condition.NM}
        assert {o.quantity for o in offers} == {1}

    def test_a_seller_who_does_not_ship_to_the_buyer_is_not_buyable(self) -> None:
        offers = read_card_offers(page(CARD_PAGE)).offers

        assert [o.seller for o in offers if not o.buyable] == ["Seller06"]

    def test_page_details(self) -> None:
        read = read_card_offers(page(CARD_PAGE))

        assert (read.metacard_id, read.destination, read.has_more) == (5761, "Sweden", True)

    def test_the_max_results_notice_ends_the_paging(self) -> None:
        shown = changed(
            page(CARD_PAGE),
            'id="MaxResultsReachedNotice" class="mt-2 d-none"',
            'id="MaxResultsReachedNotice" class="mt-2"',
        )

        assert read_card_offers(shown).has_more is False

    def test_a_hidden_button_means_no_more_results(self) -> None:
        hidden = changed(
            page(CARD_PAGE), 'id="loadMoreButton" class="btn', 'id="loadMoreButton" class="d-none btn'
        )

        assert read_card_offers(hidden).has_more is False

    def test_no_button_means_no_more_results(self) -> None:
        gone = re.sub(r'(?s)<button type="submit" id="loadMoreButton".*?</button>', "", page(CARD_PAGE))

        read = read_card_offers(gone)

        assert read.has_more is False
        assert len(read.offers) == 7

    def test_a_page_of_no_offers_is_empty(self) -> None:
        none = '<main><h1>Sylvan Safekeeper</h1><p class="noResults">No offers</p></main>'

        assert read_card_offers(none) == CardOffers(
            offers=(), has_more=False, metacard_id=None, destination=None
        )

    def test_a_row_without_its_price_names_the_article(self) -> None:
        broken = changed(page(CARD_PAGE), ">4,50 €<", "><")

        with pytest.raises(PageError, match=r"article 2051305106.*price"):
            read_card_offers(broken)

    def test_a_seller_that_ships_only_tracked_is_read_past_its_icon(self) -> None:
        # A real row (2026-11-09) puts this icon before the price, in both layouts.
        price = '<span class="color-primary small text-end text-nowrap fw-bold ">4,50 €</span>'
        icon = (
            '<span data-bs-toggle="tooltip" class="fonticon-check-circle extra-small color-primary me-2"'
            ' aria-label="Only &lt;strong&gt;tracked shipping&lt;/strong&gt; to Sweden."></span>'
        )
        tracked = changed(page(CARD_PAGE), price, icon + price)

        offers = read_card_offers(tracked).offers

        first = offers[0]
        assert (first.article_id, first.price_cents, first.tracked_only) == (2051305106, 450, True)
        assert not any(offer.tracked_only for offer in offers[1:])

    def test_a_seller_without_a_delivery_estimate_has_no_days(self) -> None:
        # A real row (2026-11-09) shows "-" for a seller with too few deliveries to estimate.
        unknown = changed(
            page(CARD_PAGE),
            '<span class="shippingTime-info">16</span>',
            '<span class="shippingTime-info">-</span>',
        )

        assert read_card_offers(unknown).offers[0].delivery_days is None

    def test_a_row_with_an_unknown_condition_names_the_article(self) -> None:
        broken = changed(
            page(CARD_PAGE),
            '<span class="badge ">NM</span>',
            '<span class="badge ">XX</span>',
        )
        with pytest.raises(PageError, match=r"article 2051305106.*'XX'"):
            read_card_offers(broken)

    def test_a_row_without_its_seller_names_the_article(self) -> None:
        broken = changed(
            page(CARD_PAGE), '<a href="/en/Magic/Users/Seller01">', '<a href="/en/Magic/Elsewhere">'
        )

        with pytest.raises(PageError, match=r"article 2051305106.*seller"):
            read_card_offers(broken)


class TestSellerOffers:
    def test_five_rows(self) -> None:
        read = read_seller_offers(page(SELLER_PAGE))

        assert (read.seller, read.seller_country, read.pages) == ("Seller01", "France", 1)
        assert [
            (o.article_id, o.card, o.expansion, o.price_cents, o.quantity, o.foil) for o in read.offers
        ] == [
            (2196491748, "Captain James T. Kirk", "Star Trek", 523, 1, True),
            (2196505214, "Captain James T. Kirk", "Star Trek", 314, 5, False),
            (2196512977, "Captain James T. Kirk (V.1)", "Star Trek: Extras", 833, 4, False),
            (2198795342, "Captain James T. Kirk (V.1)", "Star Trek: Extras", 1253, 1, True),
            (2197426518, "K'Vort-Class Raider", "Commander: Star Trek: Collector's Edition", 625, 8, True),
        ]

    def test_every_row_is_near_mint_english_and_comes_in_eleven_days(self) -> None:
        offers = read_seller_offers(page(SELLER_PAGE)).offers

        assert {(o.condition, o.language, o.seller, o.seller_country, o.delivery_days) for o in offers} == {
            (Condition.NM, "English", "Seller01", "France", 11)
        }
        assert all(o.buyable for o in offers)
        assert {(o.seller_sales, o.seller_items) for o in offers} == {(None, None)}

    def test_a_page_without_offers_is_an_empty_result(self) -> None:
        read = read_seller_offers(page(EMPTY_PAGE))

        assert read == SellerOffers(seller="Seller01", seller_country="France", offers=(), pages=1)

    def test_more_than_one_page_is_reported(self) -> None:
        paged = changed(page(SELLER_PAGE), "Page 1 of 1", "Page 1 of 3")

        assert read_seller_offers(paged).pages == 3

    def test_a_row_without_a_product_link_names_the_article(self) -> None:
        broken = changed(
            page(SELLER_PAGE), 'href="/en/Magic/Products/Singles/Star-Trek/Captain-James-T-Kirk"', 'href="/x"'
        )

        with pytest.raises(PageError, match=r"article 2196491748.*product"):
            read_seller_offers(broken)

    def test_a_row_without_its_quantity_names_the_article(self) -> None:
        broken = re.sub(r'<span class="item-count small text-end">8</span>', "", page(SELLER_PAGE))

        with pytest.raises(PageError, match=r"article 2197426518.*quantity"):
            read_seller_offers(broken)


class TestCart:
    def test_two_sellers(self) -> None:
        shipments = read_cart(page(CART_PAGE))

        assert shipments == (
            CartShipment(
                seller="Seller01",
                seller_id=1810936,
                seller_country="Austria",
                items=(
                    CartItem(
                        card="Sylvan Tutor",
                        expansion="Portal",
                        condition=Condition.PL,
                        price_cents=2489,
                        quantity=1,
                        article_id=1994831665,
                    ),
                ),
                article_value_cents=2489,
                shipping_cents=175,
                shipping_method="Brief S Priority",
                total_cents=2664,
            ),
            CartShipment(
                seller="Seller03",
                seller_id=992259476,
                seller_country="France",
                items=(
                    CartItem(
                        card="Captain James T. Kirk",
                        expansion="Star Trek",
                        condition=Condition.NM,
                        price_cents=314,
                        quantity=1,
                        article_id=2199864728,
                    ),
                ),
                article_value_cents=314,
                shipping_cents=255,
                shipping_method="Lettre Internationale (Priority Letter)",
                total_cents=569,
            ),
        )

    def test_a_block_repeated_for_the_other_layout_counts_once(self) -> None:
        body = page(CART_PAGE).removeprefix("<main>").removesuffix("</main>")

        shipments = read_cart(f"<main>{body}{body}</main>")

        assert [s.seller_id for s in shipments] == [1810936, 992259476]
        assert [len(s.items) for s in shipments] == [1, 1]

    def test_an_empty_cart_has_no_shipments(self) -> None:
        assert read_cart('<main><div id="shipments-col"></div></main>') == ()

    def test_a_block_without_its_total_names_the_seller(self) -> None:
        broken = changed(page(CART_PAGE), '<span class="strong total">5,69 €</span>', "")

        with pytest.raises(PageError, match=r"seller 992259476.*total"):
            read_cart(broken)

    def test_an_item_without_its_price_names_the_article(self) -> None:
        broken = changed(
            page(CART_PAGE), 'class="text-end text-nowrap price pe-2">24,89 €<', 'class="text-end">24,89 €<'
        )

        with pytest.raises(PageError, match=r"article 1994831665.*price"):
            read_cart(broken)


class TestWantsList:
    def test_the_probe_list(self) -> None:
        assert read_wants_list(page(WANTS_PAGE)) == WantsList(
            list_id=26107608,
            name="CMDR PROBE",
            wants=(
                Want(
                    want_id="6af1ca6e24ba6375060988f1",
                    card="Damping Sphere",
                    quantity=1,
                    languages=("English",),
                    min_condition=Condition.NM,
                ),
                Want(
                    want_id="6af1ca6e24ba6375060988f2",
                    card="K'Vort-Class Raider",
                    quantity=1,
                    languages=("English",),
                    min_condition=Condition.NM,
                ),
            ),
        )

    def test_a_want_of_any_language_has_no_languages(self) -> None:
        any_language = re.sub(
            r'(?s)<td class="languages.*?</td>',
            '<td class="languages"><span>Any</span></td>',
            page(WANTS_PAGE),
        )

        assert [w.languages for w in read_wants_list(any_language).wants] == [(), ()]

    def test_a_want_of_two_languages_lists_both_in_page_order(self) -> None:
        icons = "".join(
            '<a href="https://help.cardmarket.com/en/CardLanguage" class="d-flex me-1">'
            f'<span class="icon" aria-label="{label}" data-bs-original-title="{label}"></span></a>'
            for label in ("English", "Japanese")
        )
        two_languages = re.sub(
            r'(?s)<td class="languages.*?</td>',
            f'<td class="languages min-size p-2"><div class="d-flex flex-wrap">{icons}</div></td>',
            page(WANTS_PAGE),
        )

        assert [w.languages for w in read_wants_list(two_languages).wants] == [
            ("English", "Japanese"),
            ("English", "Japanese"),
        ]

    def test_a_want_without_its_condition_names_the_want(self) -> None:
        broken = changed(
            page(WANTS_PAGE),
            '<span class="badge ">NM</span>',
            "",
        )
        with pytest.raises(PageError, match=r"want 6af1ca6e24ba6375060988f1.*condition"):
            read_wants_list(broken)

    def test_a_list_without_wants_is_empty(self) -> None:
        emptied = re.sub(r'(?s)<tr role="row"><td class="select.*?</tr>', "", page(WANTS_PAGE))

        read = read_wants_list(emptied)

        assert (read.list_id, read.name, read.wants) == (26107608, "CMDR PROBE", ())


CREATE_FORM = (
    '<form method="POST" action="/en/Magic/PostGetAction/WantsList_CreateWantsList">'
    '<input name="wlName" id="SWWantsListName"><button type="submit">Create</button></form>'
)


def lists_page(*rows: str, form: str = CREATE_FORM) -> str:
    """A wants lists page built from the live page's description (no saved copy exists): each list's name
    in an element just before its link."""
    body = f"<main><h1>Wants</h1>{form}<div class='lists'>{''.join(rows)}</div></main>"
    return f"<html><body>{body}</body></html>"


def list_row(list_id: int, name: str, link_text: str = "Open") -> str:
    link = f'<a href="/en/Magic/Wants/{list_id}">{link_text}</a>'
    return f'<div class="row"><span class="name">{name}</span>{link}</div>'


class TestWantsLists:
    def test_each_list_has_the_name_just_before_its_link(self) -> None:
        html = lists_page(list_row(18999612, "MONTHLY WANTS"), list_row(26107608, "Commander"))

        assert read_wants_lists(html) == (
            WantsListEntry(18999612, "MONTHLY WANTS"),
            WantsListEntry(26107608, "Commander"),
        )

    def test_a_list_linked_twice_is_read_once_with_the_name_before_its_first_link(self) -> None:
        row = list_row(5, "Deck money") + '<a href="/en/Magic/Wants/5/">Edit</a>'

        assert read_wants_lists(lists_page(row)) == (WantsListEntry(5, "Deck money"),)

    def test_other_wants_links_are_not_lists(self) -> None:
        links = (
            '<a href="/en/Magic/Wants">Wants</a>'
            '<a href="/en/Magic/Wants/ShoppingWizard?idWantsList=5">Wizard</a>'
            '<a href="/en/Magic/Wants/5/AddCards">Add</a>'
        )

        assert read_wants_lists(lists_page(links, list_row(7, "Seven"))) == (WantsListEntry(7, "Seven"),)

    def test_a_name_is_trimmed_and_unescaped(self) -> None:
        row = list_row(9, "  Foo &amp; Bar \n")

        assert read_wants_lists(lists_page(row)) == (WantsListEntry(9, "Foo & Bar"),)

    def test_a_page_with_the_create_form_and_no_lists_has_none(self) -> None:
        assert read_wants_lists(lists_page()) == ()

    def test_a_page_without_the_create_form_is_not_the_lists_page(self) -> None:
        with pytest.raises(PageError, match="wants lists page: no form to create a list"):
            read_wants_lists(lists_page(list_row(1, "A"), form=""))

    def test_a_link_with_no_text_before_it_has_no_name(self) -> None:
        form = '<form action="/en/Magic/PostGetAction/WantsList_CreateWantsList"></form>'
        html = f'<html><body>{form}<a href="/en/Magic/Wants/3"></a></body></html>'

        with pytest.raises(PageError, match="list 3 has no name"):
            read_wants_lists(html)

    def test_a_challenge_and_a_sign_in_page_are_refused(self) -> None:
        with pytest.raises(PageError, match="Cloudflare"):
            read_wants_lists(CHALLENGE)
        with pytest.raises(PageError, match="sign in"):
            read_wants_lists(LOGIN)


class TestWrongPages:
    @pytest.mark.parametrize("name", list(READERS))
    def test_a_challenge_is_refused_by_every_reader(self, name: str) -> None:
        with pytest.raises(PageError, match="Cloudflare"):
            READERS[name](CHALLENGE)

    @pytest.mark.parametrize("name", list(READERS))
    def test_a_sign_in_form_is_refused_by_every_reader(self, name: str) -> None:
        with pytest.raises(PageError, match="sign in"):
            READERS[name](LOGIN)

    @pytest.mark.parametrize("name", list(READERS))
    def test_an_unrelated_page_is_refused_by_every_reader(self, name: str) -> None:
        with pytest.raises(PageError, match=r"no |heading"):
            READERS[name](UNRELATED)

    @pytest.mark.parametrize("name", list(READERS))
    def test_a_logout_link_means_signed_in_even_beside_a_username_field(self, name: str) -> None:
        with pytest.raises(PageError) as caught:
            READERS[name](SIGNED_IN_UNRELATED)

        assert "sign in" not in str(caught.value)

    @pytest.mark.parametrize("name", list(READERS))
    def test_the_page_kind_is_in_the_message(self, name: str) -> None:
        kinds = {
            CARD_PAGE: "card page",
            SELLER_PAGE: "seller offers page",
            CART_PAGE: "shopping cart",
            WANTS_PAGE: "wants list page",
        }

        with pytest.raises(PageError, match=kinds[name]):
            READERS[name](UNRELATED)

    def test_a_card_page_is_not_a_seller_page(self) -> None:
        with pytest.raises(PageError):
            read_seller_offers(page(CARD_PAGE))

    def test_a_seller_page_is_not_a_cart(self) -> None:
        with pytest.raises(PageError):
            read_cart(page(SELLER_PAGE))


NOT_FOUND = (
    "<html><head><title>%metacardName% - MTG Cards | Cardmarket</title></head><body><main>"
    '<h1 class="display-1"><span>404</span><span class="visually-hidden">Page not found</span></h1>'
    "</main></body></html>"
)


class TestNotFound:
    def test_the_404_page_is_a_card_that_was_not_found(self) -> None:
        with pytest.raises(PageNotFound, match="no card at this address"):
            read_card_offers(NOT_FOUND)

    def test_it_is_a_page_error_too(self) -> None:
        with pytest.raises(PageError):
            read_card_offers(NOT_FOUND)

    def test_a_card_whose_name_holds_404_is_still_a_card(self) -> None:
        none = '<main><h1>Area 404</h1><p class="noResults">No offers</p></main>'

        assert read_card_offers(none).offers == ()


class TestPageState:
    @pytest.mark.parametrize(
        ("html", "state"),
        [
            (CHALLENGE, PageState.CHALLENGE),
            (LOGIN, PageState.SIGNED_OUT),
            (SIGNED_IN_UNRELATED, PageState.SIGNED_IN),
            (UNRELATED, PageState.UNKNOWN),
        ],
    )
    def test_each_kind_of_page(self, html: str, state: PageState) -> None:
        assert page_state(html) is state


class TestPrices:
    @pytest.mark.parametrize(
        ("text", "cents"),
        [
            ("4,50 €", 450),
            ("0,05 €", 5),
            ("24,89 €", 2489),
            ("1.234,50 €", 123450),
            ("12.345.678,00 €", 1234567800),
            ("  3,14\xa0€ ", 314),
        ],
    )
    def test_cents(self, text: str, cents: int) -> None:
        assert parse_price_cents(text) == cents

    @pytest.mark.parametrize("text", ["", "4.50 €", "4,5 €", "1234.567,00 €", "free", "4,50 $"])
    def test_a_text_that_is_not_a_price_is_refused(self, text: str) -> None:
        with pytest.raises(PageError, match="not a price"):
            parse_price_cents(text, "article 7")


class TestCondition:
    def test_order_is_best_first(self) -> None:
        assert list(Condition) == [
            Condition.MT,
            Condition.NM,
            Condition.EX,
            Condition.GD,
            Condition.LP,
            Condition.PL,
            Condition.PO,
        ]

    @pytest.mark.parametrize(
        ("condition", "minimum", "expected"),
        [
            (Condition.NM, Condition.NM, True),
            (Condition.MT, Condition.NM, True),
            (Condition.EX, Condition.NM, False),
            (Condition.PO, Condition.PO, True),
            (Condition.PL, Condition.LP, False),
        ],
    )
    def test_at_least(self, condition: Condition, minimum: Condition, expected: bool) -> None:  # noqa: FBT001
        assert condition.at_least(minimum) is expected
