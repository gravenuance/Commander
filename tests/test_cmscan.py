import threading
from collections.abc import Callable, Mapping

import pytest

from commander.cmpages import Condition, Offer
from commander.cmscan import (
    Foil,
    ScanFilters,
    ScanProgress,
    ScanRequest,
    ScanResult,
    ScanStage,
    ScanStatus,
    card_page_url,
    card_slug,
    choose_base_sellers,
    scan,
    seller_page_url,
)
from commander.cmsession import BrowserError, ChallengeError, LoadTimeout, SignedOut
from commander.offerlimits import OfferLimits
from commander.sellersolve import WantedCard
from conftest import FIXTURES

SYLVAN = "Sylvan Safekeeper"
KIRK = "Captain James T. Kirk"
CARD_PAGE = "cm_card_offers.html"
SELLER_PAGE = "cm_seller_wants.html"
EMPTY_SELLER_PAGE = "cm_seller_empty.html"
CARDS = "https://www.cardmarket.com/en/Magic/Cards"
USERS = "https://www.cardmarket.com/en/Magic/Users"
SYLVAN_URL = f"{CARDS}/Sylvan-Safekeeper?language=1"
KIRK_URL = f"{CARDS}/Captain-James-T-Kirk?language=1"
CARD_B_URL = f"{CARDS}/Card-B?language=1"
CARD_C_URL = f"{CARDS}/Card-C?language=1"
SELLER01_URL = f"{USERS}/Seller01/Offers/Singles?idWantslist=999"
UNRELATED = "<html><head><title>Cardmarket</title></head><body><main><h1>Help</h1></main></body></html>"
SIGN_IN = "<html><body><form><input name='username'><input type='password'></form></body></html>"
NOT_FOUND = (
    "<html><head><title>%metacardName% - MTG Cards | Cardmarket</title></head><body><main>"
    '<h1 class="display-1"><span>404</span><span class="visually-hidden">Page not found</span></h1>'
    "</main></body></html>"
)

# The fixture card page's articles, by seller, as the page lists them: Seller06 does not ship to the buyer.
FIXTURE_IDS = {
    "Seller01": 2051305106,
    "Seller02": 2021641662,
    "Seller03": 2167577992,
    "Seller04": 2078427428,
    "Seller05": 2194092882,
    "Seller06": 2144958945,
    "Seller07": 2147649428,
}
BUYABLE_FIXTURE_IDS = {i for seller, i in FIXTURE_IDS.items() if seller != "Seller06"}


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def card_page(
    card: str, *, sellers: Mapping[str, str] | None = None, ids: Mapping[int, int] | None = None
) -> str:
    """The fixture card page as another card's: its heading, and optionally its sellers and article ids."""
    html = fixture(CARD_PAGE).replace(SYLVAN, card)
    for old, new in (sellers or {}).items():
        assert old in html
        html = html.replace(old, new)
    for old_id, new_id in (ids or {}).items():
        assert str(old_id) in html
        html = html.replace(str(old_id), str(new_id))
    return html


def other_sellers(prefix: str) -> dict[str, str]:
    """Seller02 to Seller07 renamed, so a second card page shares only Seller01 with the fixture."""
    return {f"Seller0{n}": f"{prefix}0{n}" for n in range(2, 8)}


def shifted_ids() -> dict[int, int]:
    return {article: article + 1 for article in FIXTURE_IDS.values()}


