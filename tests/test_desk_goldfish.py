"""The Goldfish tab's bridge: the report, a queued swap's headline figures, the cache that keeps them and the
finishers setting (ADR 0054).

The fixture deck is Magda with a library of 8 cards: six Mountains, Battered Golem and Disruptor Flute, so a
hand and the first draw show every card and each card does the same for mana in every game.
"""

import shutil
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from commander import desk as desk_module
from commander.desk import Desk, DeskSettings
from commander.goldfish import POLICY_VERSION
from commander.models import Deck
from commander.store import Workspace
from conftest import PASSWORD, FakeArchidekt, Json, build_card_db
from test_desk import DECK, deferred_desk, desk, fake, items, make_desk, probing, profile_file, settings
from test_desk_account import Env
from test_goldfish_load import OVERRUN_TAG

__all__ = ["desk", "fake", "settings"]  # fixtures shared with test_desk


class Plays:
    """Counts the games played and the swaps compared, as the bridge asks for them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.games = 0
        self.comparisons = 0
        monkeypatch.setattr(desk_module, "run", probing(desk_module.run, self._game))
        monkeypatch.setattr(desk_module, "compare_swaps", probing(desk_module.compare_swaps, self._swap))

    def _game(self) -> None:
        self.games += 1

    def _swap(self) -> None:
        self.comparisons += 1


@pytest.fixture
def plays(monkeypatch: pytest.MonkeyPatch) -> Plays:
    return Plays(monkeypatch)


def queue(desk: Desk, name: str, cut: str) -> str:
    """Put a card on the board and queue it to take the place of a deck card; its id."""
    card = cast("Json", desk.propose(DECK, name)["card"])
    card_id = str(card["id"])
    assert "error" not in desk.decide(DECK, card_id, "add", cut, "")
    return card_id


def mana_at(figures: Json, turn: int) -> tuple[object, object]:
    """The worst-tenth mana before and after at a turn, from an answer's figures."""
    [figure] = [f for f in items(figures, "figures") if f["kind"] == "mana_worst_tenth" and f["turn"] == turn]
    return figure["before"], figure["after"]


def test_the_report_comes_as_json_for_the_tab(desk: Desk) -> None:
    report = desk.goldfish(DECK)
    assert report["games"] == 10000
    assert report["seed"] == DECK
    assert report["bracket"] == 5
    assert report["turns"] == [1, 2, 3, 4, 5]
    assert report["waysToWin"] is None
    assert isinstance(report["notModelled"], list)
    assert cast("Json", report["mana"]) == {"median": [1, 2, 3, 4, 5], "worstTenth": [1, 2, 3, 4, 5]}
    assert cast("Json", report["opening"])["seven"] == 1.0
    assert str(report["keepRule"]).startswith("bracket 5 rule: keeps seven cards")


def test_a_deck_is_played_once_however_often_it_is_asked_for(desk: Desk, plays: Plays) -> None:
    first = desk.goldfish(DECK)
    assert desk.goldfish(DECK) == first
    assert plays.games == 1
    queue(desk, "Arcane Signet", "Battered Golem")
    desk.goldfish_review(DECK)
    assert plays.games == 1


def test_a_queued_swap_shows_the_headline_figures_that_moved(desk: Desk) -> None:
    # The Signet is cast on turn 2 whatever the shuffle (every card is seen by turn 1) and adds a mana
    # to the 3 that three lands make on turn 3.
    card_id = queue(desk, "Arcane Signet", "Battered Golem")
    answer = desk.goldfish_swap(DECK, card_id)
    assert mana_at(answer, 3) == (3, 4)
    figure = next(f for f in items(answer, "figures") if f["kind"] == "mana_worst_tenth")
    assert figure == {
        "kind": "mana_worst_tenth",
        "subject": "",
        "turn": 3,
        "label": "Worst-tenth mana on turn 3",
        "unit": "mana",
        "before": 3,
        "after": 4,
        "moved": True,
        "better": True,
    }
    # Only the headline figures: no per-turn medians, no other turns.
    kinds = {"mana_worst_tenth", "colour_screw", "commander_cast_by", "combo_landed_by"}
    assert {f["kind"] for f in items(answer, "figures")} <= kinds
    assert {f["turn"] for f in items(answer, "figures")} <= {3, None}


def test_review_takes_every_queued_swap_together(desk: Desk) -> None:
    queue(desk, "Arcane Signet", "Battered Golem")
    assert mana_at(desk.goldfish_review(DECK), 3) == (3, 4)
    # Mind Stone is the second rock: cast on turn 3 with the mana of three lands and the Signet.
    queue(desk, "Mind Stone", "Disruptor Flute")
    assert mana_at(desk.goldfish_review(DECK), 3) == (3, 5)


