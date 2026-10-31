import json
from dataclasses import replace
from pathlib import Path

import pytest

from commander.models import Deck
from commander.store import Workspace
from test_cli import Run, run
from test_tokens import deck_of

MAGDA_DECK = (
    "Magda, Brazen Outlaw",
    "Charismatic Conqueror",
    "Esika's Chariot",
    "Caller of the Claw",
    "Stormsplitter",
    "Lazav, the Multifarious",
    "Monkey Cage",
    "Palace Jailer",
)
PREACHER_DECK = (
    "Preacher of the Schism",
    "Charismatic Conqueror",
    "Elspeth, Sun's Champion",
    "Ravenloft Adventurer",
)
EMBLEM_LINE = (
    "Elspeth, Sun's Champion Emblem  [Emblem — Elspeth]  Creatures you control get +2/+2 and have flying"
)
CHECK_NOTE = "Scryfall links no token to these, though their text creates one"


def decks(tmp_path: Path, *decks_: Deck) -> Path:
    space = Workspace(tmp_path / "decks")
    for deck in decks_:
        space.save(deck)
    return tmp_path / "decks"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return decks(
        tmp_path,
        replace(deck_of(*MAGDA_DECK), id=1, name="Magda deck"),
        replace(deck_of(*PREACHER_DECK), id=2, name="Preacher deck"),
    )


def tokens(args: list[str], tmp_path: Path, workspace: Path, token_db_file: Path) -> Run:
    return run(["tokens", *args, "--workspace", str(workspace)], tmp_path, cache=token_db_file.parent)


def test_one_deck_lists_its_tokens_with_the_cards_that_make_them(
    tmp_path: Path, workspace: Path, token_db_file: Path
) -> None:
    result = tokens(["magda"], tmp_path, workspace, token_db_file)
    assert (result.code, result.err) == (0, "")
    assert result.out.splitlines() == [
        "Magda deck (1): 6 tokens",
        "  Copy: Esika's Chariot, Stormsplitter",
        "  G 2/2 Bear: Caller of the Claw",
        "  G 2/2 Cat: Esika's Chariot",
        "  The Monarch: Palace Jailer",
        "  C Treasure: Magda, Brazen Outlaw",
        "  W 1/1 Vampire (Lifelink): Charismatic Conqueror",
        f"{CHECK_NOTE}: Monkey Cage",
    ]


def test_without_a_deck_each_token_is_listed_once_with_the_makers_in_each_deck(
    tmp_path: Path, workspace: Path, token_db_file: Path
) -> None:
    result = tokens([], tmp_path, workspace, token_db_file)
    assert (result.code, result.err) == (0, "")
    lines = result.out.splitlines()
    undercity = lines.index("  Preacher (Ravenloft Adventurer)")
    # Undercity // The Initiative's long text is cut; its line starts as the others do.
    assert lines[undercity - 1].startswith(
        "Undercity // The Initiative  [Dungeon — Undercity // Card]  You can't"
    )
    del lines[undercity - 1]
    assert lines == [
        # The only token two decks make comes first; the rest by name.
        "W 1/1 Vampire (Lifelink)  [Token Creature — Vampire]",
        "  Magda (Charismatic Conqueror); Preacher (Charismatic Conqueror, Preacher of the Schism)",
        "G 2/2 Bear  [Token Creature — Bear]",
        "  Magda (Caller of the Claw)",
        "G 2/2 Cat  [Token Creature — Cat]",
        "  Magda (Esika's Chariot)",
        "Copy  [Token]",
        "  Magda (Esika's Chariot, Stormsplitter)",
        EMBLEM_LINE,
        "  Preacher (Elspeth, Sun's Champion)",
        "W 1/1 Soldier  [Token Creature — Soldier]",
        "  Preacher (Elspeth, Sun's Champion)",
        "The Monarch  [Card]  At the beginning of your end step, draw a card; Whenever a creature d…",
        "  Magda (Palace Jailer)",
        "C Treasure  [Token Artifact — Treasure]  {T}, Sacrifice this token: Add one mana of any color",
        "  Magda (Magda, Brazen Outlaw)",
        "  Preacher (Ravenloft Adventurer)",
        "",
        f"{CHECK_NOTE}:",
        "  Magda: Monkey Cage",
    ]


