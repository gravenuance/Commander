"""`cmdr toget` end to end, against the fake Archidekt (ADR 0010)."""

import json
import shutil
from pathlib import Path

import pytest

from commander.board import read_activity
from commander.carddb import CARD_DB_FILE
from conftest import FakeArchidekt, Json, MemoryKeyring
from test_cli_apply import Run, run, saved_deck, signed_in

FOLDER = "10000001-sample-deck"


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
    golem = next(c for c in raw_deck["cards"] if c["card"]["oracleCard"]["name"] == "Battered Golem")
    golem["label"] = "Don't Have,#f47373"
    archidekt = FakeArchidekt()
    archidekt.put(raw_deck)
    return archidekt


@pytest.fixture
def home(tmp_path: Path, card_db_file: Path, fake: FakeArchidekt) -> Path:
    cache = tmp_path / "cache"
    cache.mkdir()
    shutil.copy(card_db_file, cache / CARD_DB_FILE)
    assert (
        run(["sync", "--no-cards", "--no-ideas", "--workspace", str(tmp_path / "decks")], tmp_path, fake).code
        == 0
    )
    fake.requests.clear()
    return tmp_path


def toget(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    return run(["toget", *args, "--workspace", str(home / "decks")], home, fake)


def labels(home: Path) -> dict[str, str]:
    return {c["name"]: c["label"] for c in saved_deck(home / "decks")["cards"] if c["label"]}


def test_lists_the_cards_still_to_get(home: Path, fake: FakeArchidekt) -> None:
    assert toget(home, fake).out == "Magda: Battered Golem (Don't Have)\n"
    document = json.loads(toget(home, fake, "magda", "--json").out)
    assert document == [
        {
            "deck": 10000001,
            "short": "Magda",
            "cards": [
                {"name": "Battered Golem", "quantity": 1, "label": "Don't Have", "category": "Combo Pieces"}
            ],
        }
    ]


def test_ordered_then_arrived_change_the_label_on_archidekt_and_locally(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    ordered = toget(home, fake, "magda", "battered golem", "--ordered")
    assert (ordered.code, ordered.out) == (0, "Battered Golem: Don't Have -> Getting\n")
    assert [e["modifications"]["label"] for e in fake.card_writes()] == ["Getting,#2ccce4"]
    assert labels(home) == {"Battered Golem": "Getting"}

    arrived = toget(home, fake, "magda", "Battered Golem", "--arrived")
    assert (arrived.code, arrived.out) == (0, "Battered Golem: Getting -> no label\n")
    assert fake.card_writes()[-1]["modifications"]["label"] == ",#656565"
    assert labels(home) == {}
    assert toget(home, fake).out == "nothing to get\n"
    # A label never changes which cards are in the deck, so no snapshot is taken.
    assert not [path for _, path, _, _ in fake.writes if path.endswith("/snapshots/")]
    log = read_activity(home / "decks" / FOLDER)
    assert [(e.action, e.card, e.detail) for e in log[-2:]] == [
        ("marked ordered", "Battered Golem", "label Don't Have -> Getting"),
        ("marked arrived", "Battered Golem", "label Getting -> none"),
    ]


def test_a_mark_whose_read_back_fails_says_to_run_sync(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    fake.fail_reads_after_edit = True

    result = toget(home, fake, "magda", "Battered Golem", "--ordered")

    assert result.code == 1
    assert result.err.startswith("cmdr: Archidekt took the change, but reading the deck back failed (")
    assert result.err.endswith(" returned HTTP 404); run `cmdr sync`\n")


def test_a_mark_that_changes_nothing_writes_nothing(
    home: Path, fake: FakeArchidekt, memory_keyring: MemoryKeyring
) -> None:
    signed_in(memory_keyring)
    assert toget(home, fake, "magda", "Battered Golem", "--ordered").code == 0
    again = toget(home, fake, "magda", "Battered Golem", "--ordered")
    assert (again.code, again.err) == (1, "cmdr: Battered Golem is already marked ordered\n")
    missing = toget(home, fake, "magda", "Sol Ring", "--arrived")
    assert (missing.code, missing.err) == (1, "cmdr: Sol Ring is not in Magda on Archidekt\n")
    assert len(fake.card_writes()) == 1


def test_a_card_needs_a_step_and_a_step_needs_a_card(home: Path, fake: FakeArchidekt) -> None:
    for args in (("magda", "Battered Golem"), ("magda", "--ordered")):
        result = toget(home, fake, *args)
        assert (result.code, result.err) == (2, "cmdr: name a card together with --ordered or --arrived\n")


def test_marking_needs_a_login(home: Path, fake: FakeArchidekt) -> None:
    result = toget(home, fake, "magda", "Battered Golem", "--ordered")
    assert result.code == 1
    assert "cmdr login" in result.err
    assert not fake.card_writes()


def test_a_deck_saved_before_labels_were_kept_says_to_sync(home: Path, fake: FakeArchidekt) -> None:
    snapshot = home / "decks" / FOLDER / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["version"] = 2
    for card in document["cards"]:
        del card["label"]
    snapshot.write_text(json.dumps(document), encoding="utf-8")
    assert toget(home, fake).out == "Magda: labels unknown; run `cmdr sync`\n"


def test_cardmarket_prints_the_dont_have_cards_as_a_wants_list(home: Path, fake: FakeArchidekt) -> None:
    result = toget(home, fake, "--cardmarket")
    assert (result.code, result.out, result.err) == (0, "1 Battered Golem\n", "")


def test_cardmarket_prints_nothing_but_a_note_when_nothing_is_to_buy(home: Path, fake: FakeArchidekt) -> None:
    snapshot = home / "decks" / FOLDER / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    for entry in document["cards"]:
        if entry["name"] == "Battered Golem":
            entry["label"] = "Getting"
    snapshot.write_text(json.dumps(document), encoding="utf-8")

    result = toget(home, fake, "--cardmarket")

    assert (result.code, result.out, result.err) == (0, "", "nothing to buy\n")


def test_cardmarket_skips_a_deck_without_labels_and_says_so(home: Path, fake: FakeArchidekt) -> None:
    snapshot = home / "decks" / FOLDER / "deck.json"
    document = json.loads(snapshot.read_text(encoding="utf-8"))
    document["version"] = 2
    for entry in document["cards"]:
        del entry["label"]
    snapshot.write_text(json.dumps(document), encoding="utf-8")

    result = toget(home, fake, "--cardmarket")

    assert (result.code, result.out) == (0, "")
    assert result.err == "Magda: labels unknown; run `cmdr sync`\nnothing to buy\n"


@pytest.mark.parametrize(
    "args",
    [("--json",), ("magda",), ("magda", "Battered Golem"), ("--ordered",), ("--arrived",)],
)
def test_cardmarket_refuses_other_arguments(home: Path, fake: FakeArchidekt, args: tuple[str, ...]) -> None:
    result = toget(home, fake, "--cardmarket", *args)
    assert (result.code, result.out) == (2, "")
    assert result.err == "cmdr: --cardmarket takes no deck or card, and not --json, --ordered or --arrived\n"
    assert not fake.card_writes()
