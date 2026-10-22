"""`cmdr goldfish` end to end, against the fixture deck and the fake Archidekt (ADR 0054).

The fixture deck is Magda with a library of 8 cards: six Mountains, Battered Golem and Disruptor Flute. The
seven cards of a hand and the first draw show all of them, so each card does the same for mana in each
game: a Mountain is one mana from the turn it is played, Mana Crypt two.
"""

import json
import shutil
from pathlib import Path

import pytest

from commander.carddb import CARD_DB_FILE
from conftest import FakeArchidekt, Json
from test_cli_apply import Run, run

# The report's lines that hold whatever the commander's reading, which these tests leave to the engine's.
HEADER = [
    "Sample, deck.. (10000001): 10,000 games, seed 10000001",
    "bracket 5 on Archidekt",
    (
        "bracket 5 rule: keeps seven cards with a land, 2 mana sources in all (fast mana counts) and a "
        "way to win (a combo piece, a tutor or a finisher; with none in the deck: a card on the deck's "
        "themes or one that draws)"
    ),
    "opening hand: kept on seven 100.0%, on six 0.0%, on five or fewer 0.0%",
    "  turn                                    1      2      3      4      5",
]
TABLE = [
    "  mana, median                            1      2      3      4      5",
    "  mana, worst tenth                       1      2      3      4      5",
]
NO_WAYS = "ways to win: no Spellbook combo or finisher in the deck, so none is played"
CRYPT_MOVES = [
    "  Median mana on turn 1: 1 -> 3",
    "  Worst-tenth mana on turn 1: 1 -> 3",
    "  Median mana on turn 2: 2 -> 4",
    "  Worst-tenth mana on turn 2: 2 -> 4",
    "  Median mana on turn 3: 3 -> 5",
    "  Worst-tenth mana on turn 3: 3 -> 5",
    "  Median mana on turn 4: 4 -> 6",
    "  Worst-tenth mana on turn 4: 4 -> 6",
    "  Median mana on turn 5: 5 -> 7",
    "  Worst-tenth mana on turn 5: 5 -> 7",
]


def swapped(out: str) -> list[str]:
    """The lines after the report: the swaps and the figures that moved."""
    lines = out.splitlines()
    return lines[lines.index("swaps") :]


@pytest.fixture
def fake(raw_deck: Json) -> FakeArchidekt:
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


def goldfish(home: Path, fake: FakeArchidekt, *args: str) -> Run:
    return run(["goldfish", *args, "--workspace", str(home / "decks")], home, fake)


def plan(home: Path, text: str) -> str:
    path = home / "plan.toml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_deck_is_played_out_and_its_figures_printed_without_a_request_to_anyone(
    home: Path, fake: FakeArchidekt
) -> None:
    result = goldfish(home, fake, "magda")
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    assert lines[: len(HEADER)] == HEADER
    assert all(line in lines for line in [*TABLE, NO_WAYS])
    assert any(line.startswith("colour screw by turn 3: ") for line in lines)
    assert "swaps" not in lines
    assert fake.requests == []


def test_a_swap_adds_every_figure_that_moved_with_its_old_and_new_value(
    home: Path, fake: FakeArchidekt
) -> None:
    result = goldfish(home, fake, "magda", "--swap", "Mana Crypt=Battered Golem")
    assert (result.code, result.err) == (0, "")
    after = swapped(result.out)
    assert after[:3] == [
        "swaps",
        "  + Mana Crypt  - Battered Golem",
        "figures that moved (10,000 of 10,000 games played again)",
    ]
    assert [line for line in after if "mana" in line] == CRYPT_MOVES


def test_a_swap_that_moves_nothing_says_so(home: Path, fake: FakeArchidekt) -> None:
    result = goldfish(home, fake, "magda", "--swap", "Chaos Warp=Disruptor Flute")
    assert result.code == 0
    assert swapped(result.out) == [
        "swaps",
        "  + Chaos Warp  - Disruptor Flute",
        "no figure moved by more than the luck of the draws allows",
    ]


def test_a_plan_pairs_its_adds_with_its_removes_and_names_the_deck(home: Path, fake: FakeArchidekt) -> None:
    text = 'deck = "magda"\nadd = ["Mana Crypt"]\nremove = ["Battered Golem"]\n'
    by_plan = run(["goldfish", "--plan", plan(home, text), "--workspace", str(home / "decks")], home, fake)
    assert (by_plan.code, by_plan.err) == (0, "")
    typed = goldfish(home, fake, "magda", "--swap", "Mana Crypt=Battered Golem")
    assert by_plan.out == typed.out
    named = goldfish(home, fake, "10000001", "--plan", plan(home, text))
    assert named.out == by_plan.out


