import io
import tomllib
from pathlib import Path

import pytest

from commander.plan import (
    EXAMPLE,
    MAX_CHANGES,
    MAX_PLAN_BYTES,
    AddCard,
    PlanError,
    RemoveCard,
    parse_plan,
    read_plan,
)


def test_the_help_example_is_a_valid_plan() -> None:
    plan = parse_plan(tomllib.loads(EXAMPLE))

    assert plan.deck == "magda"
    assert plan.add == (AddCard("Arcane Signet"), AddCard("Sol Ring", 1, "c21", "Ramp"))
    assert plan.remove == (RemoveCard("Wild Growth"), RemoveCard("Mountain", keep=10))


def test_deck_may_be_a_number_and_names_are_trimmed() -> None:
    plan = parse_plan({"deck": 10004639, "add": ["  Sol Ring "], "remove": [{"name": "Shock", "keep": 0}]})

    assert plan.deck == "10004639"
    assert plan.add == (AddCard("Sol Ring"),)
    assert plan.remove == (RemoveCard("Shock", 0),)


def test_set_codes_are_matched_in_lower_case() -> None:
    plan = parse_plan({"deck": "x", "add": [{"name": "Sol Ring", "set": "C21", "quantity": 2}]})

    assert plan.add == (AddCard("Sol Ring", 2, "c21"),)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"add": ["Sol Ring"]}, "'deck' must name the deck"),
        ({"deck": " ", "add": ["Sol Ring"]}, "'deck' must name the deck"),
        ({"deck": True, "add": ["Sol Ring"]}, "'deck' must name the deck"),
        ({"deck": "x"}, "nothing to add or remove"),
        ({"deck": "x", "add": []}, "nothing to add or remove"),
        ({"deck": "x", "adds": ["Sol Ring"]}, "unknown key 'adds'"),
        ({"deck": "x", "add": "Sol Ring"}, "'add' must be a list"),
        ({"deck": "x", "add": [3]}, "add[0] must be a card name or a table"),
        ({"deck": "x", "add": [""]}, "add[0]: the card name is missing"),
        ({"deck": "x", "add": [{"quantity": 2}]}, "add[0]: the card name is missing"),
        ({"deck": "x", "add": ["x" * 201]}, "longer than 200"),
        ({"deck": "x", "add": [{"name": "Sol Ring", "keep": 1}]}, "add[0]: unknown key 'keep'"),
        ({"deck": "x", "remove": [{"name": "Shock", "set": "m21"}]}, "remove[0]: unknown key 'set'"),
        (
            {"deck": "x", "add": [{"name": "Sol Ring", "quantity": 0}]},
            "quantity must be a whole number from 1",
        ),
        ({"deck": "x", "add": [{"name": "Sol Ring", "quantity": 100}]}, "from 1 to 99"),
        ({"deck": "x", "add": [{"name": "Sol Ring", "quantity": True}]}, "quantity must be a whole number"),
        ({"deck": "x", "add": [{"name": "Sol Ring", "quantity": 1.5}]}, "quantity must be a whole number"),
        ({"deck": "x", "remove": [{"name": "Shock", "keep": -1}]}, "keep must be a whole number from 0"),
        ({"deck": "x", "add": [{"name": "Sol Ring", "set": ""}]}, "set must be text of 1-10"),
        ({"deck": "x", "add": [{"name": "Sol Ring", "category": 5}]}, "category must be text"),
        ({"deck": "x", "add": ["Sol Ring", "sol ring"]}, "listed twice in add"),
        ({"deck": "x", "remove": ["Shock", "SHOCK"]}, "listed twice in remove"),
        ({"deck": "x", "add": ["Shock"], "remove": ["shock"]}, "'shock' is in both add and remove"),
    ],
)
def test_bad_plans_name_the_spot(raw: dict[str, object], message: str) -> None:
    with pytest.raises(PlanError, match=message.replace("[", r"\[").replace("]", r"\]")):
        parse_plan(raw)


def test_a_plan_holds_a_limited_number_of_changes() -> None:
    names = [f"Card {i}" for i in range(MAX_CHANGES + 1)]

    with pytest.raises(PlanError, match=f"{MAX_CHANGES + 1} changes"):
        parse_plan({"deck": "x", "add": names})


def test_read_plan_from_a_file_with_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "plan.toml"
    path.write_bytes(b"\xef\xbb\xbf" + EXAMPLE.encode("utf-8"))

    assert read_plan(path).deck == "magda"


def test_read_plan_from_a_stream() -> None:
    assert read_plan(io.BytesIO(b'deck = 1\nremove = ["Shock"]\n')).remove == (RemoveCard("Shock"),)


def test_read_plan_errors_name_the_file(tmp_path: Path) -> None:
    path = tmp_path / "plan.toml"
    path.write_text('deck = "x"\nadd = [1]\n', encoding="utf-8")

    with pytest.raises(PlanError, match=r"plan\.toml: add\[0\]"):
        read_plan(path)
    with pytest.raises(PlanError, match="no plan file"):
        read_plan(tmp_path / "missing.toml")
    path.write_text("deck = ", encoding="utf-8")
    with pytest.raises(PlanError, match="cannot read plan"):
        read_plan(path)
    path.write_bytes(b"deck = '\xff'")
    with pytest.raises(PlanError, match="cannot read plan"):
        read_plan(path)


def test_read_plan_refuses_a_huge_stream_without_reading_it_all() -> None:
    stream = io.BytesIO(b"#" * (MAX_PLAN_BYTES * 3))

    with pytest.raises(PlanError, match="too big"):
        read_plan(stream)
    assert stream.tell() == MAX_PLAN_BYTES + 1
