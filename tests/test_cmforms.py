"""The form scripts, run under Node against a stub `document` that records what they click and set."""

import json
import subprocess
from dataclasses import dataclass, field
from typing import cast

import pytest

from commander.cmforms import (
    OK,
    read_ticks,
    set_field_script,
    submit_script,
    tick_all_wants_script,
    tick_wants_script,
)
from commander.cmsession import SUBMITTED
from pagerun import NODE, needs_node

# Runs the script given on stdin with `document` and `Event` stubbed: elements are found by their exact
# selector, a checkbox click toggles it, and a "check all" click sets every row to its own state.
HARNESS = r"""
const fs = require('fs');
const {script, elements} = JSON.parse(fs.readFileSync(0, 'utf8'));
const events = [];
class Event { constructor(type) { this.type = type; } }
const made = elements.map(spec => {
  let value = spec.value ?? '';
  return {
    spec, checked: !!spec.checked, disabled: !!spec.disabled, clicks: 0,
    get value() { return value; },
    set value(v) { value = (spec.allowed && !spec.allowed.includes(v)) ? '' : v; },
    dispatchEvent(e) { events.push([spec.selector, e.type]); },
    click() {
      this.clicks++;
      if (spec.kind === 'checkbox') this.checked = !this.checked;
      if (spec.kind === 'checkAll') {
        this.checked = !this.checked;
        made.filter(n => n.spec.group === 'rows').forEach(n => { n.checked = this.checked; });
      }
    },
    getAttribute(name) { return name === 'data-id-want' ? (spec.wantId ?? null) : null; },
  };
});
const document = {
  querySelector: sel => made.find(n => n.spec.selector === sel) ?? null,
  querySelectorAll: sel => made.filter(
    n => n.spec.group === 'rows' && sel === 'input[name="checkWantsRow[]"]'),
};
const answer = new Function('document', 'Event', 'return ' + script)(document, Event);
console.log(JSON.stringify({
  answer, events,
  clicks: made.map(n => n.clicks), checked: made.map(n => n.checked), values: made.map(n => n.value),
}));
"""


@dataclass(slots=True, frozen=True)
class Outcome:
    answer: str
    events: list[list[str]]
    clicks: list[int]
    checked: list[bool]
    values: list[str]


@dataclass(slots=True)
class Element:
    selector: str
    kind: str = "field"
    value: str = ""
    checked: bool = False
    disabled: bool = False
    group: str = ""
    want_id: str = ""
    allowed: list[str] | None = field(default=None)

    def spec(self) -> dict[str, object]:
        return {
            "selector": self.selector,
            "kind": self.kind,
            "value": self.value,
            "checked": self.checked,
            "disabled": self.disabled,
            "group": self.group,
            "wantId": self.want_id,
            "allowed": self.allowed,
        }


