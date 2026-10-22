"""`cmdr goldfish` end to end, against the fixture deck and the fake Archidekt (ADR 0054).

The fixture deck is Magda with a library of 8 cards: six Mountains, Battered Golem and Disruptor Flute. The
seven cards of a hand and the first draw show all of them, so each card does the same for mana in each
game: a Mountain is one mana from the turn it is played, Mana Crypt two.
"""

import json
import shutil
from pathlib import Path

import pytest

from commander import cli_goldfish_cuts
from commander.carddb import CARD_DB_FILE, CardDb
from commander.goldfish import FigureChange, FigureKind, GoldfishComparison
from commander.goldfish_run import GoldfishRun, SwapRequest
from commander.targets import Target, TargetSource
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


def test_a_swap_of_a_basic_takes_out_one_copy_and_plays_the_rest(home: Path, fake: FakeArchidekt) -> None:
    result = goldfish(home, fake, "magda", "--swap", "Mana Crypt=Mountain")
    assert (result.code, result.err) == (0, "")
    after = swapped(result.out)
    assert after[:2] == ["swaps", "  + Mana Crypt  - Mountain"]
    # Five Mountains are enough for the five turns, so it moves what the Crypt does for the Golem.
    assert [line for line in after if "mana" in line] == CRYPT_MOVES


def test_a_plan_that_keeps_some_copies_swaps_the_rest_and_the_add_brings_as_many(
    home: Path, fake: FakeArchidekt
) -> None:
    text = (
        'deck = "magda"\nadd = [{name = "Mana Crypt", quantity = 3}]\n'
        'remove = [{name = "Mountain", keep = 3}]\n'
    )
    result = run(["goldfish", "--plan", plan(home, text), "--workspace", str(home / "decks")], home, fake)
    assert (result.code, result.err) == (0, "")
    after = swapped(result.out)
    assert after[1] == "  + 3 x Mana Crypt  - 3 x Mountain"
    # Three Crypts make 6 beside the one Mountain of turn 1; three Mountains are all there are after turn 3.
    assert "  Median mana on turn 1: 1 -> 7" in after
    assert "  Median mana on turn 5: 5 -> 9" in after
    one = 'deck = "magda"\nadd = ["Mana Crypt"]\nremove = [{name = "Mountain", keep = 5}]\n'
    by_plan = run(["goldfish", "--plan", plan(home, one), "--workspace", str(home / "decks")], home, fake)
    assert by_plan.out == goldfish(home, fake, "magda", "--swap", "Mana Crypt=Mountain").out


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


def test_typed_swaps_and_a_plans_swaps_are_played_together_the_typed_ones_first(
    home: Path, fake: FakeArchidekt
) -> None:
    text = 'deck = "magda"\nadd = ["Mana Crypt"]\nremove = ["Battered Golem"]\n'
    result = goldfish(home, fake, "--plan", plan(home, text), "--swap", "Mind Stone=Disruptor Flute")
    assert (result.code, result.err) == (0, "")
    assert swapped(result.out)[:3] == [
        "swaps",
        "  + Mind Stone  - Disruptor Flute",
        "  + Mana Crypt  - Battered Golem",
    ]


def test_a_card_put_in_by_a_typed_swap_and_by_the_plan_is_an_error(home: Path, fake: FakeArchidekt) -> None:
    text = 'deck = "magda"\nadd = ["Mana Crypt"]\nremove = ["Battered Golem"]\n'
    result = goldfish(home, fake, "--plan", plan(home, text), "--swap", "Mana Crypt=Disruptor Flute")
    assert (result.code, result.out, result.err) == (2, "", "cmdr: Mana Crypt is put in twice\n")


def test_a_plans_removes_from_a_category_outside_the_deck_are_not_paired_with_adds(
    home: Path, fake: FakeArchidekt
) -> None:
    text = (
        'deck = "magda"\nadd = ["Mana Crypt"]\n'
        'remove = ["Battered Golem", {name = "Chaos Warp", category = "Maybeboard"}]\n'
    )
    result = goldfish(home, fake, "--plan", plan(home, text))
    assert (result.code, result.err) == (0, "")
    assert swapped(result.out)[:2] == ["swaps", "  + Mana Crypt  - Battered Golem"]
    # Too few removes from the deck is still an error: the Maybeboard one does not make up the number.
    short = (
        'deck = "magda"\nadd = ["Mana Crypt"]\nremove = [{name = "Chaos Warp", category = "Maybeboard"}]\n'
    )
    refused = goldfish(home, fake, "--plan", plan(home, short))
    assert (refused.code, refused.out) == (2, "")
    assert refused.err.endswith(
        "cmdr: the plan adds 1 cards and removes 0 from the deck; "
        "a goldfish swap pairs each add with a remove, in order\n"
    )


