import re

import pytest

from commander.cmpages import (
    CardOffers,
    CartItem,
    CartShipment,
    Condition,
    Offer,
    PageError,
    SellerOffers,
    Want,
    WantsList,
    parse_price_cents,
    read_card_offers,
    read_cart,
    read_seller_offers,
    read_wants_list,
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
        )

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
