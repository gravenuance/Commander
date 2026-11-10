"""The wants-list sync against a fake Cardmarket: a small site that keeps lists and wants and answers the
scripts the sync sends by the selectors they name (the scripts themselves are run in `test_cmforms`)."""

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from commander.cmpages import Condition, PageError, Want
from commander.cmscan import ENGLISH, JAPANESE, Foil
from commander.cmsession import SUBMITTED, BrowserError, SignedOut
from commander.cmwants import (
    APP_LIST_NAME,
    QuantityMismatch,
    WantsCancelled,
    WantsError,
    WantsProgress,
    WantsStep,
    WantsSync,
    sync_wants,
    wants_text,
)
from commander.sellersolve import WantedCard
from conftest import FIXTURES

LISTS_URL = "https://www.cardmarket.com/en/Magic/Wants"
CONDITION_OF_ID = {"1": "MT", "2": "NM", "3": "EX", "4": "GD", "5": "LP", "6": "PL", "7": "PO"}
LANGUAGE_OF_ID = {"1": "English", "7": "Japanese"}
LANGUAGE_SELECT = 'select[name="idLanguages[]"]'
NEW_LIST_ID = 100
# What Cardmarket answers for a list address that is no list of the buyer's.
NO_SUCH_LIST = "<html><head><title>Cardmarket</title></head><body><main><h1>Gone</h1></main></body></html>"
# The fields the sync fills in, by selector, and the name the fake keeps each under.
FIELDS = {
    "#SWWantsListName": "new name",
    "#AddDecklist": "import",
    "#amount": "amount",
    "#minCondition": "condition",
    "#isFoil": "foil",
}


@dataclass(slots=True)
class SiteWant:
    want_id: str
    card: str
    quantity: int
    languages: tuple[str, ...] = ()
    condition: str = "PO"
    # What the expansion cell, the yes/no columns (foil, signed, altered) and the buy price show.
    expansion: str = "Any"
    yes_no: str = "Any"
    price: str = ""


def lists_page(lists: dict[int, str]) -> str:
    links = "".join(
        f'<div><span>{name}</span><a href="/en/Magic/Wants/{list_id}">Open</a></div>'
        for list_id, name in lists.items()
    )
    form = (
        '<form action="/en/Magic/PostGetAction/WantsList_CreateWantsList">'
        '<input id="SWWantsListName" name="wlName"></form>'
    )
    return f"<html><body><main><h1>Wants</h1>{form}{links}</main></body></html>"


def list_page(list_id: int, name: str, wants: list[SiteWant]) -> str:
    rows = "".join(
        '<tr role="row">'
        f'<td class="select"><input type="checkbox" name="checkWantsRow[]" data-id-want="{w.want_id}"></td>'
        f'<td class="amount">{w.quantity}</td>'
        f'<td class="name"><a href="/en/Magic/Cards/x">{w.card}</a></td>'
        '<td class="languages">'
        + "".join(f'<span class="icon" aria-label="{language}"></span>' for language in w.languages)
        + "</td>"
        '<td class="condition"><a class="article-condition">'
        f'<span class="badge">{w.condition}</span></a></td>'
        f'<td class="expansion"><span class="visually-hidden">{w.expansion}</span></td>'
        + f'<td class="ternary-header"><span class="visually-hidden">{w.yes_no}</span></td>'
        * 3
        + f'<td data-text="0" class="buyPrice">{w.price}</td>'
        "</tr>"
        for w in wants
    )
    return (
        f"<html><body><main><h1>{name}</h1>"
        f'<a href="/en/Magic/Wants/{list_id}/AddCards">Add</a><table>{rows}</table></main></body></html>'
    )


def value_set_by(script: str) -> str:
    match = re.search(r'el\.value = ("(?:[^"\\]|\\.)*");', script)
    assert match is not None, script
    return json.loads(match[1])


def options_selected_by(script: str) -> list[str]:
    match = re.search(r"const wanted = (\[.*?\]);", script)
    assert match is not None, script
    return json.loads(match[1])


def ids_ticked_by(script: str) -> list[str]:
    match = re.search(r"new Set\((\[.*?\])\)", script)
    assert match is not None, script
    return json.loads(match[1])