def test_a_swap_that_moves_nothing_shows_nothing_and_a_card_without_a_cut_has_no_figures(
    desk: Desk, plays: Plays
) -> None:
    plain = queue(desk, "Chaos Warp", "Disruptor Flute")
    assert desk.goldfish_swap(DECK, plain) == {"figures": []}
    assert desk.goldfish_review(DECK) == {"figures": []}
    uncut = str(cast("Json", desk.propose(DECK, "Mind Stone")["card"])["id"])
    assert desk.goldfish_swap(DECK, uncut) == {"figures": []}
    desk.decide(DECK, uncut, "add", None, "")
    assert desk.goldfish_swap(DECK, uncut) == {"figures": []}
    assert plays.comparisons == 1


def test_a_board_with_nothing_queued_plays_no_games_for_review(desk: Desk, plays: Plays) -> None:
    desk.propose(DECK, "Mind Stone")
    assert desk.goldfish_review(DECK) == {"figures": []}
    assert plays.games == 0


def test_a_swap_that_cannot_be_played_is_left_out_without_an_error(desk: Desk) -> None:
    # The deck holds six Mountains and a swap takes out one, which is no swap of the card.
    partial = queue(desk, "Mind Stone", "Mountain")
    assert desk.goldfish_swap(DECK, partial) == {"figures": []}
    queue(desk, "Arcane Signet", "Battered Golem")
    assert mana_at(desk.goldfish_review(DECK), 3) == (3, 4)


def test_a_card_that_is_not_on_the_board_is_an_error(desk: Desk) -> None:
    assert desk.goldfish_swap(DECK, "nothing") == {"error": "no card 'nothing' on the board"}


def test_the_same_swap_is_compared_once_and_a_change_to_the_deck_files_plays_again(
    desk: Desk, plays: Plays
) -> None:
    card_id = queue(desk, "Arcane Signet", "Battered Golem")
    first = desk.goldfish_swap(DECK, card_id)
    assert desk.goldfish_swap(DECK, card_id) == first
    assert desk.goldfish_review(DECK) == first
    assert (plays.games, plays.comparisons) == (1, 1)
    # Saving the finishers rewrites profile.toml, which is part of what the games are played from.
    desk.save_finishers(DECK, ["Battered Golem"])
    desk.goldfish_swap(DECK, card_id)
    assert (plays.games, plays.comparisons) == (2, 2)


@pytest.mark.parametrize("part", ["bracket", "card data", "cache", "policy"])
def test_each_part_of_what_the_games_depend_on_plays_them_again_when_it_changes(
    desk: Desk, plays: Plays, monkeypatch: pytest.MonkeyPatch, part: str
) -> None:
    desk.goldfish(DECK)
    desk.goldfish(DECK)
    assert plays.games == 1

    def other_bracket(deck: Deck) -> Deck:
        return replace(deck, bracket=2)

    def other_card_data() -> str:
        return "another card database"

    def other_cache(folder: Path) -> str:
        return f"another cache than {folder.name}"

    if part == "bracket":
        monkeypatch.setattr(desk, "_effective", other_bracket)
    elif part == "card data":
        monkeypatch.setattr(desk, "_card_data_stamp", other_card_data)
    elif part == "cache":
        monkeypatch.setattr(desk_module, "cache_stamp", other_cache)
    else:
        monkeypatch.setattr(desk_module, "POLICY_VERSION", POLICY_VERSION + 1)
    desk.goldfish(DECK)
    assert plays.games == 2
    desk.goldfish(DECK)
    assert plays.games == 2


