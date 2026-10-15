import json
from pathlib import Path

from test_cli import run


def test_card_prints_text_facts_roles_and_tags(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["card", "magda, brazen outlaw"], tmp_path, cache=card_db_file.parent)
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    assert lines[:3] == [
        "Magda, Brazen Outlaw  {1}{R}",
        "Legendary Creature — Dwarf Berserker  2/1",
        "  Other Dwarves you control get +1/+0.",
    ]
    # Cheapest printing: the Kaldheim foil-less copy at EUR 6.79.
    assert "EDHREC #1,220; €6.79" in lines
    assert "tags: combat-ramp, tutor-artifact" in lines
    assert lines[-1] == "tags from Scryfall Tagger, https://tagger.scryfall.com"


def test_card_marks_game_changers_and_takes_several_names(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["card", "Jeska's Will", "Lightning Bolt"], tmp_path, cache=card_db_file.parent)
    assert result.code == 0, result.err
    assert "EDHREC #107; €33.80; game changer" in result.out
    assert "roles: ramp" in result.out
    assert "Lightning Bolt deals 3 damage to any target." in result.out


def test_card_json_carries_every_fact(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["card", "Magda, Brazen Outlaw", "--json"], tmp_path, cache=card_db_file.parent)
    assert result.code == 0, result.err
    [card] = json.loads(result.out)
    assert card["name"] == "Magda, Brazen Outlaw"
    assert (card["power"], card["toughness"], card["keywords"]) == ("2", "1", ["treasure"])
    assert (card["priceCents"], card["edhrecRank"], card["gameChanger"]) == (679, 1220, False)
    assert card["tags"] == ["combat-ramp", "tutor-artifact"]


def test_unknown_card_is_a_usage_error(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["card", "Lightning Bolt", "Lightnig Bolt"], tmp_path, cache=card_db_file.parent)
    assert result.code == 2
    assert result.out == ""
    assert "no card named 'Lightnig Bolt'" in result.err


def test_tags_lists_matches_with_card_counts(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["tags", "ramp"], tmp_path, cache=card_db_file.parent)
    assert result.code == 0, result.err
    assert result.out.splitlines()[:2] == ["ramp              7 cards", "combat-ramp       1 cards"]


def test_tags_without_matches_says_so(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["tags", "zzz"], tmp_path, cache=card_db_file.parent)
    assert (result.code, result.out) == (0, "no tag contains 'zzz'\n")


def test_tags_json(tmp_path: Path, card_db_file: Path) -> None:
    result = run(["tags", "combat", "--json"], tmp_path, cache=card_db_file.parent)
    assert json.loads(result.out) == [{"slug": "combat-ramp", "cards": 1}]