@dataclass(slots=True)
class FakeSite:
    """Lists and their wants, and what the sync's scripts do to them. Names in `unknown` are not recognised
    by the import, `renamed` maps an imported name to the one the list shows, `quantities` overrides the
    quantity a card is imported with."""

    lists: dict[int, str] = field(default_factory=dict[int, str])
    wants: dict[int, list[SiteWant]] = field(default_factory=dict[int, list[SiteWant]])
    unknown: set[str] = field(default_factory=set[str])
    renamed: dict[str, str] = field(default_factory=dict[str, str])
    quantities: dict[str, int] = field(default_factory=dict[str, int])
    # List reads that show nothing yet after the import.
    import_lag: int = 0
    # The bulk edit changes nothing.
    ignore_edits: bool = False
    # The languages a want shows come back in the opposite order to the one they were selected in.
    reverse_languages: bool = False
    # Language ids the bulk edit does not apply (a select that is not `multiple` keeps one).
    dropped_languages: set[str] = field(default_factory=set[str])
    # The languages every edited want shows whatever was selected, when set.
    shown_languages: tuple[str, ...] | None = None
    # A creation that creates nothing.
    create_fails: bool = False
    # Selectors the page does not have (their scripts answer "no element matches").
    missing: set[str] = field(default_factory=set[str])
    # Rows a tick script cannot find.
    unticked_rows: set[str] = field(default_factory=set[str])
    pages_once: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    failures: dict[str, Exception] = field(default_factory=dict[str, Exception])
    events: list[str] = field(default_factory=list[str])
    loaded: list[str] = field(default_factory=list[str])
    scripts: list[str] = field(default_factory=list[str])
    # The foil value each edited want got, one entry per want, "" when the form's foil field was not set.
    foil_set: list[str] = field(default_factory=list[str])
    on_event: Callable[[str], None] = lambda _: None
    fields: dict[str, str] = field(default_factory=dict[str, str])
    closed: bool = False
    _current: int | None = None
    _ticked: list[str] = field(default_factory=list[str])
    _pending: list[SiteWant] = field(default_factory=list[SiteWant])
    _lag_left: int = 0
    _next_want: int = 1

    def record(self, event: str) -> None:
        self.events.append(event)
        self.on_event(event)

    def close(self) -> None:
        self.closed = True

    # --- pages ----------------------------------------------------------------------------------------

    def load(self, url: str, *, cancellable: bool = True) -> str:  # noqa: ARG002 - the protocol's parameter
        self.loaded.append(url)
        self.record(f"load {url.removeprefix(LISTS_URL)}")
        if url in self.failures:
            raise self.failures[url]
        page = self._page(url)
        return self.pages_once[url].pop(0) if self.pages_once.get(url) else page

    def _page(self, url: str) -> str:
        if url == LISTS_URL:
            return lists_page(self.lists)
        match = re.fullmatch(re.escape(LISTS_URL) + r"/(\d+)(/AddDeckList)?", url)
        assert match is not None, f"the sync loaded {url}"
        list_id = int(match[1])
        if list_id not in self.lists:
            return NO_SUCH_LIST
        if match[2]:
            return "<html><body><form id='AddDecklistForm'></form></body></html>"
        self._current = list_id
        self._ticked = []
        if self._pending and self._lag_left <= 0:
            self.wants.setdefault(list_id, []).extend(self._pending)
            self._pending = []
        elif self._pending:
            self._lag_left -= 1
        return list_page(list_id, self.lists[list_id], self.wants.get(list_id, []))

    # --- scripts --------------------------------------------------------------------------------------

    def _missing(self, script: str) -> str | None:
        for selector in self.missing:
            if json.dumps(selector) in script:
                return f"no element matches {selector}"
        return None

    def act(self, script: str) -> str:
        self.scripts.append(script)
        if (refusal := self._missing(script)) is not None:
            self.record("act missing")
            return refusal
        if json.dumps(LANGUAGE_SELECT) in script:
            self.record("fill language")
            self.fields["language"] = ",".join(options_selected_by(script))
            return "ok"
        for selector, key in FIELDS.items():
            if json.dumps(selector) in script:
                self.record(f"fill {key}")
                self.fields[key] = value_set_by(script)
                return "ok"
        current = self.wants.get(self._current or 0, [])
        if 'input[name="checkAll"]' in script:
            self.record("tick all")
            self._ticked = [w.want_id for w in current if w.want_id not in self.unticked_rows]
            return json.dumps({"found": len(self._ticked), "checked": len(self._ticked)})
        if "new Set(" in script:
            ids = ids_ticked_by(script)
            self.record(f"tick {','.join(ids)}")
            tickable = {w.want_id for w in current} - self.unticked_rows
            self._ticked = [i for i in ids if i in tickable]
            return json.dumps({"found": len(self._ticked), "checked": len(self._ticked)})
        if "AddDecklistForm" in script:
            self.record("click import")
            self._import()
            return SUBMITTED
        raise AssertionError(f"unknown act script: {script}")

    def submit(self, script: str) -> str:
        self.scripts.append(script)
        if (refusal := self._missing(script)) is not None:
            self.record("submit missing")
            raise BrowserError(refusal)
        if "WantsList_CreateWantsList" in script:
            self.record("submit create")
            if not self.create_fails:
                self.lists[NEW_LIST_ID] = self.fields["new name"]
        elif "#deleteSelected" in script:
            self.record(f"submit delete {len(self._ticked)}")
            assert self._current is not None
            self.wants[self._current] = [
                w for w in self.wants[self._current] if w.want_id not in self._ticked
            ]
        elif "ModifyForm" in script:
            self.record(f"submit edit {self.fields['amount']}")
            self._edit()
        else:
            raise AssertionError(f"unknown submit script: {script}")
        return "<html><body></body></html>"

    def _import(self) -> None:
        pending: list[SiteWant] = []
        for line in self.fields["import"].splitlines():
            quantity, name = line.split(" ", 1)
            if name in self.unknown:
                continue
            pending.append(
                SiteWant(
                    f"w{self._next_want}",
                    self.renamed.get(name, name),
                    self.quantities.get(name, int(quantity)),
                )
            )
            self._next_want += 1
        self._pending = pending
        self._lag_left = self.import_lag

    def _edit(self) -> None:
        if self.ignore_edits:
            return
        assert self._current is not None
        for want in self.wants[self._current]:
            if want.want_id not in self._ticked:
                continue
            want.quantity = int(self.fields["amount"])
            if "language" in self.fields:
                ids = [i for i in self.fields["language"].split(",") if i not in self.dropped_languages]
                shown = [LANGUAGE_OF_ID[i] for i in ids]
                want.languages = tuple(reversed(shown) if self.reverse_languages else shown)
                if self.shown_languages is not None:
                    want.languages = self.shown_languages
            if "condition" in self.fields:
                want.condition = CONDITION_OF_ID[self.fields["condition"]]
        self.fields.pop("amount", None)
        self.fields.pop("language", None)
        self.fields.pop("condition", None)
        foil = self.fields.pop("foil", "")
        self.foil_set.extend(foil for _ in self._ticked)


class Gaps:
    """The pacing seam: records each wait in the site's event list and answers `uniform` with 4.5."""

    def __init__(self, site: FakeSite) -> None:
        self.site = site
        self.waits: list[float] = []
        self.bounds: list[tuple[float, float]] = []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.site.record("sleep")

    def uniform(self, low: float, high: float) -> float:
        self.bounds.append((low, high))
        return 4.5


@dataclass(slots=True)
class Run:
    result: WantsSync
    site: FakeSite
    gaps: Gaps
    progress: list[WantsProgress]


def sync(
    site: FakeSite,
    wanted: list[WantedCard],
    *,
    languages: tuple[int, ...] = (),
    min_condition: Condition | None = None,
    foil: Foil = Foil.ANY,
    cancel: threading.Event | None = None,
    list_id: int | None = None,
) -> Run:
    gaps = Gaps(site)
    progress: list[WantsProgress] = []
    result = sync_wants(
        site,
        wanted,
        languages=languages,
        min_condition=min_condition,
        foil=foil,
        list_id=list_id,
        cancel=cancel,
        on_progress=progress.append,
        sleep=gaps.sleep,
        uniform=gaps.uniform,
        clock=lambda: 0.0,
    )
    return Run(result, site, gaps, progress)


def requests_of(site: FakeSite) -> list[str]:
    """The events that are page loads or submits or the ajax import: what the pacing counts."""
    return [e for e in site.events if e.startswith(("load", "submit", "click import"))]


def app_site(*wants: SiteWant) -> FakeSite:
    """A site whose account holds another list and the app's own list 7 with `wants`."""
    return FakeSite(
        lists={1: "MONTHLY WANTS", 7: APP_LIST_NAME},
        wants={1: [SiteWant("keep", "Keep Me", 4)], 7: list(wants)},
    )