class FakeBrowser:
    """Serves one page per address, or raises what is scripted for it; an address not scripted fails."""

    def __init__(self, pages: Mapping[str, str | Exception], events: list[str] | None = None) -> None:
        self.pages = pages
        self.events = events if events is not None else []
        self.loaded: list[str] = []
        self.on_load: Callable[[str], None] = lambda _: None
        self.closed = False

    def load(self, url: str) -> str:
        self.loaded.append(url)
        self.events.append(f"load {url}")
        self.on_load(url)
        answer = self.pages[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


class Gaps:
    """The pacing seam: records each wait and answers `uniform` with 4.5."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.waits: list[float] = []
        self.bounds: list[tuple[float, float]] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.events.append(f"sleep {seconds}")

    def uniform(self, low: float, high: float) -> float:
        self.bounds.append((low, high))
        return 4.5


def run(
    pages: Mapping[str, str | Exception],
    wanted: list[str],
    *,
    wants_list_id: int | None = None,
    base_sellers: int = 15,
    filters: ScanFilters | None = None,
    limits: OfferLimits | None = None,
    cancel: threading.Event | None = None,
    on_load: Callable[[str], None] | None = None,
    sleep: Callable[[float], object] | None = None,
) -> tuple[ScanResult, FakeBrowser, Gaps, list[ScanProgress]]:
    events: list[str] = []
    browser = FakeBrowser(pages, events)
    if on_load is not None:
        browser.on_load = on_load
    gaps = Gaps(events)
    progress: list[ScanProgress] = []
    request = ScanRequest(
        [WantedCard(name, 1) for name in wanted],
        filters or ScanFilters(),
        wants_list_id=wants_list_id,
        base_sellers=base_sellers,
        limits=limits or OfferLimits(),
    )
    result = scan(
        browser,
        request,
        cancel=cancel,
        on_progress=progress.append,
        sleep=sleep or gaps.sleep,
        uniform=gaps.uniform,
    )
    return result, browser, gaps, progress


def ids_of(result: ScanResult, card: str | None = None) -> set[int]:
    return {o.article_id for o in result.offers if card is None or o.card == card}


class TestSlug:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("Sylvan Safekeeper", "Sylvan-Safekeeper"),
            ("Lim-Dûl's Vault", "Lim-Duls-Vault"),
            ("Fire // Ice", "Fire-Ice"),
            ("Delver of Secrets // Insectile Aberration", "Delver-of-Secrets-Insectile-Aberration"),
            ("Gogo, Mysterious Mime", "Gogo-Mysterious-Mime"),
            ("Peter Parker's Camera", "Peter-Parkers-Camera"),
            ("Peter Parker\N{RIGHT SINGLE QUOTATION MARK}s Camera", "Peter-Parkers-Camera"),
            ("K'Vort-Class Raider", "KVort-Class-Raider"),
            ("  Sol   Ring ", "Sol-Ring"),
            ("Jötun Grunt", "Jotun-Grunt"),
            ("Ach! Hans, Run!", "Ach-Hans-Run"),
            ("Dead // Gone", "Dead-Gone"),
        ],
    )
    def test_the_address_segment(self, name: str, slug: str) -> None:
        assert card_slug(name) == slug


class TestUrls:
    def test_a_card_page_with_the_default_english_filter(self) -> None:
        assert card_page_url("Sol Ring", ScanFilters()) == f"{CARDS}/Sol-Ring?language=1"

    def test_language_and_minimum_condition_go_in_the_query(self) -> None:
        filters = ScanFilters(language=1, min_condition=Condition.NM)

        assert card_page_url("Sol Ring", filters) == f"{CARDS}/Sol-Ring?language=1&minCondition=2"

    @pytest.mark.parametrize(
        ("condition", "number"),
        [
            (Condition.MT, 1),
            (Condition.NM, 2),
            (Condition.EX, 3),
            (Condition.GD, 4),
            (Condition.LP, 5),
            (Condition.PL, 6),
            (Condition.PO, 7),
        ],
    )
    def test_the_conditions_are_numbered_from_mint(self, condition: Condition, number: int) -> None:
        url = card_page_url("Sol Ring", ScanFilters(language=None, min_condition=condition))

        assert url == f"{CARDS}/Sol-Ring?minCondition={number}"

    def test_no_filters_leave_the_address_bare(self) -> None:
        assert card_page_url("Sol Ring", ScanFilters(language=None)) == f"{CARDS}/Sol-Ring"

    @pytest.mark.parametrize(
        ("foil", "suffix"),
        [(Foil.ANY, ""), (Foil.NON_FOIL, "?isFoil=N"), (Foil.FOIL, "?isFoil=Y")],
    )
    def test_the_foil_choice_goes_in_the_query_and_any_leaves_it_out(self, foil: Foil, suffix: str) -> None:
        url = card_page_url("Sol Ring", ScanFilters(language=None, foil=foil))

        assert url == f"{CARDS}/Sol-Ring{suffix}"

    def test_foil_follows_the_other_filters(self) -> None:
        filters = ScanFilters(language=1, min_condition=Condition.NM, foil=Foil.FOIL)

        assert card_page_url("Sol Ring", filters) == f"{CARDS}/Sol-Ring?language=1&minCondition=2&isFoil=Y"

    def test_foil_defaults_to_any(self) -> None:
        assert ScanFilters().foil is Foil.ANY

    def test_each_seller_country_is_a_bracketed_field_as_cardmarkets_own_form_sends_it(self) -> None:
        filters = ScanFilters(language=None, seller_countries=frozenset({23, 7}))

        assert card_page_url("Sol Ring", filters) == (
            f"{CARDS}/Sol-Ring?sellerCountry%5B7%5D=7&sellerCountry%5B23%5D=23"
        )

    def test_one_country_and_the_other_filters_share_the_query(self) -> None:
        filters = ScanFilters(1, Condition.NM, Foil.FOIL, frozenset({28}), 5)

        assert card_page_url("Sol Ring", filters) == (
            f"{CARDS}/Sol-Ring?language=1&minCondition=2&isFoil=Y&sellerCountry%5B28%5D=28&maxShippingTime=5"
        )

    @pytest.mark.parametrize(
        ("days", "suffix"),
        [
            (None, ""),
            (1, ""),
            (2, "?maxShippingTime=2"),
            (3, "?maxShippingTime=3"),
            (5, "?maxShippingTime=5"),
            (6, "?maxShippingTime=6"),
            # The form's "7+" cannot be narrowed, so a longer limit is applied after the offers are read.
            (7, ""),
            (10, ""),
            (14, ""),
        ],
    )
    def test_the_delivery_limit_is_in_the_query_only_where_the_form_has_the_choice(
        self, days: int | None, suffix: str
    ) -> None:
        url = card_page_url("Sol Ring", ScanFilters(language=None, max_days=days))

        assert url == f"{CARDS}/Sol-Ring{suffix}"

    def test_no_country_and_no_limit_by_default(self) -> None:
        assert (ScanFilters().seller_countries, ScanFilters().max_days) == (frozenset(), None)

    def test_the_seller_page_is_filtered_by_the_wants_list(self) -> None:
        assert seller_page_url("Seller01", 999) == f"{USERS}/Seller01/Offers/Singles?idWantslist=999"

    def test_the_seller_name_cannot_leave_its_path_segment(self) -> None:
        assert seller_page_url("a b/c?d", 5) == f"{USERS}/a%20b%2Fc%3Fd/Offers/Singles?idWantslist=5"

    def test_a_later_stock_page_goes_in_site(self) -> None:
        assert seller_page_url("Seller01", 999, 2).endswith("?idWantslist=999&site=2")


class TestRequest:
    def test_at_least_one_base_seller(self) -> None:
        with pytest.raises(ValueError, match="at least one base seller"):
            ScanRequest([WantedCard(SYLVAN, 1)], base_sellers=0)

    def test_the_limits_must_match_the_address_filters(self) -> None:
        wanted = [WantedCard(SYLVAN, 1)]

        with pytest.raises(ValueError, match="delivery limit"):
            ScanRequest(wanted, ScanFilters(max_days=7), limits=OfferLimits())
        with pytest.raises(ValueError, match="seller countries"):
            ScanRequest(wanted, ScanFilters(seller_countries=frozenset({7})), limits=OfferLimits())
        with pytest.raises(ValueError, match="seller countries"):
            ScanRequest(wanted, ScanFilters(), limits=OfferLimits(frozenset({"germany"})))


class TestPassOne:
    def pages(self) -> dict[str, str | Exception]:
        return {
            SYLVAN_URL: fixture(CARD_PAGE),
            CARD_B_URL: card_page("Card B", sellers=other_sellers("B"), ids=shifted_ids()),
        }

    def test_one_page_per_card_with_all_buyable_offers(self) -> None:
        result, browser, _, _ = run(self.pages(), [SYLVAN, "Card B"])

        assert browser.loaded == [SYLVAN_URL, CARD_B_URL]
        assert result.status is ScanStatus.COMPLETE
        assert result.stop_reason is None
        assert result.pages_read == 2
        assert result.destination == "Sweden"
        assert ids_of(result, SYLVAN) == BUYABLE_FIXTURE_IDS
        assert ids_of(result, "Card B") == {i + 1 for i in BUYABLE_FIXTURE_IDS}
        assert len(result.offers) == 12

    def test_an_offer_the_buyer_cannot_buy_is_left_out(self) -> None:
        result, _, _, _ = run(self.pages(), [SYLVAN, "Card B"])

        assert FIXTURE_IDS["Seller06"] not in ids_of(result)
        assert FIXTURE_IDS["Seller06"] + 1 not in ids_of(result)

    def test_a_card_with_more_offers_than_the_page_shows_is_reported(self) -> None:
        result, _, _, _ = run(self.pages(), [SYLVAN, "Card B"])

        assert result.cards_with_more == (SYLVAN, "Card B")

    def test_no_wants_list_means_no_second_pass(self) -> None:
        result, browser, _, _ = run(self.pages(), [SYLVAN, "Card B"])

        assert result.base_sellers == ()
        assert len(browser.loaded) == 2

    def test_a_card_wanted_twice_is_read_once(self) -> None:
        result, browser, _, _ = run(self.pages(), [SYLVAN, "sylvan  safekeeper", "Card B"])

        assert browser.loaded == [SYLVAN_URL, CARD_B_URL]
        assert result.pages_read == 2

    def test_the_filters_are_in_the_address(self) -> None:
        filters = ScanFilters(language=1, min_condition=Condition.NM)
        url = f"{CARDS}/Sylvan-Safekeeper?language=1&minCondition=2"

        result, browser, _, _ = run({url: fixture(CARD_PAGE)}, [SYLVAN], filters=filters)

        assert browser.loaded == [url]
        assert result.pages_read == 1

    def test_offers_are_named_by_the_wanted_card(self) -> None:
        page = card_page("Sylvan Safekeeper // Backside")

        result, _, _, _ = run({SYLVAN_URL: page}, [SYLVAN])

        assert {o.card for o in result.offers} == {SYLVAN}

    def test_a_card_cardmarket_has_no_page_for_is_reported_and_the_scan_goes_on(self) -> None:
        pages = {SYLVAN_URL: NOT_FOUND, CARD_B_URL: self.pages()[CARD_B_URL]}

        result, browser, _, _ = run(pages, [SYLVAN, "Card B"])

        assert result.not_found == (SYLVAN,)
        assert result.status is ScanStatus.COMPLETE
        assert result.pages_read == 2
        assert result.no_buyable == ()
        assert ids_of(result, "Card B") == {i + 1 for i in BUYABLE_FIXTURE_IDS}
        assert browser.loaded == [SYLVAN_URL, CARD_B_URL]

    def test_a_card_whose_offers_are_all_unbuyable_has_no_buyable_offer(self) -> None:
        none_buyable = card_page("Card B", ids=shifted_ids()).replace("ShoppingCart_Add", "ShoppingCart_Gone")

        result, _, _, _ = run({SYLVAN_URL: fixture(CARD_PAGE), CARD_B_URL: none_buyable}, [SYLVAN, "Card B"])

        assert result.no_buyable == ("Card B",)
        assert ids_of(result, "Card B") == set()

    def test_a_page_that_is_not_a_card_page_stops_the_scan_with_the_reason(self) -> None:
        pages = {SYLVAN_URL: fixture(CARD_PAGE), CARD_B_URL: UNRELATED, CARD_C_URL: fixture(CARD_PAGE)}

        result, browser, _, _ = run(pages, [SYLVAN, "Card B", "Card C"])

        assert result.status is ScanStatus.STOPPED
        assert result.stop_reason is not None
        assert result.stop_reason.startswith("Card B: card page: ")
        assert browser.loaded == [SYLVAN_URL, CARD_B_URL]
        assert result.pages_read == 1
        assert ids_of(result) == BUYABLE_FIXTURE_IDS
        assert result.no_buyable == ()

    def test_a_sign_in_form_stops_the_scan(self) -> None:
        result, _, _, _ = run({SYLVAN_URL: SIGN_IN}, [SYLVAN])

        assert result.status is ScanStatus.STOPPED
        assert result.stop_reason == f"{SYLVAN}: card page: Cardmarket asks to sign in; the session has ended"

    @pytest.mark.parametrize(
        "error",
        [
            ChallengeError("a check"),
            LoadTimeout("too slow"),
            SignedOut("signed out mid-scan"),
            BrowserError("window gone"),
        ],
    )
    def test_a_browser_failure_mid_scan_stops_it_and_keeps_what_was_read(self, error: BrowserError) -> None:
        pages: dict[str, str | Exception] = {SYLVAN_URL: fixture(CARD_PAGE), CARD_B_URL: error}

        result, browser, _, _ = run(pages, [SYLVAN, "Card B", "Card C"])

        assert result.status is ScanStatus.STOPPED
        assert result.stop_reason == f"Card B: {error}"
        assert result.pages_read == 1
        assert ids_of(result) == BUYABLE_FIXTURE_IDS
        assert CARD_C_URL not in browser.loaded

    def test_progress_is_reported_before_each_page(self) -> None:
        _, _, _, progress = run(self.pages(), [SYLVAN, "Card B"])

        assert progress == [
            ScanProgress(ScanStage.CARDS, 0, 2, SYLVAN),
            ScanProgress(ScanStage.CARDS, 1, 2, "Card B"),
        ]

    def test_nothing_wanted_reads_nothing(self) -> None:
        result, browser, _, _ = run({}, [])

        assert (result.status, result.pages_read, result.offers, browser.loaded) == (
            ScanStatus.COMPLETE,
            0,
            (),
            [],
        )


class TestPacing:
    def test_pages_are_a_pause_apart_and_the_first_comes_at_once(self) -> None:
        pages = {SYLVAN_URL: fixture(CARD_PAGE), CARD_B_URL: NOT_FOUND, CARD_C_URL: NOT_FOUND}

        _, browser, gaps, _ = run(pages, [SYLVAN, "Card B", "Card C"])

        assert browser.events == [
            f"load {SYLVAN_URL}",
            "sleep 4.5",
            f"load {CARD_B_URL}",
            "sleep 4.5",
            f"load {CARD_C_URL}",
        ]
        assert gaps.bounds == [(4.0, 6.0), (4.0, 6.0)]

    def test_the_default_gap_is_drawn_between_4_and_6_seconds(self) -> None:
        waits: list[float] = []
        pages = {f"{CARDS}/Card-{n}?language=1": NOT_FOUND for n in range(40)}
        browser = FakeBrowser(pages)

        scan(
            browser,
            ScanRequest([WantedCard(f"Card {n}", 1) for n in range(40)]),
            sleep=waits.append,
        )

        assert len(waits) == 39
        assert all(4.0 <= wait <= 6.0 for wait in waits)
        assert len(set(waits)) > 1

    def test_a_scan_that_fails_on_its_first_page_never_waits(self) -> None:
        _, _, gaps, _ = run({SYLVAN_URL: LoadTimeout("slow")}, [SYLVAN])

        assert gaps.waits == []

    def test_without_a_sleeper_the_wait_is_on_the_cancel_event(self) -> None:
        pages = {SYLVAN_URL: NOT_FOUND, CARD_B_URL: NOT_FOUND}
        browser = FakeBrowser(pages)

        result = scan(
            browser,
            ScanRequest([WantedCard(SYLVAN, 1), WantedCard("Card B", 1)]),
            uniform=lambda _low, _high: 0.0,
        )

        assert result.pages_read == 2


class TestCancel:
    def test_cancelling_before_the_scan_reads_nothing(self) -> None:
        cancel = threading.Event()
        cancel.set()

        result, browser, _, _ = run({SYLVAN_URL: fixture(CARD_PAGE)}, [SYLVAN], cancel=cancel)

        assert result.status is ScanStatus.CANCELLED
        assert result.stop_reason is None
        assert (browser.loaded, result.pages_read) == ([], 0)

    def test_cancelling_mid_scan_stops_before_the_next_page_and_keeps_what_was_read(self) -> None:
        cancel = threading.Event()
        pages = {
            SYLVAN_URL: fixture(CARD_PAGE),
            CARD_B_URL: card_page("Card B", sellers=other_sellers("B"), ids=shifted_ids()),
            CARD_C_URL: NOT_FOUND,
        }

        def on_load(url: str) -> None:
            if url == CARD_B_URL:
                cancel.set()

        result, browser, _, _ = run(pages, [SYLVAN, "Card B", "Card C"], cancel=cancel, on_load=on_load)

        assert result.status is ScanStatus.CANCELLED
        assert browser.loaded == [SYLVAN_URL, CARD_B_URL]
        assert result.pages_read == 2
        assert len(result.offers) == 12
        assert result.no_buyable == ()

    def test_cancelling_during_the_wait_stops_before_the_page(self) -> None:
        cancel = threading.Event()
        pages = {SYLVAN_URL: fixture(CARD_PAGE), CARD_B_URL: NOT_FOUND}

        result, browser, _, _ = run(pages, [SYLVAN, "Card B"], cancel=cancel, sleep=lambda _: cancel.set())

        assert result.status is ScanStatus.CANCELLED
        assert browser.loaded == [SYLVAN_URL]
        assert result.pages_read == 1


def offer(article_id: int, card: str, seller: str, *, buyable: bool = True) -> Offer:
    return Offer(
        article_id=article_id,
        card=card,
        seller=seller,
        seller_country="Germany",
        expansion="Set",
        condition=Condition.NM,
        language="English",
        foil=False,
        buyable=buyable,
        price_cents=100,
        quantity=1,
    )


MARKET = [
    offer(1, "A", "S1"),
    offer(2, "A", "S2"),
    offer(3, "A", "S3"),
    offer(4, "B", "S1"),
    offer(5, "B", "S2"),
    offer(6, "C", "S1"),
    offer(7, "D", "S4"),
    offer(8, "D", "S5", buyable=False),
]


class TestBaseSellers:
    # Sellers per card: A has S1 S2 S3, B has S1 S2, C only S1, D only S4 (S5's offer cannot be bought).
    # Coverage: S1 3, S2 2, S3 1, S4 1. Scarcity: S1 1/3+1/2+1 = 11/6, S4 1, S2 1/3+1/2 = 5/6, S3 1/3.

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (1, ("S1",)),
            (2, ("S1", "S2")),
            (3, ("S1", "S4", "S2")),
            (4, ("S1", "S4", "S2", "S3")),
            (10, ("S1", "S4", "S2", "S3")),
        ],
    )
    def test_the_rare_half_first_then_the_widest(self, count: int, expected: tuple[str, ...]) -> None:
        assert choose_base_sellers(MARKET, count) == expected

    def test_the_only_seller_of_a_card_outranks_wider_coverage(self) -> None:
        # S4 covers one card but is the only seller of D; S2 covers two cards that others carry too.
        assert choose_base_sellers(MARKET, 3)[:2] == ("S1", "S4")

    def test_a_seller_who_cannot_ship_to_the_buyer_is_not_chosen(self) -> None:
        assert "S5" not in choose_base_sellers(MARKET, 10)

    def test_ties_go_to_the_name_in_alphabetical_order(self) -> None:
        offers = [offer(1, "A", "Bob"), offer(2, "B", "Bob"), offer(3, "A", "Alice"), offer(4, "B", "Alice")]

        assert choose_base_sellers(offers, 1) == ("Alice",)
        assert choose_base_sellers(offers, 2) == ("Alice", "Bob")

    def test_a_card_counts_once_per_seller_however_many_printings(self) -> None:
        offers = [
            offer(1, "Fire // Ice", "S1"),
            offer(2, "Fire / Ice", "S1"),
            offer(3, "Fire // Ice (V.1)", "S1"),
            offer(4, "Other", "S2"),
            offer(5, "Other", "S3"),
            offer(6, "Third", "S2"),
            offer(7, "Third", "S3"),
        ]
        # Coverage: S1 1 card, S2 2, S3 2. Scarcity: S1 1 (only seller of the card), S2 1/2+1/2 = 1, S3 1.
        # All tie on scarcity, so the wider coverage of S2 and S3 decides; S1 would lead on three printings.
        assert choose_base_sellers(offers, 3) == ("S2", "S3", "S1")
        assert choose_base_sellers(offers, 1) == ("S2",)

    def test_no_offers_choose_no_sellers(self) -> None:
        assert choose_base_sellers([], 5) == ()


class TestPassTwo:
    """Wanted: Sylvan Safekeeper (fixture sellers 01-05 and 07 can be bought from) and Kirk, whose page shares
    only Seller01 with it. Seller01 carries both: scarcity 1/6 + 1/6, against 1/6 for every other seller."""

    # Kirk's page: the fixture's, renamed. Seller01's article takes the id the seller's stock page lists for
    # a Kirk of its own, and the others move up by one.
    MERGED_ID = 2196505214

    def kirk_ids(self) -> dict[int, int]:
        ids = shifted_ids()
        ids[FIXTURE_IDS["Seller01"]] = self.MERGED_ID
        return ids

    def pages(self, seller_page: str | None = None) -> dict[str, str | Exception]:
        return {
            SYLVAN_URL: fixture(CARD_PAGE),
            KIRK_URL: card_page(KIRK, sellers=other_sellers("K"), ids=self.kirk_ids()),
            SELLER01_URL: seller_page if seller_page is not None else fixture(SELLER_PAGE),
        }

    def run_both(
        self, pages: Mapping[str, str | Exception] | None = None
    ) -> tuple[ScanResult, FakeBrowser, Gaps, list[ScanProgress]]:
        return run(pages or self.pages(), [SYLVAN, KIRK], wants_list_id=999, base_sellers=1)

    def test_the_base_sellers_wanted_stock_is_read_after_the_cards(self) -> None:
        result, browser, _, _ = self.run_both()

        assert browser.loaded == [SYLVAN_URL, KIRK_URL, SELLER01_URL]
        assert result.base_sellers == ("Seller01",)
        assert result.pages_read == 3
        assert result.status is ScanStatus.COMPLETE

    def test_the_stock_adds_the_articles_pass_one_did_not_show(self) -> None:
        result, _, _, _ = self.run_both()

        shown_by_cards = {self.MERGED_ID, 2021641663, 2167577993, 2078427429, 2194092883, 2147649429}
        assert ids_of(result, KIRK) == shown_by_cards | {2196491748, 2196512977, 2198795342}
        assert ids_of(result, SYLVAN) == BUYABLE_FIXTURE_IDS

    def test_an_offer_for_a_card_nobody_wanted_is_dropped(self) -> None:
        result, _, _, _ = self.run_both()

        assert 2197426518 not in ids_of(result)
        assert len(result.offers) == 6 + 6 + 3

    def test_a_printing_with_a_version_suffix_is_named_by_the_wanted_card(self) -> None:
        result, _, _, _ = self.run_both()

        (versioned,) = (o for o in result.offers if o.article_id == 2196512977)
        assert (versioned.card, versioned.expansion, versioned.price_cents) == (
            KIRK,
            "Star Trek: Extras",
            833,
        )

    def test_an_article_both_passes_read_takes_the_stock_price_and_keeps_the_sales_figures(self) -> None:
        result, _, _, _ = self.run_both()

        (merged,) = (o for o in result.offers if o.article_id == self.MERGED_ID)
        assert (merged.price_cents, merged.quantity, merged.expansion) == (314, 5, "Star Trek")
        assert (merged.seller_sales, merged.seller_items, merged.delivery_days) == (60, 640, 11)
        assert merged.card == KIRK

    def test_an_article_only_the_stock_shows_has_no_sales_figures(self) -> None:
        result, _, _, _ = self.run_both()

        (added,) = (o for o in result.offers if o.article_id == 2196491748)
        assert (added.seller_sales, added.seller_items, added.delivery_days) == (None, None, 11)

    def test_more_stock_pages_than_the_one_read_are_reported(self) -> None:
        more = fixture(SELLER_PAGE).replace("Page 1 of 1", "Page 1 of 3")

        result, browser, _, _ = self.run_both(pages=self.pages(more))

        assert result.sellers_with_more_pages == ("Seller01",)
        assert browser.loaded.count(SELLER01_URL) == 1

    def test_a_seller_with_none_of_the_wants_adds_nothing(self) -> None:
        result, _, _, _ = self.run_both(pages=self.pages(fixture(EMPTY_SELLER_PAGE)))

        assert len(result.offers) == 12
        assert result.pages_read == 3

    def test_a_stock_page_that_is_not_one_stops_the_scan_and_keeps_pass_one(self) -> None:
        result, _, _, _ = self.run_both(pages=self.pages(UNRELATED))

        assert result.status is ScanStatus.STOPPED
        assert result.stop_reason is not None
        assert result.stop_reason.startswith("Seller01: seller offers page: ")
        assert len(result.offers) == 12
        assert result.pages_read == 2
        assert result.base_sellers == ("Seller01",)
        assert result.no_buyable == ()

    def test_signing_out_during_the_second_pass_stops_the_scan(self) -> None:
        result, _, _, _ = self.run_both(pages={**self.pages(), SELLER01_URL: SignedOut("session ended")})

        assert result.status is ScanStatus.STOPPED
        assert result.stop_reason == "Seller01: session ended"
        assert len(result.offers) == 12

    def test_progress_plans_the_seller_pages_once_the_sellers_are_chosen(self) -> None:
        _, _, _, progress = self.run_both()

        assert progress == [
            ScanProgress(ScanStage.CARDS, 0, 2, SYLVAN),
            ScanProgress(ScanStage.CARDS, 1, 2, KIRK),
            ScanProgress(ScanStage.SELLERS, 2, 3, "Seller01"),
        ]

    def test_the_gaps_run_through_both_passes(self) -> None:
        _, browser, gaps, _ = self.run_both()

        assert gaps.waits == [4.5, 4.5]
        assert browser.events[1] == "sleep 4.5"
        assert browser.events[3] == "sleep 4.5"

    def test_cancelling_between_the_passes_reads_no_seller(self) -> None:
        cancel = threading.Event()

        def on_load(url: str) -> None:
            if url == KIRK_URL:
                cancel.set()

        result, browser, _, _ = run(
            self.pages(), [SYLVAN, KIRK], wants_list_id=999, base_sellers=1, cancel=cancel, on_load=on_load
        )

        assert result.status is ScanStatus.CANCELLED
        assert SELLER01_URL not in browser.loaded
        assert result.pages_read == 2

    def test_with_nothing_to_buy_there_are_no_sellers_to_open(self) -> None:
        result, browser, _, _ = run({SYLVAN_URL: NOT_FOUND}, [SYLVAN], wants_list_id=999)

        assert result.base_sellers == ()
        assert browser.loaded == [SYLVAN_URL]
        assert result.not_found == (SYLVAN,)


class TestBaseSellersUnderLimits:
    """One wanted card, so every seller of the fixture page ties on scarcity and coverage and the first by
    name is the base seller: Seller01 (Italy, 16 days). The others arrive in 11 (Seller02, Hungary), 16
    (Seller03, Croatia), 6 (Seller04, Sweden), 12 (Seller05, Belgium) and 9 days (Seller07, Spain); Seller06
    cannot be bought from. The page's route averages to Sweden are 14, 7, 8, 4, 7 and 7 days for Italy,
    Hungary, Croatia, Sweden, Belgium and Spain."""

    SELLER04_URL = f"{USERS}/Seller04/Offers/Singles?idWantslist=999"
    THREE_DAYS_URL = f"{CARDS}/Sylvan-Safekeeper?language=1&maxShippingTime=3"
    SWEDEN_URL = f"{CARDS}/Sylvan-Safekeeper?language=1&sellerCountry%5B28%5D=28"
    DAYS_OF_SELLER04 = '<span class="shippingTime-info">6</span>'
    NO_DAYS = '<span class="shippingTime-info">-</span>'
    ROUTE_OF_SWEDEN = "(Avg. delivery time from Sweden to Sweden :  4 days)"

    def run_one(
        self, pages: Mapping[str, str | Exception], filters: ScanFilters, limits: OfferLimits
    ) -> tuple[ScanResult, FakeBrowser]:
        result, browser, _, _ = run(
            pages, [SYLVAN], wants_list_id=999, base_sellers=1, filters=filters, limits=limits
        )
        return result, browser

    def test_without_limits_the_first_seller_by_name_is_chosen(self) -> None:
        pages = {SYLVAN_URL: fixture(CARD_PAGE), SELLER01_URL: fixture(SELLER_PAGE)}

        result, _ = self.run_one(pages, ScanFilters(), OfferLimits())

        assert result.base_sellers == ("Seller01",)

    def test_only_sellers_from_the_chosen_countries_can_be_base_sellers(self) -> None:
        pages = {self.SWEDEN_URL: fixture(CARD_PAGE), self.SELLER04_URL: fixture(SELLER_PAGE)}
        filters = ScanFilters(seller_countries=frozenset({28}))

        result, browser = self.run_one(pages, filters, OfferLimits(frozenset({"sweden"})))

        assert result.base_sellers == ("Seller04",)
        assert browser.loaded == [self.SWEDEN_URL, self.SELLER04_URL]

    def test_only_sellers_arriving_within_the_limit_can_be_base_sellers(self) -> None:
        pages = {SYLVAN_URL: fixture(CARD_PAGE), self.SELLER04_URL: fixture(SELLER_PAGE)}

        result, browser = self.run_one(pages, ScanFilters(max_days=7), OfferLimits(max_days=7))

        assert result.base_sellers == ("Seller04",)
        assert browser.loaded == [SYLVAN_URL, self.SELLER04_URL]

    def test_a_seller_without_an_estimate_is_chosen_on_its_route_average(self) -> None:
        card = fixture(CARD_PAGE).replace(self.DAYS_OF_SELLER04, self.NO_DAYS)
        pages = {SYLVAN_URL: card, self.SELLER04_URL: fixture(SELLER_PAGE)}

        result, _ = self.run_one(pages, ScanFilters(max_days=7), OfferLimits(max_days=7))

        # Nobody else is from Sweden, so Seller04 is judged by Sweden's 4-day route average.
        assert result.base_sellers == ("Seller04",)

    def test_a_route_average_over_the_limit_leaves_the_seller_out(self) -> None:
        card = fixture(CARD_PAGE).replace(self.DAYS_OF_SELLER04, self.NO_DAYS)
        pages = {self.THREE_DAYS_URL: card}

        result, browser = self.run_one(pages, ScanFilters(max_days=3), OfferLimits(max_days=3))

        # Seller04 is judged at Sweden's 4-day average, over the limit of 3, and no one else is as fast.
        assert result.base_sellers == ()
        assert browser.loaded == [self.THREE_DAYS_URL]

    def test_a_seller_with_neither_an_estimate_nor_a_route_average_is_left_out(self) -> None:
        card = fixture(CARD_PAGE).replace(self.DAYS_OF_SELLER04, self.NO_DAYS)
        card = card.replace(self.ROUTE_OF_SWEDEN, "(no average yet)")
        pages = {SYLVAN_URL: card, f"{USERS}/Seller07/Offers/Singles?idWantslist=999": fixture(SELLER_PAGE)}

        result, _ = self.run_one(pages, ScanFilters(max_days=7), OfferLimits(max_days=7))

        # Seller04 is out, and the next-fastest, Seller07 (9 days), is over the limit too.
        assert result.base_sellers == ()
        assert result.pages_read == 1

    def test_the_offers_read_are_all_kept_for_the_solver_to_judge(self) -> None:
        pages = {SYLVAN_URL: fixture(CARD_PAGE), self.SELLER04_URL: fixture(SELLER_PAGE)}

        result, _ = self.run_one(pages, ScanFilters(max_days=7), OfferLimits(max_days=7))

        assert ids_of(result) >= BUYABLE_FIXTURE_IDS