def test_the_figures_are_json_with_the_swaps_and_what_moved(home: Path, fake: FakeArchidekt) -> None:
    result = goldfish(home, fake, "magda", "--swap", "Mana Crypt=Battered Golem", "--json")
    assert (result.code, result.err) == (0, "")
    document = json.loads(result.out)
    assert set(document) == {"deck", "report", "swaps", "changes", "headline", "replayed"}
    assert document["deck"] == {"id": 10000001, "name": "Sample, deck..", "short": "Magda"}
    assert document["swaps"] == [{"add": "Mana Crypt", "cut": "Battered Golem", "copies": 1}]
    assert document["report"]["games"] == 10000
    assert document["report"]["mana"] == {"median": [1, 2, 3, 4, 5], "worstTenth": [1, 2, 3, 4, 5]}
    # Mana Crypt's 2 beside a turn-1 Mountain pays Magda's {1}{R} a turn early, in every game.
    assert [(c["label"], c["before"], c["after"]) for c in document["changes"]][:4] == [
        ("Commander cast by turn 1", 0.0, 1.0),
        ("Commander median turn", 2, 1),
        ("Median mana on turn 1", 1, 3),
        ("Worst-tenth mana on turn 1", 1, 3),
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
            ("magda", "--swap", "Mana Crypt=Disruptor Flute", "--swap", "Mind Stone=Disruptor Flute"),
            "cmdr: Disruptor Flute is cut twice\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Battered Golem", "--swap", "Mana Crypt=Disruptor Flute"),
            "cmdr: Mana Crypt is put in twice\n",
        ),
        (
            ("magda", "--swap", "Mana Crypt=Battered Golem", "--swap", "mana crypt=Disruptor Flute"),
            "cmdr: Mana Crypt is put in twice\n",
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
            (
                'deck = "magda"\nadd = [{name = "Mana Crypt", quantity = 2}]\n'
                'remove = [{name = "Mountain", keep = 5}]\n'
            ),
            "cmdr: Mountain: the plan removes 1 and adds 2 Mana Crypt; a goldfish swap pairs equal numbers\n",
        ),
        (
            'deck = "magda"\nadd = [{name = "Mana Crypt", quantity = 3}]\nremove = ["Mountain"]\n',
            "cmdr: Mountain: the plan removes 6 and adds 3 Mana Crypt; a goldfish swap pairs equal numbers\n",
        ),
        (
            'deck = "magda"\nadd = ["Mana Crypt"]\nremove = [{name = "Mountain", keep = 6}]\n',
            "cmdr: Mountain: the plan keeps 6 of the 6 the deck holds\n",
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
    assert 'cmdr goldfish ellivere --cuts "Cultivate"' in help_text


# --- --cuts: a board card against the deck cards it could replace (ADR 0056) ----------------------------

GOLEM, FLUTE = "Battered Golem", "Disruptor Flute"
SCREW_WORSE = FigureChange(FigureKind.COLOUR_SCREW, "", None, 0.0, 0.3, moved=True)
CUTS_HEADER = "Sample, deck.. (10000001): Arcane Signet against its cut targets, 10,000 games, seed 10000001"


def put_on_board(home: Path, fake: FakeArchidekt, name: str) -> None:
    added = run(["board", "add", "magda", name, "--workspace", str(home / "decks")], home, fake)
    assert added.code == 0, added.err


def test_a_board_card_is_played_against_each_card_it_could_replace(home: Path, fake: FakeArchidekt) -> None:
    put_on_board(home, fake, "Arcane Signet")
    result = goldfish(home, fake, "magda", "--cuts", "arcane signet")
    assert (result.code, result.err) == (0, "")
    # The Signet is cast on turn 2 and adds a mana to the three that three lands make on turn 3. With no
    # shares read it scores only the 0.10 of replacing a card like it, and the Golem is its one target.
    assert result.out.splitlines() == [
        CUTS_HEADER,
        "1. Battered Golem (score 0.10)",
        "     Worst-tenth mana on turn 3: 3 -> 4",
    ]
    assert fake.requests == []


def test_the_cuts_come_as_json_with_the_figures_of_each(home: Path, fake: FakeArchidekt) -> None:
    put_on_board(home, fake, "Arcane Signet")
    document = json.loads(goldfish(home, fake, "magda", "--cuts", "Arcane Signet", "--json").out)
    assert document["deck"] == {"id": 10000001, "name": "Sample, deck..", "short": "Magda"}
    assert (document["card"], document["copies"]) == ("Arcane Signet", 1)
    [cut] = document["cuts"]
    assert {key: cut[key] for key in ("name", "place", "scorePlace", "score", "movedUp", "why")} == {
        "name": GOLEM,
        "place": 1,
        "scorePlace": 1,
        "score": 0.1,
        "movedUp": False,
        "why": None,
    }
    assert [(f["label"], f["before"], f["after"]) for f in cut["figures"]] == [
        ("Worst-tenth mana on turn 3", 3, 4)
    ]


def stub_targets(monkeypatch: pytest.MonkeyPatch, scores: dict[str, float]) -> list[str]:
    """The card's targets with these scores, each swap moving a colour screw figure only when it cuts the
    Golem; the cuts compared, in order, are the list returned."""
    compared: list[str] = []

    def targets(*_: object) -> tuple[Target, ...]:
        return tuple(Target(name, f"{name} goes", TargetSource.ROLE, score) for name, score in scores.items())

    def compare(
        played: GoldfishRun, _db: CardDb, swaps: list[SwapRequest], *, lenient: bool = False
    ) -> GoldfishComparison:
        assert not lenient
        compared.append(swaps[0].cut)
        figures = (SCREW_WORSE,) if swaps[0].cut == GOLEM else ()
        return GoldfishComparison(played.report, played.report, figures, ())

    monkeypatch.setattr(cli_goldfish_cuts, "targets_for", targets)
    monkeypatch.setattr(cli_goldfish_cuts, "compare_swaps", compare)
    return compared


def test_targets_that_tie_come_in_the_order_of_their_figures_and_the_one_moved_up_says_why(
    home: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_on_board(home, fake, "Arcane Signet")
    compared = stub_targets(monkeypatch, {GOLEM: 0.50, FLUTE: 0.45})
    result = goldfish(home, fake, "magda", "--cuts", "Arcane Signet")
    assert result.code == 0
    assert result.out.splitlines() == [
        CUTS_HEADER,
        "1. Disruptor Flute (score 0.45)",
        "     goldfish pick, up from place 2: Colour screw is worse with Battered Golem",
        "     no headline figure moved",
        "2. Battered Golem (score 0.50)",
        "     Colour screw: 0.0% -> 30.0%",
    ]
    assert compared == [GOLEM, FLUTE]
    document = json.loads(goldfish(home, fake, "magda", "--cuts", "Arcane Signet", "--json").out)
    assert [(c["name"], c["place"], c["scorePlace"], c["movedUp"]) for c in document["cuts"]] == [
        (FLUTE, 1, 2, True),
        (GOLEM, 2, 1, False),
    ]


def test_targets_that_do_not_tie_keep_the_scores_order_and_still_show_their_figures(
    home: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    put_on_board(home, fake, "Arcane Signet")
    compared = stub_targets(monkeypatch, {GOLEM: 0.90, FLUTE: 0.45})
    result = goldfish(home, fake, "magda", "--cuts", "Arcane Signet")
    assert [line for line in result.out.splitlines() if line[0].isdigit()] == [
        "1. Battered Golem (score 0.90)",
        "2. Disruptor Flute (score 0.45)",
    ]
    assert "goldfish pick" not in result.out
    assert compared == [GOLEM, FLUTE]


def test_a_card_that_is_not_on_the_board_or_has_no_targets_is_an_error(
    home: Path, fake: FakeArchidekt, monkeypatch: pytest.MonkeyPatch
) -> None:
    off = goldfish(home, fake, "magda", "--cuts", "Chaos Warp")
    assert (off.code, off.out) == (2, "")
    assert off.err == "cmdr: 'Chaos Warp' is not on the board of Magda; `cmdr board add` puts it there\n"
    put_on_board(home, fake, "Arcane Signet")
    stub_targets(monkeypatch, {})
    none = goldfish(home, fake, "magda", "--cuts", "Arcane Signet")
    assert (none.code, none.out) == (2, "")
    assert none.err == "cmdr: Arcane Signet has no cut targets in Magda\n"


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("magda", "--cuts", "Arcane Signet", "--swap", "Mana Crypt=Battered Golem"), "leave out --swap"),
        (("--cuts", "Arcane Signet"), "name a deck for --cuts"),
    ],
)
def test_cuts_asks_for_a_deck_and_takes_no_swaps(
    home: Path, fake: FakeArchidekt, args: tuple[str, ...], message: str
) -> None:
    result = goldfish(home, fake, *args)
    assert (result.code, result.out) == (2, "")
    assert message in result.err