def cards(*pairs: tuple[str, int]) -> list[WantedCard]:
    return [WantedCard(name, quantity) for name, quantity in pairs]


def summary(run: Run) -> list[tuple[str, int]]:
    return [(w.card, w.quantity) for w in run.result.imported]


class TestTheList:
    def test_the_list_named_for_the_app_is_found_and_not_created(self) -> None:
        site = app_site()

        run = sync(site, cards(("Sol Ring", 1)))

        assert run.result.list_id == 7
        assert "submit create" not in site.events
        assert site.lists == {1: "MONTHLY WANTS", 7: "Commander"}

    def test_a_missing_list_is_created_with_the_app_name_and_then_used(self) -> None:
        site = FakeSite(lists={1: "MONTHLY WANTS"}, wants={1: [SiteWant("keep", "Keep Me", 4)]})

        run = sync(site, cards(("Sol Ring", 1)))

        assert site.fields["new name"] == "Commander"
        assert site.lists == {1: "MONTHLY WANTS", 100: "Commander"}
        assert run.result.list_id == 100
        assert site.events[:6] == ["load ", "fill new name", "sleep", "submit create", "sleep", "load "]
        assert site.loaded[-3:] == [f"{LISTS_URL}/100", f"{LISTS_URL}/100/AddDeckList", f"{LISTS_URL}/100"]

    def test_a_list_that_is_not_there_after_creating_it_is_an_error(self) -> None:
        site = FakeSite(lists={1: "MONTHLY WANTS"}, create_fails=True)

        with pytest.raises(WantsError, match="not on the lists page after creating it"):
            sync(site, cards(("Sol Ring", 1)))

    def test_two_lists_of_the_name_are_not_guessed_between(self) -> None:
        site = FakeSite(lists={7: "Commander", 8: "Commander"})

        with pytest.raises(WantsError, match="lists 7, 8 are all named 'Commander'"):
            sync(site, cards(("Sol Ring", 1)))

        assert site.loaded == [LISTS_URL]

    def test_another_list_is_never_touched(self) -> None:
        site = app_site(SiteWant("old", "Old Card", 1))

        sync(site, cards(("Sol Ring", 1)), languages=(ENGLISH,), min_condition=Condition.NM)

        assert [w.card for w in site.wants[1]] == ["Keep Me"]
        assert not any(url.endswith(("/1", "/1/AddDeckList")) for url in site.loaded)

    def test_a_page_that_is_not_the_lists_page_stops_the_sync(self) -> None:
        site = app_site()
        site.pages_once[LISTS_URL] = ["<html><body><main><h1>Help</h1></main></body></html>"]

        with pytest.raises(PageError, match="wants lists page"):
            sync(site, cards(("Sol Ring", 1)))


