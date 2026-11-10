"""Keeps the app's own wants list on Cardmarket equal to the cards to buy (ADR 0067).

`sync_wants` finds or creates the list named `APP_LIST_NAME` and changes only what differs. The wants of a
wanted card are kept when they are all exactly as asked (the asked language and condition, no other limit)
and add up to the wanted quantity; every other want is deleted, and only the wanted cards without kept wants
are imported, as the text `cmdr toget --cardmarket` prints. The owner's language and minimum condition are
set on the imported wants alone, one quantity group at a time, because the bulk form overwrites each ticked
want's quantity with its one amount field. A list that is already right takes two requests. The list is read
back to check the result. It only ever acts on the list it found or created, and checks that list's id and
name on the page before deleting from it. It pages like the scan: one request at a time, 4 to 6 seconds
apart. A page that is not the expected one stops it with a `PageError` or `BrowserError`.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from commander.cmforms import (
    OK,
    read_ticks,
    select_options_script,
    set_field_script,
    submit_script,
    tick_all_wants_script,
    tick_wants_script,
)
from commander.cmpages import (
    Condition,
    PageError,
    Want,
    WantsList,
    WantsListEntry,
    read_wants_list,
    read_wants_lists,
)
from commander.cmscan import ENGLISH, JAPANESE, MAX_GAP_S, MIN_GAP_S, Foil
from commander.cmsession import SUBMITTED, pacer_for
from commander.sellersolve import card_key, merged_wants

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from commander.cmsession import FormBrowser, RequestPacer
    from commander.sellersolve import WantedCard

log = logging.getLogger(__name__)

# Letters, digits, spaces and hyphens, at most 30: what the create form accepts.
APP_LIST_NAME = "Commander"
WANTS_URL = "https://www.cardmarket.com/en/Magic/Wants"
# The import runs in the page without loading one; the list is read again until it holds the wants.
IMPORT_READS = 4

_NEW_LIST_NAME = "#SWWantsListName"
_NEW_LIST_SUBMIT = 'form[action$="WantsList_CreateWantsList"] [type="submit"]'
_IMPORT_TEXT = "#AddDecklist"
_IMPORT_SUBMIT = '#AddDecklistForm button[type="submit"]'
_DELETE_SELECTED = "#deleteSelected"
_EDIT_AMOUNT = "#amount"
_EDIT_LANGUAGE = 'select[name="idLanguages[]"]'
_EDIT_CONDITION = "#minCondition"
_EDIT_FOIL = "#isFoil"
_EDIT_SUBMIT = 'input[type="submit"][form="ModifyForm"]'
# The names the list page shows for the languages the sync can set.
_LANGUAGE_LABELS = {ENGLISH: "English", JAPANESE: "Japanese"}
# How many wants a failure message names.
_NAMED = 5


class WantsError(PageError):
    """The wants list is not in a state the sync can work with; the message says what was found."""


class WantsCancelled(Exception):  # noqa: N818 - control flow for the caller, not an error
    """The sync was cancelled between two steps; the list is left as far as it got."""


class WantsStep(Enum):
    LISTS = "lists"
    EMPTY = "empty"
    IMPORT = "import"
    EDIT = "edit"
    VERIFY = "verify"


@dataclass(slots=True, frozen=True)
class WantsProgress:
    """The step about to run and what it works on."""

    step: WantsStep
    detail: str


@dataclass(slots=True, frozen=True)
class QuantityMismatch:
    """A wanted card whose wants on the list add up to another quantity."""

    card: str
    wanted: int
    got: int


@dataclass(slots=True, frozen=True)
class WantsSync:
    list_id: int
    # The list as read back at the end.
    imported: tuple[Want, ...]
    # Wanted cards, by the name the want gave, that no want on the list matches (Cardmarket lacks them).
    unmatched: tuple[str, ...]
    quantity_mismatch: tuple[QuantityMismatch, ...]


def wants_text(wanted: Sequence[WantedCard]) -> str:
    """The cards as "<quantity> <name>" lines, a card wanted twice summed, in the order first wanted."""
    return "".join(f"{want.quantity} {want.name}\n" for want in merged_wants(wanted).values())


def _list_url(list_id: int) -> str:
    return f"{WANTS_URL}/{list_id}"


class _Sync:
    def __init__(
        self,
        browser: FormBrowser,
        wanted: Sequence[WantedCard],
        *,
        languages: tuple[int, ...],
        min_condition: Condition | None,
        foil: Foil,
        cancel: threading.Event,
        on_progress: Callable[[WantsProgress], None],
        sleep: Callable[[float], object],
        uniform: Callable[[float, float], float],
        pacer: RequestPacer,
    ) -> None:
        unknown = [language for language in languages if language not in _LANGUAGE_LABELS]
        if unknown or len(set(languages)) != len(languages):
            raise ValueError(f"the wants sync cannot set the languages {languages}")
        self._browser = browser
        self._wanted = merged_wants(wanted)
        self._languages = languages
        self._min_condition = min_condition
        self._foil = foil
        self._cancel = cancel
        self._on_progress = on_progress
        self._sleep = sleep
        self._uniform = uniform
        # The last request's end, shared with the scan that follows on the same browser.
        self._pacer = pacer

    def run(self) -> WantsSync:
        list_id = self._find_or_create_list()
        current = self._open_list(list_id, WantsStep.LISTS)
        if not self._wanted:
            if current.wants:
                self._empty(current)
            return WantsSync(list_id, (), (), ())
        kept = self._kept(current)
        kept_ids = {want.want_id for want in kept}
        stale = [want for want in current.wants if want.want_id not in kept_ids]
        kept_cards = {card_key(want.card) for want in kept}
        missing = [want for key, want in self._wanted.items() if key not in kept_cards]
        log.info(
            "the wants list: keeping %d wants, deleting %d, importing %d cards",
            len(kept),
            len(stale),
            len(missing),
        )
        if stale:
            current = self._delete(current, stale, kept_ids)
        if missing:
            current = self._import(list_id, missing, kept_ids)
            current = self._edit(current, kept_ids)
        self._check_filters(current)
        return self._report(current)

    # --- pacing ---------------------------------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise WantsCancelled

    def _begin_request(self, step: WantsStep, detail: str) -> None:
        """Before every request: stop on cancel, wait out the gap since the last request, tell the caller."""
        self._check_cancel()
        self._pacer.wait(lambda: self._uniform(MIN_GAP_S, MAX_GAP_S), self._sleep)
        self._check_cancel()
        self._on_progress(WantsProgress(step, detail))

    def _load(self, url: str, step: WantsStep, detail: str) -> str:
        self._begin_request(step, detail)
        log.debug("loading %s", url)
        try:
            return self._browser.load(url)
        finally:
            self._pacer.mark()

    def _submit(self, script: str, step: WantsStep, detail: str) -> None:
        """Click a submit that loads a page; the page it answers is not read, the next step loads its own."""
        self._begin_request(step, detail)
        try:
            self._browser.submit(script)
        finally:
            self._pacer.mark()

    def _act(self, script: str, what: str) -> None:
        """A script on the current page that changes a field and sends nothing; it must answer `OK`."""
        answer = self._browser.act(script)
        if answer != OK:
            raise WantsError(f"{what}: {answer}")

    def _tick(self, script: str, expected: int, what: str) -> None:
        answer = self._browser.act(script)
        ticks = read_ticks(answer)
        if ticks is None:
            raise WantsError(f"{what}: {answer}")
        found, checked = ticks
        if found != expected or checked != expected:
            raise WantsError(f"{what}: expected {expected} ticked, found {found} rows, {checked} ticked")

    # --- the list -------------------------------------------------------------------------------------

    def _find_or_create_list(self) -> int:
        page = self._load(WANTS_URL, WantsStep.LISTS, "wants lists")
        found = self._named_list(read_wants_lists(page))
        if found is not None:
            return found.list_id
        log.info("creating the wants list %r", APP_LIST_NAME)
        self._act(set_field_script(_NEW_LIST_NAME, APP_LIST_NAME), "the new list's name")
        self._submit(submit_script(_NEW_LIST_SUBMIT), WantsStep.LISTS, f"creating {APP_LIST_NAME}")
        # The page a creation answers with is not relied on: the lists page says whether it is there.
        created = self._named_list(read_wants_lists(self._load(WANTS_URL, WantsStep.LISTS, "wants lists")))
        if created is None:
            raise WantsError(f"the list {APP_LIST_NAME!r} is not on the lists page after creating it")
        return created.list_id

    @staticmethod
    def _named_list(lists: Sequence[WantsListEntry]) -> WantsListEntry | None:
        named = [entry for entry in lists if entry.name == APP_LIST_NAME]
        if len(named) > 1:
            ids = ", ".join(str(entry.list_id) for entry in named)
            raise WantsError(f"lists {ids} are all named {APP_LIST_NAME!r}; not guessing which to change")
        return named[0] if named else None

    def _open_list(self, list_id: int, step: WantsStep) -> WantsList:
        """Load the list's page and read it; refuses a page that is not the app's list."""
        page = read_wants_list(self._load(_list_url(list_id), step, f"list {list_id}"))
        if page.list_id != list_id or page.name != APP_LIST_NAME:
            raise WantsError(
                f"refusing to change wants list {page.list_id} {page.name!r}: expected list {list_id} "
                f"{APP_LIST_NAME!r}"
            )
        return page

    def _empty(self, current: WantsList) -> None:
        count = len(current.wants)
        self._tick(tick_all_wants_script(), count, "emptying the list")
        self._submit(submit_script(_DELETE_SELECTED), WantsStep.EMPTY, f"deleting {count} wants")
        left = self._open_list(current.list_id, WantsStep.EMPTY).wants
        if left:
            raise WantsError(f"the list still holds {len(left)} wants after deleting them")

    def _delete(self, current: WantsList, stale: Sequence[Want], kept_ids: set[str]) -> WantsList:
        """Delete exactly the `stale` wants and read the list again; it must then hold the kept wants."""
        ids = [want.want_id for want in stale]
        self._tick(tick_wants_script(ids), len(ids), "deleting the wants that differ")
        self._submit(submit_script(_DELETE_SELECTED), WantsStep.EMPTY, f"deleting {len(ids)} wants")
        left = self._open_list(current.list_id, WantsStep.EMPTY)
        left_ids = {want.want_id for want in left.wants}
        if extra := left_ids - kept_ids:
            raise WantsError(f"the list still holds {len(extra)} wants after deleting them")
        if gone := kept_ids - left_ids:
            raise WantsError(f"{len(gone)} kept wants are gone after deleting the others")
        return left

    def _kept(self, current: WantsList) -> tuple[Want, ...]:
        """The wants to leave alone: those of each wanted card whose wants are all exactly as asked (the
        language and condition asked for, the import's any and Poor when none is, and no other limit) and add
        up to the wanted quantity. A foil choice keeps nothing: which yes/no column is foil is not read."""
        if self._foil is not Foil.ANY:
            return ()
        by_card: dict[str, list[Want]] = defaultdict(list)
        for want in current.wants:
            by_card[card_key(want.card)].append(want)
        kept_keys = {
            key
            for key, wants in by_card.items()
            if key in self._wanted
            and sum(want.quantity for want in wants) == self._wanted[key].quantity
            and all(self._exactly_as_asked(want) for want in wants)
        }
        return tuple(want for want in current.wants if card_key(want.card) in kept_keys)

    def _exactly_as_asked(self, want: Want) -> bool:
        condition = self._min_condition if self._min_condition is not None else Condition.PO
        languages = sorted(_LANGUAGE_LABELS[language] for language in self._languages)
        return want.plain and want.min_condition is condition and sorted(want.languages) == languages

    # --- the import -----------------------------------------------------------------------------------

    def _import(self, list_id: int, cards: Sequence[WantedCard], before_ids: set[str]) -> WantsList:
        """Import `cards` into the list, which holds the wants `before_ids`, and read it back."""
        self._load(f"{_list_url(list_id)}/AddDeckList", WantsStep.IMPORT, "import form")
        self._act(set_field_script(_IMPORT_TEXT, wants_text(cards)), "the import text")
        self._begin_request(WantsStep.IMPORT, f"{len(cards)} cards")
        try:
            answer = self._browser.act(submit_script(_IMPORT_SUBMIT))
        finally:
            self._pacer.mark()
        if answer != SUBMITTED:
            raise WantsError(f"importing the cards: {answer}")
        return self._read_imported(list_id, before_ids)

    def _read_imported(self, list_id: int, before_ids: set[str]) -> WantsList:
        """The list once the import shows on it. The import is an ajax post with no page to wait for, so the
        list is read again, each time a gap later, until it holds a want it did not hold before."""
        current = self._open_list(list_id, WantsStep.IMPORT)
        for _ in range(IMPORT_READS - 1):
            if _new_wants(current, before_ids):
                return current
            log.info("the import has not shown on the list yet")
            current = self._open_list(list_id, WantsStep.IMPORT)
        if not _new_wants(current, before_ids):
            log.warning("no new wants on the list %d reads after the import", IMPORT_READS)
        return current

    # --- the language and condition ---------------------------------------------------------------------

    def _edit(self, current: WantsList, kept_ids: set[str]) -> WantsList:
        """Set the language and condition on the wants that are not kept, one quantity group a bulk edit,
        and read the list again. Nothing is sent when no filter is asked."""
        fresh = _new_wants(current, kept_ids)
        if not fresh or not self._filters_asked():
            return current
        groups: dict[int, list[str]] = defaultdict(list)
        for want in fresh:
            groups[want.quantity].append(want.want_id)
        list_id = current.list_id
        for index, quantity in enumerate(sorted(groups)):
            if index > 0:
                self._open_list(list_id, WantsStep.EDIT)
            ids = groups[quantity]
            what = f"editing the {len(ids)} wants of quantity {quantity}"
            self._tick(tick_wants_script(ids), len(ids), what)
            self._act(set_field_script(_EDIT_AMOUNT, str(quantity)), what)
            if self._languages:
                chosen = [str(language) for language in self._languages]
                self._act(select_options_script(_EDIT_LANGUAGE, chosen), what)
            if self._min_condition is not None:
                self._act(set_field_script(_EDIT_CONDITION, str(_condition_id(self._min_condition))), what)
            if self._foil is not Foil.ANY:
                self._act(set_field_script(_EDIT_FOIL, self._foil.code), what)
            self._submit(submit_script(_EDIT_SUBMIT), WantsStep.EDIT, f"quantity {quantity}")
        return self._open_list(list_id, WantsStep.VERIFY)

    def _filters_asked(self) -> bool:
        return bool(self._languages) or self._min_condition is not None or self._foil is not Foil.ANY

    def _check_filters(self, current: WantsList) -> None:
        """Every want must carry the language and condition asked for; a list that does not would hide the
        sellers' stock from the scan. The foil choice is not read back: the list shows it in cells whose
        markup is not confirmed; setting the field checks that it kept the value."""
        wrong = [want for want in current.wants if not self._has_filters(want)]
        if wrong:
            names = ", ".join(want.card for want in wrong[:_NAMED])
            more = f" and {len(wrong) - _NAMED} more" if len(wrong) > _NAMED else ""
            raise WantsError(f"{len(wrong)} wants lack the language or condition set: {names}{more}")

    def _has_filters(self, want: Want) -> bool:
        if self._min_condition is not None and want.min_condition is not self._min_condition:
            return False
        if not self._languages:
            return True
        return sorted(want.languages) == sorted(_LANGUAGE_LABELS[language] for language in self._languages)

    # --- the result -----------------------------------------------------------------------------------

    def _report(self, current: WantsList) -> WantsSync:
        got: dict[str, int] = defaultdict(int)
        for want in current.wants:
            got[card_key(want.card)] += want.quantity
        unmatched: list[str] = []
        mismatched: list[QuantityMismatch] = []
        for key, asked in self._wanted.items():
            if key not in got:
                unmatched.append(asked.name)
            elif got[key] != asked.quantity:
                mismatched.append(QuantityMismatch(asked.name, asked.quantity, got[key]))
        if unmatched:
            log.warning("Cardmarket did not match %d wanted cards: %s", len(unmatched), ", ".join(unmatched))
        return WantsSync(current.list_id, current.wants, tuple(unmatched), tuple(mismatched))


def _new_wants(current: WantsList, before_ids: set[str]) -> tuple[Want, ...]:
    return tuple(want for want in current.wants if want.want_id not in before_ids)


def _condition_id(condition: Condition) -> int:
    """Cardmarket numbers the conditions from 1 (Mint) to 7 (Poor)."""
    return list(Condition).index(condition) + 1


def sync_wants(
    browser: FormBrowser,
    wanted: Sequence[WantedCard],
    *,
    languages: tuple[int, ...],
    min_condition: Condition | None,
    foil: Foil = Foil.ANY,
    cancel: threading.Event | None = None,
    on_progress: Callable[[WantsProgress], None] | None = None,
    sleep: Callable[[float], object] | None = None,
    uniform: Callable[[float, float], float] = random.uniform,
    clock: Callable[[], float] = time.monotonic,
) -> WantsSync:
    """Make the app's wants list hold exactly `wanted`, at `languages` (Cardmarket's ids, none for any),
    `min_condition` (None for the import's Poor) and `foil` (any leaves the import's blank field), when any
    is given, and read it back.

    Raises `WantsCancelled` when `cancel` is set between two steps, `WantsError` (a `PageError`) when the list
    or a page is not as expected, and the browser's own errors. `sleep` waits out the gap between requests;
    the default waits on `cancel`, so a cancel ends the wait too. The gap is counted from the browser's last
    request, whichever step made it (a scan before the sync); `clock` is used only for a browser that keeps
    none.
    """
    stop = cancel if cancel is not None else threading.Event()
    return _Sync(
        browser,
        wanted,
        languages=languages,
        min_condition=min_condition,
        foil=foil,
        cancel=stop,
        on_progress=on_progress if on_progress is not None else _ignore,
        sleep=sleep if sleep is not None else stop.wait,
        uniform=uniform,
        pacer=pacer_for(browser, clock),
    ).run()


def _ignore(_: WantsProgress) -> None:
    return