@pytest.mark.parametrize("ask", ["report", "swap", "review"])
def test_a_sync_between_reading_the_deck_and_stamping_it_does_not_keep_the_old_games_as_current(
    desk: Desk, settings: DeskSettings, plays: Plays, monkeypatch: pytest.MonkeyPatch, ask: str
) -> None:
    card_id = queue(desk, "Arcane Signet", "Battered Golem")
    deck_file = next(settings.workspace.glob(f"{DECK}-*")) / "deck.json"
    load = Workspace.load_folder
    synced: list[bool] = []

    def load_then_sync(workspace: Workspace, folder: Path) -> Deck | None:
        found = load(workspace, folder)
        if not synced:
            # A sync rewrites the snapshot just after this read.
            synced.append(True)
            deck_file.write_text(deck_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return found

    def ask_for_figures() -> Json:
        if ask == "report":
            return desk.goldfish(DECK)
        return desk.goldfish_swap(DECK, card_id) if ask == "swap" else desk.goldfish_review(DECK)

    monkeypatch.setattr(Workspace, "load_folder", load_then_sync)
    ask_for_figures()
    assert (synced, plays.games) == ([True], 1)
    ask_for_figures()
    assert plays.games == 2


def test_signing_in_again_forgets_the_games_kept_for_the_last_account(
    tmp_path: Path, raw_deck: Json, card_db_file: Path, plays: Plays
) -> None:
    env = Env(tmp_path, raw_deck, card_db_file)
    account = env.desk()
    account.sign_in("TESTER", PASSWORD)
    account.goldfish(DECK)
    account.goldfish(DECK)
    assert plays.games == 1
    account.sign_out()
    account.sign_in("TESTER", PASSWORD)
    account.goldfish(DECK)
    assert plays.games == 2


def test_two_callers_asking_for_the_same_deck_at_once_play_it_once(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release = threading.Event(), threading.Event()
    played: list[int] = []

    def hold() -> None:
        played.append(1)
        started.set()
        assert release.wait(timeout=30)

    monkeypatch.setattr(desk_module, "run", probing(desk_module.run, hold))
    answers: list[Json] = []
    first = threading.Thread(target=lambda: answers.append(desk.goldfish(DECK)))
    first.start()
    assert started.wait(timeout=30)
    second = threading.Thread(target=lambda: answers.append(desk.goldfish(DECK)))
    second.start()
    release.set()
    first.join(timeout=60)
    second.join(timeout=60)
    assert played == [1]
    assert len(answers) == 2
    assert answers[0] == answers[1]
    assert "error" not in answers[0]


# --- finishers --------------------------------------------------------------------------------------------


def finishers(answer: Json) -> list[tuple[str, bool]]:
    return [(str(f["name"]), bool(f["auto"])) for f in items(answer, "finishers")]


def test_a_deck_with_no_finisher_has_none_listed_and_one_the_owner_adds_is_not_automatic(
    desk: Desk, settings: DeskSettings
) -> None:
    before = desk.settings(DECK)
    assert (before["finishers"], before["finishersRemoved"]) == ([], [])
    saved = desk.save_finishers(DECK, ["  battered golem "])
    assert finishers(saved) == [("Battered Golem", False)]
    assert saved["finishersRemoved"] == []
    text = profile_file(settings).read_text(encoding="utf-8")
    assert 'finishers = ["Battered Golem"]' in text
    assert "# What Sample" in text
    assert desk.settings(DECK) == saved
    assert desk.save_finishers(DECK, [])["finishers"] == []


def test_the_finishers_the_owner_adds_are_played_as_finishers(desk: Desk) -> None:
    desk.save_finishers(DECK, ["Battered Golem"])
    ways = cast("Json", desk.goldfish(DECK)["waysToWin"])
    assert ways["combos"] == []
    assert ways["finishers"] == [{"name": "Battered Golem", "castableBy": [0.0, 0.0, 1.0, 1.0, 1.0]}]


@pytest.fixture
def tagged(settings: DeskSettings, tmp_path: Path) -> DeskSettings:
    """The same settings over card data in which the Tagger marks the Disruptor Flute as a finisher."""
    shutil.copy(build_card_db(tmp_path / "tagged", [OVERRUN_TAG]), settings.card_db)
    return settings


def test_a_finisher_the_tagger_marks_is_listed_as_automatic_and_the_owner_can_take_it_out(
    tagged: DeskSettings, fake: FakeArchidekt
) -> None:
    desk = make_desk(tagged, fake)
    assert finishers(desk.settings(DECK)) == [("Disruptor Flute", True)]
    out = desk.save_finishers(DECK, [])
    assert (finishers(out), out["finishersRemoved"]) == ([], ["Disruptor Flute"])
    assert 'not_finishers = ["Disruptor Flute"]' in profile_file(tagged).read_text(encoding="utf-8")
    both = desk.save_finishers(DECK, ["Disruptor Flute", "Battered Golem"])
    assert finishers(both) == [("Disruptor Flute", True), ("Battered Golem", False)]
    assert both["finishersRemoved"] == []
    text = profile_file(tagged).read_text(encoding="utf-8")
    assert 'finishers = ["Battered Golem"]' in text
    assert "not_finishers = []" in text


def test_a_finisher_that_is_no_card_or_a_bad_payload_is_refused_and_the_file_stays(
    desk: Desk, settings: DeskSettings
) -> None:
    desk.save_finishers(DECK, ["Battered Golem"])
    before = profile_file(settings).read_bytes()
    assert desk.save_finishers(DECK, ["Battered Golem", "Nonesuch Relic"]) == {
        "error": "No card named 'Nonesuch Relic' (check the spelling)"
    }
    assert desk.save_finishers(DECK, "Battered Golem") == {
        "error": "the finishers must be a list of card names"
    }
    assert desk.save_finishers(DECK, [3]) == {"error": "each of the finishers needs a card name"}
    assert desk.save_finishers(DECK, [" "]) == {"error": "each of the finishers needs a card name"}
    long = desk.save_finishers(DECK, ["x" * 201])
    assert long == {"error": f"{'x' * 30}...: a card name is at most 200 characters"}
    assert profile_file(settings).read_bytes() == before


def test_the_finishers_wait_while_an_update_runs(settings: DeskSettings, fake: FakeArchidekt) -> None:
    desk, pending = deferred_desk(settings, fake)
    before = profile_file(settings).read_bytes()
    desk.sync()
    assert len(pending) == 1
    assert desk.save_finishers(DECK, ["Battered Golem"]) == {
        "error": "Wait for Magda to finish syncing, then save the finishers."
    }
    assert profile_file(settings).read_bytes() == before