class TestListHint:
    def test_a_list_id_that_is_the_apps_list_is_opened_without_the_lists_page(self) -> None:
        site = app_site(SiteWant("old", "Old Card", 1))

        run = sync(site, cards(("Sol Ring", 1)), list_id=7)

        assert LISTS_URL not in site.loaded
        assert site.loaded[0] == f"{LISTS_URL}/7"
        assert run.result.list_id == 7
        assert [w.card for w in site.wants[7]] == ["Sol Ring"]

    def test_a_list_that_is_already_right_takes_one_request_with_the_hint_and_two_without(self) -> None:
        wanted = cards(("Sol Ring", 1))
        hinted = app_site(SiteWant("w1", "Sol Ring", 1))
        plain = app_site(SiteWant("w1", "Sol Ring", 1))

        sync(hinted, wanted, list_id=7)
        sync(plain, wanted)

        assert requests_of(hinted) == ["load /7"]
        assert requests_of(plain) == ["load ", "load /7"]

    def test_a_list_id_of_another_list_falls_back_to_the_lists_page_and_changes_nothing_there(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        site = app_site(SiteWant("old", "Old Card", 1))

        with caplog.at_level(logging.INFO, logger="commander.cmwants"):
            run = sync(site, cards(("Sol Ring", 1)), list_id=1)

        assert site.loaded[:3] == [f"{LISTS_URL}/1", LISTS_URL, f"{LISTS_URL}/7"]
        assert site.events[:3] == ["load /1", "sleep", "load "]
        assert not any(url.startswith(f"{LISTS_URL}/1/") for url in site.loaded)
        assert [w.card for w in site.wants[1]] == ["Keep Me"]
        assert run.result.list_id == 7
        assert [w.card for w in site.wants[7]] == ["Sol Ring"]
        assert "list 1" in caplog.text

    def test_a_list_id_whose_page_shows_another_id_under_the_apps_name_falls_back(self) -> None:
        site = app_site(SiteWant("old", "Old Card", 1))
        site.pages_once[f"{LISTS_URL}/7"] = [list_page(8, APP_LIST_NAME, [SiteWant("x", "Other", 1)])]

        run = sync(site, cards(("Sol Ring", 1)), list_id=7)

        assert site.loaded[:3] == [f"{LISTS_URL}/7", LISTS_URL, f"{LISTS_URL}/7"]
        assert run.result.list_id == 7
        assert [w.card for w in site.wants[7]] == ["Sol Ring"]

    def test_a_list_id_that_is_no_list_falls_back_to_the_list_named_for_the_app(self) -> None:
        site = app_site()

        run = sync(site, cards(("Sol Ring", 1)), list_id=99)

        assert site.loaded[:3] == [f"{LISTS_URL}/99", LISTS_URL, f"{LISTS_URL}/7"]
        assert run.result.list_id == 7

    def test_a_list_id_that_is_no_list_falls_back_to_creating_the_list_when_there_is_none(self) -> None:
        site = FakeSite(lists={1: "MONTHLY WANTS"}, wants={1: [SiteWant("keep", "Keep Me", 4)]})

        run = sync(site, cards(("Sol Ring", 1)), list_id=99)

        assert site.lists == {1: "MONTHLY WANTS", 100: "Commander"}
        assert run.result.list_id == 100

    def test_a_page_that_is_not_a_list_page_at_all_falls_back(self) -> None:
        site = app_site()
        site.pages_once[f"{LISTS_URL}/7"] = ["<html><body><main><h1>Help</h1></main></body></html>"]

        run = sync(site, cards(("Sol Ring", 1)), list_id=7)

        assert site.loaded[:2] == [f"{LISTS_URL}/7", LISTS_URL]
        assert run.result.list_id == 7

    def test_a_browser_error_on_the_hinted_page_ends_the_sync_instead_of_falling_back(self) -> None:
        site = app_site()
        site.failures[f"{LISTS_URL}/7"] = SignedOut("the session has ended")

        with pytest.raises(SignedOut):
            sync(site, cards(("Sol Ring", 1)), list_id=7)

        assert site.loaded == [f"{LISTS_URL}/7"]

    def test_a_cancel_before_the_hinted_page_loads_nothing(self) -> None:
        site = app_site()
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(WantsCancelled):
            sync(site, cards(("Sol Ring", 1)), list_id=7, cancel=cancel)

        assert site.loaded == []


class TestEmptying:
    def test_a_list_with_wants_is_emptied_before_the_import(self) -> None:
        site = app_site(SiteWant("old1", "Old One", 1), SiteWant("old2", "Old Two", 2))

        run = sync(site, cards(("Sol Ring", 1)))

        assert [e for e in site.events if not e.startswith(("sleep", "load"))] == [
            "tick old1,old2",
            "submit delete 2",
            "fill import",
            "click import",
        ]
        assert summary(run) == [("Sol Ring", 1)]

    def test_a_list_that_already_holds_the_cards_as_asked_is_left_as_it_is(self) -> None:
        site = app_site(
            SiteWant("w1", "Sol Ring", 1, ("English",), "NM"),
            SiteWant("w2", "Arcane Signet", 2, ("English",), "NM"),
        )

        wanted = cards(("Sol Ring", 1), ("Arcane Signet", 2))

        run = sync(site, wanted, languages=(ENGLISH,), min_condition=Condition.NM)

        assert requests_of(site) == ["load ", "load /7"]
        assert [e for e in site.events if e.startswith(("tick", "fill", "click", "submit"))] == []
        assert summary(run) == [("Sol Ring", 1), ("Arcane Signet", 2)]
        assert (run.result.list_id, run.result.unmatched, run.result.quantity_mismatch) == (7, (), ())

    def test_a_card_wanted_twice_held_as_two_wants_is_left_as_it_is_too(self) -> None:
        site = app_site(SiteWant("w1", "Sol Ring", 1), SiteWant("w2", "Sol Ring", 1))

        sync(site, cards(("Sol Ring", 2)))

        assert requests_of(site) == ["load ", "load /7"]

    @pytest.mark.parametrize(
        ("held", "foil"),
        [
            (SiteWant("w1", "Sol Ring", 2, ("English",), "NM"), Foil.ANY),
            (SiteWant("w1", "Mana Crypt", 1, ("English",), "NM"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English",), "EX"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English", "Japanese"), "NM"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English",), "NM", expansion="Alpha"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English",), "NM", yes_no="Yes"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English",), "NM", price="2,00 €"), Foil.ANY),
            (SiteWant("w1", "Sol Ring", 1, ("English",), "NM"), Foil.NON_FOIL),
        ],
        ids=[
            "quantity",
            "other card",
            "condition",
            "languages",
            "expansion",
            "yes-no column",
            "buy price",
            "foil asked",
        ],
    )
    def test_a_list_that_differs_in_anything_is_made_again(self, held: SiteWant, foil: Foil) -> None:
        site = app_site(held)

        sync(site, cards(("Sol Ring", 1)), languages=(ENGLISH,), min_condition=Condition.NM, foil=foil)

        assert "submit delete 1" in site.events
        assert "click import" in site.events

    def test_with_no_language_or_condition_asked_a_list_that_has_them_is_made_again(self) -> None:
        site = app_site(SiteWant("w1", "Sol Ring", 1, ("English",), "NM"))

        sync(site, cards(("Sol Ring", 1)))

        assert "click import" in site.events

    def test_an_empty_list_is_not_emptied(self) -> None:
        site = app_site()

        sync(site, cards(("Sol Ring", 1)))

        assert not any(e.startswith(("tick all", "submit delete")) for e in site.events)

    def test_a_list_that_still_holds_wants_after_the_delete_is_an_error(self) -> None:
        site = app_site(SiteWant("old", "Old One", 1))
        # The delete submits but the reloaded list still shows the want.
        original = site.wants[7]
        site.pages_once[f"{LISTS_URL}/7"] = [
            list_page(7, APP_LIST_NAME, original),
            list_page(7, APP_LIST_NAME, original),
        ]

        with pytest.raises(WantsError, match="still holds 1 wants after deleting them"):
            sync(site, cards(("Sol Ring", 1)))

    def test_a_row_the_page_does_not_tick_stops_before_deleting(self) -> None:
        site = app_site(SiteWant("a", "A", 1), SiteWant("b", "B", 1))
        site.unticked_rows = {"b"}

        with pytest.raises(
            WantsError, match="deleting the wants that differ: expected 2 ticked, found 1 rows, 1 ticked"
        ):
            sync(site, cards(("Sol Ring", 1)))

    def test_an_empty_wanted_list_only_empties_the_list(self) -> None:
        site = app_site(SiteWant("old", "Old One", 1))

        run = sync(site, [], languages=(ENGLISH,), min_condition=Condition.NM)

        assert run.result == WantsSync(7, (), (), ())
        assert "fill import" not in site.events
        assert site.wants[7] == []


def held(want_id: str, card: str, quantity: int = 1) -> SiteWant:
    """A want exactly as the sync makes it for English, Near Mint."""
    return SiteWant(want_id, card, quantity, ("English",), "NM")


def sync_asked(site: FakeSite, wanted: list[WantedCard]) -> Run:
    return sync(site, wanted, languages=(ENGLISH,), min_condition=Condition.NM)


def ticks_and_deletes(site: FakeSite) -> list[str]:
    return [e for e in site.events if e.startswith(("tick", "submit delete"))]


class TestChangingOnlyWhatDiffers:
    def test_a_card_added_to_a_one_card_list_is_imported_and_edited_alone(self) -> None:
        site = app_site(held("k1", "Sol Ring"))

        run = sync_asked(site, cards(("Sol Ring", 1), ("Arcane Signet", 1)))

        assert not any(e.startswith(("tick all", "submit delete")) for e in site.events)
        assert site.fields["import"] == "1 Arcane Signet\n"
        assert [e for e in site.events if e.startswith("tick")] == ["tick w1"]
        assert requests_of(site) == [
            "load ",
            "load /7",
            "load /7/AddDeckList",
            "click import",
            "load /7",
            "submit edit 1",
            "load /7",
        ]
        assert [(w.want_id, w.card, w.languages, w.min_condition) for w in run.result.imported] == [
            ("k1", "Sol Ring", ("English",), Condition.NM),
            ("w1", "Arcane Signet", ("English",), Condition.NM),
        ]
        assert (run.result.unmatched, run.result.quantity_mismatch) == ((), ())

    def test_a_card_no_longer_wanted_is_deleted_alone_and_nothing_is_imported(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("old", "Old One"))

        run = sync_asked(site, cards(("Sol Ring", 1)))

        assert ticks_and_deletes(site) == ["tick old", "submit delete 1"]
        assert "click import" not in site.events
        assert requests_of(site) == ["load ", "load /7", "submit delete 1", "load /7"]
        assert summary(run) == [("Sol Ring", 1)]

    def test_a_kept_card_whose_quantity_changed_is_deleted_and_imported_again(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("k2", "Arcane Signet", 2))

        run = sync_asked(site, cards(("Sol Ring", 3), ("Arcane Signet", 2)))

        assert ticks_and_deletes(site) == ["tick k1", "submit delete 1", "tick w1"]
        assert site.fields["import"] == "3 Sol Ring\n"
        assert [(w.card, w.quantity) for w in site.wants[7]] == [("Arcane Signet", 2), ("Sol Ring", 3)]
        assert run.result.quantity_mismatch == ()

    def test_a_card_wanted_at_a_quantity_split_over_two_wants_is_kept(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("k2", "Sol Ring"), held("k3", "Arcane Signet"))

        sync_asked(site, cards(("Sol Ring", 2), ("Arcane Signet", 1)))

        assert requests_of(site) == ["load ", "load /7"]

    def test_wants_with_the_wrong_condition_language_or_expansion_are_made_again(self) -> None:
        site = app_site(
            held("a", "Card A"),
            SiteWant("b", "Card B", 1, ("English",), "EX"),
            SiteWant("c", "Card C", 1, ("English", "Japanese"), "NM"),
            SiteWant("d", "Card D", 1, ("English",), "NM", expansion="Alpha"),
        )

        run = sync_asked(site, cards(("Card A", 1), ("Card B", 1), ("Card C", 1), ("Card D", 1)))

        assert ticks_and_deletes(site)[:2] == ["tick b,c,d", "submit delete 3"]
        assert site.fields["import"] == "1 Card B\n1 Card C\n1 Card D\n"
        assert [e for e in site.events if e.startswith("tick")] == ["tick b,c,d", "tick w1,w2,w3"]
        assert [(w.want_id, w.card) for w in run.result.imported] == [
            ("a", "Card A"),
            ("w1", "Card B"),
            ("w2", "Card C"),
            ("w3", "Card D"),
        ]
        assert all(
            w.languages == ("English",) and w.min_condition is Condition.NM for w in run.result.imported
        )

    def test_new_wants_of_different_quantities_are_edited_by_group_and_kept_ones_not(self) -> None:
        site = app_site(held("k1", "Sol Ring"))

        sync_asked(site, cards(("Sol Ring", 1), ("Arcane Signet", 1), ("Counterspell", 2)))

        assert [e for e in site.events if e.startswith(("tick", "submit edit"))] == [
            "tick w1",
            "submit edit 1",
            "tick w2",
            "submit edit 2",
        ]

    def test_with_no_filter_asked_the_new_wants_are_not_edited(self) -> None:
        site = app_site(SiteWant("k1", "Sol Ring", 1))

        run = sync(site, cards(("Sol Ring", 1), ("Arcane Signet", 1)))

        assert not any(e.startswith(("tick", "submit")) for e in site.events)
        assert summary(run) == [("Sol Ring", 1), ("Arcane Signet", 1)]

    def test_a_foil_choice_keeps_nothing(self) -> None:
        site = app_site(held("k1", "Sol Ring"))

        sync(
            site,
            cards(("Sol Ring", 1)),
            languages=(ENGLISH,),
            min_condition=Condition.NM,
            foil=Foil.NON_FOIL,
        )

        assert ticks_and_deletes(site)[:2] == ["tick k1", "submit delete 1"]
        assert site.fields["import"] == "1 Sol Ring\n"

    def test_an_import_that_shows_late_is_waited_for_with_kept_wants_on_the_list(self) -> None:
        site = app_site(held("k1", "Sol Ring"))
        site.import_lag = 2

        run = sync_asked(site, cards(("Sol Ring", 1), ("Arcane Signet", 1)))

        assert [w.card for w in run.result.imported] == ["Sol Ring", "Arcane Signet"]
        # One read before the import form, three after it (two without the card, one with it) and the
        # read after the edit.
        assert site.loaded.count(f"{LISTS_URL}/7") == 5
        assert run.result.unmatched == ()

    def test_an_import_that_never_shows_is_warned_about_and_reported_unmatched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        site = app_site(held("k1", "Sol Ring"))
        site.import_lag = 99

        with caplog.at_level(logging.WARNING):
            run = sync_asked(site, cards(("Sol Ring", 1), ("Arcane Signet", 1)))

        assert summary(run) == [("Sol Ring", 1)]
        assert run.result.unmatched == ("Arcane Signet",)
        assert site.loaded.count(f"{LISTS_URL}/7") == 5
        assert "no new wants on the list 4 reads after the import" in caplog.text

    def test_a_delete_that_leaves_another_want_than_the_kept_ones_is_an_error(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("old", "Old One"))
        both = list(site.wants[7])
        site.pages_once[f"{LISTS_URL}/7"] = [
            list_page(7, APP_LIST_NAME, both),
            list_page(7, APP_LIST_NAME, both),
        ]

        with pytest.raises(WantsError, match="still holds 1 wants after deleting them"):
            sync_asked(site, cards(("Sol Ring", 1)))

        assert "click import" not in site.events

    def test_a_delete_that_takes_a_kept_want_too_is_an_error(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("old", "Old One"))
        site.pages_once[f"{LISTS_URL}/7"] = [
            list_page(7, APP_LIST_NAME, list(site.wants[7])),
            list_page(7, APP_LIST_NAME, []),
        ]

        with pytest.raises(WantsError, match="1 kept wants are gone after deleting the others"):
            sync_asked(site, cards(("Sol Ring", 1)))

    def test_a_row_the_page_cannot_tick_stops_before_deleting_the_others(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("old", "Old One"))
        site.unticked_rows = {"old"}

        with pytest.raises(WantsError, match="expected 1 ticked, found 0 rows, 0 ticked"):
            sync_asked(site, cards(("Sol Ring", 1)))

        assert not any(e.startswith("submit delete") for e in site.events)

    def test_a_cancel_after_the_delete_leaves_the_import_unsent(self) -> None:
        site = app_site(held("k1", "Sol Ring"), held("old", "Old One"))
        stop = threading.Event()
        site.on_event = lambda event: stop.set() if event.startswith("submit delete") else None

        with pytest.raises(WantsCancelled):
            sync(
                site,
                cards(("Sol Ring", 1), ("Arcane Signet", 1)),
                languages=(ENGLISH,),
                min_condition=Condition.NM,
                cancel=stop,
            )

        assert "fill import" not in site.events
        assert [w.want_id for w in site.wants[7]] == ["k1"]


class TestRefusals:
    def test_a_page_with_another_name_is_not_emptied(self) -> None:
        site = app_site(SiteWant("old", "Old One", 1))
        site.pages_once[f"{LISTS_URL}/7"] = [list_page(7, "MONTHLY WANTS", [SiteWant("old", "Old One", 1)])]

        with pytest.raises(WantsError, match="refusing to change wants list 7 'MONTHLY WANTS'"):
            sync(site, cards(("Sol Ring", 1)))

        assert not any(e.startswith(("tick", "submit")) for e in site.events)

    def test_a_page_with_another_id_is_not_emptied(self) -> None:
        site = app_site(SiteWant("old", "Old One", 1))
        probe = (FIXTURES / "cm_wants_list.html").read_text(encoding="utf-8")
        site.pages_once[f"{LISTS_URL}/7"] = [probe.replace("CMDR PROBE", APP_LIST_NAME)]

        with pytest.raises(
            WantsError, match="refusing to change wants list 34567890 'Commander': expected list 7"
        ):
            sync(site, cards(("Sol Ring", 1)))

        assert not any(e.startswith(("tick", "submit")) for e in site.events)

    def test_the_probe_page_under_its_own_name_is_refused_too(self) -> None:
        site = app_site(SiteWant("old", "Old One", 1))
        site.pages_once[f"{LISTS_URL}/7"] = [(FIXTURES / "cm_wants_list.html").read_text(encoding="utf-8")]

        with pytest.raises(WantsError, match="'CMDR PROBE'"):
            sync(site, cards(("Sol Ring", 1)))

    def test_a_missing_import_form_stops_the_sync_with_the_selector(self) -> None:
        site = app_site()
        site.missing = {"#AddDecklist"}

        with pytest.raises(WantsError, match="the import text: no element matches #AddDecklist"):
            sync(site, cards(("Sol Ring", 1)))

    def test_a_browser_error_ends_the_sync_with_that_error(self) -> None:
        site = app_site()
        site.failures[f"{LISTS_URL}/7/AddDeckList"] = SignedOut("the session has ended")

        with pytest.raises(SignedOut):
            sync(site, cards(("Sol Ring", 1)))


class TestImport:
    def test_the_text_is_one_quantity_and_name_per_line(self) -> None:
        site = app_site()

        sync(site, cards(("Sol Ring", 1), ("Fire // Ice", 2), ("Lim-Dûl's Vault", 1)))

        assert site.fields["import"] == "1 Sol Ring\n2 Fire // Ice\n1 Lim-Dûl's Vault\n"

    def test_a_card_wanted_twice_is_one_line_with_the_summed_quantity(self) -> None:
        assert wants_text(cards(("Sol Ring", 1), ("Arcane Signet", 2), ("sol ring", 2))) == (
            "3 Sol Ring\n2 Arcane Signet\n"
        )

    def test_the_import_text_is_the_toget_export_format(self) -> None:
        assert wants_text(cards(("Command Tower", 3))) == "3 Command Tower\n"

    def test_the_list_read_back_is_the_result(self) -> None:
        site = app_site()

        run = sync(site, cards(("Sol Ring", 1), ("Arcane Signet", 2)))

        assert run.result.imported == (
            Want("w1", "Sol Ring", 1, (), Condition.PO, plain=True),
            Want("w2", "Arcane Signet", 2, (), Condition.PO, plain=True),
        )
        assert run.result.unmatched == ()
        assert run.result.quantity_mismatch == ()

    def test_an_import_that_shows_late_is_waited_for_by_reading_again(self) -> None:
        site = app_site()
        site.import_lag = 2

        run = sync(site, cards(("Sol Ring", 1)))

        assert summary(run) == [("Sol Ring", 1)]
        # One read before the import form, then three reads after it: two empty, one with the card.
        assert site.loaded.count(f"{LISTS_URL}/7") == 4

    def test_an_import_that_never_shows_reports_every_card_unmatched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        site = app_site()
        site.import_lag = 99

        with caplog.at_level(logging.WARNING):
            run = sync(site, cards(("Sol Ring", 1), ("Arcane Signet", 1)))

        assert run.result.imported == ()
        assert run.result.unmatched == ("Sol Ring", "Arcane Signet")
        assert site.loaded.count(f"{LISTS_URL}/7") == 5
        assert "no new wants on the list 4 reads after the import" in caplog.text

    def test_a_script_that_does_not_click_the_import_is_an_error(self) -> None:
        site = app_site()
        site.missing = {'#AddDecklistForm button[type="submit"]'}

        with pytest.raises(WantsError, match="importing the cards: no element matches #AddDecklistForm"):
            sync(site, cards(("Sol Ring", 1)))


class TestReporting:
    def test_a_name_cardmarket_does_not_know_is_reported_not_fatal(self) -> None:
        site = app_site()
        site.unknown = {"Nonesuch Card"}

        run = sync(site, cards(("Sol Ring", 1), ("Nonesuch Card", 2)), languages=(ENGLISH,))

        assert run.result.unmatched == ("Nonesuch Card",)
        assert summary(run) == [("Sol Ring", 1)]

    def test_a_name_listed_without_its_accents_still_matches(self) -> None:
        site = app_site()
        site.renamed = {"Fíli and Kíli, Joyous": "Fili and Kili, Joyous"}

        run = sync(site, cards(("Fíli and Kíli, Joyous", 1)))

        assert run.result.unmatched == ()
        assert summary(run) == [("Fili and Kili, Joyous", 1)]

    def test_a_name_listed_with_another_face_separator_still_matches(self) -> None:
        site = app_site()
        site.renamed = {"Fire // Ice": "Fire / Ice"}

        run = sync(site, cards(("Fire // Ice", 1)))

        assert run.result.unmatched == ()

    def test_a_quantity_that_differs_is_reported_with_both_numbers(self) -> None:
        site = app_site()
        site.quantities = {"Sol Ring": 2}

        run = sync(site, cards(("Sol Ring", 3), ("Arcane Signet", 1)))

        assert run.result.quantity_mismatch == (QuantityMismatch("Sol Ring", 3, 2),)
        assert run.result.unmatched == ()

    def test_the_unmatched_are_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        site = app_site()
        site.unknown = {"Nonesuch Card"}

        with caplog.at_level(logging.WARNING):
            sync(site, cards(("Nonesuch Card", 1), ("Sol Ring", 1)))

        assert "did not match 1 wanted cards: Nonesuch Card" in caplog.text


class TestFilters:
    def test_wants_are_edited_one_quantity_group_at_a_time(self) -> None:
        site = app_site()

        run = sync(
            site,
            cards(("Sol Ring", 1), ("Arcane Signet", 1), ("Counterspell", 2), ("Command Tower", 3)),
            languages=(ENGLISH,),
            min_condition=Condition.NM,
        )

        edits = [
            e
            for e in site.events
            if e.startswith(("tick ", "fill amount", "fill language", "fill condition", "submit edit"))
        ]
        assert edits == [
            "tick w1,w2",
            "fill amount",
            "fill language",
            "fill condition",
            "submit edit 1",
            "tick w3",
            "fill amount",
            "fill language",
            "fill condition",
            "submit edit 2",
            "tick w4",
            "fill amount",
            "fill language",
            "fill condition",
            "submit edit 3",
        ]
        assert [(w.card, w.quantity, w.languages, w.min_condition) for w in run.result.imported] == [
            ("Sol Ring", 1, ("English",), Condition.NM),
            ("Arcane Signet", 1, ("English",), Condition.NM),
            ("Counterspell", 2, ("English",), Condition.NM),
            ("Command Tower", 3, ("English",), Condition.NM),
        ]
        assert run.result.quantity_mismatch == ()

    def test_the_amount_field_is_set_to_each_groups_own_quantity(self) -> None:
        site = app_site()

        sync(
            site,
            cards(("A", 1), ("B", 2), ("C", 3)),
            languages=(ENGLISH,),
            min_condition=Condition.EX,
        )

        amounts = [value_set_by(s) for s in site.scripts if '"#amount"' in s]
        conditions = [value_set_by(s) for s in site.scripts if '"#minCondition"' in s]
        languages = [options_selected_by(s) for s in site.scripts if "idLanguages" in s]
        assert amounts == ["1", "2", "3"]
        assert conditions == ["3", "3", "3"]
        assert languages == [["1"], ["1"], ["1"]]

    def test_japanese_alone_selects_option_seven_only(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1)), languages=(JAPANESE,))

        assert [options_selected_by(s) for s in site.scripts if "idLanguages" in s] == [["7"]]
        assert [w.languages for w in run.result.imported] == [("Japanese",)]

    def test_both_selects_the_english_and_the_japanese_option_in_every_group(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1), ("B", 2)), languages=(ENGLISH, JAPANESE))

        assert [options_selected_by(s) for s in site.scripts if "idLanguages" in s] == [
            ["1", "7"],
            ["1", "7"],
        ]
        assert [w.languages for w in run.result.imported] == [("English", "Japanese")] * 2

    def test_a_want_showing_both_languages_in_the_other_order_passes_the_check(self) -> None:
        site = app_site()
        site.reverse_languages = True

        run = sync(site, cards(("A", 1)), languages=(ENGLISH, JAPANESE))

        assert [w.languages for w in run.result.imported] == [("Japanese", "English")]

    def test_a_want_that_kept_only_one_of_both_languages_fails_the_check(self) -> None:
        site = app_site()
        site.dropped_languages = {"7"}

        with pytest.raises(WantsError, match="2 wants lack the language or condition set: A, B"):
            sync(site, cards(("A", 1), ("B", 1)), languages=(ENGLISH, JAPANESE))

    def test_a_want_with_an_extra_language_fails_the_check_for_one_language(self) -> None:
        site = app_site()
        site.shown_languages = ("English", "Japanese")

        with pytest.raises(WantsError, match="1 wants lack the language or condition set: A"):
            sync(site, cards(("A", 1)), languages=(ENGLISH,))

    def test_a_language_the_sync_cannot_set_is_refused_before_anything_loads(self) -> None:
        site = app_site()

        with pytest.raises(ValueError, match="cannot set the languages"):
            sync(site, cards(("A", 1)), languages=(99,))
        with pytest.raises(ValueError, match="cannot set the languages"):
            sync(site, cards(("A", 1)), languages=(ENGLISH, ENGLISH))

        assert site.loaded == []

    def test_the_list_is_reloaded_between_two_groups_and_once_at_the_end(self) -> None:
        site = app_site()

        sync(site, cards(("A", 1), ("B", 2)), languages=(ENGLISH,))

        assert [e for e in site.events if e.startswith(("load", "submit edit"))] == [
            "load ",
            "load /7",
            "load /7/AddDeckList",
            "load /7",
            "submit edit 1",
            "load /7",
            "submit edit 2",
            "load /7",
        ]

    def test_without_a_language_and_condition_nothing_is_edited(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1), ("B", 2)))

        assert not any(e.startswith(("tick", "submit edit", "fill amount")) for e in site.events)
        assert requests_of(site) == [
            "load ",
            "load /7",
            "load /7/AddDeckList",
            "click import",
            "load /7",
        ]
        assert [w.languages for w in run.result.imported] == [(), ()]

    def test_a_condition_alone_leaves_the_language_field_alone(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1)), min_condition=Condition.LP)

        assert "fill language" not in site.events
        assert "fill condition" in site.events
        assert [(w.languages, w.min_condition) for w in run.result.imported] == [((), Condition.LP)]

    def test_a_language_alone_leaves_the_condition_field_alone(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1)), languages=(ENGLISH,))

        assert "fill condition" not in site.events
        assert [(w.languages, w.min_condition) for w in run.result.imported] == [(("English",), Condition.PO)]

    @pytest.mark.parametrize(("foil", "code"), [(Foil.NON_FOIL, "N"), (Foil.FOIL, "Y")])
    def test_a_foil_choice_is_set_on_every_want_in_the_bulk_edit(self, foil: Foil, code: str) -> None:
        site = app_site()

        sync(site, cards(("A", 1), ("B", 1), ("C", 2)), foil=foil)

        assert [value_set_by(s) for s in site.scripts if '"#isFoil"' in s] == [code, code]
        assert site.foil_set == [code, code, code]
        # A foil choice alone is a filter: the language and condition fields stay as the import left them.
        assert "fill language" not in site.events
        assert "fill condition" not in site.events

    def test_any_foil_leaves_the_blank_field_alone(self) -> None:
        site = app_site()

        sync(site, cards(("A", 1)), languages=(ENGLISH,), min_condition=Condition.NM, foil=Foil.ANY)

        assert "fill foil" not in site.events
        assert site.foil_set == [""]

    def test_any_foil_alone_edits_nothing(self) -> None:
        site = app_site()

        sync(site, cards(("A", 1)), foil=Foil.ANY)

        assert not any(e.startswith(("tick", "submit edit")) for e in site.events)

    def test_a_page_without_the_foil_field_stops_before_the_submit(self) -> None:
        site = app_site()
        site.missing = {"#isFoil"}

        with pytest.raises(WantsError, match="editing the 1 wants of quantity 1: no element matches #isFoil"):
            sync(site, cards(("A", 1)), foil=Foil.NON_FOIL)

        assert "submit edit 1" not in site.events

    def test_wants_that_did_not_take_the_filters_are_an_error(self) -> None:
        site = app_site()
        site.ignore_edits = True

        with pytest.raises(WantsError, match="2 wants lack the language or condition set: A, B"):
            sync(site, cards(("A", 1), ("B", 1)), languages=(ENGLISH,), min_condition=Condition.NM)

    def test_the_failure_names_five_wants_and_counts_the_rest(self) -> None:
        site = app_site()
        site.ignore_edits = True

        with pytest.raises(WantsError, match=r"7 wants lack .*: A, B, C, D, E and 2 more"):
            sync(
                site,
                cards(*[(name, 1) for name in "ABCDEFG"]),
                languages=(ENGLISH,),
                min_condition=Condition.NM,
            )

    def test_a_field_the_page_cannot_set_stops_before_the_submit(self) -> None:
        site = app_site()
        site.missing = {"#minCondition"}

        with pytest.raises(
            WantsError, match="editing the 1 wants of quantity 1: no element matches #minCondition"
        ):
            sync(site, cards(("A", 1)), languages=(ENGLISH,), min_condition=Condition.NM)

        assert "submit edit 1" not in site.events

    def test_a_page_that_hides_a_row_stops_the_edit_before_the_submit(self) -> None:
        site = app_site()
        site.unticked_rows = {"w2"}

        with pytest.raises(WantsError, match="editing the 2 wants of quantity 1: expected 2 ticked"):
            sync(site, cards(("A", 1), ("B", 1)), languages=(ENGLISH,))

        assert "submit edit 1" not in site.events