def run(script: str, *elements: Element) -> Outcome:
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 - node and a fixed program, no shell
        [NODE, "-e", HARNESS],
        input=json.dumps({"script": script, "elements": [e.spec() for e in elements]}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    data = cast("dict[str, object]", json.loads(done.stdout))
    return Outcome(
        answer=cast("str", data["answer"]),
        events=cast("list[list[str]]", data["events"]),
        clicks=cast("list[int]", data["clicks"]),
        checked=cast("list[bool]", data["checked"]),
        values=cast("list[str]", data["values"]),
    )


def rows(*ids: str, ticked: tuple[str, ...] = ()) -> list[Element]:
    return [Element(f"row {i}", kind="checkbox", group="rows", want_id=i, checked=i in ticked) for i in ids]


@needs_node
class TestSetField:
    def test_the_field_takes_the_value_and_hears_input_and_change(self) -> None:
        out = run(set_field_script("#amount", "3"), Element("#amount"))

        assert out.answer == OK
        assert out.values == ["3"]
        assert out.events == [["#amount", "input"], ["#amount", "change"]]

    def test_text_with_quotes_newlines_and_code_is_only_ever_a_value(self) -> None:
        text = "1 Evil\"); alert(1);//\n2 It's </script> \\ Fíli\n"

        out = run(set_field_script("#AddDecklist", text), Element("#AddDecklist"))

        assert out.answer == OK
        assert out.values == [text]

    def test_a_missing_field_is_named(self) -> None:
        assert run(set_field_script("#amount", "3")).answer == "no element matches #amount"

    def test_a_select_without_the_option_keeps_nothing_and_says_so(self) -> None:
        out = run(set_field_script("#minCondition", "9"), Element("#minCondition", allowed=["1", "2"]))

        assert out.answer == 'the field #minCondition kept ""'

    def test_a_select_with_the_option_takes_it(self) -> None:
        out = run(set_field_script("#minCondition", "2"), Element("#minCondition", allowed=["1", "2"]))

        assert out.answer == OK
        assert out.values == ["2"]


@needs_node
class TestSubmit:
    def test_it_clicks_the_button_once_and_says_submitted(self) -> None:
        out = run(submit_script("#deleteSelected"), Element("#deleteSelected"))

        assert out.answer == SUBMITTED
        assert out.clicks == [1]

    def test_a_missing_button_is_named_and_nothing_is_clicked(self) -> None:
        assert run(submit_script("#deleteSelected")).answer == "no element matches #deleteSelected"

    def test_a_disabled_button_is_not_clicked(self) -> None:
        out = run(submit_script("#deleteSelected"), Element("#deleteSelected", disabled=True))

        assert out.answer == "#deleteSelected is disabled"
        assert out.clicks == [0]


@needs_node
class TestTickWants:
    def test_exactly_the_named_rows_end_ticked(self) -> None:
        out = run(tick_wants_script(["a", "c"]), *rows("a", "b", "c", ticked=("b",)))

        assert out.checked == [True, False, True]
        assert out.clicks == [1, 1, 1]
        assert read_ticks(out.answer) == (2, 2)

    def test_a_row_already_ticked_is_left_alone(self) -> None:
        out = run(tick_wants_script(["a"]), *rows("a", "b", ticked=("a",)))

        assert out.clicks == [0, 0]
        assert read_ticks(out.answer) == (1, 1)

    def test_an_id_without_a_row_is_not_found(self) -> None:
        out = run(tick_wants_script(["a", "zz"]), *rows("a", "b"))

        assert read_ticks(out.answer) == (1, 1)

    def test_an_id_that_could_be_code_is_only_an_id(self) -> None:
        out = run(tick_wants_script(['a"]); throw 1; (["']), *rows("a"))

        assert read_ticks(out.answer) == (0, 0)


@needs_node
class TestTickAll:
    def test_every_row_ends_ticked(self) -> None:
        box = Element('input[name="checkAll"]', kind="checkAll")

        out = run(tick_all_wants_script(), box, *rows("a", "b", "c"))

        assert out.checked == [True, True, True, True]
        assert read_ticks(out.answer) == (3, 3)

    def test_a_box_already_ticked_is_not_clicked_to_untick_it(self) -> None:
        box = Element('input[name="checkAll"]', kind="checkAll", checked=True)

        out = run(tick_all_wants_script(), box, *rows("a", ticked=("a",)))

        assert out.clicks == [0, 0]
        assert read_ticks(out.answer) == (1, 1)

    def test_a_page_without_the_box_says_so(self) -> None:
        assert run(tick_all_wants_script(), *rows("a")).answer == 'no element matches input[name="checkAll"]'


class TestReadTicks:
    def test_the_two_counts_are_read(self) -> None:
        assert read_ticks('{"found": 4, "checked": 3}') == (4, 3)

    @pytest.mark.parametrize(
        "answer",
        ["no element matches #x", "[1, 2]", '{"found": 4}', '{"found": "4", "checked": 3}', "", "null"],
    )
    def test_anything_else_is_not_ticks(self, answer: str) -> None:
        assert read_ticks(answer) is None
