"""The typed accessors for untrusted JSON."""

import json

import pytest

from commander.jsonshape import ShapeError, opt_float, req_float


def test_a_number_reads_as_a_float_whole_or_not() -> None:
    assert req_float({"x": 0}, "x", "card") == 0.0
    assert req_float({"x": 1.5}, "x", "card") == 1.5
    assert opt_float({}, "x", "card") is None


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "1" + "0" * 400])
def test_a_number_that_is_not_finite_is_refused_with_its_path(text: str) -> None:
    # Python's json parses NaN and Infinity, and a 400-digit integer that no float holds.
    parsed = json.loads(f'{{"x": {text}}}')
    with pytest.raises(ShapeError, match=r"card\.x: expected a finite number"):
        req_float(parsed, "x", "card")


@pytest.mark.parametrize("value", [True, "1.5", None, [1.0]])
def test_a_value_that_is_not_a_number_is_refused(value: object) -> None:
    with pytest.raises(ShapeError, match=r"card\.x: expected a number"):
        req_float({"x": value}, "x", "card")