class TestPacing:
    def test_every_request_but_the_first_waits_four_to_six_seconds(self) -> None:
        site = app_site()

        run = sync(
            site, cards(("A", 1), ("B", 2), ("C", 3)), languages=(ENGLISH,), min_condition=Condition.NM
        )

        # Lists, list, import form, import click, read back, then three edits (a submit each, and a reload
        # before all but the first), and the final read: 5 + (1 + 2 + 2) + 1 = 11 requests.
        assert len(requests_of(site)) == 11
        assert run.gaps.waits == [4.5] * 10
        assert set(run.gaps.bounds) == {(4.0, 6.0)}

    def test_the_gap_comes_before_the_request_it_precedes(self) -> None:
        site = app_site(SiteWant("old", "Old", 1))

        sync(site, cards(("A", 1)))

        assert site.events == [
            "load ",
            "sleep",
            "load /7",
            "tick old",
            "sleep",
            "submit delete 1",
            "sleep",
            "load /7",
            "sleep",
            "load /7/AddDeckList",
            "fill import",
            "sleep",
            "click import",
            "sleep",
            "load /7",
        ]

    def test_acts_that_only_fill_a_field_are_not_paced(self) -> None:
        site = app_site()

        sync(site, cards(("A", 1)), languages=(ENGLISH,))

        for index, event in enumerate(site.events):
            if event.startswith(("fill", "tick")):
                assert site.events[index - 1] != "sleep", event