def test_one_deck_as_json(tmp_path: Path, workspace: Path, token_db_file: Path) -> None:
    result = tokens(["magda", "--json"], tmp_path, workspace, token_db_file)
    assert (result.code, result.err) == (0, "")
    document = json.loads(result.out)
    assert (document["id"], document["name"], document["checkByHand"]) == (1, "Magda deck", ["Monkey Cage"])
    assert [t["name"] for t in document["tokens"]] == [
        "Copy",
        "Bear",
        "Cat",
        "The Monarch",
        "Treasure",
        "Vampire",
    ]
    assert document["tokens"][0] == {
        "oracleId": "88c78601-87f0-45e7-b2e0-e7ffcfb1cb70",
        "name": "Copy",
        "kind": "helper",
        "typeLine": "Token",
        "power": None,
        "toughness": None,
        "colours": "",
        "text": "(This token can be used to represent a token that's a copy of a permanent.)",
        "scryfallId": "2115c141-f2fb-4bfd-a8b5-2943bad9f418",
        "makers": ["Esika's Chariot", "Stormsplitter"],
    }
    vampire = document["tokens"][-1]
    assert (vampire["power"], vampire["toughness"], vampire["colours"]) == ("1", "1", "W")


def test_all_decks_as_json_list_each_token_once_with_its_decks(
    tmp_path: Path, workspace: Path, token_db_file: Path
) -> None:
    result = tokens(["--json"], tmp_path, workspace, token_db_file)
    assert (result.code, result.err) == (0, "")
    document = json.loads(result.out)
    assert [t["name"] for t in document["tokens"]] == [
        "Vampire",
        "Bear",
        "Cat",
        "Copy",
        "Elspeth, Sun's Champion Emblem",
        "Soldier",
        "The Monarch",
        "Treasure",
        "Undercity // The Initiative",
    ]
    assert document["tokens"][0]["decks"] == [
        {"id": 1, "deck": "Magda", "makers": ["Charismatic Conqueror"]},
        {"id": 2, "deck": "Preacher", "makers": ["Charismatic Conqueror", "Preacher of the Schism"]},
    ]
    assert document["checkByHand"] == [{"id": 1, "deck": "Magda", "cards": ["Monkey Cage"]}]


def test_a_deck_that_makes_no_token_says_so(tmp_path: Path, token_db_file: Path) -> None:
    space = decks(tmp_path, replace(deck_of("Battered Golem", "Mountain"), name="Golem deck"))
    one = tokens(["golem"], tmp_path, space, token_db_file)
    assert (one.code, one.out) == (0, "Golem deck (1): 0 tokens\n")
    every = tokens([], tmp_path, space, token_db_file)
    assert (every.code, every.out) == (0, "no deck makes a token\n")
    assert json.loads(tokens(["--json"], tmp_path, space, token_db_file).out) == {
        "tokens": [],
        "checkByHand": [],
    }


def test_one_token_is_singular(tmp_path: Path, token_db_file: Path) -> None:
    space = decks(tmp_path, replace(deck_of("Magda, Brazen Outlaw"), name="Magda deck"))
    assert tokens(["magda"], tmp_path, space, token_db_file).out.splitlines()[0] == "Magda deck (1): 1 token"


def test_an_unknown_deck_is_a_usage_error(tmp_path: Path, workspace: Path, token_db_file: Path) -> None:
    result = tokens(["nobody"], tmp_path, workspace, token_db_file)
    assert result.code == 2
    assert result.out == ""
    assert "no deck matches 'nobody'" in result.err


def test_without_card_data_it_says_to_sync(tmp_path: Path, workspace: Path) -> None:
    result = run(["tokens", "--workspace", str(workspace)], tmp_path, cache=tmp_path / "no-cache")
    assert result.code == 1
    assert "run `cmdr sync`" in result.err


def test_the_help_gives_examples_before_the_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["tokens", "--help"], tmp_path).code == 0
    help_text = capsys.readouterr().out
    assert help_text.index("examples:") < help_text.index("options:")
    assert "cmdr tokens ellivere --json" in help_text