def test_the_figures_are_json_with_the_swaps_and_what_moved(home: Path, fake: FakeArchidekt) -> None:
    result = goldfish(home, fake, "magda", "--swap", "Mana Crypt=Battered Golem", "--json")
    assert (result.code, result.err) == (0, "")
    document = json.loads(result.out)
    assert set(document) == {"deck", "report", "swaps", "changes", "headline", "replayed"}
    assert document["deck"] == {"id": 10000001, "name": "Sample, deck..", "short": "Magda"}
    assert document["swaps"] == [{"add": "Mana Crypt", "cut": "Battered Golem", "copies": 1}]
    assert document["report"]["games"] == 10000
    assert document["report"]["mana"] == {"median": [1, 2, 3, 4, 5], "worstTenth": [1, 2, 3, 4, 5]}
    assert [c["label"] for c in document["changes"]][:2] == [
        "Median mana on turn 1",
        "Worst-tenth mana on turn 1",
    ]
    # Bracket 5's headline turn is the third.
    mana = [c for c in document["headline"] if c["kind"] == "mana_worst_tenth"]
    assert [(c["label"], c["before"], c["after"]) for c in mana] == [("Worst-tenth mana on turn 3", 3, 5)]
    assert all(c["kind"] in {"mana_worst_tenth", "colour_screw"} for c in document["headline"])
    assert document["replayed"] == 10000


def test_a_plain_run_is_json_without_swaps(home: Path, fake: FakeArchidekt) -> None:
    document = json.loads(goldfish(home, fake, "magda", "--json").out)
    assert set(document) == {"deck", "report"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("nobody",), "cmdr: no deck matches 'nobody'; `cmdr list` shows them all\n"),
        ((), "cmdr: name a deck, or give --plan\n"),
        (
            ("magda", "--swap", "Mana Crypt"),
            "cmdr: --swap takes \"<card in>=<card out>\", not 'Mana Crypt'\n",
        ),
        (
            ("magda", "--swap", "=Battered Golem"),
            "cmdr: --swap takes \"<card in>=<card out>\", not '=Battered Golem'\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Chaos Warp"),
            "cmdr: Chaos Warp is not in the deck, so it cannot be swapped out\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Magda, Brazen Outlaw"),
            "cmdr: Magda, Brazen Outlaw is a commander, so it cannot be swapped out\n",
        ),
        (
            ("magda", "--swap", "Battered Golem=Disruptor Flute"),
            "cmdr: Battered Golem is in the deck already\n",
        ),
        (
            ("magda", "--swap", "Not A Card=Disruptor Flute"),
            "cmdr: 'Not A Card' is no card the card data knows\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Mountain"),
            "cmdr: the deck holds 6 of Mountain and 1 are swapped; only a swap of all of them is played\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Disruptor Flute", "--swap", "Mind Stone=Disruptor Flute"),
            "cmdr: Disruptor Flute is cut twice\n",
        ),
    ],
)
def test_a_swap_or_deck_that_cannot_be_played_is_a_usage_error_with_a_short_message(
    home: Path, fake: FakeArchidekt, args: tuple[str, ...], message: str
) -> None:
    result = goldfish(home, fake, *args)
    assert (result.code, result.out, result.err) == (2, "", message)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            'deck = "magda"\nadd = ["Mana Crypt", "Mind Stone"]\nremove = ["Battered Golem"]\n',
            (
                "cmdr: the plan adds 2 cards and removes 1 from the deck; "
                "a goldfish swap pairs each add with a remove, in order\n"
            ),
        ),
        (
            'deck = "magda"\nadd = ["Mana Crypt"]\nremove = [{name = "Mountain", keep = 5}]\n',
            "cmdr: Mountain: the plan keeps 5, which is not played as a swap\n",
        ),
        ("deck = 12\nadd = []\n", "plan.toml: the plan has nothing to add or remove\n"),
    ],
)
def test_a_plan_that_cannot_be_paired_is_a_usage_error(
    home: Path, fake: FakeArchidekt, text: str, message: str
) -> None:
    result = run(["goldfish", "--plan", plan(home, text), "--workspace", str(home / "decks")], home, fake)
    assert (result.code, result.out) == (2, "")
    assert result.err.endswith(message)


def test_a_missing_plan_file_is_a_usage_error(home: Path, fake: FakeArchidekt) -> None:
    result = goldfish(home, fake, "magda", "--plan", str(home / "nothing.toml"))
    assert (result.code, result.out) == (2, "")
    assert result.err.startswith("cmdr: no plan file at ")


def test_the_help_gives_examples_before_the_flags(
    home: Path, fake: FakeArchidekt, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["goldfish", "--help"], home, fake).code == 0
    help_text = capsys.readouterr().out
    assert help_text.index("examples:") < help_text.index("options:")
    assert "cmdr goldfish ellivere --json" in help_text