class TestCancel:
    def test_a_cancel_before_the_start_loads_nothing(self) -> None:
        site = app_site()
        stop = threading.Event()
        stop.set()

        with pytest.raises(WantsCancelled):
            sync(site, cards(("A", 1)), cancel=stop)

        assert site.events == []

    def test_a_cancel_during_the_first_page_stops_before_the_gap(self) -> None:
        site = app_site()
        stop = threading.Event()
        site.on_event = lambda event: stop.set() if event == "load " else None

        with pytest.raises(WantsCancelled):
            sync(site, cards(("A", 1)), cancel=stop)

        assert site.events == ["load "]

    def test_a_cancel_during_the_gap_stops_before_the_next_request(self) -> None:
        site = app_site()
        stop = threading.Event()
        site.on_event = lambda event: stop.set() if event == "sleep" else None

        with pytest.raises(WantsCancelled):
            sync(site, cards(("A", 1)), cancel=stop)

        assert site.events == ["load ", "sleep"]

    def test_a_cancel_between_two_edits_leaves_the_second_unsent(self) -> None:
        site = app_site()
        stop = threading.Event()
        site.on_event = lambda event: stop.set() if event == "submit edit 1" else None

        with pytest.raises(WantsCancelled):
            sync(site, cards(("A", 1), ("B", 2)), languages=(ENGLISH,), cancel=stop)

        assert "submit edit 2" not in site.events
        assert [w.languages for w in site.wants[7]] == [("English",), ()]

    def test_a_cancel_after_the_emptying_leaves_the_import_unsent(self) -> None:
        site = app_site(SiteWant("old", "Old", 1))
        stop = threading.Event()
        site.on_event = lambda event: stop.set() if event.startswith("submit delete") else None

        with pytest.raises(WantsCancelled):
            sync(site, cards(("A", 1)), cancel=stop)

        assert "fill import" not in site.events


class TestProgress:
    def test_each_request_is_announced_before_it_is_made(self) -> None:
        site = app_site()

        run = sync(site, cards(("A", 1)))

        assert run.progress == [
            WantsProgress(WantsStep.LISTS, "wants lists"),
            WantsProgress(WantsStep.LISTS, "list 7"),
            WantsProgress(WantsStep.IMPORT, "import form"),
            WantsProgress(WantsStep.IMPORT, "1 cards"),
            WantsProgress(WantsStep.IMPORT, "list 7"),
        ]

    def test_the_emptying_and_the_edits_have_their_own_steps(self) -> None:
        site = app_site(SiteWant("old", "Old", 1))

        run = sync(site, cards(("A", 1)), languages=(ENGLISH,))

        assert [p.step for p in run.progress] == [
            WantsStep.LISTS,
            WantsStep.LISTS,
            WantsStep.EMPTY,
            WantsStep.EMPTY,
            WantsStep.IMPORT,
            WantsStep.IMPORT,
            WantsStep.IMPORT,
            WantsStep.EDIT,
            WantsStep.VERIFY,
        ]
